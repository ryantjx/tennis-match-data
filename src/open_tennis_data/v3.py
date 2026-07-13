"""Open Tennis Data v3 Parquet build, query, and validation services."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from open_tennis_data.schema import SCHEMA_VERSION, SOURCE_LICENSES, TOURS

ARCHIVE_REPOSITORY = "Aneeshers/tennis-sackmann-archive"
ARCHIVE_RESOLVE = f"https://huggingface.co/datasets/{ARCHIVE_REPOSITORY}/resolve"
USER_AGENT = "open-tennis-data/3.0 (https://github.com/ryantjx/tennis-match-data)"
MAX_PARQUET_BYTES = 75 * 1024 * 1024
NORMAL_COMMIT_BYTES = 25 * 1024 * 1024
MATCH_ROW_GROUP_SIZE = 16_384
OBSERVATION_ROW_GROUP_SIZE = 32_768
RANKING_ROW_GROUP_SIZE = 65_536
DOWNLOAD_ROW_GROUP_SIZE = 65_536
DOWNLOAD_COMPRESSION_LEVEL = 19

DOWNLOAD_FILENAMES = (
    "mens.parquet",
    "womens.parquet",
    "atp.parquet",
    "wta.parquet",
    "all-matches.parquet",
)

RANKING_KEYS = {
    "atp": ("70s", "80s", "90s", "00s", "10s", "20s", "current"),
    "wta": ("80s", "90s", "00s", "10s", "20s", "current"),
}


@dataclass(frozen=True)
class SourceFile:
    kind: str
    tour: str
    year: int | None
    label: str
    source_path: str
    local_path: Path
    url: str
    revision: str
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_years(value: str) -> list[int]:
    years: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            start, end = (int(item) for item in part.split(":", 1))
            years.update(range(start, end + 1))
        else:
            years.add(int(part))
    if not years or min(years) < 1968:
        raise ValueError("years must contain values from 1968 onward")
    return sorted(years)


def _request(url: str, *, attempts: int = 4) -> tuple[bytes, str]:
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                revision = response.headers.get("X-Repo-Commit", "unknown")
                return response.read(), revision
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def resolve_archive_revision() -> str:
    probe = f"{ARCHIVE_RESOLVE}/main/atp/atp_matches_2025.csv?download=true"
    _, revision = _request(probe)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError(f"archive did not expose a pinned revision: {revision!r}")
    return revision


def _match_specs(years: Sequence[int]) -> Iterator[tuple[str, int, str, str]]:
    for tour in TOURS:
        for year in years:
            yield tour, year, "tour", f"{tour}/{tour}_matches_{year}.csv"
            if tour == "atp" and year >= 1978:
                yield tour, year, "qual_chall", f"atp/atp_matches_qual_chall_{year}.csv"
            if tour == "atp" and year >= 1991:
                yield tour, year, "futures", f"atp/atp_matches_futures_{year}.csv"
            if tour == "wta":
                yield tour, year, "qual_itf", f"wta/wta_matches_qual_itf_{year}.csv"


def _source_specs(
    years: Sequence[int], include_rankings: bool
) -> list[tuple[str, str, int | None, str, str]]:
    specs: list[tuple[str, str, int | None, str, str]] = [
        ("players", tour, None, "players", f"{tour}/{tour}_players.csv") for tour in TOURS
    ]
    specs.extend(
        ("matches", tour, year, label, path) for tour, year, label, path in _match_specs(years)
    )
    if include_rankings:
        for tour in TOURS:
            for key in RANKING_KEYS[tour]:
                specs.append(("rankings", tour, None, key, f"{tour}/{tour}_rankings_{key}.csv"))
    return specs


def download_sources(
    temporary: Path,
    years: Sequence[int],
    *,
    include_rankings: bool = True,
    workers: int = 12,
) -> tuple[list[SourceFile], str]:
    revision = resolve_archive_revision()
    specs = _source_specs(years, include_rankings)
    temporary.mkdir(parents=True, exist_ok=True)

    def download(spec: tuple[str, str, int | None, str, str]) -> SourceFile:
        kind, tour, year, label, source_path = spec
        suffix = str(year) if year is not None else label
        local = temporary / f"{kind}__{tour}__{suffix}__{label}.csv"
        url = f"{ARCHIVE_RESOLVE}/{revision}/{source_path}?download=true"
        content, observed_revision = _request(url)
        if observed_revision not in ("unknown", revision):
            raise RuntimeError(
                f"revision drift for {source_path}: {observed_revision} != {revision}"
            )
        local.write_bytes(content)
        return SourceFile(
            kind=kind,
            tour=tour,
            year=year,
            label=label,
            source_path=source_path,
            local_path=local.resolve(),
            url=url,
            revision=revision,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    found: list[SourceFile] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(download, spec): spec for spec in specs}
        for position, future in enumerate(as_completed(futures), start=1):
            found.append(future.result())
            if position % 25 == 0 or position == len(specs):
                print(f"downloaded {position}/{len(specs)} pinned source files", flush=True)
    return sorted(
        found, key=lambda item: (item.kind, item.tour, item.year or 0, item.label)
    ), revision


def _quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _required_row(cursor: duckdb.DuckDBPyConnection) -> tuple[Any, ...]:
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("expected a query result row")
    return row


def _sql_list(paths: Iterable[Path]) -> str:
    return "[" + ",".join(_quoted(path.resolve()) for path in paths) + "]"


def _level_expression() -> str:
    return """
        CASE
          WHEN source_label = 'futures' THEN 'itf'
          WHEN tour = 'atp' AND upper(coalesce(tourney_level, '')) = 'G' THEN 'grand_slam'
          WHEN tour = 'atp' AND upper(coalesce(tourney_level, '')) = 'M' THEN 'masters_1000'
          WHEN tour = 'atp' AND upper(coalesce(tourney_level, '')) = 'F' THEN 'tour_finals'
          WHEN tour = 'atp' AND upper(coalesce(tourney_level, '')) = 'C' THEN 'challenger'
          WHEN tour = 'atp' AND upper(coalesce(tourney_level, '')) = 'D' THEN 'team'
          WHEN tour = 'atp' AND upper(coalesce(tourney_level, '')) = 'O' THEN 'olympics'
          WHEN tour = 'atp' AND upper(coalesce(tourney_level, '')) IN ('S', '15', '25') THEN 'itf'
          WHEN tour = 'wta' AND upper(coalesce(tourney_level, '')) = 'G' THEN 'grand_slam'
          WHEN tour = 'wta' AND upper(coalesce(tourney_level, '')) IN ('PM', 'T1') THEN 'wta_1000'
          WHEN tour = 'wta' AND upper(coalesce(tourney_level, '')) IN ('P', 'T2') THEN 'wta_500'
          WHEN tour = 'wta' AND upper(coalesce(tourney_level, '')) IN ('T3', 'T4', 'T5') THEN 'wta_250'
          WHEN tour = 'wta' AND upper(coalesce(tourney_level, '')) = 'C' THEN 'wta_125'
          WHEN tour = 'wta' AND upper(coalesce(tourney_level, '')) = 'F' THEN 'tour_finals'
          WHEN tour = 'wta' AND upper(coalesce(tourney_level, '')) = 'D' THEN 'team'
          WHEN tour = 'wta' AND upper(coalesce(tourney_level, '')) = 'O' THEN 'olympics'
          WHEN tour = 'wta' AND regexp_full_match(upper(coalesce(tourney_level, '')), '[0-9]+(\\+H)?') THEN 'itf'
          ELSE 'other'
        END
    """


def _level_detail_expression() -> str:
    return """
        CASE
          WHEN source_label = 'futures' AND trim(coalesce(tourney_level, '')) IN ('15', '25')
            THEN 'M' || trim(tourney_level)
          WHEN tour = 'wta' AND regexp_full_match(trim(coalesce(tourney_level, '')), '[0-9]+')
            THEN 'W' || trim(tourney_level)
          WHEN tour = 'wta' AND upper(coalesce(tourney_level, '')) = 'C' THEN 'WTA125'
          ELSE nullif(trim(coalesce(tourney_level, '')), '')
        END
    """


def _round_expression() -> str:
    return """
        CASE upper(trim(coalesce(round, '')))
          WHEN 'BR' THEN 'BR'
          WHEN 'ER' THEN 'ER'
          WHEN 'Q1' THEN 'Q1'
          WHEN 'Q2' THEN 'Q2'
          WHEN 'Q3' THEN 'Q3'
          WHEN 'Q4' THEN 'Q4'
          WHEN 'R128' THEN 'R128'
          WHEN 'R64' THEN 'R64'
          WHEN 'R32' THEN 'R32'
          WHEN 'R16' THEN 'R16'
          WHEN 'QF' THEN 'QF'
          WHEN 'SF' THEN 'SF'
          WHEN 'F' THEN 'F'
          WHEN 'RR' THEN 'RR'
          ELSE upper(trim(coalesce(round, 'UNKNOWN')))
        END
    """


def _round_order_expression(column: str = "round_name") -> str:
    return f"""
        CASE {column}
          WHEN 'BR' THEN 0 WHEN 'ER' THEN 1
          WHEN 'Q1' THEN 2 WHEN 'Q2' THEN 3 WHEN 'Q3' THEN 4 WHEN 'Q4' THEN 5
          WHEN 'R128' THEN 10 WHEN 'R64' THEN 20 WHEN 'R32' THEN 30
          WHEN 'R16' THEN 40 WHEN 'QF' THEN 50 WHEN 'SF' THEN 60
          WHEN 'F' THEN 70 WHEN 'RR' THEN 80 ELSE 999
        END
    """


def _create_source_file_table(
    connection: duckdb.DuckDBPyConnection, sources: Sequence[SourceFile]
) -> None:
    connection.execute(
        """
        CREATE TABLE source_files (
          kind VARCHAR, tour VARCHAR, year INTEGER, source_label VARCHAR,
          source_path VARCHAR, local_path VARCHAR, source_url VARCHAR,
          revision VARCHAR, sha256 VARCHAR
        )
        """
    )
    connection.executemany(
        "INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                item.kind,
                item.tour,
                item.year,
                item.label,
                item.source_path,
                str(item.local_path),
                item.url,
                item.revision,
                item.sha256,
            )
            for item in sources
        ],
    )


def _create_match_tables(
    connection: duckdb.DuckDBPyConnection, sources: Sequence[SourceFile], as_of: date
) -> None:
    match_sources = [item for item in sources if item.kind == "matches"]
    connection.execute(
        f"""
        CREATE TABLE raw_matches AS
        SELECT csv.*, files.tour, files.year AS source_year, files.source_label,
               files.source_path, files.source_url, files.revision, files.sha256 AS source_sha256
        FROM read_csv({_sql_list(item.local_path for item in match_sources)},
                      header=true, all_varchar=true, union_by_name=true,
                      filename=true, null_padding=true) csv
        JOIN source_files files ON csv.filename = files.local_path
        """
    )
    connection.execute(
        """
        CREATE TABLE raw_match_ranked AS
        SELECT *,
          sha256(concat_ws('|', tour, source_label, coalesce(tourney_id, ''),
            coalesce(match_num, ''), coalesce(round, ''), coalesce(winner_id, ''),
            coalesce(winner_name, ''), coalesce(loser_id, ''), coalesce(loser_name, ''),
            coalesce(score, ''))) AS row_fingerprint,
          source_label || ':' || coalesce(tourney_id, '') || ':' || coalesce(match_num, '')
            AS source_match_id,
          row_number() OVER (
            PARTITION BY tour, source_label, coalesce(tourney_id, ''),
              coalesce(match_num, ''), coalesce(round, ''), coalesce(winner_id, ''),
              coalesce(winner_name, ''), coalesce(loser_id, ''), coalesce(loser_name, ''),
              coalesce(score, '')
            ORDER BY source_path
          ) AS duplicate_ordinal
        FROM raw_matches
        """
    )
    connection.execute(
        f"""
        CREATE TABLE normalized_base AS
        SELECT *,
          CASE WHEN upper(trim(coalesce(round, ''))) LIKE 'Q%' THEN 'qualifying' ELSE 'main' END AS draw,
          {_round_expression()} AS round_name,
          {_level_expression()} AS canonical_level,
          {_level_detail_expression()} AS level_detail,
          CASE upper(trim(coalesce(surface, '')))
            WHEN 'HARD' THEN 'hard' WHEN 'CLAY' THEN 'clay' WHEN 'GRASS' THEN 'grass'
            WHEN 'CARPET' THEN 'carpet' ELSE NULL END AS canonical_surface,
          try_strptime(trim(coalesce(tourney_date, '')), '%Y%m%d')::DATE AS event_date,
          CASE WHEN trim(coalesce(winner_id, '')) <> '' THEN tour || ':' || trim(winner_id)
               ELSE 'name:' || substr(sha256(lower(trim(coalesce(winner_name, '')))), 1, 20) END AS winner_player_id,
          CASE WHEN trim(coalesce(loser_id, '')) <> '' THEN tour || ':' || trim(loser_id)
               ELSE 'name:' || substr(sha256(lower(trim(coalesce(loser_name, '')))), 1, 20) END AS loser_player_id
        FROM raw_match_ranked
        WHERE duplicate_ordinal = 1
        """
    )
    connection.execute(
        f"""
        CREATE TABLE normalized_identified AS
        SELECT *,
          'event:' || tour || ':' || substr(sha256(concat_ws('|', tour, source_label,
             coalesce(tourney_id, ''), draw)), 1, 20) AS event_id,
          {_round_order_expression()} AS round_order,
          concat_ws('|', tour, source_label, coalesce(tourney_id, ''), draw, round_name,
             least(winner_player_id, loser_player_id), greatest(winner_player_id, loser_player_id))
             AS canonical_match_key
        FROM normalized_base
        """
    )
    connection.execute(
        """
        CREATE TABLE normalized AS
        SELECT *, row_number() OVER (
          PARTITION BY canonical_match_key ORDER BY source_match_id, row_fingerprint
        ) AS match_ordinal
        FROM normalized_identified
        """
    )
    connection.execute(
        f"""
        CREATE TABLE matches AS
        SELECT
          'match:' || tour || ':' || substr(sha256(canonical_match_key || '|' || match_ordinal), 1, 20) AS match_id,
          event_id, tour, source_year::SMALLINT AS year, 'singles'::VARCHAR AS discipline,
          trim(coalesce(tourney_name, 'Unknown event')) AS event_name,
          canonical_level AS level, level_detail, nullif(trim(coalesce(tourney_level, '')), '') AS source_level,
          canonical_surface AS surface, false AS indoor, event_date AS event_start_date,
          NULL::VARCHAR AS event_city, NULL::VARCHAR AS event_country,
          draw, round_name AS round, round_order::SMALLINT AS round_order,
          NULL::VARCHAR AS bracket_slot, NULL::DATE AS played_on,
          CASE WHEN event_date IS NULL THEN 'unknown' ELSE 'event_only' END AS played_on_precision,
          winner_player_id AS player1_id, nullif(trim(coalesce(winner_name, '')), '') AS player1_name,
          nullif(trim(coalesce(winner_ioc, '')), '') AS player1_country,
          loser_player_id AS player2_id, nullif(trim(coalesce(loser_name, '')), '') AS player2_name,
          nullif(trim(coalesce(loser_ioc, '')), '') AS player2_country,
          winner_player_id AS winner_id, loser_player_id AS loser_id, 1::TINYINT AS winner_side,
          nullif(trim(coalesce(winner_seed, '')), '') AS player1_seed,
          nullif(trim(coalesce(loser_seed, '')), '') AS player2_seed,
          nullif(trim(coalesce(winner_entry, '')), '') AS player1_entry,
          nullif(trim(coalesce(loser_entry, '')), '') AS player2_entry,
          try_cast(winner_rank AS INTEGER) AS player1_rank,
          try_cast(loser_rank AS INTEGER) AS player2_rank,
          try_cast(winner_rank_points AS INTEGER) AS player1_rank_points,
          try_cast(loser_rank_points AS INTEGER) AS player2_rank_points,
          CASE
            WHEN regexp_matches(upper(coalesce(score, '')), 'W/O|(^| )WO($| )') THEN 'walkover'
            WHEN regexp_matches(upper(coalesce(score, '')), 'RET') THEN 'retired'
            WHEN regexp_matches(upper(coalesce(score, '')), 'DEF') THEN 'defaulted'
            WHEN regexp_matches(upper(coalesce(score, '')), 'ABD|ABN') THEN 'abandoned'
            ELSE 'completed' END AS status,
          CASE
            WHEN regexp_matches(upper(coalesce(score, '')), 'W/O|(^| )WO($| )') THEN 'walkover'
            WHEN regexp_matches(upper(coalesce(score, '')), 'RET') THEN 'retired'
            WHEN regexp_matches(upper(coalesce(score, '')), 'DEF') THEN 'defaulted'
            WHEN regexp_matches(upper(coalesce(score, '')), 'ABD|ABN') THEN 'abandoned'
            ELSE NULL END AS termination,
          nullif(trim(coalesce(score, '')), '') AS score,
          try_cast(best_of AS TINYINT) AS best_of,
          DATE {_quoted(as_of.isoformat())} AS first_observed_on,
          DATE {_quoted(as_of.isoformat())} AS last_updated_on,
          'sackmann'::VARCHAR AS preferred_source, 1::SMALLINT AS source_count
        FROM normalized
        ORDER BY tour, source_year, canonical_level, event_date, event_id, round_order, match_id
        """
    )
    connection.execute(
        """
        CREATE TABLE events AS
        SELECT event_id, tour, source_year::SMALLINT AS year, 'singles'::VARCHAR AS discipline,
          draw, arg_min(trim(coalesce(tourney_name, 'Unknown event')), source_match_id) AS event_name,
          arg_min(canonical_level, source_match_id) AS level,
          arg_min(level_detail, source_match_id) AS level_detail,
          arg_min(nullif(trim(coalesce(tourney_level, '')), ''), source_match_id) AS source_level,
          arg_min(canonical_surface, source_match_id) AS surface, false AS indoor,
          min(event_date) AS event_start_date, NULL::DATE AS event_end_date,
          NULL::VARCHAR AS city, NULL::VARCHAR AS country,
          try_cast(max(try_cast(draw_size AS INTEGER)) AS INTEGER) AS draw_size,
          bool_or(canonical_level = 'team') AS team_event,
          'sackmann'::VARCHAR AS source, source_label,
          arg_min(coalesce(tourney_id, ''), source_match_id) AS source_event_id
        FROM normalized GROUP BY event_id, tour, source_year, draw, source_label
        ORDER BY tour, year, event_start_date, event_id
        """
    )
    connection.execute(
        """
        CREATE TABLE match_stats AS
        SELECT
          'match:' || tour || ':' || substr(sha256(canonical_match_key || '|' || match_ordinal), 1, 20) AS match_id,
          tour, source_year::SMALLINT AS year,
          try_cast(minutes AS INTEGER) AS duration_minutes,
          try_cast(w_ace AS INTEGER) AS player1_aces,
          try_cast(w_df AS INTEGER) AS player1_double_faults,
          try_cast(w_svpt AS INTEGER) AS player1_service_points,
          try_cast(w_1stIn AS INTEGER) AS player1_first_serves_in,
          try_cast(w_1stWon AS INTEGER) AS player1_first_serves_won,
          try_cast(w_2ndWon AS INTEGER) AS player1_second_serves_won,
          try_cast(w_SvGms AS INTEGER) AS player1_service_games,
          try_cast(w_bpSaved AS INTEGER) AS player1_break_points_saved,
          try_cast(w_bpFaced AS INTEGER) AS player1_break_points_faced,
          try_cast(l_ace AS INTEGER) AS player2_aces,
          try_cast(l_df AS INTEGER) AS player2_double_faults,
          try_cast(l_svpt AS INTEGER) AS player2_service_points,
          try_cast(l_1stIn AS INTEGER) AS player2_first_serves_in,
          try_cast(l_1stWon AS INTEGER) AS player2_first_serves_won,
          try_cast(l_2ndWon AS INTEGER) AS player2_second_serves_won,
          try_cast(l_SvGms AS INTEGER) AS player2_service_games,
          try_cast(l_bpSaved AS INTEGER) AS player2_break_points_saved,
          try_cast(l_bpFaced AS INTEGER) AS player2_break_points_faced
        FROM normalized
        WHERE coalesce(minutes, w_ace, w_df, w_svpt, w_1stIn, w_1stWon, w_2ndWon,
                       w_SvGms, w_bpSaved, w_bpFaced, l_ace, l_df, l_svpt, l_1stIn,
                       l_1stWon, l_2ndWon, l_SvGms, l_bpSaved, l_bpFaced) IS NOT NULL
        ORDER BY tour, year, match_id
        """
    )
    connection.execute(
        f"""
        CREATE TABLE observations AS
        SELECT
          'match:' || tour || ':' || substr(sha256(canonical_match_key || '|' || match_ordinal), 1, 20) AS match_id,
          event_id, tour, source_year::SMALLINT AS year, 'sackmann'::VARCHAR AS source,
          source_label, coalesce(tourney_id, '') AS source_event_id, source_match_id,
          row_fingerprint, source_url, revision, source_sha256,
          DATE {_quoted(as_of.isoformat())} AS retrieved_on,
          {_quoted(SOURCE_LICENSES["sackmann"])}::VARCHAR AS license
        FROM normalized ORDER BY tour, year, source_label, source_match_id, row_fingerprint
        """
    )
    connection.execute(
        """
        CREATE TABLE quarantine AS
        SELECT tour, source_year::SMALLINT AS year, source_label, source_path,
          source_match_id, row_fingerprint, 'duplicate_source_row'::VARCHAR AS reason
        FROM raw_match_ranked WHERE duplicate_ordinal > 1
        ORDER BY tour, year, source_label, source_match_id
        """
    )


def _create_player_tables(
    connection: duckdb.DuckDBPyConnection, sources: Sequence[SourceFile]
) -> None:
    player_sources = [item for item in sources if item.kind == "players"]
    connection.execute(
        f"""
        CREATE TABLE raw_players AS
        SELECT csv.*, files.tour, files.source_url, files.revision
        FROM read_csv({_sql_list(item.local_path for item in player_sources)},
                      header=true, all_varchar=true, union_by_name=true,
                      filename=true, null_padding=true) csv
        JOIN source_files files ON csv.filename = files.local_path
        """
    )
    connection.execute(
        """
        CREATE TABLE player_candidates AS
        SELECT tour || ':' || trim(player_id) AS player_id, tour,
          nullif(trim(concat_ws(' ', name_first, name_last)), '') AS name,
          nullif(trim(ioc), '') AS country,
          try_strptime(trim(coalesce(dob, '')), '%Y%m%d')::DATE AS birth_date,
          nullif(trim(hand), '') AS hand, try_cast(height AS SMALLINT) AS height_cm,
          'sackmann'::VARCHAR AS source, trim(player_id) AS source_player_id,
          source_url, revision, 0 AS source_priority
        FROM raw_players WHERE trim(coalesce(player_id, '')) <> ''
        UNION ALL
        SELECT player1_id, tour, player1_name, player1_country, NULL::DATE, NULL::VARCHAR,
          NULL::SMALLINT, preferred_source, split_part(player1_id, ':', 2), NULL::VARCHAR,
          NULL::VARCHAR, 1
        FROM matches
        UNION ALL
        SELECT player2_id, tour, player2_name, player2_country, NULL::DATE, NULL::VARCHAR,
          NULL::SMALLINT, preferred_source, split_part(player2_id, ':', 2), NULL::VARCHAR,
          NULL::VARCHAR, 1
        FROM matches
        """
    )
    connection.execute(
        """
        CREATE TABLE players AS
        SELECT player_id, tour, arg_min(name, source_priority) AS name,
          arg_min(country, source_priority) AS country,
          arg_min(birth_date, source_priority) AS birth_date,
          arg_min(hand, source_priority) AS hand,
          arg_min(height_cm, source_priority) AS height_cm,
          arg_min(source, source_priority) AS preferred_source,
          arg_min(source_player_id, source_priority) AS preferred_source_player_id
        FROM player_candidates GROUP BY player_id, tour ORDER BY tour, player_id
        """
    )


def _ingest_wikimedia(
    connection: duckdb.DuckDBPyConnection,
    *,
    year: int,
    as_of: date,
    workers: int,
) -> dict[str, int]:
    """Merge current reusable Wikimedia results without using intermediate JSON files."""
    from open_tennis_data.fixtures import parse_wikimedia_fixture_page
    from open_tennis_data.model import normalize_text
    from open_tennis_data.sources.wikimedia import discover_pages, fetch_page, parse_page

    def semantic_score(value: str | None) -> str:
        normalized = re.sub(r"\([^)]*\)|\[[^]]*\]", "", (value or "").upper())
        normalized = re.sub(r"\b(?:RET|W/O|WO|DEF|ABD|ABN)\b", "", normalized)
        return re.sub(r"\s+", " ", normalized).strip()

    player_rows = connection.execute(
        "SELECT player_id, tour, name, country, birth_date, hand, height_cm FROM players"
    ).fetchall()
    players_by_tour: dict[str, dict[str, dict[str, Any]]] = {tour: {} for tour in TOURS}
    original_player_ids: set[str] = set()
    for player_id, tour, name, country, birth_date, hand, height_cm in player_rows:
        original_player_ids.add(player_id)
        players_by_tour[tour][player_id] = {
            "id": player_id,
            "name": name,
            "country": country,
            "birth_date": birth_date,
            "hand": hand,
            "height_cm": height_cm,
            "source_ids": {tour: player_id.split(":", 1)[-1]},
        }

    tasks: list[tuple[str, str]] = []
    for tour in TOURS:
        tasks.extend((tour, title) for title in discover_pages(year, tour))

    pages: list[tuple[str, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_page, title): tour for tour, title in tasks}
        for future in as_completed(futures):
            pages.append((futures[future], future.result()))

    parsed_events: list[dict[str, Any]] = []
    for tour, page in sorted(pages, key=lambda item: (item[0], item[1]["title"])):
        parsed = parse_page(page, tour, as_of, players_by_tour[tour])
        if parsed.get("matches"):
            parsed["_page"] = page
            parsed_events.append(parsed)

    existing_events: dict[tuple[str, int, str, str], list[tuple[Any, ...]]] = {}
    for row in connection.execute(
        "SELECT tour, year, draw, event_name, event_id, level, level_detail, source_level, "
        "surface, indoor, event_start_date, event_end_date, city, country, draw_size, team_event FROM events"
    ).fetchall():
        existing_event_key = (row[0], int(row[1]), row[2], normalize_text(row[3]))
        existing_events.setdefault(existing_event_key, []).append(row)

    existing_matches: dict[tuple[str, str, str, str], tuple[Any, ...]] = {}
    for row in connection.execute(
        "SELECT match_id, event_id, round, player1_id, player2_id, winner_id, score, status FROM matches"
    ).fetchall():
        existing_match_key = (
            str(row[1]),
            str(row[2]),
            min(str(row[3]), str(row[4])),
            max(str(row[3]), str(row[4])),
        )
        existing_matches[existing_match_key] = row

    event_inserts: list[tuple[Any, ...]] = []
    match_inserts: list[tuple[Any, ...]] = []
    observation_inserts: list[tuple[Any, ...]] = []
    conflict_inserts: list[tuple[Any, ...]] = []
    fixture_inserts: list[tuple[Any, ...]] = []
    linked_matches: set[str] = set()
    seen_new_events: set[str] = set()
    seen_observations: set[tuple[str, str]] = set()

    for document in parsed_events:
        metadata = document["event"]
        page = document["_page"]
        parsed_event_key = (
            metadata["tour"],
            int(metadata["year"]),
            metadata["draw"],
            normalize_text(metadata["name"]),
        )
        candidates = existing_events.get(parsed_event_key, [])
        if len(candidates) == 1:
            candidate = candidates[0]
            event_id = candidate[4]
            event_level, level_detail, source_level = candidate[5], candidate[6], candidate[7]
            surface, indoor, event_start = candidate[8], candidate[9], candidate[10]
            event_end, city, country, draw_size, team_event = candidate[11:16]
        else:
            source_event_id = str(page.get("wikidata_id") or page["page_id"])
            event_id = (
                "event:"
                + metadata["tour"]
                + ":"
                + hashlib.sha256(
                    f"{metadata['tour']}|wikimedia|{source_event_id}|{metadata['draw']}".encode()
                ).hexdigest()[:20]
            )
            event_level, level_detail, source_level = "other", None, None
            surface, indoor, event_start = None, False, None
            event_end = city = country = draw_size = None
            team_event = False
            if event_id not in seen_new_events:
                event_inserts.append(
                    (
                        event_id,
                        metadata["tour"],
                        int(metadata["year"]),
                        "singles",
                        metadata["draw"],
                        metadata["name"],
                        event_level,
                        level_detail,
                        source_level,
                        surface,
                        indoor,
                        event_start,
                        event_end,
                        city,
                        country,
                        draw_size,
                        team_event,
                        "wikimedia",
                        "wikimedia",
                        source_event_id,
                    )
                )
                seen_new_events.add(event_id)

        source_catalog = document["source_catalog"]["wikimedia"]
        source_event_id = str(page.get("wikidata_id") or page["page_id"])
        source_sha = hashlib.sha256(page["content"].encode("utf-8")).hexdigest()
        for match in document["matches"]:
            player1, player2 = match["players"]
            match_key = (
                event_id,
                match["round"],
                min(player1["id"], player2["id"]),
                max(player1["id"], player2["id"]),
            )
            existing = existing_matches.get(match_key)
            source_match_id = match["sources"][0]["source_match_id"]
            observation_key = ("wikimedia", source_match_id)
            if observation_key in seen_observations:
                continue
            seen_observations.add(observation_key)
            row_fingerprint = hashlib.sha256(
                "|".join(
                    [
                        source_event_id,
                        source_match_id,
                        match["round"],
                        player1["id"],
                        player2["id"],
                        match["winner_id"],
                        match.get("score") or "",
                    ]
                ).encode()
            ).hexdigest()
            if existing:
                canonical_match_id = existing[0]
                linked_matches.add(canonical_match_id)
                if existing[5] != match["winner_id"] or semantic_score(
                    existing[6]
                ) != semantic_score(match.get("score")):
                    conflict_inserts.append(
                        (
                            "conflict:"
                            + hashlib.sha256(
                                f"{canonical_match_id}|wikimedia".encode()
                            ).hexdigest()[:20],
                            "match",
                            canonical_match_id,
                            "sackmann",
                            "wikimedia",
                            "winner_or_score",
                            f"{existing[5]}|{existing[6] or ''}",
                            f"{match['winner_id']}|{match.get('score') or ''}",
                            "open",
                        )
                    )
            else:
                canonical_key = "|".join(match_key)
                canonical_match_id = (
                    "match:"
                    + metadata["tour"]
                    + ":"
                    + hashlib.sha256((canonical_key + "|1").encode()).hexdigest()[:20]
                )
                winner_id = match["winner_id"]
                loser_id = player2["id"] if winner_id == player1["id"] else player1["id"]
                winner_side = 1 if winner_id == player1["id"] else 2
                termination = "walkover" if match["status"] == "walkover" else None
                match_inserts.append(
                    (
                        canonical_match_id,
                        event_id,
                        metadata["tour"],
                        int(metadata["year"]),
                        "singles",
                        metadata["name"],
                        event_level,
                        level_detail,
                        source_level,
                        surface,
                        indoor,
                        event_start,
                        city,
                        country,
                        metadata["draw"],
                        match["round"],
                        999,
                        match.get("bracket_slot"),
                        None,
                        "unknown",
                        player1["id"],
                        player1.get("name"),
                        player1.get("country"),
                        player2["id"],
                        player2.get("name"),
                        player2.get("country"),
                        winner_id,
                        loser_id,
                        winner_side,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        match["status"],
                        termination,
                        match.get("score"),
                        None,
                        as_of,
                        as_of,
                        "wikimedia",
                        1,
                    )
                )
                existing_matches[match_key] = (
                    canonical_match_id,
                    event_id,
                    match["round"],
                    player1["id"],
                    player2["id"],
                    winner_id,
                    match.get("score"),
                    match["status"],
                )
            observation_inserts.append(
                (
                    canonical_match_id,
                    event_id,
                    metadata["tour"],
                    int(metadata["year"]),
                    "wikimedia",
                    "wikimedia",
                    source_event_id,
                    source_match_id,
                    row_fingerprint,
                    source_catalog["url"],
                    str(page["revision_id"]),
                    source_sha,
                    as_of,
                    SOURCE_LICENSES["wikimedia"],
                )
            )

    for tour, page in sorted(pages, key=lambda item: (item[0], item[1]["title"])):
        fixture_document = parse_wikimedia_fixture_page(page, tour, as_of, players_by_tour[tour])
        if not fixture_document:
            continue
        metadata = fixture_document["event"]
        fixture_event_key = (
            tour,
            int(metadata["year"]),
            metadata["draw"],
            normalize_text(metadata["name"]),
        )
        candidates = existing_events.get(fixture_event_key, [])
        source_event_id = str(page.get("wikidata_id") or page["page_id"])
        if len(candidates) == 1:
            candidate = candidates[0]
            event_id, event_level, surface = candidate[4], candidate[5], candidate[8]
        else:
            event_id = (
                "event:"
                + tour
                + ":"
                + hashlib.sha256(
                    f"{tour}|wikimedia|{source_event_id}|{metadata['draw']}".encode()
                ).hexdigest()[:20]
            )
            event_level, surface = "other", None
            if event_id not in seen_new_events:
                event_inserts.append(
                    (
                        event_id,
                        tour,
                        int(metadata["year"]),
                        "singles",
                        metadata["draw"],
                        metadata["name"],
                        event_level,
                        None,
                        None,
                        surface,
                        False,
                        None,
                        None,
                        None,
                        None,
                        None,
                        False,
                        "wikimedia",
                        "wikimedia",
                        source_event_id,
                    )
                )
                seen_new_events.add(event_id)
        for fixture in fixture_document["fixtures"]:
            pair = fixture.get("players", [None, None])
            player1 = pair[0] or {}
            player2 = pair[1] or {}
            source_match_id = fixture["sources"][0]["source_match_id"]
            fixture_inserts.append(
                (
                    fixture["match_id"],
                    event_id,
                    tour,
                    int(metadata["year"]),
                    metadata["name"],
                    event_level,
                    surface,
                    metadata["draw"],
                    fixture["round"],
                    player1.get("id"),
                    player1.get("name"),
                    player2.get("id"),
                    player2.get("name"),
                    fixture.get("scheduled_on"),
                    fixture.get("scheduled_at"),
                    fixture.get("date_source"),
                    fixture["status"],
                    as_of,
                    "wikimedia",
                    source_match_id,
                )
            )

    if event_inserts:
        connection.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            event_inserts,
        )
    if match_inserts:
        connection.executemany(
            "INSERT INTO matches VALUES (" + ",".join("?" for _ in range(45)) + ")",
            match_inserts,
        )
    if observation_inserts:
        connection.executemany(
            "INSERT INTO observations VALUES (" + ",".join("?" for _ in range(14)) + ")",
            observation_inserts,
        )
    if linked_matches:
        connection.execute(
            "UPDATE matches SET source_count = 2, last_updated_on = ? WHERE match_id IN ("
            + ",".join("?" for _ in linked_matches)
            + ")",
            [as_of, *sorted(linked_matches)],
        )

    new_player_rows: list[tuple[Any, ...]] = []
    for tour in TOURS:
        for player_id, player in players_by_tour[tour].items():
            if player_id in original_player_ids:
                continue
            new_player_rows.append(
                (
                    player_id,
                    tour,
                    player.get("name"),
                    player.get("country"),
                    player.get("birth_date"),
                    player.get("hand"),
                    player.get("height_cm"),
                    "wikimedia",
                    player_id.split(":", 1)[-1],
                )
            )
    if new_player_rows:
        connection.executemany(
            "INSERT INTO players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", new_player_rows
        )

    connection.execute(
        """
        CREATE TABLE wikimedia_conflicts (
          conflict_id VARCHAR, entity_type VARCHAR, canonical_id VARCHAR,
          source_a VARCHAR, source_b VARCHAR, field VARCHAR,
          value_a VARCHAR, value_b VARCHAR, status VARCHAR
        )
        """
    )
    if conflict_inserts:
        connection.executemany(
            "INSERT INTO wikimedia_conflicts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", conflict_inserts
        )
    connection.execute(
        """
        CREATE TABLE wikimedia_fixtures (
          fixture_id VARCHAR, event_id VARCHAR, tour VARCHAR, year SMALLINT,
          event_name VARCHAR, level VARCHAR, surface VARCHAR, draw VARCHAR, round VARCHAR,
          player1_id VARCHAR, player1_name VARCHAR, player2_id VARCHAR, player2_name VARCHAR,
          scheduled_on DATE, scheduled_at TIMESTAMP, date_source VARCHAR, status VARCHAR,
          observed_on DATE, source VARCHAR, source_match_id VARCHAR
        )
        """
    )
    if fixture_inserts:
        connection.executemany(
            "INSERT INTO wikimedia_fixtures VALUES (" + ",".join("?" for _ in range(20)) + ")",
            fixture_inserts,
        )
    return {
        "pages": len(parsed_events),
        "new_matches": len(match_inserts),
        "linked_matches": len(linked_matches),
        "conflicts": len(conflict_inserts),
        "fixtures": len(fixture_inserts),
    }


def _create_ranking_tables(
    connection: duckdb.DuckDBPyConnection, sources: Sequence[SourceFile]
) -> None:
    ranking_sources = [item for item in sources if item.kind == "rankings"]
    connection.execute(
        f"""
        CREATE TABLE raw_rankings AS
        SELECT csv.*, files.tour, files.source_label, files.source_path,
          files.source_url, files.revision, files.sha256 AS source_sha256
        FROM read_csv({_sql_list(item.local_path for item in ranking_sources)},
                      header=true, all_varchar=true, union_by_name=true,
                      filename=true, null_padding=true) csv
        JOIN source_files files ON csv.filename = files.local_path
        """
    )
    connection.execute(
        """
        CREATE TABLE rankings AS
        SELECT * EXCLUDE (dedupe) FROM (
          SELECT tour, year(try_strptime(trim(ranking_date), '%Y%m%d'))::SMALLINT AS year,
            try_strptime(trim(ranking_date), '%Y%m%d')::DATE AS ranking_date,
            tour || ':' || trim(player) AS player_id,
            try_cast(rank AS INTEGER) AS rank,
            try_cast(points AS INTEGER) AS points,
            CASE WHEN tour = 'wta' THEN try_cast(tours AS INTEGER) ELSE NULL END AS tournaments_played,
            'sackmann'::VARCHAR AS source, source_path, source_url, revision, source_sha256,
            row_number() OVER (
              PARTITION BY tour, trim(ranking_date), trim(player)
              ORDER BY CASE WHEN source_label = 'current' THEN 0 ELSE 1 END, source_path
            ) AS dedupe
          FROM raw_rankings
          WHERE try_strptime(trim(coalesce(ranking_date, '')), '%Y%m%d') IS NOT NULL
            AND trim(coalesce(player, '')) <> ''
        ) WHERE dedupe = 1 ORDER BY tour, year, ranking_date, rank, player_id
        """
    )


def _create_identity_and_reports(
    connection: duckdb.DuckDBPyConnection, sources: Sequence[SourceFile], as_of: date
) -> None:
    connection.execute(
        """
        CREATE TABLE event_links AS
        SELECT source, source_label, source_event_id, draw, event_id, tour, year,
          false AS provisional FROM events
        UNION
        SELECT observations.source, observations.source_label, observations.source_event_id,
          matches.draw, observations.event_id, observations.tour, observations.year,
          false AS provisional
        FROM observations JOIN matches USING (match_id, event_id, tour, year)
        ORDER BY source, source_label, source_event_id, draw, tour, year, event_id
        """
    )
    connection.execute(
        """
        CREATE TABLE player_links AS
        SELECT preferred_source AS source, preferred_source_player_id AS source_player_id,
          player_id, tour, false AS provisional FROM players ORDER BY source, source_player_id
        """
    )
    connection.execute(
        """
        CREATE TABLE match_links AS
        SELECT source, source_match_id, row_fingerprint, match_id, event_id, tour, year,
          false AS provisional FROM observations ORDER BY source, source_match_id, row_fingerprint
        """
    )
    connection.execute(
        """
        CREATE TABLE conflicts AS SELECT * FROM wikimedia_conflicts
        """
    )
    connection.execute(
        """
        CREATE TABLE fixtures AS SELECT * FROM wikimedia_fixtures
        """
    )
    connection.execute(
        """
        CREATE TABLE corrections (
          correction_id VARCHAR, match_id VARCHAR, field VARCHAR, corrected_value VARCHAR,
          source_url VARCHAR, contributor VARCHAR, contributed_on DATE,
          license VARCHAR, status VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE coverage AS
        SELECT 'matches'::VARCHAR AS table_name, tour, year, level, draw,
          count(*)::BIGINT AS row_count, count(DISTINCT event_id)::BIGINT AS event_count,
          count(played_on)::BIGINT AS exact_date_count,
          count(score)::BIGINT AS score_count,
          count(stats.match_id)::BIGINT AS statistics_count,
          min(event_start_date) AS minimum_date, max(event_start_date) AS maximum_date,
          min(preferred_source) AS source
        FROM matches LEFT JOIN match_stats stats USING (match_id, tour, year)
        GROUP BY tour, year, level, draw ORDER BY tour, year, level, draw
        """
    )
    connection.execute(
        f"""
        CREATE TABLE health AS
        SELECT tour, DATE {_quoted(as_of.isoformat())} AS as_of,
          count(*)::BIGINT AS match_count,
          count(DISTINCT event_id)::BIGINT AS event_count,
          min(event_start_date) AS earliest_event_date,
          max(event_start_date) AS latest_event_date,
          (SELECT max(ranking_date) FROM rankings r WHERE r.tour = m.tour) AS latest_ranking_date,
          (SELECT count(*) FROM rankings r WHERE r.tour = m.tour)::BIGINT AS ranking_row_count,
          (SELECT count(*) FROM quarantine q WHERE q.tour = m.tour)::BIGINT AS quarantined_rows,
          CASE
            WHEN (SELECT count(*) FROM rankings r WHERE r.tour = m.tour) = 0 THEN 'unhealthy'
            WHEN date_diff('day', (SELECT max(ranking_date) FROM rankings r WHERE r.tour = m.tour),
                           DATE {_quoted(as_of.isoformat())}) > 14 THEN 'stale'
            ELSE 'healthy' END AS status
        FROM matches m GROUP BY tour ORDER BY tour
        """
    )
    connection.execute(
        """
        CREATE TABLE source_audit AS
        SELECT f.kind, f.tour, f.year, f.source_label, f.source_path, f.source_url,
          f.revision, f.sha256,
          CASE
            WHEN f.kind = 'matches' THEN (SELECT count(*) FROM raw_matches r WHERE r.source_path = f.source_path)
            WHEN f.kind = 'players' THEN (SELECT count(*) FROM raw_players r WHERE r.filename = f.local_path)
            WHEN f.kind = 'rankings' THEN (SELECT count(*) FROM raw_rankings r WHERE r.source_path = f.source_path)
            ELSE 0 END::BIGINT AS source_rows,
          CASE WHEN f.kind = 'matches' THEN
            (SELECT count(*) FROM observations o WHERE o.source_label = f.source_label AND o.year = f.year AND o.tour = f.tour)
            ELSE NULL END::BIGINT AS normalized_rows,
          CASE WHEN f.kind = 'matches' THEN
            (SELECT count(*) FROM quarantine q WHERE q.source_path = f.source_path)
            ELSE 0 END::BIGINT AS quarantined_rows
        FROM source_files f ORDER BY kind, tour, year, source_label
        """
    )


def _copy_parquet(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    path: Path,
    dataset_version: str,
    *,
    row_group_size: int,
    compression_level: int = 6,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection.execute(
        f"""
        COPY ({query}) TO {_quoted(path)} (
          FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL {compression_level},
          ROW_GROUP_SIZE {row_group_size},
          KV_METADATA {{schema_version: '{SCHEMA_VERSION}', dataset_version: '{dataset_version}'}}
        )
        """
    )


def create_direct_downloads(root: Path, output: Path) -> dict[str, dict[str, int]]:
    """Create rolling ATP/WTA aliases and an all-records Parquet download set.

    Each file contains completed matches plus the best-effort fixture rows. The
    flat union keeps every match column and adds record and scheduling fields so
    clients can distinguish completed matches from fixtures without a join.
    """
    root = root.resolve()
    output = output.resolve()
    catalog = root / "catalog" / "catalog.parquet"
    if not catalog.exists():
        raise ValueError("downloads require an existing v3 dataset catalog")
    match_files = sorted((root / "matches").glob("tour=*/year=*/matches.parquet"))
    fixture_files = sorted((root / "fixtures").glob("tour=*/current.parquet"))
    if not match_files or not fixture_files:
        raise ValueError("downloads require match and fixture Parquet files")

    connection = duckdb.connect()
    dataset_version = str(
        _required_row(
            connection.execute(
                f"SELECT dataset_version FROM read_parquet({_quoted(catalog)}) LIMIT 1"
            )
        )[0]
    )
    union_query = f"""
        WITH completed AS (
          SELECT 'completed'::VARCHAR AS record_type, match_id AS record_id,
            false AS is_fixture, NULL::VARCHAR AS fixture_id,
            NULL::DATE AS scheduled_on, NULL::TIMESTAMP AS scheduled_at,
            NULL::VARCHAR AS schedule_date_source,
            NULL::DATE AS fixture_observed_on,
            NULL::VARCHAR AS fixture_source,
            NULL::VARCHAR AS fixture_source_match_id,
            m.*
          FROM read_parquet({_sql_list(match_files)}, union_by_name=true) m
        ), scheduled AS (
          SELECT 'fixture'::VARCHAR AS record_type, fixture_id AS record_id,
            true AS is_fixture, fixture_id, scheduled_on, scheduled_at,
            date_source AS schedule_date_source,
            observed_on AS fixture_observed_on, source AS fixture_source,
            source_match_id AS fixture_source_match_id,
            'singles'::VARCHAR AS discipline,
            f.* EXCLUDE (
              fixture_id, scheduled_on, scheduled_at, date_source,
              observed_on, source, source_match_id
            )
          FROM read_parquet({_sql_list(fixture_files)}, union_by_name=true) f
        )
        SELECT * FROM completed
        UNION ALL BY NAME
        SELECT * FROM scheduled
    """
    order = (
        "tour, is_fixture, year, coalesce(scheduled_on, event_start_date), "
        "event_id, round_order, record_id"
    )
    output.mkdir(parents=True, exist_ok=True)
    for filename in DOWNLOAD_FILENAMES:
        path = output / filename
        if path.exists():
            path.unlink()

    for tour in TOURS:
        destination = output / f"{tour}.parquet"
        _copy_parquet(
            connection,
            f"SELECT * FROM ({union_query}) records "
            f"WHERE tour={_quoted(tour)} ORDER BY {order}",
            destination,
            dataset_version,
            row_group_size=DOWNLOAD_ROW_GROUP_SIZE,
            compression_level=DOWNLOAD_COMPRESSION_LEVEL,
        )
    shutil.copy2(output / "atp.parquet", output / "mens.parquet")
    shutil.copy2(output / "wta.parquet", output / "womens.parquet")
    _copy_parquet(
        connection,
        f"SELECT * FROM ({union_query}) records ORDER BY {order}",
        output / "all-matches.parquet",
        dataset_version,
        row_group_size=DOWNLOAD_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )

    expected_schema: list[tuple[str, str]] | None = None
    summary: dict[str, dict[str, int]] = {}
    for filename in DOWNLOAD_FILENAMES:
        path = output / filename
        if path.stat().st_size > MAX_PARQUET_BYTES:
            raise RuntimeError(f"direct download exceeds 75 MB: {filename}")
        schema = [
            (row[0], row[1])
            for row in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet({_quoted(path)})"
            ).fetchall()
        ]
        if expected_schema is None:
            expected_schema = schema
        elif schema != expected_schema:
            raise RuntimeError(f"direct download schema drift: {filename}")
        rows, fixtures = _required_row(
            connection.execute(
                f"SELECT count(*), count(*) FILTER (WHERE is_fixture) "
                f"FROM read_parquet({_quoted(path)})"
            )
        )
        summary[filename] = {
            "rows": int(rows),
            "fixtures": int(fixtures),
            "bytes": path.stat().st_size,
        }
    if sha256_file(output / "atp.parquet") != sha256_file(output / "mens.parquet"):
        raise RuntimeError("ATP and men's direct download aliases differ")
    if sha256_file(output / "wta.parquet") != sha256_file(output / "womens.parquet"):
        raise RuntimeError("WTA and women's direct download aliases differ")
    if summary["all-matches.parquet"]["fixtures"] == 0:
        raise RuntimeError("direct downloads contain no future fixtures")
    connection.close()
    return summary


def _write_partitioned_tables(
    connection: duckdb.DuckDBPyConnection,
    output: Path,
    dataset_version: str,
) -> None:
    for table, filename, row_group in (
        ("matches", "matches.parquet", MATCH_ROW_GROUP_SIZE),
        ("events", "events.parquet", MATCH_ROW_GROUP_SIZE),
        ("match_stats", "match-stats.parquet", OBSERVATION_ROW_GROUP_SIZE),
        ("observations", "observations.parquet", OBSERVATION_ROW_GROUP_SIZE),
        ("rankings", "rankings.parquet", RANKING_ROW_GROUP_SIZE),
    ):
        partitions = connection.execute(
            f"SELECT DISTINCT tour, year FROM {table} ORDER BY tour, year"
        ).fetchall()
        for tour, year in partitions:
            destination = output / table / f"tour={tour}" / f"year={year}" / filename
            _copy_parquet(
                connection,
                f"SELECT * FROM {table} WHERE tour = {_quoted(tour)} AND year = {int(year)}",
                destination,
                dataset_version,
                row_group_size=row_group,
            )

    for tour in TOURS:
        _copy_parquet(
            connection,
            f"SELECT * FROM players WHERE tour = {_quoted(tour)}",
            output / "players" / f"tour={tour}" / "players.parquet",
            dataset_version,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )
        _copy_parquet(
            connection,
            f"SELECT * FROM fixtures WHERE tour = {_quoted(tour)}",
            output / "fixtures" / f"tour={tour}" / "current.parquet",
            dataset_version,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )

    for tour, year in connection.execute(
        "SELECT DISTINCT tour, year FROM match_links ORDER BY tour, year"
    ).fetchall():
        _copy_parquet(
            connection,
            f"SELECT * FROM match_links WHERE tour = {_quoted(tour)} AND year = {int(year)}",
            output
            / "identity"
            / "matches"
            / f"tour={tour}"
            / f"year={year}"
            / "match-links.parquet",
            dataset_version,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )

    for table, relative, row_group in (
        ("coverage", "coverage/coverage.parquet", MATCH_ROW_GROUP_SIZE),
        ("source_audit", "coverage/source-audit.parquet", MATCH_ROW_GROUP_SIZE),
        ("health", "health/health.parquet", MATCH_ROW_GROUP_SIZE),
        ("event_links", "identity/event-links.parquet", OBSERVATION_ROW_GROUP_SIZE),
        ("player_links", "identity/player-links.parquet", OBSERVATION_ROW_GROUP_SIZE),
        ("conflicts", "conflicts/conflicts.parquet", MATCH_ROW_GROUP_SIZE),
        ("quarantine", "quarantine/quarantine.parquet", MATCH_ROW_GROUP_SIZE),
    ):
        _copy_parquet(
            connection,
            f"SELECT * FROM {table}",
            output / relative,
            dataset_version,
            row_group_size=row_group,
        )


def _table_name_for_path(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    first = relative.parts[0]
    if first == "identity":
        return path.stem.replace("-", "_")
    if first == "coverage" and path.stem == "source-audit":
        return "source_audit"
    return first


def _create_catalog(
    connection: duckdb.DuckDBPyConnection, output: Path, dataset_version: str, revision: str
) -> None:
    records: list[tuple[Any, ...]] = []
    for path in sorted(output.rglob("*.parquet")):
        if path.name == "catalog.parquet":
            continue
        relative = path.relative_to(output).as_posix()
        table = _table_name_for_path(path, output)
        tour_match = re.search(r"tour=([^/]+)", relative)
        year_match = re.search(r"year=([0-9]{4})", relative)
        count = _required_row(
            connection.execute(f"SELECT count(*) FROM read_parquet({_quoted(path)})")
        )[0]
        records.append(
            (
                relative,
                table,
                tour_match.group(1) if tour_match else None,
                int(year_match.group(1)) if year_match else None,
                int(count),
                path.stat().st_size,
                sha256_file(path),
                SCHEMA_VERSION,
                dataset_version,
                revision,
            )
        )
    connection.execute(
        """
        CREATE TABLE catalog (
          path VARCHAR, table_name VARCHAR, tour VARCHAR, year INTEGER,
          row_count BIGINT, byte_size BIGINT, sha256 VARCHAR,
          schema_version INTEGER, dataset_version VARCHAR, source_revision VARCHAR
        )
        """
    )
    connection.executemany("INSERT INTO catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
    _copy_parquet(
        connection,
        "SELECT * FROM catalog ORDER BY table_name, tour, year, path",
        output / "catalog" / "catalog.parquet",
        dataset_version,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )


def build_dataset(
    output: Path,
    years: Sequence[int],
    *,
    as_of: date,
    dataset_version: str | None = None,
    workers: int = 12,
) -> dict[str, Any]:
    dataset_version = dataset_version or as_of.strftime("%Y.%m.%d")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="open-tennis-v3-") as temporary_name:
        temporary = Path(temporary_name)
        sources, revision = download_sources(temporary / "sources", years, workers=workers)
        generated = temporary / "generated"
        database = temporary / "build.duckdb"
        connection = duckdb.connect(str(database))
        connection.execute("SET preserve_insertion_order = false")
        connection.execute("SET threads = 4")
        _create_source_file_table(connection, sources)
        _create_match_tables(connection, sources, as_of)
        _create_player_tables(connection, sources)
        wikimedia = _ingest_wikimedia(
            connection, year=max(years), as_of=as_of, workers=min(workers, 12)
        )
        print(
            "Wikimedia: "
            f"{wikimedia['pages']} pages, {wikimedia['new_matches']} new matches, "
            f"{wikimedia['linked_matches']} linked, {wikimedia['fixtures']} fixtures, "
            f"{wikimedia['conflicts']} conflicts",
            flush=True,
        )
        _create_ranking_tables(connection, sources)
        _create_identity_and_reports(connection, sources, as_of)
        _write_partitioned_tables(connection, generated, dataset_version)
        corrections_path = generated.parent / "corrections.parquet"
        _copy_parquet(
            connection,
            "SELECT * FROM corrections",
            corrections_path,
            dataset_version,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
        _create_catalog(connection, generated, dataset_version, revision)
        connection.close()
        validation = validate_dataset(generated)
        if validation:
            raise RuntimeError("generated dataset failed validation:\n" + "\n".join(validation))
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(generated, output)
        contribution_root = output.parent / "contributions"
        contribution_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(corrections_path, contribution_root / "corrections.parquet")
    catalog_rows = _required_row(
        duckdb.connect().execute(
            f"SELECT sum(row_count), sum(byte_size), count(*) FROM read_parquet({_quoted(output / 'catalog/catalog.parquet')})"
        )
    )
    return {
        "dataset_version": dataset_version,
        "source_revision": revision,
        "catalog_rows": int(catalog_rows[2]),
        "logical_rows": int(catalog_rows[0] or 0),
        "bytes": int(catalog_rows[1] or 0),
    }


def _replace_parquet(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    path: Path,
    dataset_version: str,
    *,
    row_group_size: int,
) -> None:
    temporary = path.with_name(path.stem + ".tmp.parquet")
    _copy_parquet(
        connection,
        query,
        temporary,
        dataset_version,
        row_group_size=row_group_size,
    )
    os.replace(temporary, path)


def refresh_wikimedia_dataset(
    root: Path,
    *,
    as_of: date,
    dataset_version: str | None = None,
    workers: int = 12,
) -> dict[str, int]:
    """Replace only current Wikimedia rows, fixtures, and affected v3 metadata."""
    root = root.resolve()
    dataset_version = dataset_version or as_of.strftime("%Y.%m.%d")
    year = as_of.year
    catalog_path = root / "catalog" / "catalog.parquet"
    if not catalog_path.exists():
        raise ValueError("refresh requires an existing v3 dataset")
    metadata_connection = duckdb.connect()
    revision_row = _required_row(
        metadata_connection.execute(
            f"SELECT source_revision FROM read_parquet({_quoted(catalog_path)}) LIMIT 1"
        )
    )
    revision = revision_row[0]
    connection = duckdb.connect()

    def load(table: str, files: Sequence[Path]) -> None:
        if not files:
            raise ValueError(f"missing existing {table} partitions")
        connection.execute(
            f"CREATE TABLE {table} AS SELECT * FROM read_parquet({_sql_list(files)}, union_by_name=true)"
        )

    load("matches", _data_files(root, "matches", TOURS, [year]))
    load("events", _data_files(root, "events", TOURS, [year]))
    load("observations", _data_files(root, "observations", TOURS, [year]))
    load("players", _data_files(root, "players", TOURS, None))
    load("match_stats", _data_files(root, "match_stats", TOURS, [year]))
    old_counts = {
        tour: _required_row(
            connection.execute(
                "SELECT count(*), count(DISTINCT event_id) FROM matches WHERE tour = ?", [tour]
            )
        )
        for tour in TOURS
    }
    connection.execute("DELETE FROM observations WHERE source = 'wikimedia'")
    connection.execute("DELETE FROM matches WHERE preferred_source = 'wikimedia'")
    connection.execute("DELETE FROM events WHERE source = 'wikimedia'")
    connection.execute("UPDATE matches SET source_count = 1 WHERE source_count > 1")
    wikimedia = _ingest_wikimedia(connection, year=year, as_of=as_of, workers=workers)

    for table, filename, row_group in (
        ("matches", "matches.parquet", MATCH_ROW_GROUP_SIZE),
        ("events", "events.parquet", MATCH_ROW_GROUP_SIZE),
        ("observations", "observations.parquet", OBSERVATION_ROW_GROUP_SIZE),
    ):
        for tour in TOURS:
            _replace_parquet(
                connection,
                f"SELECT * FROM {table} WHERE tour={_quoted(tour)} ORDER BY ALL",
                root / table / f"tour={tour}" / f"year={year}" / filename,
                dataset_version,
                row_group_size=row_group,
            )
    for tour in TOURS:
        _replace_parquet(
            connection,
            f"SELECT * FROM players WHERE tour={_quoted(tour)} ORDER BY player_id",
            root / "players" / f"tour={tour}" / "players.parquet",
            dataset_version,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )
        _replace_parquet(
            connection,
            f"SELECT * FROM wikimedia_fixtures WHERE tour={_quoted(tour)} ORDER BY fixture_id",
            root / "fixtures" / f"tour={tour}" / "current.parquet",
            dataset_version,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
        _replace_parquet(
            connection,
            f"SELECT source, source_match_id, row_fingerprint, match_id, event_id, tour, year, "
            f"false AS provisional FROM observations WHERE tour={_quoted(tour)} ORDER BY source, source_match_id",
            root / "identity" / "matches" / f"tour={tour}" / f"year={year}" / "match-links.parquet",
            dataset_version,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )

    connection.execute(
        f"CREATE TABLE event_links_all AS SELECT * FROM read_parquet({_quoted(root / 'identity/event-links.parquet')})"
    )
    connection.execute("DELETE FROM event_links_all WHERE year = ?", [year])
    connection.execute(
        """
        INSERT INTO event_links_all
        SELECT source, source_label, source_event_id, draw, event_id, tour, year, false FROM events
        UNION
        SELECT o.source, o.source_label, o.source_event_id, m.draw, o.event_id, o.tour, o.year, false
        FROM observations o JOIN matches m USING(match_id, event_id, tour, year)
        """
    )
    _replace_parquet(
        connection,
        "SELECT DISTINCT * FROM event_links_all ORDER BY source, source_label, source_event_id, draw, tour, year, event_id",
        root / "identity" / "event-links.parquet",
        dataset_version,
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
    )
    _replace_parquet(
        connection,
        "SELECT preferred_source AS source, preferred_source_player_id AS source_player_id, "
        "player_id, tour, false AS provisional FROM players ORDER BY source, source_player_id",
        root / "identity" / "player-links.parquet",
        dataset_version,
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
    )
    _replace_parquet(
        connection,
        "SELECT * FROM wikimedia_conflicts ORDER BY conflict_id",
        root / "conflicts" / "conflicts.parquet",
        dataset_version,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )

    connection.execute(
        f"CREATE TABLE coverage_all AS SELECT * FROM read_parquet({_quoted(root / 'coverage/coverage.parquet')})"
    )
    connection.execute("DELETE FROM coverage_all WHERE year = ?", [year])
    connection.execute(
        """
        INSERT INTO coverage_all
        SELECT 'matches', matches.tour, matches.year, level, draw,
          count(*)::BIGINT, count(DISTINCT event_id)::BIGINT, count(played_on)::BIGINT,
          count(score)::BIGINT, count(stats.match_id)::BIGINT,
          min(event_start_date), max(event_start_date), min(preferred_source)
        FROM matches LEFT JOIN match_stats stats USING(match_id, tour, year)
        GROUP BY matches.tour, matches.year, level, draw
        """
    )
    _replace_parquet(
        connection,
        "SELECT * FROM coverage_all ORDER BY tour, year, level, draw",
        root / "coverage" / "coverage.parquet",
        dataset_version,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )

    connection.execute(
        f"CREATE TABLE health_all AS SELECT * FROM read_parquet({_quoted(root / 'health/health.parquet')})"
    )
    for tour in TOURS:
        new_match_count, new_event_count = _required_row(
            connection.execute(
                "SELECT count(*), count(DISTINCT event_id) FROM matches WHERE tour=?", [tour]
            )
        )
        old_match_count, old_event_count = old_counts[tour]
        connection.execute(
            "UPDATE health_all SET match_count=match_count-?+?, event_count=event_count-?+?, as_of=? WHERE tour=?",
            [old_match_count, new_match_count, old_event_count, new_event_count, as_of, tour],
        )
    _replace_parquet(
        connection,
        "SELECT * FROM health_all ORDER BY tour",
        root / "health" / "health.parquet",
        dataset_version,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    connection.close()

    catalog_path.unlink()
    catalog_connection = duckdb.connect()
    _create_catalog(catalog_connection, root, dataset_version, revision)
    catalog_connection.close()
    errors = validate_dataset(root)
    if errors:
        raise RuntimeError("refreshed dataset failed validation:\n" + "\n".join(errors))
    return wikimedia


def _semantically_equal_parquet(
    connection: duckdb.DuckDBPyConnection, old: Path, new: Path
) -> bool:
    old_schema = connection.execute(
        f"DESCRIBE SELECT * FROM read_parquet({_quoted(old)})"
    ).fetchall()
    new_schema = connection.execute(
        f"DESCRIBE SELECT * FROM read_parquet({_quoted(new)})"
    ).fetchall()
    if [(row[0], row[1]) for row in old_schema] != [(row[0], row[1]) for row in new_schema]:
        return False
    columns = [row[0] for row in new_schema]
    volatile = {
        "first_observed_on",
        "last_updated_on",
        "source_url",
        "revision",
        "retrieved_on",
        "as_of",
        "dataset_version",
        "source_revision",
    }
    selected = [column for column in columns if column not in volatile]
    if not selected:
        selected = columns
    projection = ",".join('"' + column.replace('"', '""') + '"' for column in selected)
    different = _required_row(
        connection.execute(
            f"""
        SELECT EXISTS(
          (SELECT {projection} FROM read_parquet({_quoted(new)})
           EXCEPT SELECT {projection} FROM read_parquet({_quoted(old)}))
          UNION ALL
          (SELECT {projection} FROM read_parquet({_quoted(old)})
           EXCEPT SELECT {projection} FROM read_parquet({_quoted(new)}))
          LIMIT 1
        )
        """
        )
    )[0]
    return not bool(different)


def promote_dataset(generated: Path, target: Path) -> dict[str, int]:
    """Promote only semantically changed Parquet files and rebuild the target catalog."""
    generated = generated.resolve()
    target = target.resolve()
    generated_catalog = generated / "catalog" / "catalog.parquet"
    if not generated_catalog.exists():
        raise ValueError("generated dataset has no catalog")
    connection = duckdb.connect()
    dataset_version, revision = _required_row(
        connection.execute(
            f"SELECT dataset_version, source_revision FROM read_parquet({_quoted(generated_catalog)}) LIMIT 1"
        )
    )
    new_paths = {
        path.relative_to(generated)
        for path in generated.rglob("*.parquet")
        if path != generated_catalog
    }
    changed = 0
    changed_bytes = 0
    for relative in sorted(new_paths):
        source = generated / relative
        destination = target / relative
        if destination.exists() and _semantically_equal_parquet(connection, destination, source):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.stem + ".promote.parquet")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        changed += 1
        changed_bytes += destination.stat().st_size
    for old in sorted(target.rglob("*.parquet")):
        if old == target / "catalog" / "catalog.parquet":
            continue
        if old.relative_to(target) not in new_paths:
            old.unlink()
            changed += 1
    target_catalog = target / "catalog" / "catalog.parquet"
    if target_catalog.exists():
        target_catalog.unlink()
    catalog_connection = duckdb.connect()
    _create_catalog(catalog_connection, target, str(dataset_version), str(revision))
    catalog_connection.close()
    errors = validate_dataset(target)
    if errors:
        raise RuntimeError("promoted dataset failed validation:\n" + "\n".join(errors))
    return {"changed_files": changed, "changed_bytes": changed_bytes}


def _data_files(
    root: Path, table: str, tours: Sequence[str], years: Sequence[int] | None
) -> list[Path]:
    catalog_path = root / "catalog" / "catalog.parquet"
    connection = duckdb.connect()
    predicates = [f"table_name = {_quoted(table)}"]
    if tours:
        predicates.append("tour IN (" + ",".join(_quoted(tour) for tour in tours) + ")")
    if years:
        predicates.append("year IN (" + ",".join(str(year) for year in years) + ")")
    rows = connection.execute(
        f"SELECT path FROM read_parquet({_quoted(catalog_path)}) WHERE {' AND '.join(predicates)} ORDER BY path"
    ).fetchall()
    return [root / row[0] for row in rows]


def register_views(
    connection: duckdb.DuckDBPyConnection,
    root: Path,
    *,
    tours: Sequence[str] = (),
    years: Sequence[int] | None = None,
) -> None:
    for table in (
        "matches",
        "events",
        "match_stats",
        "observations",
        "rankings",
        "players",
        "fixtures",
    ):
        files = _data_files(root, table, tours, years if table != "players" else None)
        if not files:
            continue
        connection.execute(
            f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM read_parquet({_sql_list(files)}, union_by_name=true)"
        )


def query_dataset(
    root: Path,
    sql: str,
    *,
    tours: Sequence[str] = (),
    years: Sequence[int] | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    connection = duckdb.connect()
    register_views(connection, root, tours=tours, years=years)
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    return columns, cursor.fetchall()


def extract_dataset(
    root: Path,
    output: Path,
    *,
    tours: Sequence[str],
    years: Sequence[int] | None,
    levels: Sequence[str],
) -> int:
    connection = duckdb.connect()
    register_views(connection, root, tours=tours, years=years)
    predicates: list[str] = []
    if levels:
        predicates.append("level IN (" + ",".join(_quoted(level) for level in levels) + ")")
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    output.parent.mkdir(parents=True, exist_ok=True)
    connection.execute(
        f"COPY (SELECT * FROM matches{where} ORDER BY tour, year, level, event_start_date, event_id, round_order, match_id) "
        f"TO {_quoted(output)} (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, ROW_GROUP_SIZE {MATCH_ROW_GROUP_SIZE}, "
        f"KV_METADATA {{schema_version: '{SCHEMA_VERSION}'}})"
    )
    return int(
        _required_row(connection.execute(f"SELECT count(*) FROM read_parquet({_quoted(output)})"))[
            0
        ]
    )


def add_correction(
    path: Path,
    *,
    match_id: str,
    field: str,
    corrected_value: str,
    source_url: str,
    contributor: str,
    contributed_on: date,
) -> str:
    if not source_url.startswith(("https://", "http://")):
        raise ValueError("source_url must be an HTTP(S) URL")
    correction_id = (
        "correction:"
        + hashlib.sha256(
            "|".join([match_id, field, corrected_value, source_url]).encode()
        ).hexdigest()[:20]
    )
    connection = duckdb.connect()
    if path.exists():
        connection.execute(
            f"CREATE TABLE corrections AS SELECT * FROM read_parquet({_quoted(path)})"
        )
        metadata = connection.execute(
            f"SELECT value FROM parquet_kv_metadata({_quoted(path)}) WHERE key = 'dataset_version'"
        ).fetchone()
        dataset_version = (
            (metadata[0].decode() if metadata and isinstance(metadata[0], bytes) else metadata[0])
            if metadata
            else contributed_on.strftime("%Y.%m.%d")
        )
    else:
        connection.execute(
            """
            CREATE TABLE corrections (
              correction_id VARCHAR, match_id VARCHAR, field VARCHAR, corrected_value VARCHAR,
              source_url VARCHAR, contributor VARCHAR, contributed_on DATE,
              license VARCHAR, status VARCHAR
            )
            """
        )
        dataset_version = contributed_on.strftime("%Y.%m.%d")
    connection.execute("DELETE FROM corrections WHERE correction_id = ?", [correction_id])
    connection.execute(
        "INSERT INTO corrections VALUES (?, ?, ?, ?, ?, ?, ?, 'CC0-1.0', 'proposed')",
        [correction_id, match_id, field, corrected_value, source_url, contributor, contributed_on],
    )
    temporary = path.with_suffix(".tmp.parquet")
    _copy_parquet(
        connection,
        "SELECT * FROM corrections ORDER BY correction_id",
        temporary,
        str(dataset_version),
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, path)
    return correction_id


def validate_dataset(root: Path) -> list[str]:
    errors: list[str] = []
    root = root.resolve()
    catalog = root / "catalog" / "catalog.parquet"
    if not catalog.exists():
        return ["missing catalog/catalog.parquet"]
    connection = duckdb.connect()
    catalog_rows = connection.execute(
        f"SELECT * FROM read_parquet({_quoted(catalog)}) ORDER BY path"
    ).fetchall()
    catalog_columns = [item[0] for item in connection.description]
    positions = {name: index for index, name in enumerate(catalog_columns)}
    schemas: dict[str, list[tuple[str, str]]] = {}
    for row in catalog_rows:
        path = root / row[positions["path"]]
        table_name = row[positions["table_name"]]
        if not path.exists():
            errors.append(f"missing catalog file: {path.relative_to(root)}")
            continue
        if path.stat().st_size > MAX_PARQUET_BYTES:
            errors.append(f"file exceeds 75 MB: {path.relative_to(root)}")
        if sha256_file(path) != row[positions["sha256"]]:
            errors.append(f"checksum mismatch: {path.relative_to(root)}")
        metadata = connection.execute(
            f"SELECT key, value FROM parquet_kv_metadata({_quoted(path)})"
        ).fetchall()
        decoded = {
            (key.decode() if isinstance(key, bytes) else key): (
                value.decode() if isinstance(value, bytes) else value
            )
            for key, value in metadata
        }
        if decoded.get("schema_version") != str(SCHEMA_VERSION):
            errors.append(f"missing schema_version=3 metadata: {path.relative_to(root)}")
        schema = [
            (item[0], item[1])
            for item in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet({_quoted(path)})"
            ).fetchall()
        ]
        if table_name in schemas and schemas[table_name] != schema:
            errors.append(f"schema drift in {table_name}: {path.relative_to(root)}")
        else:
            schemas[table_name] = schema
        parquet_rows = connection.execute(
            f"SELECT DISTINCT compression, row_group_id, row_group_num_rows "
            f"FROM parquet_metadata({_quoted(path)})"
        ).fetchall()
        if parquet_rows and any(item[0] != "ZSTD" for item in parquet_rows):
            errors.append(f"non-ZSTD compression: {path.relative_to(root)}")
        expected_group_size = (
            RANKING_ROW_GROUP_SIZE
            if table_name == "rankings"
            else OBSERVATION_ROW_GROUP_SIZE
            if table_name
            in {
                "observations",
                "match_stats",
                "match_links",
                "event_links",
                "player_links",
                "players",
            }
            else MATCH_ROW_GROUP_SIZE
        )
        if any(int(item[2]) > expected_group_size + 2048 for item in parquet_rows):
            errors.append(f"oversized row group: {path.relative_to(root)}")

    register_views(connection, root)
    checks = {
        "duplicate match IDs": "SELECT count(*) - count(DISTINCT match_id) FROM matches",
        "duplicate event IDs": "SELECT count(*) - count(DISTINCT event_id) FROM events",
        "duplicate player IDs": "SELECT count(*) - count(DISTINCT player_id) FROM players",
        "orphan match events": "SELECT count(*) FROM matches m LEFT JOIN events e USING(event_id) WHERE e.event_id IS NULL",
        "orphan match players": "SELECT count(*) FROM matches m LEFT JOIN players p ON m.player1_id=p.player_id WHERE p.player_id IS NULL",
        "invalid winners": "SELECT count(*) FROM matches WHERE winner_id NOT IN (player1_id, player2_id)",
        "false exact dates": "SELECT count(*) FROM matches WHERE played_on IS NULL AND played_on_precision='day'",
        "missing statistics": "SELECT CASE WHEN count(*) = 0 THEN 1 ELSE 0 END FROM match_stats",
    }
    for label, sql in checks.items():
        value = int(_required_row(connection.execute(sql))[0])
        if value:
            errors.append(f"{label}: {value}")
    audits = connection.execute(
        f"SELECT source_path, source_rows, normalized_rows, quarantined_rows FROM read_parquet({_quoted(root / 'coverage/source-audit.parquet')}) WHERE kind='matches'"
    ).fetchall()
    for source_path, source_rows, normalized_rows, quarantined_rows in audits:
        if int(source_rows or 0) != int(normalized_rows or 0) + int(quarantined_rows or 0):
            errors.append(
                f"source reconciliation failed for {source_path}: {source_rows} != {normalized_rows}+{quarantined_rows}"
            )
    unhealthy = connection.execute(
        f"SELECT tour, status FROM read_parquet({_quoted(root / 'health/health.parquet')}) WHERE status = 'unhealthy'"
    ).fetchall()
    errors.extend(f"health {tour}: {status}" for tour, status in unhealthy)
    return errors


def format_rows(columns: Sequence[str], rows: Sequence[Sequence[Any]], output: Any = None) -> None:
    output = output or sys.stdout
    output.write("\t".join(columns) + "\n")
    for row in rows:
        output.write("\t".join("" if value is None else str(value) for value in row) + "\n")


def shell(root: Path) -> int:
    connection = duckdb.connect()
    register_views(connection, root)
    print("Open Tennis Data v3 DuckDB shell. End statements with ';'. Use .quit to exit.")
    buffer: list[str] = []
    while True:
        try:
            line = input("otd> " if not buffer else "...  ")
        except EOFError:
            break
        if line.strip() in {".quit", ".exit"}:
            break
        buffer.append(line)
        if not line.rstrip().endswith(";"):
            continue
        sql = "\n".join(buffer)
        buffer = []
        try:
            cursor = connection.execute(sql)
            format_rows([item[0] for item in cursor.description], cursor.fetchall())
        except duckdb.Error as exc:
            print(f"error: {exc}", file=sys.stderr)
    return 0
