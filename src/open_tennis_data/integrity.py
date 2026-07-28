"""Source-neutral v3.1 integrity contracts used by v3.2 adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NO_PLAY_STATUSES = {"walkover", "cancelled", "pre_play_default"}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def content_id(prefix: str, value: Any, *, length: int = 24) -> str:
    return f"{prefix}_{hashlib.sha256(canonical_json(value)).hexdigest()[:length]}"


def utc_timestamp(value: str | datetime) -> datetime:
    instant = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if instant.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return instant.astimezone(UTC).replace(microsecond=0)


@dataclass(frozen=True)
class EvidenceArtifact:
    source_label: str
    source_url: str
    publisher_revision: str | None
    retrieved_at: str
    publisher_modified_at: str | None
    media_type: str
    byte_length: int
    sha256: str
    storage_mode: str
    retention_basis: str

    @property
    def evidence_artifact_id(self) -> str:
        return content_id("artifact", asdict(self))


@dataclass(frozen=True)
class ArtifactEvaluation:
    evidence_artifact_id: str
    policy_revision: str
    evaluated_at: str
    eligible_for_public_projection: bool
    allowed_fields: tuple[str, ...]
    allowed_roles: tuple[str, ...]
    reason: str
    supersedes_evaluation_id: str | None = None

    @property
    def evaluation_id(self) -> str:
        return content_id("evaluation", asdict(self))


@dataclass(frozen=True)
class ScheduleObservation:
    source_label: str
    source_event_id: str
    source_fixture_id: str
    asserted_local_date: str | None
    asserted_local_time: str | None
    court: str | None
    status: str
    source_timezone: str | None
    source_venue_text: str | None
    evidence_artifact_id: str
    observed_at: str
    parser_version: str
    policy_revision: str

    @property
    def observation_id(self) -> str:
        return content_id("schedule", asdict(self))


@dataclass(frozen=True)
class ScheduleObservationLink:
    observation_id: str
    fixture_key: str
    match_id: str
    resolution_method: str
    confidence_basis: str
    linked_at: str
    supersedes_link_revision_id: str | None
    review_status: str

    @property
    def link_revision_id(self) -> str:
        return content_id("schedule_link", asdict(self))


def stable_fixture_key(
    *,
    tour: str,
    event_edition_id: str,
    draw: str,
    round_name: str,
    draw_position: str,
) -> str:
    return content_id(
        "fixture",
        {
            "tour": tour,
            "event_edition_id": event_edition_id,
            "draw": draw,
            "round": round_name,
            "draw_position": draw_position,
        },
    )


def lifecycle_match_id(fixture_key: str) -> str:
    return content_id("match", {"fixture_key": fixture_key})


def validate_revision_chain(
    rows: Sequence[Mapping[str, Any]],
    *,
    id_field: str,
    predecessor_field: str,
    scope_fields: Iterable[str],
) -> list[str]:
    """Validate a single-parent append-only chain without mutating any row."""
    errors: list[str] = []
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        identifier = str(row.get(id_field) or "")
        if not identifier:
            errors.append(f"missing {id_field}")
            continue
        if identifier in by_id:
            errors.append(f"duplicate {id_field}: {identifier}")
        by_id[identifier] = row
    for identifier, row in by_id.items():
        predecessor = row.get(predecessor_field)
        if not predecessor:
            continue
        predecessor_id = str(predecessor)
        parent = by_id.get(predecessor_id)
        if parent is None:
            errors.append(f"{identifier}: missing predecessor {predecessor_id}")
            continue
        for field in scope_fields:
            if row.get(field) != parent.get(field):
                errors.append(
                    f"{identifier}: predecessor changes scope field {field}"
                )
        seen = {identifier}
        cursor: Mapping[str, Any] | None = parent
        while cursor is not None:
            cursor_id = str(cursor.get(id_field) or "")
            if cursor_id in seen:
                errors.append(f"{identifier}: revision cycle")
                break
            seen.add(cursor_id)
            previous = cursor.get(predecessor_field)
            cursor = by_id.get(str(previous)) if previous else None
    return sorted(set(errors))


def validate_material_date(
    *,
    played_on: date | None,
    source_year: int,
    source_cutoff_at: str | datetime,
    event_start: date | None,
    event_end: date | None,
    venue_timezone: str | None,
    status: str,
    has_event_or_draw_identity: bool,
    evidence_dimensions: set[str],
) -> list[str]:
    errors: list[str] = []
    if status in NO_PLAY_STATUSES and played_on is not None:
        errors.append("no-play outcome cannot have a played date")
    if played_on is None:
        return errors
    if played_on.year != source_year:
        errors.append("played date is outside the declared source season")
    cutoff = utc_timestamp(source_cutoff_at)
    if played_on > cutoff.date() + timedelta(days=1):
        errors.append("played date is after the conservative source cutoff")
    if event_start and played_on < event_start:
        errors.append("played date is before the event window")
    if event_end and played_on > event_end + timedelta(days=1):
        errors.append("played date is after the event window")
    if not venue_timezone:
        errors.append("played date lacks venue timezone evidence")
    else:
        try:
            ZoneInfo(venue_timezone)
        except ZoneInfoNotFoundError:
            errors.append("played date has invalid IANA timezone")
    if not has_event_or_draw_identity:
        errors.append("date reconciliation lacks event or draw identity")
    if not evidence_dimensions.intersection(
        {"round", "score", "source_match_id", "draw_position"}
    ):
        errors.append("date reconciliation is participant-only")
    return errors
