"""Approved match-level date sources and conservative reconciliation.

Tournament dates are deliberately absent from the output path in this module.
Every accepted observation represents a source assertion at day precision.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from python_calamine import CalamineWorkbook

USER_AGENT = "open-tennis-data (https://github.com/ryantjx/tennis-match-data)"
TENNIS_DATA_BASE = "http://www.tennis-data.co.uk"
WTA_API_BASE = "https://api.wtatennis.com/tennis"
TENNIS_TV_API_BASE = "https://api.tennistv.com/tennis/v1"


@dataclass(frozen=True)
class DateSource:
    kind: str
    tour: str
    year: int
    source_label: str
    source_path: str
    source_url: str
    revision: str
    sha256: str
    license: str
    source_rows: int
    parser_version: str = "3.3"
    policy_revision: str = "v3-2026-07-24"

    @property
    def source_file_id(self) -> str:
        context = "|".join(
            (
                self.source_label,
                self.source_url,
                self.revision,
                self.sha256,
                self.kind,
                self.tour,
            )
        )
        return "source_file_" + hashlib.sha256(context.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class DateRow:
    tour: str
    source_year: int
    played_on: date
    tournament: str
    location: str
    round: str
    winner: str
    loser: str
    score: str | None
    source_file_id: str
    source_match_id: str
    row_fingerprint: str
    observed_at: datetime | None = None
    source_timezone: str | None = None
    venue_timezone: str | None = None
    date_role: str = "played"
    date_precision: str = "day"
    parser_version: str = "3.3"
    policy_revision: str = "v3-2026-07-24"


@dataclass(frozen=True)
class CanonicalMatch:
    match_id: str
    tour: str
    year: int
    tournament_name: str
    tournament_start: date | None
    tournament_end: date | None
    round: str
    player1_name: str
    player2_name: str
    score: str | None


@dataclass(frozen=True)
class ReconciledDate:
    row: DateRow
    match_id: str | None
    candidate_match_ids: tuple[str, ...]
    reason: str | None
    match_method: str


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def parse_excel_date(value: Any) -> date | None:
    """Parse Calamine values, Excel serials, and supported source strings."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = float(value)
        if not 1 <= serial <= 100_000:
            return None
        return date(1899, 12, 30) + timedelta(days=int(serial))
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def _download(url: str, attempts: int = 4) -> bytes:
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def _json_request(
    base: str, path: str, params: dict[str, Any], headers: dict[str, str]
) -> tuple[Any, str, str]:
    query = urllib.parse.urlencode(params)
    url = f"{base}{path}?{query}" if query else f"{base}{path}"
    for attempt in range(4):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
            return json.loads(payload), url, hashlib.sha256(payload).hexdigest()
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def tennis_data_url(tour: str, year: int, suffix: str) -> str:
    directory = str(year) if tour == "atp" else f"{year}w"
    return f"{TENNIS_DATA_BASE}/{directory}/{year}.{suffix}"


def fetch_tennis_data_file(tour: str, year: int, destination: Path) -> tuple[Path, str]:
    """Fetch XLSX or legacy XLS, returning the final path and source URL."""
    minimum = 2000 if tour == "atp" else 2007
    if tour not in {"atp", "wta"} or year < minimum:
        raise ValueError(f"tennis-data.co.uk does not cover {tour}/{year}")
    last_error: Exception | None = None
    for suffix in ("xlsx", "xls"):
        url = tennis_data_url(tour, year, suffix)
        try:
            payload = _download(url)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            last_error = exc
            continue
        if payload[:4] not in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
            last_error = ValueError(f"unexpected spreadsheet payload from {url}")
            continue
        final = destination.with_suffix(".xlsx" if payload.startswith(b"PK") else ".xls")
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(payload)
        return final, url
    raise RuntimeError(f"could not download {tour}/{year}") from last_error


def _score_from_row(row: dict[str, Any]) -> str | None:
    sets: list[str] = []
    for number in range(1, 6):
        winner = row.get(f"W{number}")
        loser = row.get(f"L{number}")
        if winner in (None, "") or loser in (None, ""):
            continue
        try:
            winner_games = int(float(str(winner)))
            loser_games = int(float(str(loser)))
        except (TypeError, ValueError):
            continue
        sets.append(f"{winner_games}-{loser_games}")
    suffix = str(row.get("Comment") or "").strip().upper()
    if suffix in {"RETIRED", "RET"}:
        suffix = "RET"
    elif suffix in {"WALKOVER", "W/O", "WO"}:
        suffix = "W/O"
    elif suffix in {"DEFAULTED", "DEF"}:
        suffix = "DEF"
    else:
        suffix = ""
    result = " ".join((*sets, suffix)).strip()
    return result or None


def parse_tennis_data_file(
    path: Path, tour: str, year: int, source_url: str
) -> tuple[DateSource, list[DateRow], list[dict[str, Any]]]:
    """Parse a yearly XLS/XLSX file and retain malformed date rows as rejects."""
    payload_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    workbook = CalamineWorkbook.from_path(str(path))
    rows = workbook.get_sheet_by_index(0).to_python()
    workbook.close()
    if not rows:
        raise ValueError(f"empty spreadsheet: {path}")
    headers = [str(value).strip() for value in rows[0]]
    source = DateSource(
        kind="match_dates",
        tour=tour,
        year=year,
        source_label="tennis-data.co.uk",
        source_path=source_url.removeprefix(TENNIS_DATA_BASE + "/"),
        source_url=source_url,
        revision=f"sha256:{payload_sha}",
        sha256=payload_sha,
        license="Tennis-Data redistribution terms",
        source_rows=max(0, len(rows) - 1),
    )
    parsed: list[DateRow] = []
    rejected: list[dict[str, Any]] = []
    for ordinal, values in enumerate(rows[1:], 2):
        record = {header: values[index] if index < len(values) else None for index, header in enumerate(headers)}
        fingerprint = _digest(record)
        source_match_id = f"tennis-data:{tour}:{year}:{ordinal}"
        played_on = parse_excel_date(record.get("Date"))
        required = (record.get("Winner"), record.get("Loser"), record.get("Tournament"))
        if tour == "atp" and year < 2003:
            rejected.append(
                {
                    "tour": tour,
                    "year": year,
                    "source_label": source.source_label,
                    "source_path": source.source_path,
                    "source_file_id": source.source_file_id,
                    "source_match_id": source_match_id,
                    "row_fingerprint": fingerprint,
                    "candidate_match_ids": None,
                    "reason": "tournament_date_not_match_date",
                }
            )
            continue
        if played_on is None or not all(str(value or "").strip() for value in required):
            rejected.append(
                {
                    "tour": tour,
                    "year": year,
                    "source_label": source.source_label,
                    "source_path": source.source_path,
                    "source_file_id": source.source_file_id,
                    "source_match_id": source_match_id,
                    "row_fingerprint": fingerprint,
                    "candidate_match_ids": None,
                    "reason": "invalid_exact_date_source_row",
                }
            )
            continue
        parsed.append(
            DateRow(
                tour=tour,
                source_year=year,
                played_on=played_on,
                tournament=str(record.get("Tournament") or "").strip(),
                location=str(record.get("Location") or "").strip(),
                round=str(record.get("Round") or "").strip(),
                winner=str(record.get("Winner") or "").strip(),
                loser=str(record.get("Loser") or "").strip(),
                score=_score_from_row(record),
                source_file_id=source.source_file_id,
                source_match_id=source_match_id,
                row_fingerprint=fingerprint,
            )
        )
    return source, parsed, rejected


def local_calendar_date(timestamp: Any, venue_timezone: str | None) -> date | None:
    """Convert an aware source timestamp to the venue's local calendar day."""
    if timestamp in (None, "") or not venue_timezone:
        return None
    text = str(timestamp).strip().replace("Z", "+00:00")
    try:
        instant = datetime.fromisoformat(text)
        zone = ZoneInfo(venue_timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if instant.tzinfo is None:
        return None
    return instant.astimezone(zone).date()


def _tournament_timezone(tournament: dict[str, Any]) -> str | None:
    group = tournament.get("tournamentGroup") or {}
    for container in (tournament, group, group.get("metadata") or {}):
        for key in (
            "venue_timezone",
            "venueTimezone",
            "timeZone",
            "timezone",
            "Timezone",
            "TimeZone",
        ):
            value = container.get(key)
            if value:
                return str(value)
    return None


def _text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    ascii_value = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def _name_parts(value: str, *, source_order: bool) -> tuple[str, str]:
    parts = _text(value).split()
    if not parts:
        return "", ""
    if source_order and len(parts) > 1:
        initial_count = 0
        for token in reversed(parts):
            if len(token) != 1:
                break
            initial_count += 1
        initial_count = max(initial_count, 1)
        return " ".join(parts[:-initial_count]), "".join(parts[-initial_count:])
    return " ".join(parts[1:]), parts[0][:1]


def _name_matches(source_name: str, canonical_name: str) -> bool:
    source_surname, source_initial = _name_parts(source_name, source_order=True)
    canonical_surname, canonical_initial = _name_parts(canonical_name, source_order=False)
    if (
        not source_surname
        or not canonical_surname
        or source_initial[:1] != canonical_initial[:1]
    ):
        return False
    source_tokens = source_surname.split()
    canonical_tokens = canonical_surname.split()
    return (
        source_surname == canonical_surname
        or source_tokens[-1:] == canonical_tokens[-1:]
        or source_surname in canonical_surname
        or canonical_surname in source_surname
    )


def _round(value: str) -> str:
    normalized = _text(value)
    mapping = {
        "final": "F",
        "semifinals": "SF",
        "semi finals": "SF",
        "quarterfinals": "QF",
        "quarter finals": "QF",
        "round robin": "RR",
    }
    return mapping.get(normalized, "")


def _normalized_score(value: str | None) -> str:
    text = re.sub(r"\([^)]*\)|\[[^]]*\]", "", (value or "").upper())
    text = re.sub(r"\b(?:RETIRED|RET|W/O|WO|WALKOVER|DEF|DEFAULTED|ABD|ABN)\b", "", text)
    return re.sub(r"[^0-9-]+", " ", text).strip()


def _event_matches(source: DateRow, match: CanonicalMatch) -> bool:
    source_event = _text(source.tournament)
    canonical_event = _text(match.tournament_name)
    location = _text(source.location)
    ignored = {"open", "championships", "international", "classic", "tennis", "tournament"}
    source_tokens = set(source_event.split()) - ignored
    canonical_tokens = set(canonical_event.split()) - ignored
    return bool(source_tokens & canonical_tokens) or bool(location and location in canonical_event)


def reconcile_date_rows(
    rows: Iterable[DateRow], matches: Sequence[CanonicalMatch]
) -> list[ReconciledDate]:
    """Require a unique evidence-supported candidate; never assign ties."""
    by_participants: dict[tuple[str, str, str, str, str], list[CanonicalMatch]] = {}
    for match in matches:
        winner_surname, winner_initial = _name_parts(match.player1_name, source_order=False)
        loser_surname, loser_initial = _name_parts(match.player2_name, source_order=False)
        key = (
            match.tour,
            winner_surname.split()[-1] if winner_surname else "",
            winner_initial[:1],
            loser_surname.split()[-1] if loser_surname else "",
            loser_initial[:1],
        )
        by_participants.setdefault(key, []).append(match)
    results: list[ReconciledDate] = []
    for row in rows:
        candidates: list[tuple[int, CanonicalMatch]] = []
        winner_surname, winner_initial = _name_parts(row.winner, source_order=True)
        loser_surname, loser_initial = _name_parts(row.loser, source_order=True)
        key = (
            row.tour,
            winner_surname.split()[-1] if winner_surname else "",
            winner_initial[:1],
            loser_surname.split()[-1] if loser_surname else "",
            loser_initial[:1],
        )
        for match in by_participants.get(key, []):
            if match.year not in {row.source_year - 1, row.source_year, row.source_year + 1}:
                continue
            if not _name_matches(row.winner, match.player1_name) or not _name_matches(
                row.loser, match.player2_name
            ):
                continue
            if match.tournament_start is not None:
                end = match.tournament_end or match.tournament_start + timedelta(days=21)
                if not match.tournament_start - timedelta(days=2) <= row.played_on <= end + timedelta(days=3):
                    continue
            score = 1
            if _event_matches(row, match):
                score += 4
            source_round = _round(row.round)
            if source_round and source_round == match.round:
                score += 2
            if row.score and _normalized_score(row.score) == _normalized_score(match.score):
                score += 2
            candidates.append((score, match))
        if not candidates:
            results.append(ReconciledDate(row, None, (), "unmatched_exact_date", "none"))
            continue
        best = max(score for score, _ in candidates)
        best_matches = sorted(
            {match.match_id: match for score, match in candidates if score == best}.values(),
            key=lambda item: item.match_id,
        )
        if len(best_matches) != 1:
            results.append(
                ReconciledDate(
                    row,
                    None,
                    tuple(item.match_id for item in best_matches),
                    "ambiguous_exact_date",
                    "ambiguous",
                )
            )
            continue
        method = "participants+window"
        if best >= 5:
            method += "+event"
        if best >= 7:
            method += "+round_or_score"
        results.append(ReconciledDate(row, best_matches[0].match_id, (), None, method))
    return results


def quarantine_conflicting_dates(
    reconciled: Sequence[ReconciledDate],
) -> tuple[list[ReconciledDate], list[ReconciledDate]]:
    accepted = [item for item in reconciled if item.match_id is not None]
    dates: dict[str, set[date]] = {}
    for item in accepted:
        dates.setdefault(str(item.match_id), set()).add(item.row.played_on)
    conflicts = {match_id for match_id, values in dates.items() if len(values) > 1}
    kept: list[ReconciledDate] = []
    rejected: list[ReconciledDate] = []
    for item in reconciled:
        if item.match_id in conflicts:
            rejected.append(
                ReconciledDate(
                    item.row,
                    None,
                    (str(item.match_id),),
                    "conflicting_exact_date",
                    "conflict",
                )
            )
        else:
            kept.append(item)
    return kept, rejected


def parse_wta_api_completed(
    matches: Sequence[dict[str, Any]],
    *,
    tournament: dict[str, Any],
    source: DateSource,
) -> tuple[list[DateRow], list[dict[str, Any]]]:
    """Parse final WTA API singles rows using ``MatchTimeStamp`` only."""
    parsed: list[DateRow] = []
    rejected: list[dict[str, Any]] = []
    group = tournament.get("tournamentGroup") or {}
    event_name = str(
        tournament.get("name")
        or group.get("name")
        or group.get("metadata", {}).get("tournament_summary_heading")
        or ""
    )
    location = str(tournament.get("city") or tournament.get("location") or "")
    venue_timezone = _tournament_timezone(tournament)
    for ordinal, match in enumerate(matches, 1):
        if match.get("DrawMatchType") != "S" or match.get("MatchState") != "F":
            continue
        winner_side = str(match.get("Winner") or "")
        timestamp = match.get("MatchTimeStamp")
        played_on = local_calendar_date(timestamp, venue_timezone)
        player_a = " ".join(
            str(match.get(field) or "").strip()
            for field in ("PlayerNameLastA", "PlayerNameFirstA")
        ).strip()
        player_b = " ".join(
            str(match.get(field) or "").strip()
            for field in ("PlayerNameLastB", "PlayerNameFirstB")
        ).strip()
        fingerprint = _digest(match)
        source_match_id = str(match.get("MatchID") or match.get("MatchId") or ordinal)
        if winner_side not in {"1", "2"} or played_on is None or not player_a or not player_b:
            rejected.append(
                {
                    "tour": "wta",
                    "year": source.year,
                    "source_label": source.source_label,
                    "source_path": source.source_path,
                    "source_file_id": source.source_file_id,
                    "source_match_id": source_match_id,
                    "row_fingerprint": fingerprint,
                    "candidate_match_ids": None,
                    "reason": "invalid_exact_date_source_row",
                }
            )
            continue
        winner, loser = (player_a, player_b) if winner_side == "1" else (player_b, player_a)
        parsed.append(
            DateRow(
                tour="wta",
                source_year=source.year,
                played_on=played_on,
                tournament=event_name,
                location=location,
                round=str(match.get("RoundID") or ""),
                winner=winner,
                loser=loser,
                score=str(match.get("Score") or "") or None,
                source_file_id=source.source_file_id,
                source_match_id=source_match_id,
                row_fingerprint=fingerprint,
                source_timezone="UTC",
                venue_timezone=venue_timezone,
                parser_version=source.parser_version,
                policy_revision=source.policy_revision,
            )
        )
    return parsed, rejected


def parse_tennis_tv_completed(
    matches: Sequence[dict[str, Any]],
    *,
    tournament: dict[str, Any],
    source: DateSource,
) -> tuple[list[DateRow], list[dict[str, Any]]]:
    """Parse completed Tennis TV singles rows using ``MatchDate`` evidence."""
    parsed: list[DateRow] = []
    rejected: list[dict[str, Any]] = []
    venue_timezone = _tournament_timezone(tournament)

    def player(team: dict[str, Any] | None) -> str:
        team = team or {}
        first = team.get("PlayerFirstNameFull") or team.get("PlayerFirstName") or ""
        last = team.get("PlayerLastName") or ""
        return f"{last} {str(first)[:1]}".strip()

    for ordinal, match in enumerate(matches, 1):
        match_id = str(match.get("MatchId") or match.get("MatchCode") or ordinal)
        status = str(match.get("Status") or "").lower()
        winner_value = match.get("WinningPlayerId") or match.get("Winner")
        result = str(match.get("ResultString") or "").strip()
        if not (winner_value or result or status in {"finished", "complete", "completed"}):
            continue
        team1 = match.get("PlayerTeam1") or {}
        team2 = match.get("PlayerTeam2") or {}
        if team1.get("PartnerId") or team2.get("PartnerId"):
            continue
        timestamp = match.get("MatchDate")
        played_on = local_calendar_date(timestamp, venue_timezone)
        first, second = player(team1), player(team2)
        winner_id = str(winner_value or "")
        first_id = str(team1.get("PlayerId") or "")
        winner, loser = (first, second) if not winner_id or winner_id == first_id else (second, first)
        fingerprint = _digest(match)
        if played_on is None or not winner or not loser or not result:
            rejected.append(
                {
                    "tour": "atp",
                    "year": source.year,
                    "source_label": source.source_label,
                    "source_path": source.source_path,
                    "source_file_id": source.source_file_id,
                    "source_match_id": match_id,
                    "row_fingerprint": fingerprint,
                    "candidate_match_ids": None,
                    "reason": "invalid_exact_date_source_row",
                }
            )
            continue
        parsed.append(
            DateRow(
                tour="atp",
                source_year=source.year,
                played_on=played_on,
                tournament=str(tournament.get("Name") or tournament.get("TournamentName") or ""),
                location=str(tournament.get("Location") or ""),
                round=str(match.get("RoundName") or match.get("Round") or ""),
                winner=winner,
                loser=loser,
                score=result,
                source_file_id=source.source_file_id,
                source_match_id=match_id,
                row_fingerprint=fingerprint,
                source_timezone="UTC",
                venue_timezone=venue_timezone,
                parser_version=source.parser_version,
                policy_revision=source.policy_revision,
            )
        )
    return parsed, rejected


def fetch_live_completed_sources(
    year: int, through: date
) -> list[tuple[DateSource, list[DateRow], list[dict[str, Any]]]]:
    """Fetch current WTA API and Tennis TV completed-match date evidence."""
    start = date(year, 1, 1).isoformat()
    end = through.isoformat()
    results: list[tuple[DateSource, list[DateRow], list[dict[str, Any]]]] = []

    wta_payload, _, _ = _json_request(
        WTA_API_BASE,
        "/tournaments/",
        {"page": 0, "pageSize": 100, "excludeLevels": "ITF", "from": start, "to": end},
        {"account": "wta"},
    )
    for tournament in wta_payload.get("content") or []:
        group = tournament.get("tournamentGroup") or {}
        group_id = group.get("id")
        tournament_year = tournament.get("year")
        if group_id is None or tournament_year is None:
            continue
        payload, url, payload_sha = _json_request(
            WTA_API_BASE,
            f"/tournaments/{group_id}/{tournament_year}/matches",
            {"from": start, "to": end},
            {"account": "wta"},
        )
        relevant = [
            item
            for item in payload.get("matches") or []
            if item.get("DrawMatchType") == "S" and item.get("MatchState") == "F"
        ]
        source = DateSource(
            "match_dates", "wta", year, "wta-api", url, url,
            f"sha256:{payload_sha}", payload_sha, "WTA data terms", len(relevant),
        )
        parsed, rejected = parse_wta_api_completed(
            relevant, tournament=tournament, source=source
        )
        results.append((source, parsed, rejected))

    tv_payload, _, _ = _json_request(
        TENNIS_TV_API_BASE,
        "/tournaments",
        {"from": start, "to": end},
        {"account-id": "35"},
    )
    tournaments = tv_payload if isinstance(tv_payload, list) else (
        tv_payload.get("tournaments") or tv_payload.get("content") or []
    )
    for tournament in tournaments:
        if str(tournament.get("gender") or "").upper() not in {"ATP", "JOINT"}:
            continue
        tournament_id = tournament.get("id")
        tournament_year = tournament.get("year")
        if tournament_id is None or tournament_year is None:
            continue
        payload, url, payload_sha = _json_request(
            TENNIS_TV_API_BASE,
            "/matches",
            {"tournamentId": tournament_id, "year": tournament_year},
            {"account-id": "35"},
        )
        rows = payload.get("matches") or [] if isinstance(payload, dict) else []
        relevant = [
            item
            for item in rows
            if not (item.get("PlayerTeam1") or {}).get("PartnerId")
            and not (item.get("PlayerTeam2") or {}).get("PartnerId")
            and (
                item.get("WinningPlayerId")
                or item.get("Winner")
                or item.get("ResultString")
                or str(item.get("Status") or "").lower()
                in {"finished", "complete", "completed"}
            )
        ]
        source = DateSource(
            "match_dates", "atp", year, "tennis-tv", url, url,
            f"sha256:{payload_sha}", payload_sha, "Tennis TV data terms", len(relevant),
        )
        parsed, rejected = parse_tennis_tv_completed(
            relevant, tournament=tournament, source=source
        )
        results.append((source, parsed, rejected))
    return results
