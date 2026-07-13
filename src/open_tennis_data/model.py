"""Canonical identifiers and normalization helpers."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

ROUND_ALIASES = {
    "first round": "R128",
    "second round": "R64",
    "third round": "R32",
    "fourth round": "R16",
    "round of 128": "R128",
    "round of 64": "R64",
    "round of 32": "R32",
    "round of 16": "R16",
    "quarterfinals": "QF",
    "quarter-finals": "QF",
    "quarterfinal": "QF",
    "semifinals": "SF",
    "semi-finals": "SF",
    "semifinal": "SF",
    "final": "F",
    "qualifying first round": "Q1",
    "qualifying second round": "Q2",
    "qualifying round": "Q3",
}


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def slugify(value: str) -> str:
    return normalize_text(value).replace(" ", "-") or "unknown-event"


def canonical_round(value: str) -> str:
    stripped = re.sub(r"\s+", " ", (value or "").strip())
    if not stripped:
        return ""
    upper = stripped.upper()
    if re.fullmatch(r"(?:R(?:16|32|64|128)|Q[123]|QF|SF|F|RR)", upper):
        return upper
    return ROUND_ALIASES.get(stripped.lower(), stripped)


def canonical_player_id(tour: str, source_id: str, name: str) -> str:
    if source_id:
        if source_id.startswith(("atp:", "wta:", "wikimedia:")):
            return source_id
        return f"{tour}:{source_id}"
    return f"name:{hashlib.sha256(normalize_text(name).encode()).hexdigest()[:16]}"


def match_id(
    tour: str,
    year: int,
    event_name: str,
    draw: str,
    round_name: str,
    player_names: Sequence[str],
) -> str:
    players = sorted(normalize_text(name) for name in player_names)
    identity = "|".join(
        [tour, str(year), normalize_text(event_name), draw, canonical_round(round_name), *players]
    )
    return f"{tour}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def new_event(
    *,
    tour: str,
    year: int,
    event_name: str,
    draw: str,
    event_id: str,
    event_start_date: str | None,
    surface: str | None,
    level: str | None,
) -> dict[str, Any]:
    return {
        "kind": "event",
        "event": {
            "id": event_id,
            "name": event_name,
            "tour": tour,
            "year": year,
            "draw": draw,
            "discipline": "singles",
            "level": level,
            "surface": surface,
            "start_date": event_start_date,
        },
        "source_catalog": {},
        "matches": [],
    }


def semantic_match(match: Mapping[str, Any]) -> tuple[Any, ...]:
    score = match.get("score") or ""
    if isinstance(score, dict):
        score = score.get("raw", "")
    score = re.sub(r"\([^)]*\)|\[[^]]*\]", "", str(score).upper())
    score = re.sub(r"\b(?:RET|W/O|WO|DEF|ABD|ABN)\b", "", score)
    score = re.sub(r"\s+", " ", score).strip()
    return (
        match.get("winner_id"),
        match.get("status"),
        match.get("round"),
        score,
    )
