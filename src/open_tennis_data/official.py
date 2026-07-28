"""Permission-gated ATP/WTA capture and deterministic offline parsing."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from open_tennis_data.integrity import canonical_json, content_id
from open_tennis_data.source_policy import SourcePolicyRegistry


@dataclass(frozen=True)
class OfficialMatchObservation:
    source_label: str
    source_event_id: str
    source_match_id: str
    tour: str
    route: str
    round: str | None
    player1_name: str | None
    player1_id: str | None
    player2_name: str | None
    player2_id: str | None
    winner_name: str | None
    status: str
    score: str | None
    local_date: str | None
    local_time: str | None
    ordering_label: str | None
    court: str | None
    source_url: str
    artifact_sha256: str

    @property
    def observation_id(self) -> str:
        return content_id("official", asdict(self))


class _MatchElementParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {name: value or "" for name, value in attrs}
        if "data-match-id" not in values:
            return
        self.rows.append(
            {
                name.removeprefix("data-").replace("-", "_"): value
                for name, value in values.items()
                if name.startswith("data-")
            }
        )


def _walk_match_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        keys = {str(key).casefold() for key in value}
        if (
            {"matchid", "match_id", "id"} & keys
            and {"player1", "playerone", "competitora", "teamone"} & keys
        ):
            found.append(value)
        for child in value.values():
            found.extend(_walk_match_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_match_dicts(child))
    return found


def _value(row: dict[str, Any], *names: str) -> str | None:
    lowered = {str(key).casefold().replace("_", ""): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.casefold().replace("_", ""))
        if value not in (None, ""):
            if isinstance(value, dict):
                for key in ("name", "displayName", "fullName", "id"):
                    if value.get(key) not in (None, ""):
                        return str(value[key]).strip()
            return str(value).strip()
    return None


def parse_official_html(
    payload: bytes,
    *,
    source_label: str,
    source_event_id: str,
    route: str,
    source_url: str,
) -> tuple[list[OfficialMatchObservation], list[dict[str, str]]]:
    """Parse immutable rendered HTML; this function performs no network access."""
    text = payload.decode("utf-8", errors="replace")
    lowered = text.casefold()
    if any(marker in lowered for marker in ("captcha", "access denied", "sign in to continue")):
        return [], [{"reason": "blocked_or_authenticated_page", "route": route}]
    if "loading" in lowered and "data-match-id" not in lowered:
        return [], [{"reason": "rendered_content_unavailable", "route": route}]
    parser = _MatchElementParser()
    parser.feed(text)
    rows: list[dict[str, Any]] = list(parser.rows)
    for marker in ("__NEXT_DATA__", "application/json"):
        position = 0
        while True:
            position = text.find(marker, position)
            if position < 0:
                break
            start = text.find(">", position)
            end = text.find("</script>", start)
            if start > 0 and end > start:
                try:
                    rows.extend(_walk_match_dicts(json.loads(text[start + 1 : end])))
                except json.JSONDecodeError:
                    pass
            position = max(position + 1, end)
    digest = hashlib.sha256(payload).hexdigest()
    observations: list[OfficialMatchObservation] = []
    rejected: list[dict[str, str]] = []
    for index, row in enumerate(rows, 1):
        displayed_tour = (_value(row, "tour", "tourMarker") or "").casefold()
        if displayed_tour not in {"atp", "wta"}:
            rejected.append(
                {
                    "reason": "missing_explicit_tour_marker",
                    "route": route,
                    "source_match_id": _value(row, "match_id", "matchId", "id")
                    or str(index),
                }
            )
            continue
        match_id = _value(row, "match_id", "matchId", "id") or content_id(
            "source_match", row
        )
        player1 = _value(row, "player1", "playerOne", "competitorA", "teamOne")
        player2 = _value(row, "player2", "playerTwo", "competitorB", "teamTwo")
        status = (_value(row, "status", "matchStatus") or "fixture").casefold()
        observations.append(
            OfficialMatchObservation(
                source_label=source_label,
                source_event_id=source_event_id,
                source_match_id=match_id,
                tour=displayed_tour,
                route=route,
                round=_value(row, "round", "roundName"),
                player1_name=player1,
                player1_id=_value(row, "player1_id", "playerOneId", "competitorAId"),
                player2_name=player2,
                player2_id=_value(row, "player2_id", "playerTwoId", "competitorBId"),
                winner_name=_value(row, "winner", "winnerName"),
                status=status,
                score=_value(row, "score", "displayScore"),
                local_date=_value(row, "date", "localDate", "scheduleDate"),
                local_time=_value(row, "time", "localTime", "startTime"),
                ordering_label=_value(row, "ordering", "timeLabel", "notBefore"),
                court=_value(row, "court", "courtName"),
                source_url=source_url,
                artifact_sha256=digest,
            )
        )
    unique: dict[tuple[str, str, str], OfficialMatchObservation] = {}
    for observation in observations:
        key = (
            observation.tour,
            observation.source_event_id,
            observation.source_match_id,
        )
        current = unique.get(key)
        if current and canonical_json(asdict(current)) != canonical_json(asdict(observation)):
            rejected.append(
                {
                    "reason": "conflicting_duplicate_source_match",
                    "route": route,
                    "source_match_id": observation.source_match_id,
                }
            )
            unique.pop(key, None)
        elif key not in unique:
            unique[key] = observation
    return list(unique.values()), rejected


def capture_rendered_page(
    *,
    source_label: str,
    url: str,
    route: str,
    fields: set[str],
    policy: SourcePolicyRegistry,
    timeout_ms: int = 30_000,
) -> bytes:
    """Capture rendered HTML only after the fail-closed policy gate passes."""
    policy.require_network_request(
        source_label,
        url,
        route=route,
        fields=fields,
        use="private_evaluation",
    )
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        payload = page.content().encode("utf-8")
        browser.close()
    return payload


def parse_oop_text(
    text: str,
    *,
    source_label: str,
    source_event_id: str,
    source_url: str,
    artifact_sha256: str,
) -> tuple[list[OfficialMatchObservation], list[dict[str, str]]]:
    """Parse normalized OOP text extracted from an immutable PDF fixture.

    Each match line uses the fixture contract
    ``MATCH|id|tour|round|player1|player2|date|time|ordering|court``. The
    explicit per-row tour marker is required for combined events.
    """
    observations: list[OfficialMatchObservation] = []
    rejected: list[dict[str, str]] = []
    for ordinal, line in enumerate(text.splitlines(), 1):
        if not line.strip().startswith("MATCH|"):
            continue
        values = [item.strip() for item in line.split("|")]
        if len(values) != 10:
            rejected.append(
                {"reason": "malformed_oop_match_line", "line": str(ordinal)}
            )
            continue
        (
            _,
            match_id,
            tour,
            round_name,
            player1,
            player2,
            local_date,
            local_time,
            ordering,
            court,
        ) = values
        tour = tour.casefold()
        if tour not in {"atp", "wta"}:
            rejected.append(
                {
                    "reason": "missing_explicit_tour_marker",
                    "source_match_id": match_id or str(ordinal),
                }
            )
            continue
        observations.append(
            OfficialMatchObservation(
                source_label=source_label,
                source_event_id=source_event_id,
                source_match_id=match_id or content_id("oop_match", values),
                tour=tour,
                route="order-of-play-pdf",
                round=round_name or None,
                player1_name=player1 or None,
                player1_id=None,
                player2_name=player2 or None,
                player2_id=None,
                winner_name=None,
                status="fixture",
                score=None,
                local_date=local_date or None,
                local_time=local_time or None,
                ordering_label=ordering or None,
                court=court or None,
                source_url=source_url,
                artifact_sha256=artifact_sha256,
            )
        )
    if not observations and not rejected:
        rejected.append({"reason": "oop_matches_not_found"})
    return observations, rejected


def parse_official_pdf(
    payload: bytes,
    *,
    source_label: str,
    source_event_id: str,
    source_url: str,
) -> tuple[list[OfficialMatchObservation], list[dict[str, str]]]:
    """Extract and parse an offline PDF; malformed documents fail closed."""
    digest = hashlib.sha256(payload).hexdigest()
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(payload))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        # pypdf raises several backend-specific parse exceptions. Treat all as
        # an immutable artifact rejection; no fallback network path is allowed.
        return [], [{"reason": "malformed_pdf", "detail": type(exc).__name__}]
    return parse_oop_text(
        text,
        source_label=source_label,
        source_event_id=source_event_id,
        source_url=source_url,
        artifact_sha256=digest,
    )


def parse_official_fixture_tree(root: Path) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for path in sorted(root.glob("**/*.html")):
        parts = path.stem.split("__")
        if len(parts) < 4 or parts[0] not in {"atp", "wta"}:
            rejected.append({"reason": "invalid_fixture_filename", "path": str(path)})
            continue
        source_label = f"{parts[0]}-site"
        route, event_id = parts[1], parts[2]
        parsed, invalid = parse_official_html(
            path.read_bytes(),
            source_label=source_label,
            source_event_id=event_id,
            route=route,
            source_url=f"offline://{path.name}",
        )
        observations.extend(
            {"observation_id": item.observation_id, **asdict(item)} for item in parsed
        )
        rejected.extend(invalid)
    for path in sorted(root.glob("**/*.pdf")):
        parts = path.stem.split("__")
        if len(parts) < 3 or parts[0] not in {"atp", "wta"}:
            rejected.append({"reason": "invalid_fixture_filename", "path": str(path)})
            continue
        source_label = f"{parts[0]}-oop"
        parsed, invalid = parse_official_pdf(
            path.read_bytes(),
            source_label=source_label,
            source_event_id=parts[2],
            source_url=f"offline://{path.name}",
        )
        observations.extend(
            {"observation_id": item.observation_id, **asdict(item)} for item in parsed
        )
        rejected.extend(invalid)
    return {"observations": observations, "rejections": rejected}
