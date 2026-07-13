"""Reusable Wikimedia future draw-slot parsing for Parquet."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

import mwparserfromhell

from open_tennis_data.model import canonical_round, normalize_text
from open_tennis_data.sources import wikimedia


def fixture_id(*parts: Any) -> str:
    identity = "|".join(str(part or "") for part in parts)
    return f"fixture-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _plain(value: str) -> str:
    return str(mwparserfromhell.parse(value or "").strip_code()).strip(" ' \n\t")


def _parameter_map(template: Any) -> dict[str, str]:
    return {
        str(parameter.name).strip(): str(parameter.value).strip() for parameter in template.params
    }


def _player_from_markup(
    markup: str,
    tour: str,
    players: dict[str, dict[str, Any]],
    names: dict[str, str],
) -> dict[str, Any] | None:
    plain = normalize_text(_plain(markup))
    if not plain or plain in {
        "bye",
        "tbd",
        "to be determined",
        "qualifier",
        "lucky loser",
        "wild card",
    }:
        return None
    return wikimedia._player_from_markup(markup, tour, players, names)


def _slot(
    parameters: Mapping[str, str], round_number: int, team_number: int, kind: str
) -> str | None:
    return next(
        (
            key
            for key in parameters
            if re.fullmatch(rf"RD{round_number}-{kind}0*{team_number}", key, re.I)
        ),
        None,
    )


def parse_wikimedia_fixture_page(
    page: Mapping[str, Any],
    tour: str,
    as_of: date,
    players: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    title_match = wikimedia.TITLE_PATTERN.match(str(page["title"]))
    if not title_match:
        return None
    year = int(title_match.group("year"))
    event_name = title_match.group("event").strip()
    draw = "qualifying" if title_match.group("qualifying") else "main"
    names = {
        normalize_text(player.get("name", "")): player_id
        for player_id, player in players.items()
        if player.get("name")
    }
    fixtures: list[dict[str, Any]] = []
    bracket_index = 0
    code = mwparserfromhell.parse(str(page["content"]))
    for template in code.filter_templates(recursive=True):
        if "teambracket" not in _plain(str(template.name)).lower():
            continue
        bracket_index += 1
        parameters = _parameter_map(template)
        parsed_keys = [
            match
            for key in parameters
            if (match := re.match(r"RD(\d+)-team(\d+)$", key, flags=re.I))
        ]
        for round_number in sorted({int(match.group(1)) for match in parsed_keys}):
            team_numbers = sorted(
                int(match.group(2)) for match in parsed_keys if int(match.group(1)) == round_number
            )
            team_count = max(team_numbers, default=0)
            round_name = wikimedia._round_name(parameters, round_number, team_count)
            for first_number in range(1, team_count + 1, 2):
                second_number = first_number + 1
                first_key = _slot(parameters, round_number, first_number, "team")
                second_key = _slot(parameters, round_number, second_number, "team")
                if not first_key or not second_key:
                    continue
                first_markup, second_markup = parameters[first_key], parameters[second_key]
                if ("'''" in first_markup) != ("'''" in second_markup):
                    continue
                pair = [
                    _player_from_markup(first_markup, tour, players, names),
                    _player_from_markup(second_markup, tour, players, names),
                ]
                if pair == [None, None]:
                    continue
                match_number = (first_number + 1) // 2
                source_match_id = f"{page['page_id']}:{bracket_index}:{round_number}:{match_number}"
                fixtures.append(
                    {
                        "match_id": fixture_id("wikimedia", source_match_id),
                        "round": canonical_round(round_name),
                        "bracket_slot": f"b{bracket_index}-r{round_number}-m{match_number}",
                        "status": "tentative",
                        "scheduled_on": None,
                        "scheduled_at": None,
                        "date_source": "draw_slot",
                        "as_of": as_of.isoformat(),
                        "players": pair,
                        "sources": [{"key": "wikimedia", "source_match_id": source_match_id}],
                    }
                )
    if not fixtures:
        return None
    return {
        "event": {
            "tour": tour,
            "year": year,
            "name": event_name,
            "draw": draw,
            "discipline": "singles",
        },
        "fixtures": fixtures,
    }
