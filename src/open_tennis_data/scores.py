"""Tennis score parsing for Sackmann and Wikimedia notations."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

TERMINATIONS = {
    "RET": "retired",
    "W/O": "walkover",
    "WO": "walkover",
    "DEF": "defaulted",
    "ABD": "abandoned",
    "ABN": "abandoned",
}


def _clean(value: str) -> str:
    value = re.sub(r"'{2,3}", "", value or "")
    value = re.sub(r"<sup>.*?</sup>", "", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return value.strip()


def parse_sackmann_score(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    upper = raw.upper()
    termination = next((value for key, value in TERMINATIONS.items() if key in upper), None)
    sets: list[dict[str, Any]] = []
    for token in raw.split():
        if token.upper() in TERMINATIONS:
            continue
        match = re.match(r"\[?(\d+)-(\d+)(?:\((\d+)\))?\]?", token)
        if not match:
            continue
        sets.append(
            {
                "winner_games": int(match.group(1)),
                "loser_games": int(match.group(2)),
                "tiebreak_loser_points": int(match.group(3)) if match.group(3) else None,
            }
        )
    return {"raw": raw, "sets": sets, "termination": termination}


def parse_bracket_scores(
    first: Sequence[str], second: Sequence[str], winner_index: int
) -> dict[str, Any]:
    sets: list[dict[str, Any]] = []
    termination: str | None = None
    raw_parts: list[str] = []
    for left_raw, right_raw in zip(first, second):
        left_upper = left_raw.upper()
        right_upper = right_raw.upper()
        for key, value in TERMINATIONS.items():
            if key in left_upper or key in right_upper:
                termination = value
        if re.search(r"<sup>\s*r\s*</sup>|\bret\.?\b", left_raw + " " + right_raw, re.I):
            termination = "retired"
        left = _clean(left_raw)
        right = _clean(right_raw)
        left_number = re.search(r"\d+", left)
        right_number = re.search(r"\d+", right)
        if not left_number or not right_number:
            continue
        left_games, right_games = int(left_number.group()), int(right_number.group())
        winner_games = left_games if winner_index == 0 else right_games
        loser_games = right_games if winner_index == 0 else left_games
        sets.append(
            {
                "winner_games": winner_games,
                "loser_games": loser_games,
                "tiebreak_loser_points": None,
            }
        )
        raw_parts.append(f"{winner_games}-{loser_games}")
    return {"raw": " ".join(raw_parts), "sets": sets, "termination": termination}
