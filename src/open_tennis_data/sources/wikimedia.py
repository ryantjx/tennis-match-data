"""Fresh completed-result ingestion from reusable Wikimedia draw pages."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import mwparserfromhell

from open_tennis_data.model import (
    canonical_round,
    match_id,
    new_event,
    normalize_text,
)
from open_tennis_data.schema import SOURCE_LICENSES
from open_tennis_data.scores import parse_bracket_scores

API_URL = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "open-tennis-data/3.0 (https://github.com/ryantjx/tennis-match-data)"
TITLE_PATTERN = re.compile(
    r"^(?P<year>\d{4})\s+(?P<event>.+?)\s+[–-]\s+"
    r"(?:(?:Men|Women)'s\s+)?[Ss]ingles(?P<qualifying>\s+qualifying)?$"
)
MONTHS = {
    month.lower(): number
    for number, month in enumerate(
        (
            "",
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
    )
    if month
}


def api(params: Mapping[str, Any], attempts: int = 3) -> dict[str, Any]:
    query = {"format": "json", "formatversion": 2, "maxlag": 5, **params}
    request = urllib.request.Request(
        f"{API_URL}?{urllib.parse.urlencode(query)}", headers={"User-Agent": USER_AGENT}
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def discover_pages(year: int, tour: str) -> list[str]:
    category = f"Category:{year} {'ATP' if tour == 'atp' else 'WTA'} Tour"
    titles: list[str] = []
    continuation: str | None = None
    while True:
        params: dict[str, Any] = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": category,
            "cmnamespace": 0,
            "cmlimit": 500,
        }
        if continuation:
            params["cmcontinue"] = continuation
        data = api(params)
        titles.extend(item["title"] for item in data.get("query", {}).get("categorymembers", []))
        continuation = data.get("continue", {}).get("cmcontinue")
        if not continuation:
            break
    excluded = ("doubles", "mixed", "boys'", "girls'", "wheelchair", "quad")
    result = []
    for title in titles:
        lowered = title.lower()
        if "singles" not in lowered or any(value in lowered for value in excluded):
            continue
        if tour == "atp" and " – women's singles" in lowered:
            continue
        if tour == "wta" and " – men's singles" in lowered:
            continue
        if TITLE_PATTERN.match(title):
            result.append(title)
    return sorted(set(result))


def fetch_page(title: str) -> dict[str, Any]:
    data = api(
        {
            "action": "query",
            "prop": "revisions|pageprops",
            "titles": title,
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
        }
    )
    page = data["query"]["pages"][0]
    revision = page["revisions"][0]
    return {
        "title": page["title"],
        "page_id": page["pageid"],
        "wikidata_id": page.get("pageprops", {}).get("wikibase_item"),
        "revision_id": revision["revid"],
        "revision_timestamp": revision["timestamp"],
        "content": revision["slots"]["main"]["content"],
    }


def fetch_page_optional(title: str) -> dict[str, Any] | None:
    data = api(
        {
            "action": "query",
            "prop": "revisions|pageprops",
            "titles": title,
            "redirects": 1,
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
        }
    )
    page = data.get("query", {}).get("pages", [{}])[0]
    revisions = page.get("revisions", [])
    if page.get("missing") or not revisions:
        return None
    revision = revisions[0]
    return {
        "title": page["title"],
        "page_id": page["pageid"],
        "wikidata_id": page.get("pageprops", {}).get("wikibase_item"),
        "revision_id": revision["revid"],
        "revision_timestamp": revision["timestamp"],
        "content": revision["slots"]["main"]["content"],
    }


def fetch_pages_optional(titles: list[str], batch_size: int = 20) -> dict[str, dict[str, Any]]:
    """Fetch optional tournament pages in batches to respect MediaWiki limits."""
    found: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(titles), batch_size):
        batch = titles[offset : offset + batch_size]
        data = api(
            {
                "action": "query",
                "prop": "revisions|pageprops",
                "titles": "|".join(batch),
                "redirects": 1,
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
            }
        )
        aliases = {title: title for title in batch}
        for item in data.get("query", {}).get("normalized", []):
            aliases[str(item["to"])] = aliases.pop(str(item["from"]), str(item["from"]))
        for item in data.get("query", {}).get("redirects", []):
            aliases[str(item["to"])] = aliases.get(str(item["from"]), str(item["from"]))
        for page in data.get("query", {}).get("pages", []):
            revisions = page.get("revisions", [])
            if page.get("missing") or not revisions:
                continue
            revision = revisions[0]
            original_title = aliases.get(str(page["title"]), str(page["title"]))
            found[original_title] = {
                "title": page["title"],
                "page_id": page["pageid"],
                "wikidata_id": page.get("pageprops", {}).get("wikibase_item"),
                "revision_id": revision["revid"],
                "revision_timestamp": revision["timestamp"],
                "content": revision["slots"]["main"]["content"],
            }
    return found


def fetch_page_revisions(titles: list[str], batch_size: int = 50) -> dict[str, str]:
    """Fetch revision IDs without downloading page content."""
    revisions: dict[str, str] = {}
    for offset in range(0, len(titles), batch_size):
        batch = titles[offset : offset + batch_size]
        data = api(
            {
                "action": "query",
                "prop": "revisions",
                "titles": "|".join(batch),
                "redirects": 1,
                "rvprop": "ids",
            }
        )
        aliases = {title: title for title in batch}
        for item in data.get("query", {}).get("normalized", []):
            aliases[str(item["to"])] = aliases.pop(str(item["from"]), str(item["from"]))
        for item in data.get("query", {}).get("redirects", []):
            aliases[str(item["to"])] = aliases.get(str(item["from"]), str(item["from"]))
        for page in data.get("query", {}).get("pages", []):
            page_revisions = page.get("revisions", [])
            if page.get("missing") or not page_revisions:
                continue
            original_title = aliases.get(str(page["title"]), str(page["title"]))
            revisions[original_title] = str(page_revisions[0]["revid"])
    return revisions


def _plain(value: str) -> str:
    return str(mwparserfromhell.parse(value or "").strip_code()).strip(" ' \n\t")


def _parameter_map(template: Any) -> dict[str, str]:
    return {
        str(parameter.name).strip(): str(parameter.value).strip() for parameter in template.params
    }


def _date_range(value: str, year: int) -> tuple[date | None, date | None]:
    plain = _plain(value).replace("–", "-").replace("—", "-")
    plain = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", plain, flags=re.I)
    patterns = (
        re.search(
            r"(?P<start>\d{1,2})\s*-\s*(?P<end>\d{1,2})\s+"
            r"(?P<month>[A-Za-z]+)(?:\s*,?\s*(?P<year>\d{4}))?",
            plain,
        ),
        re.search(
            r"(?P<month>[A-Za-z]+)\s+(?P<start>\d{1,2})\s*-\s*"
            r"(?P<end>\d{1,2})(?:\s*,?\s*(?P<year>\d{4}))?",
            plain,
        ),
        re.search(
            r"between\s+(?P<start>\d{1,2})\s+and\s+(?P<end>\d{1,2})\s+"
            r"(?P<month>[A-Za-z]+)(?:\s+(?P<year>\d{4}))?",
            plain,
            flags=re.I,
        ),
    )
    match = next((item for item in patterns if item), None)
    if match:
        month = MONTHS.get(match.group("month").lower())
        parsed_year = int(match.group("year") or year)
        if month:
            try:
                return (
                    date(parsed_year, month, int(match.group("start"))),
                    date(parsed_year, month, int(match.group("end"))),
                )
            except ValueError:
                return None, None
    single = re.search(
        r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]+)(?:\s+(?P<year>\d{4}))?",
        plain,
    )
    if single and (month := MONTHS.get(single.group("month").lower())):
        try:
            parsed = date(int(single.group("year") or year), month, int(single.group("day")))
            return parsed, parsed
        except ValueError:
            pass
    return None, None


def parse_tournament_page(page: Mapping[str, Any], tour: str, year: int) -> dict[str, Any]:
    parameters: dict[str, str] = {}
    code = mwparserfromhell.parse(str(page["content"]))
    for template in code.filter_templates(recursive=True):
        name = _plain(str(template.name)).lower()
        if "infobox" in name and "tennis" in name:
            parameters = {
                str(key).strip().lower().replace(" ", "_"): value
                for key, value in _parameter_map(template).items()
            }
            break
    start_date, end_date = _date_range(parameters.get("date", ""), year)
    if start_date is None:
        start_date, end_date = _date_range(str(page["content"])[:4000], year)
    location = _plain(parameters.get("location", ""))
    city = country = None
    if location:
        parts = [part.strip() for part in location.split(",") if part.strip()]
        city = parts[0] if parts else None
        country = parts[-1] if len(parts) > 1 else None
    surface_text = _plain(parameters.get("surface", "")).lower()
    surface = next(
        (value for value in ("hard", "clay", "grass", "carpet") if value in surface_text),
        None,
    )
    source_url = (
        "https://en.wikipedia.org/wiki/"
        + urllib.parse.quote(str(page["title"]).replace(" ", "_"))
    )
    return {
        "tour": tour,
        "year": year,
        "event_name": re.sub(rf"^{year}\s+", "", str(page["title"])).strip(),
        "start_date": start_date,
        "end_date": end_date,
        "city": city,
        "country": country,
        "surface": surface,
        "indoor": "indoor" in surface_text,
        "source_url": source_url,
        "source_tournament_id": str(page.get("wikidata_id") or page["page_id"]),
    }


def _player_from_markup(
    markup: str,
    tour: str,
    players: dict[str, dict[str, Any]],
    names: dict[str, str],
) -> dict[str, Any] | None:
    code = mwparserfromhell.parse(markup)
    links = code.filter_wikilinks(recursive=True)
    if links:
        page_title = str(links[0].title).strip()
        display_name = re.sub(r"\s+\([^)]*\)$", "", page_title).strip()
    else:
        page_title = ""
        display_name = _plain(markup)
    if not display_name:
        return None
    country_match = re.search(r"flagicon\s*\|\s*([^}|]+)", markup, flags=re.I)
    country = country_match.group(1).strip().upper() if country_match else None
    normalized = normalize_text(display_name)
    player_id = names.get(normalized)
    if not player_id:
        key = page_title or display_name
        player_id = f"wikimedia:{urllib.parse.quote(key.replace(' ', '_'), safe=':_()-')}"
        players[player_id] = {
            "id": player_id,
            "name": display_name,
            "country": country,
            "birth_date": None,
            "hand": None,
            "height_cm": None,
            "source_ids": {"wikipedia": page_title or None},
            "sources": [
                {
                    "key": "wikimedia",
                    "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote((page_title or display_name).replace(' ', '_'))}",
                    "license": SOURCE_LICENSES["wikimedia"],
                }
            ],
        }
        names[normalized] = player_id
    elif country and not players[player_id].get("country"):
        players[player_id]["country"] = country
    player = players[player_id]
    return {
        "id": player_id,
        "name": player.get("name") or display_name,
        "country": player.get("country"),
    }


def _round_name(parameters: Mapping[str, str], round_number: int, team_count: int) -> str:
    explicit = _plain(parameters.get(f"RD{round_number}", ""))
    if explicit:
        return canonical_round(explicit)
    if team_count <= 8:
        return {1: "QF", 2: "SF", 3: "F"}.get(round_number, f"RD{round_number}")
    return f"RD{round_number}"


def parse_page(
    page: Mapping[str, Any],
    tour: str,
    observed_on: date,
    players: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    title_match = TITLE_PATTERN.match(page["title"])
    if not title_match:
        raise ValueError(f"unsupported Wikimedia draw title: {page['title']}")
    year = int(title_match.group("year"))
    event_name = title_match.group("event").strip()
    draw = "qualifying" if title_match.group("qualifying") else "main"
    event = new_event(
        tour=tour,
        year=year,
        event_name=event_name,
        draw=draw,
        event_id=(
            f"wikidata:{page['wikidata_id']}"
            if page.get("wikidata_id")
            else f"wikipedia:{page['page_id']}"
        ),
        event_start_date=None,
        surface=None,
        level=None,
    )
    names = {
        normalize_text(player.get("name", "")): player_id
        for player_id, player in players.items()
        if player.get("name")
    }
    source_url = (
        f"https://en.wikipedia.org/wiki/{urllib.parse.quote(page['title'].replace(' ', '_'))}"
    )
    retrieved_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    event["source_catalog"] = {
        "wikimedia": {
            "key": "wikimedia",
            "url": source_url,
            "revision": str(page["revision_id"]),
            "revision_timestamp": page["revision_timestamp"],
            "retrieved_at": retrieved_at,
            "license": SOURCE_LICENSES["wikimedia"],
        }
    }
    deduplicated: dict[str, dict[str, Any]] = {}
    code = mwparserfromhell.parse(page["content"])
    bracket_index = 0
    for template in code.filter_templates(recursive=True):
        template_name = _plain(str(template.name)).lower()
        if "teambracket" not in template_name:
            continue
        bracket_index += 1
        parameters = _parameter_map(template)
        team_keys = [re.match(r"RD(\d+)-team(\d+)$", key, flags=re.I) for key in parameters]
        parsed_keys = [match for match in team_keys if match]
        if not parsed_keys:
            continue
        rounds = sorted({int(match.group(1)) for match in parsed_keys})
        for round_number in rounds:
            team_numbers = sorted(
                int(match.group(2)) for match in parsed_keys if int(match.group(1)) == round_number
            )
            team_count = max(team_numbers, default=0)
            round_name = _round_name(parameters, round_number, team_count)
            for first_number in range(1, team_count + 1, 2):
                second_number = first_number + 1
                first_key = next(
                    (
                        key
                        for key in parameters
                        if re.fullmatch(rf"RD{round_number}-team0*{first_number}", key, re.I)
                    ),
                    None,
                )
                second_key = next(
                    (
                        key
                        for key in parameters
                        if re.fullmatch(rf"RD{round_number}-team0*{second_number}", key, re.I)
                    ),
                    None,
                )
                if not first_key or not second_key:
                    continue
                first_markup, second_markup = parameters[first_key], parameters[second_key]
                first_player = _player_from_markup(first_markup, tour, players, names)
                second_player = _player_from_markup(second_markup, tour, players, names)
                if not first_player or not second_player:
                    continue
                first_bold = "'''" in first_markup
                second_bold = "'''" in second_markup
                if first_bold == second_bold:
                    continue
                winner_index = 0 if first_bold else 1
                score_values: list[list[str]] = [[], []]
                for side, number in enumerate((first_number, second_number)):
                    for set_number in range(1, 6):
                        key = next(
                            (
                                key
                                for key in parameters
                                if re.fullmatch(
                                    rf"RD{round_number}-score0*{number}-{set_number}", key, re.I
                                )
                            ),
                            None,
                        )
                        score_values[side].append(parameters.get(key, "") if key else "")
                score = parse_bracket_scores(score_values[0], score_values[1], winner_index)
                status = "walkover" if score["termination"] == "walkover" else "completed"
                if not score["sets"] and status != "walkover":
                    continue
                pair = [first_player, second_player]
                identifier = match_id(
                    tour, year, event_name, draw, round_name, [player["name"] for player in pair]
                )
                match = {
                    "match_id": identifier,
                    "round": round_name,
                    "bracket_slot": f"b{bracket_index}-r{round_number}-m{(first_number + 1) // 2}",
                    "status": status,
                    "played_on": None,
                    "first_completed_observed_on": observed_on.isoformat(),
                    "players": pair,
                    "winner_id": pair[winner_index]["id"],
                    "score": score["raw"],
                    "sources": [
                        {
                            "key": "wikimedia",
                            "source_match_id": (
                                f"{page['page_id']}:{bracket_index}:{round_number}:{(first_number + 1) // 2}"
                            ),
                        }
                    ],
                }
                old = deduplicated.get(identifier)
                if not old or len(match["score"].split()) > len(old["score"].split()):
                    deduplicated[identifier] = match
    event["matches"] = sorted(
        deduplicated.values(), key=lambda match: (match["round"], match["match_id"])
    )
    return event
