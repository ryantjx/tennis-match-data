"""Open Tennis Data Parquet build, query, and validation services."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from open_tennis_data.model import source_slot_match_id
from open_tennis_data.schema import (
    MATCH_COLUMNS,
    MATCH_SCHEMA,
    SCHEMA_METADATA_KEY,
    SCHEMA_VERSION,
    SOURCE_LICENSES,
    TOURS,
)

ARCHIVE_REPOSITORY = "Aneeshers/tennis-sackmann-archive"
ARCHIVE_RESOLVE = f"https://huggingface.co/datasets/{ARCHIVE_REPOSITORY}/resolve"
USER_AGENT = "open-tennis-data (https://github.com/ryantjx/tennis-match-data)"
MAX_PARQUET_BYTES = 75 * 1024 * 1024
NORMAL_COMMIT_BYTES = 25 * 1024 * 1024
MATCH_ROW_GROUP_SIZE = 65_536
OBSERVATION_ROW_GROUP_SIZE = 32_768
RANKING_ROW_GROUP_SIZE = 65_536
DOWNLOAD_ROW_GROUP_SIZE = MATCH_ROW_GROUP_SIZE
DOWNLOAD_COMPRESSION_LEVEL = 19
MATCH_COMPRESSION_LEVEL = 19
STRING_DICTIONARY_PAGE_SIZE_LIMIT = 1_048_576

DOWNLOAD_FILENAMES = (
    "mens.parquet",
    "womens.parquet",
    "atp.parquet",
    "wta.parquet",
    "all-matches.parquet",
)
TOURNAMENT_DOWNLOAD_FILENAME = "tournaments.parquet"
PROVENANCE_DOWNLOAD_FILENAME = "provenance.parquet"
SOURCES_DOWNLOAD_FILENAME = "sources.parquet"
FIXTURE_COLUMNS = MATCH_COLUMNS

TOURNAMENT_COLUMNS = (
    "tournament_id",
    "tour",
    "year",
    "tournament_name",
    "level",
    "surface",
    "indoor",
    "start_date",
    "end_date",
    "city",
    "country",
    "source_url",
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


def _request_revision(url: str, *, attempts: int = 4) -> str:
    """Read repository revision headers without downloading the response body."""
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT}, method="HEAD"
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.headers.get("X-Repo-Commit", "unknown")
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def resolve_archive_revision() -> str:
    probe = f"{ARCHIVE_RESOLVE}/main/atp/atp_matches_2025.csv?download=true"
    revision = _request_revision(probe)
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
    years: Sequence[int], include_rankings: bool, current_rankings_only: bool = False
) -> list[tuple[str, str, int | None, str, str]]:
    specs: list[tuple[str, str, int | None, str, str]] = [
        ("players", tour, None, "players", f"{tour}/{tour}_players.csv") for tour in TOURS
    ]
    specs.extend(
        ("matches", tour, year, label, path) for tour, year, label, path in _match_specs(years)
    )
    if include_rankings:
        for tour in TOURS:
            keys = ("current",) if current_rankings_only else RANKING_KEYS[tour]
            for key in keys:
                specs.append(("rankings", tour, None, key, f"{tour}/{tour}_rankings_{key}.csv"))
    return specs


def download_sources(
    temporary: Path,
    years: Sequence[int],
    *,
    include_rankings: bool = True,
    current_rankings_only: bool = False,
    workers: int = 12,
    revision: str | None = None,
) -> tuple[list[SourceFile], str]:
    revision = revision or resolve_archive_revision()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("source revision must be a 40-character lowercase Git SHA")
    specs = _source_specs(years, include_rankings, current_rankings_only)
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
               ELSE 'name:' || substr(sha256(lower(trim(coalesce(loser_name, '')))), 1, 20) END AS loser_player_id,
          CASE
            WHEN duplicate_ordinal > 1 THEN 'duplicate_source_row'
            WHEN
              CASE WHEN trim(coalesce(winner_id, '')) <> '' THEN tour || ':' || trim(winner_id)
                   ELSE 'name:' || substr(sha256(lower(trim(coalesce(winner_name, '')))), 1, 20) END
              =
              CASE WHEN trim(coalesce(loser_id, '')) <> '' THEN tour || ':' || trim(loser_id)
                   ELSE 'name:' || substr(sha256(lower(trim(coalesce(loser_name, '')))), 1, 20) END
              THEN 'invalid_participants'
            WHEN
              coalesce(try_cast(minutes AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_ace AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_df AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_svpt AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_1stIn AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_1stWon AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_2ndWon AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_SvGms AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_bpSaved AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_bpFaced AS BIGINT) < 0, false)
              OR coalesce(try_cast(l_ace AS BIGINT) < 0, false)
              OR coalesce(try_cast(l_df AS BIGINT) < 0, false)
              OR coalesce(try_cast(l_svpt AS BIGINT) < 0, false)
              OR coalesce(try_cast(l_1stIn AS BIGINT) < 0, false)
              OR coalesce(try_cast(l_1stWon AS BIGINT) < 0, false)
              OR coalesce(try_cast(l_2ndWon AS BIGINT) < 0, false)
              OR coalesce(try_cast(l_SvGms AS BIGINT) < 0, false)
              OR coalesce(try_cast(l_bpSaved AS BIGINT) < 0, false)
              OR coalesce(try_cast(l_bpFaced AS BIGINT) < 0, false)
              OR coalesce(try_cast(w_1stIn AS BIGINT) > try_cast(w_svpt AS BIGINT), false)
              OR coalesce(try_cast(l_1stIn AS BIGINT) > try_cast(l_svpt AS BIGINT), false)
              OR coalesce(try_cast(w_1stWon AS BIGINT) > try_cast(w_1stIn AS BIGINT), false)
              OR coalesce(try_cast(l_1stWon AS BIGINT) > try_cast(l_1stIn AS BIGINT), false)
              OR coalesce(try_cast(w_bpSaved AS BIGINT) > try_cast(w_bpFaced AS BIGINT), false)
              OR coalesce(try_cast(l_bpSaved AS BIGINT) > try_cast(l_bpFaced AS BIGINT), false)
              THEN 'invalid_statistics'
            ELSE NULL
          END AS rejection_reason
        FROM raw_match_ranked
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
        WHERE rejection_reason IS NULL
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
          source_match_id, row_fingerprint, rejection_reason::VARCHAR AS reason
        FROM normalized_base WHERE rejection_reason IS NOT NULL
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
    fixture_years: Sequence[int] | None = None,
) -> dict[str, int]:
    """Merge current reusable Wikimedia results without using intermediate JSON files."""
    from open_tennis_data.fixtures import parse_wikimedia_fixture_page
    from open_tennis_data.model import normalize_text
    from open_tennis_data.sources.wikimedia import (
        discover_pages,
        fetch_page,
        fetch_pages_optional,
        parse_page,
        parse_tournament_page,
    )

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

    fixture_years = tuple(sorted(set(fixture_years or (year,))))
    tasks: list[tuple[str, int, str]] = []
    for tour in TOURS:
        for fixture_year in fixture_years:
            tasks.extend(
                (tour, fixture_year, title)
                for title in discover_pages(fixture_year, tour)
            )

    pages: list[tuple[str, int, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_page, title): (tour, page_year)
            for tour, page_year, title in tasks
        }
        for future in as_completed(futures):
            tour, page_year = futures[future]
            pages.append((tour, page_year, future.result()))

    tournament_tasks: dict[tuple[str, int, str], str] = {}
    for tour, page_year, page in pages:
        title_match = re.match(r"^(\d{4})\s+(.+?)\s+[–-]", str(page["title"]))
        if not title_match:
            continue
        event_name = title_match.group(2).strip()
        tournament_tasks[(tour, page_year, event_name)] = f"{page_year} {event_name}"
    tournament_metadata: list[dict[str, Any]] = []
    tournament_pages = fetch_pages_optional(sorted(set(tournament_tasks.values())))
    tasks_by_title: dict[str, list[tuple[str, int, str]]] = {}
    for key, title in tournament_tasks.items():
        tasks_by_title.setdefault(title, []).append(key)
    for title, tournament_page in tournament_pages.items():
        for tour, page_year, event_name in tasks_by_title.get(title, []):
            metadata = parse_tournament_page(tournament_page, tour, page_year)
            metadata["event_name"] = event_name
            tournament_metadata.append(metadata)
    connection.execute(
        """
        CREATE TABLE wikimedia_tournament_metadata (
          tour VARCHAR, year SMALLINT, event_key VARCHAR, event_name VARCHAR,
          start_date DATE, end_date DATE, city VARCHAR, country VARCHAR,
          surface VARCHAR, indoor BOOLEAN, source_url VARCHAR,
          source_tournament_id VARCHAR
        )
        """
    )
    if tournament_metadata:
        connection.executemany(
            "INSERT INTO wikimedia_tournament_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item["tour"],
                    item["year"],
                    normalize_text(item["event_name"]),
                    item["event_name"],
                    item["start_date"],
                    item["end_date"],
                    item["city"],
                    item["country"],
                    item["surface"],
                    item["indoor"],
                    item["source_url"],
                    item["source_tournament_id"],
                )
                for item in tournament_metadata
            ],
        )

    connection.execute(
        """
        CREATE TABLE wikimedia_page_audit (
          kind VARCHAR, tour VARCHAR, year SMALLINT, title VARCHAR,
          source_url VARCHAR, revision VARCHAR, sha256 VARCHAR
        )
        """
    )
    page_audit_rows: list[tuple[Any, ...]] = []
    for tour, page_year, page in pages:
        title = str(page["title"])
        page_audit_rows.append(
            (
                "fixtures",
                tour,
                page_year,
                title,
                "https://en.wikipedia.org/wiki/"
                + urllib.parse.quote(title.replace(" ", "_")),
                str(page["revision_id"]),
                hashlib.sha256(str(page["content"]).encode("utf-8")).hexdigest(),
            )
        )
    for title, page in tournament_pages.items():
        for tour, page_year, _ in tasks_by_title.get(title, []):
            page_audit_rows.append(
                (
                    "tournaments",
                    tour,
                    page_year,
                    title,
                    "https://en.wikipedia.org/wiki/"
                    + urllib.parse.quote(str(page["title"]).replace(" ", "_")),
                    str(page["revision_id"]),
                    hashlib.sha256(str(page["content"]).encode("utf-8")).hexdigest(),
                )
            )
    if page_audit_rows:
        connection.executemany(
            "INSERT INTO wikimedia_page_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
            page_audit_rows,
        )

    parsed_events: list[dict[str, Any]] = []
    for tour, page_year, page in sorted(
        pages, key=lambda item: (item[0], item[1], item[2]["title"])
    ):
        if page_year != year:
            continue
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
                canonical_match_id = source_slot_match_id(
                    "wikimedia", source_match_id
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

    for tour, _, page in sorted(
        pages, key=lambda item: (item[0], item[1], item[2]["title"])
    ):
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
                    "https://en.wikipedia.org/wiki/"
                    + urllib.parse.quote(str(page["title"]).replace(" ", "_")),
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
                    player.get("source_ids", {}).get("wikipedia") or player_id,
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
    metadata_by_key = {
        (str(item["tour"]), int(item["year"]), normalize_text(str(item["event_name"]))): item
        for item in tournament_metadata
    }
    connection.execute(
        """
        CREATE TABLE wikimedia_tournament_event_metadata (
          event_id VARCHAR, start_date DATE, end_date DATE, city VARCHAR,
          country VARCHAR, surface VARCHAR, indoor BOOLEAN, source_url VARCHAR,
          source_tournament_id VARCHAR
        )
        """
    )
    metadata_rows: list[tuple[Any, ...]] = []
    for event_id, event_tour, event_year, event_name in connection.execute(
        "SELECT event_id, tour, year, event_name FROM events"
    ).fetchall():
        item = metadata_by_key.get(
            (str(event_tour), int(event_year), normalize_text(str(event_name)))
        )
        if item:
            metadata_rows.append(
                (
                    event_id,
                    item["start_date"],
                    item["end_date"],
                    item["city"],
                    item["country"],
                    item["surface"],
                    item["indoor"],
                    item["source_url"],
                    item["source_tournament_id"],
                )
            )
    if metadata_rows:
        connection.executemany(
            "INSERT INTO wikimedia_tournament_event_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            metadata_rows,
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
              ORDER BY CASE WHEN source_label = 'current' THEN 0 ELSE 1 END,
                source_path, try_cast(rank AS INTEGER),
                try_cast(points AS INTEGER) DESC NULLS LAST,
                try_cast(tours AS INTEGER) DESC NULLS LAST
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
          correction_id VARCHAR, entity_type VARCHAR, entity_id VARCHAR,
          field VARCHAR, corrected_value VARCHAR,
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
        SELECT 'source_file_' || substr(sha256(concat_ws('|', 'sackmann', f.source_url,
                 f.revision, f.sha256)), 1, 20) AS source_file_id,
          f.kind, f.tour, f.year, f.source_label, f.source_path, f.source_url,
          f.revision, f.sha256, 'CC-BY-NC-SA-4.0'::VARCHAR AS license,
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


def _create_lean_tables(connection: duckdb.DuckDBPyConnection, as_of: date) -> None:
    """Normalize temporary ingestion tables into the published lean contract."""
    connection.execute(
        """
        UPDATE players AS player SET name=correction.corrected_value
        FROM corrections AS correction
        WHERE correction.status='approved' AND correction.entity_type='player'
          AND correction.field='name' AND correction.entity_id=player.player_id
        """
    )
    for table in ("matches", "fixtures"):
        connection.execute(
            f"""
            UPDATE {table} AS record SET player1_name=player.name
            FROM players AS player WHERE record.player1_id=player.player_id
            """
        )
        connection.execute(
            f"""
            UPDATE {table} AS record SET player2_name=player.name
            FROM players AS player WHERE record.player2_id=player.player_id
            """
        )
    connection.execute(
        """
        CREATE TABLE event_tournaments AS
        SELECT e.event_id, e.tour, e.year, e.draw, e.source, e.source_label,
          e.source_event_id,
          'tournament_' || tour || '_' || year || '_' || substr(
            sha256(concat_ws('|', tour, year,
              CASE WHEN w.source_tournament_id IS NOT NULL THEN w.source_tournament_id
                WHEN source = 'wikimedia'
                THEN lower(regexp_replace(event_name, '[^a-zA-Z0-9]+', '', 'g'))
                ELSE coalesce(nullif(source_event_id, ''),
                  lower(regexp_replace(event_name, '[^a-zA-Z0-9]+', '', 'g')))
              END)), 1, 12
          ) AS tournament_id,
          event_name AS tournament_name, level,
          coalesce(w.surface, e.surface) AS surface,
          coalesce(w.indoor, e.indoor) AS indoor,
          coalesce(w.start_date, e.event_start_date) AS start_date,
          coalesce(w.end_date, e.event_end_date) AS end_date,
          coalesce(w.city, e.city) AS city,
          coalesce(w.country, e.country) AS country,
          w.source_url AS tournament_source_url
        FROM events e
        LEFT JOIN wikimedia_tournament_event_metadata w USING(event_id)
        """
    )
    for field in ("tournament_name", "level", "surface", "city", "country"):
        connection.execute(
            f"""
            UPDATE event_tournaments AS tournament
            SET {field}=correction.corrected_value
            FROM corrections AS correction
            WHERE correction.status='approved' AND correction.entity_type='tournament'
              AND correction.field={_quoted(field)}
              AND correction.entity_id=tournament.tournament_id
            """
        )
    for field in ("start_date", "end_date"):
        connection.execute(
            f"""
            UPDATE event_tournaments AS tournament
            SET {field}=try_cast(correction.corrected_value AS DATE)
            FROM corrections AS correction
            WHERE correction.status='approved' AND correction.entity_type='tournament'
              AND correction.field={_quoted(field)}
              AND correction.entity_id=tournament.tournament_id
            """
        )
    connection.execute(
        """
        UPDATE event_tournaments AS tournament
        SET indoor=try_cast(correction.corrected_value AS BOOLEAN)
        FROM corrections AS correction
        WHERE correction.status='approved' AND correction.entity_type='tournament'
          AND correction.field='indoor'
          AND correction.entity_id=tournament.tournament_id
        """
    )
    connection.execute(
        """
        CREATE TABLE event_source_urls AS
        SELECT event_id, min(source_url) AS source_url
        FROM observations WHERE source_url IS NOT NULL GROUP BY event_id
        """
    )
    connection.execute(
        """
        CREATE TABLE fixture_source_urls AS
        SELECT event_id, min(source) AS source_url
        FROM fixtures WHERE starts_with(source, 'http') GROUP BY event_id
        """
    )
    connection.execute(
        """
        CREATE TABLE tournaments_lean AS
        SELECT et.tournament_id, et.tour, et.year,
          arg_min(et.tournament_name, et.event_id) AS tournament_name,
          arg_min(et.level, et.event_id) AS level,
          arg_min(et.surface, et.event_id) AS surface,
          bool_or(et.indoor) AS indoor,
          min(et.start_date) AS start_date,
          max(et.end_date) AS end_date,
          arg_min(et.city, et.event_id) AS city,
          arg_min(et.country, et.event_id) AS country,
          coalesce(min(et.tournament_source_url), min(eu.source_url), min(fu.source_url)) AS source_url
        FROM event_tournaments et
        LEFT JOIN event_source_urls eu USING(event_id)
        LEFT JOIN fixture_source_urls fu USING(event_id)
        GROUP BY et.tournament_id, et.tour, et.year
        ORDER BY et.tour, et.year, start_date, et.tournament_id
        """
    )
    connection.execute(
        """
        CREATE TABLE matches_lean AS
        SELECT m.played_on AS date, m.match_id, et.tournament_id,
          et.tournament_name, m.tour, m.year::SMALLINT AS year, m.draw, m.round,
          m.discipline AS format,
          [m.player1_id]::VARCHAR[] AS player1_id,
          [m.player1_name]::VARCHAR[] AS player1_name, m.player1_seed,
          [m.player2_id]::VARCHAR[] AS player2_id,
          [m.player2_name]::VARCHAR[] AS player2_name, m.player2_seed,
          [m.winner_id]::VARCHAR[] AS winner_id, m.status, m.score,
          coalesce(m.best_of, CASE
            WHEN m.tour='atp' AND et.level='grand_slam' AND m.draw='main' THEN 5
            ELSE 3 END)::TINYINT AS best_of
        FROM matches m JOIN event_tournaments et USING(event_id, tour, year)
        ORDER BY date NULLS LAST, et.tournament_id, m.draw,
          m.round_order, m.match_id
        """
    )
    connection.execute(
        """
        CREATE TABLE fixtures_lean AS
        SELECT f.scheduled_on AS date, f.fixture_id AS match_id, et.tournament_id,
          et.tournament_name, f.tour, f.year::SMALLINT AS year, f.draw, f.round,
          'singles'::VARCHAR AS format,
          CASE WHEN f.player1_id IS NULL THEN NULL ELSE [f.player1_id]::VARCHAR[] END AS player1_id,
          CASE WHEN f.player1_name IS NULL THEN NULL ELSE [f.player1_name]::VARCHAR[] END AS player1_name,
          NULL::VARCHAR AS player1_seed,
          CASE WHEN f.player2_id IS NULL THEN NULL ELSE [f.player2_id]::VARCHAR[] END AS player2_id,
          CASE WHEN f.player2_name IS NULL THEN NULL ELSE [f.player2_name]::VARCHAR[] END AS player2_name,
          NULL::VARCHAR AS player2_seed, NULL::VARCHAR[] AS winner_id,
          'fixture'::VARCHAR AS status, NULL::VARCHAR AS score,
          CASE WHEN f.tour='atp' AND et.level='grand_slam' AND f.draw='main'
            THEN 5 ELSE 3 END::TINYINT AS best_of
        FROM fixtures f JOIN event_tournaments et USING(event_id, tour, year)
        ORDER BY date NULLS LAST, et.tournament_id, f.draw, f.round, f.fixture_id
        """
    )
    for field in ("round", "player1_seed", "player2_seed", "status", "score"):
        connection.execute(
            f"""
            UPDATE matches_lean AS match SET {field}=correction.corrected_value
            FROM corrections AS correction
            WHERE correction.status='approved' AND correction.entity_type='match'
              AND correction.field={_quoted(field)}
              AND correction.entity_id=match.match_id
            """
        )
    for field in ("round", "player1_seed", "player2_seed"):
        connection.execute(
            f"""
            UPDATE fixtures_lean AS match SET {field}=correction.corrected_value
            FROM corrections AS correction
            WHERE correction.status='approved' AND correction.entity_type='match'
              AND correction.field={_quoted(field)}
              AND correction.entity_id=match.match_id
            """
        )
    connection.execute(
        """
        UPDATE matches_lean AS match SET date=try_cast(correction.corrected_value AS DATE)
        FROM corrections AS correction
        WHERE correction.status='approved' AND correction.entity_type='match'
          AND correction.field='date' AND correction.entity_id=match.match_id
        """
    )
    connection.execute(
        """
        UPDATE fixtures_lean AS match SET date=try_cast(correction.corrected_value AS DATE)
        FROM corrections AS correction
        WHERE correction.status='approved' AND correction.entity_type='match'
          AND correction.field='date' AND correction.entity_id=match.match_id
        """
    )
    connection.execute(
        """
        UPDATE matches_lean AS match SET best_of=try_cast(correction.corrected_value AS TINYINT)
        FROM corrections AS correction
        WHERE correction.status='approved' AND correction.entity_type='match'
          AND correction.field='best_of' AND correction.entity_id=match.match_id
        """
    )
    connection.execute(
        """
        UPDATE fixtures_lean AS match
        SET best_of=try_cast(correction.corrected_value AS TINYINT)
        FROM corrections AS correction
        WHERE correction.status='approved' AND correction.entity_type='match'
          AND correction.field='best_of' AND correction.entity_id=match.match_id
        """
    )
    connection.execute(
        """
        CREATE TABLE observations_lean AS
        WITH combined AS (
          SELECT match_id, tour, year,
            'source_file_' || substr(sha256(concat_ws('|', source, source_url,
              revision, source_sha256)), 1, 20) AS source_file_id,
            source_match_id
          FROM observations
          UNION ALL
          SELECT f.fixture_id AS match_id, f.tour, f.year,
            'source_file_' || substr(sha256(concat_ws('|', 'wikimedia', a.source_url,
              a.revision, a.sha256)), 1, 20) AS source_file_id,
            f.source_match_id
          FROM fixtures f
          JOIN wikimedia_page_audit a ON a.kind='fixtures' AND a.tour=f.tour
            AND a.year=f.year AND a.source_url=f.source
        )
        SELECT * FROM combined
        QUALIFY count(DISTINCT match_id) OVER (
          PARTITION BY source_file_id,source_match_id)=1
        ORDER BY tour, year, source_file_id, source_match_id, match_id
        """
    )
    connection.execute(
        """
        CREATE TABLE tournament_sources AS
        SELECT DISTINCT et.source, et.source_event_id AS source_tournament_id,
          et.tournament_id, et.tour, et.year,
          coalesce(et.tournament_source_url, eu.source_url, fu.source_url) AS source_url
        FROM event_tournaments et
        LEFT JOIN event_source_urls eu USING(event_id)
        LEFT JOIN fixture_source_urls fu USING(event_id)
        ORDER BY et.source, source_tournament_id, et.tour, et.year, et.tournament_id
        """
    )
    connection.execute(
        """
        CREATE TABLE source_audit_lean AS
        SELECT * FROM source_audit
        UNION ALL BY NAME
        SELECT 'source_file_' || substr(sha256(concat_ws('|', source, source_url,
                 revision, source_sha256)), 1, 20) AS source_file_id,
          'matches'::VARCHAR AS kind, tour, year, source AS source_label,
          source_url AS source_path, source_url, revision, source_sha256 AS sha256,
          'CC-BY-SA-4.0'::VARCHAR AS license,
          count(*)::BIGINT AS source_rows, count(*)::BIGINT AS normalized_rows,
          0::BIGINT AS quarantined_rows
        FROM observations
        WHERE source <> 'sackmann'
        GROUP BY source, tour, year, source_url, revision, source_sha256
        UNION ALL BY NAME
        SELECT 'source_file_' || substr(sha256(concat_ws('|', 'wikimedia', source_url,
                 revision, sha256)), 1, 20) AS source_file_id,
          kind, tour, year, 'wikimedia'::VARCHAR AS source_label,
          title AS source_path, source_url, revision, sha256,
          'CC-BY-SA-4.0'::VARCHAR AS license,
          NULL::BIGINT AS source_rows, NULL::BIGINT AS normalized_rows,
          0::BIGINT AS quarantined_rows
        FROM wikimedia_page_audit
        """
    )
    connection.execute(
        """
        CREATE TABLE coverage_lean AS
        SELECT 'matches'::VARCHAR AS table_name, m.tour, m.year, t.level, m.draw,
          count(*)::BIGINT AS row_count,
          count(DISTINCT m.tournament_id)::BIGINT AS tournament_count,
          count(m.score)::BIGINT AS score_count,
          count(s.match_id)::BIGINT AS statistics_count,
          min(t.start_date) AS minimum_date, max(t.end_date) AS maximum_date
        FROM matches_lean m
        JOIN tournaments_lean t USING(tournament_id, tour, year)
        LEFT JOIN match_stats s USING(match_id, tour, year)
        GROUP BY m.tour, m.year, t.level, m.draw
        ORDER BY m.tour, m.year, t.level, m.draw
        """
    )
    connection.execute(
        f"""
        CREATE TABLE health_lean AS
        SELECT m.tour, DATE {_quoted(as_of.isoformat())} AS as_of,
          count(*)::BIGINT AS match_count,
          count(DISTINCT m.tournament_id)::BIGINT AS tournament_count,
          min(t.start_date) AS earliest_tournament_date,
          max(coalesce(t.end_date, t.start_date)) AS latest_tournament_date,
          (SELECT max(ranking_date) FROM rankings r WHERE r.tour=m.tour) AS latest_ranking_date,
          (SELECT count(*) FROM rankings r WHERE r.tour=m.tour)::BIGINT AS ranking_row_count,
          (SELECT count(*) FROM quarantine q WHERE q.tour=m.tour)::BIGINT AS quarantined_rows,
          CASE
            WHEN (SELECT count(*) FROM rankings r WHERE r.tour=m.tour) = 0 THEN 'unhealthy'
            WHEN date_diff('day', (SELECT max(ranking_date) FROM rankings r WHERE r.tour=m.tour),
                           DATE {_quoted(as_of.isoformat())}) > 14 THEN 'stale'
            ELSE 'healthy' END AS status
        FROM matches_lean m JOIN tournaments_lean t USING(tournament_id, tour, year)
        GROUP BY m.tour ORDER BY m.tour
        """
    )
    for table in (
        "matches",
        "fixtures",
        "observations",
        "source_audit",
        "coverage",
        "health",
    ):
        connection.execute(f"DROP TABLE {table}")
        connection.execute(f"ALTER TABLE {table}_lean RENAME TO {table}")
    connection.execute("ALTER TABLE tournaments_lean RENAME TO tournaments")
    connection.execute("DROP TABLE events")
    connection.execute("DROP TABLE event_links")
    connection.execute("DROP TABLE match_links")


def _copy_parquet(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    path: Path,
    *,
    row_group_size: int,
    compression_level: int = 6,
    match_shaped: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if match_shaped:
        row_group_size = MATCH_ROW_GROUP_SIZE
        compression_level = MATCH_COMPRESSION_LEVEL
        connection.execute("SET threads=1")
    metadata = (
        f", KV_METADATA {{{SCHEMA_METADATA_KEY}: {_quoted(SCHEMA_VERSION)}}}"
        if match_shaped
        else ""
    )
    connection.execute(
        f"""
        COPY ({query}) TO {_quoted(path)} (
          FORMAT PARQUET, PARQUET_VERSION 'V2', COMPRESSION ZSTD,
          COMPRESSION_LEVEL {compression_level}, ROW_GROUP_SIZE {row_group_size},
          STRING_DICTIONARY_PAGE_SIZE_LIMIT {STRING_DICTIONARY_PAGE_SIZE_LIMIT}{metadata}
        )
        """
    )


def create_direct_downloads(
    root: Path, output: Path, *, future_only: bool = False
) -> dict[str, dict[str, int]]:
    """Create separate completed-match or fixture release assets."""
    root = root.resolve()
    output = output.resolve()
    if not (root / "catalog" / "catalog.parquet").exists():
        raise ValueError("downloads require an existing dataset catalog")
    match_files = sorted((root / "matches").glob("tour=*/year=*/matches.parquet"))
    fixture_files = sorted((root / "fixtures").glob("tour=*/current.parquet"))
    tournament_files = sorted(
        (root / "tournaments").glob("tour=*/year=*/tournaments.parquet")
    )
    observation_files = sorted(
        (root / "observations").glob("tour=*/year=*/observations.parquet")
    )
    source_audit = root / "coverage" / "source-audit.parquet"
    if (
        not match_files
        or not fixture_files
        or not tournament_files
        or not observation_files
        or not source_audit.exists()
    ):
        raise ValueError("downloads require match, fixture, and tournament Parquet files")

    connection = duckdb.connect()
    as_of = _required_row(
        connection.execute(
            f"SELECT as_of FROM read_parquet({_quoted(root / 'catalog/catalog.parquet')}) LIMIT 1"
        )
    )[0]
    if not isinstance(as_of, date):
        raise ValueError(f"catalog as_of must be a DATE, got {as_of!r}")
    source_files = fixture_files if future_only else match_files
    records_query = (
        f"SELECT * FROM read_parquet({_sql_list(source_files)}, "
        "union_by_name=true, hive_partitioning=false)"
    )
    if future_only:
        records_query += (
            f" WHERE date IS NULL OR date >= DATE {_quoted(as_of.isoformat())}"
        )
    order = "date NULLS LAST, tournament_id, draw, round, match_id"
    output.mkdir(parents=True, exist_ok=True)
    for filename in (
        *DOWNLOAD_FILENAMES,
        TOURNAMENT_DOWNLOAD_FILENAME,
        PROVENANCE_DOWNLOAD_FILENAME,
        SOURCES_DOWNLOAD_FILENAME,
    ):
        path = output / filename
        if path.exists():
            path.unlink()

    for tour in TOURS:
        destination = output / f"{tour}.parquet"
        _copy_parquet(
            connection,
            f"SELECT * FROM ({records_query}) records "
            f"WHERE tour={_quoted(tour)} ORDER BY {order}",
            destination,
            row_group_size=DOWNLOAD_ROW_GROUP_SIZE,
            compression_level=DOWNLOAD_COMPRESSION_LEVEL,
            match_shaped=True,
        )
    shutil.copy2(output / "atp.parquet", output / "mens.parquet")
    shutil.copy2(output / "wta.parquet", output / "womens.parquet")
    _copy_parquet(
        connection,
        f"SELECT * FROM ({records_query}) records ORDER BY {order}",
        output / "all-matches.parquet",
        row_group_size=DOWNLOAD_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
        match_shaped=True,
    )
    _copy_parquet(
        connection,
        f"SELECT * FROM read_parquet({_sql_list(tournament_files)}, union_by_name=true) "
        "ORDER BY tour, year, start_date, tournament_id",
        output / TOURNAMENT_DOWNLOAD_FILENAME,
        row_group_size=DOWNLOAD_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )
    _copy_parquet(
        connection,
        f"WITH records AS ({records_query}), observations AS ("
        f"SELECT * FROM read_parquet({_sql_list(observation_files)}, "
        "union_by_name=true, hive_partitioning=false)) "
        "SELECT DISTINCT o.* FROM observations o JOIN records r "
        "USING(match_id,tour,year) ORDER BY tour,year,source_file_id,source_match_id,match_id",
        output / PROVENANCE_DOWNLOAD_FILENAME,
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )
    _copy_parquet(
        connection,
        f"SELECT s.* FROM read_parquet({_quoted(source_audit)}) s "
        f"JOIN (SELECT DISTINCT source_file_id FROM read_parquet("
        f"{_quoted(output / PROVENANCE_DOWNLOAD_FILENAME)})) p USING(source_file_id) "
        "ORDER BY kind,tour,year,source_label,source_file_id",
        output / SOURCES_DOWNLOAD_FILENAME,
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
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
        rows = int(
            _required_row(
                connection.execute(f"SELECT count(*) FROM read_parquet({_quoted(path)})")
            )[0]
        )
        summary[filename] = {
            "rows": rows,
            "fixtures": rows if future_only else 0,
            "bytes": path.stat().st_size,
        }
        if future_only:
            invalid_rows = int(
                _required_row(
                    connection.execute(
                        f"SELECT count(*) FROM read_parquet({_quoted(path)}) "
                        f"WHERE date < DATE {_quoted(as_of.isoformat())}"
                    )
                )[0]
            )
            if invalid_rows:
                raise RuntimeError(
                    f"future direct download contains {invalid_rows} past rows: "
                    f"{filename}"
                )
    tournament_path = output / TOURNAMENT_DOWNLOAD_FILENAME
    tournament_rows = int(
        _required_row(
            connection.execute(
                f"SELECT count(*) FROM read_parquet({_quoted(tournament_path)})"
            )
        )[0]
    )
    if tournament_path.stat().st_size > MAX_PARQUET_BYTES:
        raise RuntimeError("direct tournament download exceeds 75 MB")
    summary[TOURNAMENT_DOWNLOAD_FILENAME] = {
        "rows": tournament_rows,
        "fixtures": 0,
        "bytes": tournament_path.stat().st_size,
    }
    for filename in (PROVENANCE_DOWNLOAD_FILENAME, SOURCES_DOWNLOAD_FILENAME):
        path = output / filename
        rows = int(
            _required_row(
                connection.execute(f"SELECT count(*) FROM read_parquet({_quoted(path)})")
            )[0]
        )
        summary[filename] = {"rows": rows, "fixtures": 0, "bytes": path.stat().st_size}
    if sha256_file(output / "atp.parquet") != sha256_file(output / "mens.parquet"):
        raise RuntimeError("ATP and men's direct download aliases differ")
    if sha256_file(output / "wta.parquet") != sha256_file(output / "womens.parquet"):
        raise RuntimeError("WTA and women's direct download aliases differ")
    connection.close()
    return summary


def _write_partitioned_tables(
    connection: duckdb.DuckDBPyConnection,
    output: Path,
) -> None:
    for table, filename, row_group in (
        ("matches", "matches.parquet", MATCH_ROW_GROUP_SIZE),
        ("tournaments", "tournaments.parquet", MATCH_ROW_GROUP_SIZE),
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
                f"SELECT * FROM {table} WHERE tour = {_quoted(tour)} AND year = {int(year)}"
                + (
                    " ORDER BY date NULLS LAST,tournament_id,draw,round,match_id"
                    if table == "matches"
                    else " ORDER BY ALL"
                ),
                destination,
                row_group_size=row_group,
                match_shaped=table == "matches",
            )

    for tour in TOURS:
        _copy_parquet(
            connection,
            f"SELECT * FROM players WHERE tour = {_quoted(tour)} ORDER BY ALL",
            output / "players" / f"tour={tour}" / "players.parquet",
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )
        _copy_parquet(
            connection,
            f"SELECT * FROM fixtures WHERE tour = {_quoted(tour)} "
            "ORDER BY date NULLS LAST,tournament_id,draw,round,match_id",
            output / "fixtures" / f"tour={tour}" / "current.parquet",
            row_group_size=MATCH_ROW_GROUP_SIZE,
            match_shaped=True,
        )

    for table, relative, row_group in (
        ("coverage", "coverage/coverage.parquet", MATCH_ROW_GROUP_SIZE),
        ("source_audit", "coverage/source-audit.parquet", MATCH_ROW_GROUP_SIZE),
        ("health", "health/health.parquet", MATCH_ROW_GROUP_SIZE),
        (
            "tournament_sources",
            "identity/tournament-sources.parquet",
            OBSERVATION_ROW_GROUP_SIZE,
        ),
        ("player_links", "identity/player-links.parquet", OBSERVATION_ROW_GROUP_SIZE),
        ("conflicts", "conflicts/conflicts.parquet", MATCH_ROW_GROUP_SIZE),
        ("quarantine", "quarantine/quarantine.parquet", MATCH_ROW_GROUP_SIZE),
    ):
        _copy_parquet(
            connection,
            f"SELECT * FROM {table} ORDER BY ALL",
            output / relative,
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
    connection: duckdb.DuckDBPyConnection, output: Path, as_of: date, revision: str
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
                as_of,
                revision,
            )
        )
    connection.execute(
        """
        CREATE TABLE catalog (
          path VARCHAR, table_name VARCHAR, tour VARCHAR, year INTEGER,
          row_count BIGINT, byte_size BIGINT, sha256 VARCHAR,
          as_of DATE, source_revision VARCHAR
        )
        """
    )
    connection.executemany("INSERT INTO catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
    _copy_parquet(
        connection,
        "SELECT * FROM catalog ORDER BY table_name, tour, year, path",
        output / "catalog" / "catalog.parquet",
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )


def build_dataset(
    output: Path,
    years: Sequence[int],
    *,
    as_of: date,
    workers: int = 12,
    current_rankings_only: bool = False,
    source_revision: str | None = None,
) -> dict[str, Any]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="open-tennis-data-") as temporary_name:
        temporary = Path(temporary_name)
        sources, revision = download_sources(
            temporary / "sources",
            years,
            workers=workers,
            current_rankings_only=current_rankings_only,
            revision=source_revision,
        )
        generated = temporary / "generated"
        database = temporary / "build.duckdb"
        connection = duckdb.connect(str(database))
        connection.execute("SET preserve_insertion_order = false")
        connection.execute("SET threads = 4")
        _create_source_file_table(connection, sources)
        _create_match_tables(connection, sources, as_of)
        _create_player_tables(connection, sources)
        wikimedia = _ingest_wikimedia(
            connection,
            year=max(years),
            as_of=as_of,
            workers=min(workers, 12),
            fixture_years=(max(years), max(years) + 1),
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
        contribution_path = output.parent / "contributions" / "corrections.parquet"
        if contribution_path.exists():
            correction_columns = {
                row[0]
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet({_quoted(contribution_path)})"
                ).fetchall()
            }
            if {"entity_type", "entity_id"}.issubset(correction_columns):
                connection.execute("DROP TABLE corrections")
                connection.execute(
                    f"CREATE TABLE corrections AS SELECT * FROM read_parquet("
                    f"{_quoted(contribution_path)})"
                )
        _create_lean_tables(connection, as_of)
        _write_partitioned_tables(connection, generated)
        corrections_path = generated.parent / "corrections.parquet"
        _copy_parquet(
            connection,
            "SELECT * FROM corrections",
            corrections_path,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
        _create_catalog(connection, generated, as_of, revision)
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
        "as_of": as_of,
        "source_revision": revision,
        "catalog_rows": int(catalog_rows[2]),
        "logical_rows": int(catalog_rows[0] or 0),
        "bytes": int(catalog_rows[1] or 0),
    }


def bootstrap_dataset(
    output: Path,
    *,
    through_year: int,
    as_of: date,
    workers: int = 12,
) -> dict[str, Any]:
    """Build complete history only when the destination is uninitialized."""
    output = output.resolve()
    if (output / "catalog" / "catalog.parquet").exists() or any(
        output.rglob("*.parquet")
    ):
        raise ValueError("bootstrap requires an empty, uninitialized data directory")
    return build_dataset(
        output,
        list(range(1968, through_year + 1)),
        as_of=as_of,
        workers=workers,
    )


def _merge_parquet_query(path: Path, query: str) -> None:
    connection = duckdb.connect()
    _replace_parquet(connection, query, path, row_group_size=MATCH_ROW_GROUP_SIZE)
    connection.close()


def _copy_generated_partition(
    generated: Path,
    staged: Path,
    table: str,
    tour: str,
    year: int,
    filename: str,
) -> None:
    source = generated / table / f"tour={tour}" / f"year={year}" / filename
    destination = staged / table / f"tour={tour}" / f"year={year}" / filename
    if destination.exists():
        destination.unlink()
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _reuse_tournament_ids(
    generated: Path, existing: Path, years: Sequence[int]
) -> int:
    """Apply persisted source crosswalk IDs to newly generated mutable partitions."""
    old_sources = existing / "identity" / "tournament-sources.parquet"
    new_sources = generated / "identity" / "tournament-sources.parquet"
    if not old_sources.exists() or not new_sources.exists():
        return 0
    year_sql = ",".join(str(year) for year in sorted(set(years)))
    connection = duckdb.connect()
    connection.execute(
        f"""
        CREATE TABLE tournament_id_reuse AS
        SELECT n.tournament_id AS generated_id, min(o.tournament_id) AS established_id
        FROM read_parquet({_quoted(new_sources)}) n
        JOIN read_parquet({_quoted(old_sources)}) o
          USING(source,source_tournament_id,tour,year)
        WHERE n.year IN ({year_sql}) AND n.tournament_id <> o.tournament_id
        GROUP BY n.tournament_id
        HAVING count(DISTINCT o.tournament_id)=1
        """
    )
    reused = int(
        _required_row(connection.execute("SELECT count(*) FROM tournament_id_reuse"))[0]
    )
    if not reused:
        connection.close()
        return 0
    for path in [
        *generated.glob("matches/tour=*/year=*/matches.parquet"),
        *generated.glob("tournaments/tour=*/year=*/tournaments.parquet"),
        *generated.glob("fixtures/tour=*/current.parquet"),
    ]:
        columns = {
            row[0]
            for row in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet({_quoted(path)})"
            ).fetchall()
        }
        ordering = (
            " ORDER BY date NULLS LAST,tournament_id,draw,round,match_id"
            if {"date", "tournament_id", "draw", "round", "match_id"}.issubset(columns)
            else " ORDER BY ALL"
        )
        _replace_parquet(
            connection,
            f"SELECT p.* REPLACE (coalesce(r.established_id,p.tournament_id) "
            f"AS tournament_id) FROM read_parquet({_quoted(path)}) p "
            "LEFT JOIN tournament_id_reuse r ON p.tournament_id=r.generated_id"
            + ordering,
            path,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
    _replace_parquet(
        connection,
        f"SELECT p.* REPLACE (coalesce(r.established_id,p.tournament_id) AS tournament_id) "
        f"FROM read_parquet({_quoted(new_sources)}) p LEFT JOIN tournament_id_reuse r "
        "ON p.tournament_id=r.generated_id",
        new_sources,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    connection.close()
    return reused


def _reuse_match_ids(generated: Path, existing: Path, years: Sequence[int]) -> int:
    """Reuse established source or semantic match IDs in mutable partitions."""
    year_set = sorted(set(years))
    old_observations = [
        existing / "observations" / f"tour={tour}" / f"year={year}" / "observations.parquet"
        for tour in TOURS
        for year in year_set
    ]
    new_observations = [
        generated / "observations" / f"tour={tour}" / f"year={year}" / "observations.parquet"
        for tour in TOURS
        for year in year_set
    ]
    old_observations = [path for path in old_observations if path.exists()]
    new_observations = [path for path in new_observations if path.exists()]
    old_sources = existing / "coverage/source-audit.parquet"
    new_sources = generated / "coverage/source-audit.parquet"
    if not old_observations or not new_observations or not old_sources.exists() or not new_sources.exists():
        return 0
    old_matches = sorted(existing.glob("matches/tour=*/year=*/matches.parquet"))
    new_matches = sorted(generated.glob("matches/tour=*/year=*/matches.parquet"))
    connection = duckdb.connect()
    connection.execute(
        f"""
        CREATE TABLE source_match_id_reuse AS
        WITH old_keys AS (
          SELECT o.match_id, o.tour, o.year, o.source_match_id,
            s.source_label, s.source_url
          FROM read_parquet({_sql_list(old_observations)}, union_by_name=true,
                            hive_partitioning=false) o
          JOIN read_parquet({_quoted(old_sources)}) s USING(source_file_id)
        ), new_keys AS (
          SELECT o.match_id, o.tour, o.year, o.source_match_id,
            s.source_label, s.source_url
          FROM read_parquet({_sql_list(new_observations)}, union_by_name=true,
                            hive_partitioning=false) o
          JOIN read_parquet({_quoted(new_sources)}) s USING(source_file_id)
        )
        SELECT n.match_id AS generated_id, min(o.match_id) AS established_id
        FROM new_keys n JOIN old_keys o
          USING(tour,year,source_match_id,source_label,source_url)
        WHERE n.match_id<>o.match_id
        GROUP BY n.match_id
        HAVING count(DISTINCT o.match_id)=1
        """
    )
    if old_matches and new_matches:
        connection.execute(
            f"""
            CREATE TABLE semantic_match_id_reuse AS
            SELECT n.match_id AS generated_id, min(o.match_id) AS established_id
            FROM read_parquet({_sql_list(new_matches)}, union_by_name=true,
                              hive_partitioning=false) n
            JOIN read_parquet({_sql_list(old_matches)}, union_by_name=true,
                              hive_partitioning=false) o
              ON n.tournament_id=o.tournament_id AND n.tour=o.tour AND n.year=o.year
             AND n.draw=o.draw AND n.round=o.round
             AND ((n.player1_id=o.player1_id AND n.player2_id=o.player2_id)
               OR (n.player1_id=o.player2_id AND n.player2_id=o.player1_id))
            LEFT JOIN source_match_id_reuse s ON n.match_id=s.generated_id
            WHERE s.generated_id IS NULL AND n.match_id<>o.match_id
            GROUP BY n.match_id
            HAVING count(DISTINCT o.match_id)=1
            """
        )
    else:
        connection.execute(
            "CREATE TABLE semantic_match_id_reuse (generated_id VARCHAR, established_id VARCHAR)"
        )
    connection.execute(
        "CREATE TABLE match_id_reuse AS SELECT * FROM source_match_id_reuse "
        "UNION ALL SELECT * FROM semantic_match_id_reuse"
    )
    collision_count = int(
        _required_row(
            connection.execute(
                "SELECT count(*) FROM (SELECT established_id FROM match_id_reuse "
                "GROUP BY established_id HAVING count(DISTINCT generated_id)>1)"
            )
        )[0]
    )
    if collision_count:
        connection.close()
        raise RuntimeError(
            f"match identity collision quarantined during refresh: {collision_count}"
        )
    reused = int(_required_row(connection.execute("SELECT count(*) FROM match_id_reuse"))[0])
    if not reused:
        connection.close()
        return 0
    for path in [
        *new_matches,
        *generated.glob("fixtures/tour=*/current.parquet"),
        *new_observations,
        *generated.glob("match_stats/tour=*/year=*/match-stats.parquet"),
    ]:
        columns = {
            row[0]
            for row in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet({_quoted(path)}, "
                "hive_partitioning=false)"
            ).fetchall()
        }
        ordering = (
            " ORDER BY date NULLS LAST,tournament_id,draw,round,match_id"
            if {"date", "tournament_id", "draw", "round", "match_id"}.issubset(
                columns
            )
            else " ORDER BY ALL"
        )
        _replace_parquet(
            connection,
            f"SELECT p.* REPLACE (coalesce(r.established_id,p.match_id) AS match_id) "
            f"FROM read_parquet({_quoted(path)}, hive_partitioning=false) p "
            "LEFT JOIN match_id_reuse r ON p.match_id=r.generated_id" + ordering,
            path,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
    connection.close()
    return reused


def _reuse_player_ids(generated: Path, existing: Path) -> int:
    """Apply persisted source-player crosswalks before match identity resolution."""
    old_links = existing / "identity/player-links.parquet"
    new_links = generated / "identity/player-links.parquet"
    if not old_links.exists() or not new_links.exists():
        return 0
    connection = duckdb.connect()
    connection.execute(
        f"""
        CREATE TABLE player_id_reuse AS
        SELECT n.player_id AS generated_id, min(o.player_id) AS established_id
        FROM read_parquet({_quoted(new_links)}) n
        JOIN read_parquet({_quoted(old_links)}) o
          USING(source,source_player_id,tour)
        WHERE n.player_id<>o.player_id
        GROUP BY n.player_id
        HAVING count(DISTINCT o.player_id)=1
        """
    )
    collision_count = int(
        _required_row(
            connection.execute(
                "SELECT count(*) FROM (SELECT established_id FROM player_id_reuse "
                "GROUP BY established_id HAVING count(DISTINCT generated_id)>1)"
            )
        )[0]
    )
    if collision_count:
        connection.close()
        raise RuntimeError(
            f"player identity collision quarantined during refresh: {collision_count}"
        )
    reused = int(_required_row(connection.execute("SELECT count(*) FROM player_id_reuse"))[0])
    if not reused:
        connection.close()
        return 0
    for path in generated.glob("players/tour=*/players.parquet"):
        _replace_parquet(
            connection,
            f"SELECT p.* REPLACE(coalesce(r.established_id,p.player_id) AS player_id) "
            f"FROM read_parquet({_quoted(path)}) p LEFT JOIN player_id_reuse r "
            "ON p.player_id=r.generated_id ORDER BY ALL",
            path,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )
    for path in [
        *generated.glob("matches/tour=*/year=*/matches.parquet"),
        *generated.glob("fixtures/tour=*/current.parquet"),
    ]:
        _replace_parquet(
            connection,
            "SELECT p.* REPLACE ("
            "list_transform(p.player1_id,x->coalesce(m.id_map[x],x)) AS player1_id,"
            "list_transform(p.player2_id,x->coalesce(m.id_map[x],x)) AS player2_id,"
            "list_transform(p.winner_id,x->coalesce(m.id_map[x],x)) AS winner_id) "
            f"FROM read_parquet({_quoted(path)}, hive_partitioning=false) p "
            "CROSS JOIN (SELECT map(list(generated_id),list(established_id)) id_map "
            "FROM player_id_reuse) m "
            "ORDER BY date NULLS LAST,tournament_id,draw,round,match_id",
            path,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
    _replace_parquet(
        connection,
        f"SELECT p.* REPLACE(coalesce(r.established_id,p.player_id) AS player_id) "
        f"FROM read_parquet({_quoted(new_links)}) p LEFT JOIN player_id_reuse r "
        "ON p.player_id=r.generated_id ORDER BY ALL",
        new_links,
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
    )
    connection.close()
    return reused


def _rebuild_health(root: Path, as_of: date) -> None:
    connection = duckdb.connect()
    match_files = sorted((root / "matches").glob("tour=*/year=*/matches.parquet"))
    tournament_files = sorted(
        (root / "tournaments").glob("tour=*/year=*/tournaments.parquet")
    )
    ranking_files = sorted((root / "rankings").glob("tour=*/year=*/rankings.parquet"))
    quarantine = root / "quarantine" / "quarantine.parquet"
    query = f"""
        WITH matches AS (
          SELECT * FROM read_parquet({_sql_list(match_files)}, union_by_name=true)
        ), tournaments AS (
          SELECT * FROM read_parquet({_sql_list(tournament_files)}, union_by_name=true)
        ), rankings AS (
          SELECT * FROM read_parquet({_sql_list(ranking_files)}, union_by_name=true)
        ), quarantine AS (
          SELECT * FROM read_parquet({_quoted(quarantine)})
        )
        SELECT m.tour, DATE {_quoted(as_of.isoformat())} AS as_of,
          count(*)::BIGINT AS match_count,
          count(DISTINCT m.tournament_id)::BIGINT AS tournament_count,
          min(t.start_date) AS earliest_tournament_date,
          max(coalesce(t.end_date,t.start_date)) AS latest_tournament_date,
          (SELECT max(ranking_date) FROM rankings r WHERE r.tour=m.tour) AS latest_ranking_date,
          (SELECT count(*) FROM rankings r WHERE r.tour=m.tour)::BIGINT AS ranking_row_count,
          (SELECT count(*) FROM quarantine q WHERE q.tour=m.tour)::BIGINT AS quarantined_rows,
          CASE
            WHEN (SELECT count(*) FROM rankings r WHERE r.tour=m.tour)=0 THEN 'unhealthy'
            WHEN date_diff('day',(SELECT max(ranking_date) FROM rankings r WHERE r.tour=m.tour),
                           DATE {_quoted(as_of.isoformat())}) > 14 THEN 'stale'
            ELSE 'healthy' END AS status
        FROM matches m JOIN tournaments t USING(tournament_id,tour,year)
        GROUP BY m.tour ORDER BY m.tour
    """
    _replace_parquet(
        connection,
        query,
        root / "health" / "health.parquet",
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    connection.close()


def _refresh_years(
    root: Path,
    years: Sequence[int],
    *,
    as_of: date,
    workers: int,
) -> dict[str, Any]:
    root = root.resolve()
    catalog = root / "catalog" / "catalog.parquet"
    if not catalog.exists():
        raise ValueError("refresh requires an initialized lean dataset")
    years = sorted(set(int(year) for year in years))
    before_errors = validate_dataset(root)
    if before_errors:
        raise RuntimeError("existing dataset failed validation:\n" + "\n".join(before_errors))
    with tempfile.TemporaryDirectory(prefix="open-tennis-refresh-") as temporary_name:
        temporary = Path(temporary_name)
        generated = temporary / "generated"
        corrections = root.parent / "contributions/corrections.parquet"
        if corrections.exists():
            temporary_contributions = temporary / "contributions"
            temporary_contributions.mkdir(parents=True)
            shutil.copy2(corrections, temporary_contributions / "corrections.parquet")
        build_summary = build_dataset(
            generated,
            years,
            as_of=as_of,
            workers=workers,
            current_rankings_only=True,
        )
        _reuse_tournament_ids(
            generated, root, [*years, as_of.year + 1]
        )
        _reuse_player_ids(generated, root)
        _reuse_match_ids(generated, root, [*years, as_of.year + 1])
        staged = temporary / "staged"
        shutil.copytree(root, staged)
        baseline_catalog = temporary / "baseline-catalog.parquet"
        shutil.copy2(catalog, baseline_catalog)

        partition_specs = (
            ("matches", "matches.parquet"),
            ("observations", "observations.parquet"),
            ("match_stats", "match-stats.parquet"),
            ("rankings", "rankings.parquet"),
        )
        for tour in TOURS:
            for year in years:
                for table, filename in partition_specs:
                    _copy_generated_partition(
                        generated, staged, table, tour, year, filename
                    )
            for tournament_year in sorted({*years, as_of.year + 1}):
                _copy_generated_partition(
                    generated,
                    staged,
                    "tournaments",
                    tour,
                    tournament_year,
                    "tournaments.parquet",
                )
            for table, filename in (
                ("players", "players.parquet"),
                ("fixtures", "current.parquet"),
            ):
                source = generated / table / f"tour={tour}" / filename
                destination = staged / table / f"tour={tour}" / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        year_sql = ",".join(str(year) for year in years)
        tournament_year_sql = ",".join(
            str(year) for year in sorted({*years, as_of.year + 1})
        )
        old_coverage = staged / "coverage" / "coverage.parquet"
        new_coverage = generated / "coverage" / "coverage.parquet"
        _merge_parquet_query(
            old_coverage,
            f"SELECT * FROM read_parquet({_quoted(old_coverage)}) WHERE year NOT IN ({year_sql}) "
            f"UNION ALL BY NAME SELECT * FROM read_parquet({_quoted(new_coverage)}) "
            f"WHERE year IN ({year_sql}) ORDER BY tour,year,level,draw",
        )
        old_audit = staged / "coverage" / "source-audit.parquet"
        new_audit = generated / "coverage" / "source-audit.parquet"
        _merge_parquet_query(
            old_audit,
            f"SELECT * FROM read_parquet({_quoted(old_audit)}) WHERE "
            f"(kind='matches' AND year NOT IN ({year_sql})) OR "
            f"(kind='rankings' AND source_label<>'current') OR "
            f"(kind IN ('fixtures','tournaments') AND year NOT IN ({tournament_year_sql})) "
            f"UNION ALL BY NAME SELECT * FROM read_parquet({_quoted(new_audit)}) WHERE "
            f"(kind='matches' AND year IN ({year_sql})) OR kind IN ('rankings','players') OR "
            f"(kind IN ('fixtures','tournaments') AND year IN ({tournament_year_sql})) "
            f"ORDER BY kind,tour,year,source_label",
        )
        old_quarantine = staged / "quarantine" / "quarantine.parquet"
        new_quarantine = generated / "quarantine" / "quarantine.parquet"
        _merge_parquet_query(
            old_quarantine,
            f"SELECT * FROM read_parquet({_quoted(old_quarantine)}) WHERE year NOT IN ({year_sql}) "
            f"UNION ALL BY NAME SELECT * FROM read_parquet({_quoted(new_quarantine)}) "
            f"WHERE year IN ({year_sql}) ORDER BY tour,year,source_label,source_match_id",
        )
        old_sources = staged / "identity" / "tournament-sources.parquet"
        new_sources = generated / "identity" / "tournament-sources.parquet"
        _merge_parquet_query(
            old_sources,
            f"SELECT * FROM read_parquet({_quoted(old_sources)}) "
            f"WHERE year NOT IN ({tournament_year_sql}) UNION ALL BY NAME "
            f"SELECT * FROM read_parquet({_quoted(new_sources)}) "
            f"WHERE year IN ({tournament_year_sql}) "
            f"ORDER BY source,source_tournament_id,tour,year,tournament_id",
        )
        old_player_links = staged / "identity/player-links.parquet"
        new_player_links = generated / "identity/player-links.parquet"
        _merge_parquet_query(
            old_player_links,
            f"SELECT * FROM read_parquet({_quoted(old_player_links)}) UNION "
            f"SELECT * FROM read_parquet({_quoted(new_player_links)}) ORDER BY ALL",
        )
        old_conflicts = staged / "conflicts" / "conflicts.parquet"
        new_conflicts = generated / "conflicts" / "conflicts.parquet"
        _merge_parquet_query(
            old_conflicts,
            f"SELECT * FROM read_parquet({_quoted(old_conflicts)}) UNION "
            f"SELECT * FROM read_parquet({_quoted(new_conflicts)}) ORDER BY conflict_id",
        )
        _rebuild_health(staged, as_of)
        staged_catalog = staged / "catalog" / "catalog.parquet"
        staged_catalog.unlink()
        catalog_connection = duckdb.connect()
        _create_catalog(
            catalog_connection,
            staged,
            as_of,
            str(build_summary["source_revision"]),
        )
        catalog_connection.close()
        errors = validate_dataset(
            staged,
            baseline_catalog=baseline_catalog,
            immutable_before_year=min(years),
        )
        if errors:
            raise RuntimeError("refreshed dataset failed validation:\n" + "\n".join(errors))
        promoted = promote_dataset(staged, root)
    return {
        **promoted,
        "years": years,
        "as_of": as_of,
        "source_revision": build_summary["source_revision"],
    }


def refresh_current_dataset(
    root: Path, *, as_of: date, workers: int = 12
) -> dict[str, Any]:
    return _refresh_years(root, [as_of.year], as_of=as_of, workers=workers)


def refresh_fixtures_dataset(
    root: Path, *, as_of: date, workers: int = 12
) -> dict[str, Any]:
    # The current implementation shares the isolated current-year build so a
    # fixture that becomes a result is reconciled atomically in the same run.
    return _refresh_years(root, [as_of.year], as_of=as_of, workers=workers)


def _entity_records(
    root: Path, table: str, identifier: str, years: Sequence[int]
) -> dict[str, dict[str, Any]]:
    connection = duckdb.connect()
    files = _data_files(root, table, TOURS, years if table != "fixtures" else None)
    if not files:
        connection.close()
        return {}
    year_sql = ",".join(str(year) for year in sorted(set(years)))
    cursor = connection.execute(
        f"SELECT * FROM read_parquet({_sql_list(files)}, union_by_name=true) "
        f"WHERE year IN ({year_sql})"
    )
    columns = [item[0] for item in cursor.description]
    rows = cursor.fetchall()
    connection.close()
    position = columns.index(identifier)
    return {
        str(row[position]): dict(zip(columns, row, strict=True))
        for row in rows
    }


def _entity_changes(
    old: dict[str, dict[str, Any]],
    new: dict[str, dict[str, Any]],
    tracked_fields: Sequence[str],
) -> dict[str, Any]:
    common = old.keys() & new.keys()
    modified = [key for key in common if old[key] != new[key]]
    field_changes = {
        field: sum(old[key].get(field) != new[key].get(field) for key in modified)
        for field in tracked_fields
    }
    return {
        "added": len(new.keys() - old.keys()),
        "removed": len(old.keys() - new.keys()),
        "modified": len(modified),
        "before": len(old),
        "after": len(new),
        "field_changes": field_changes,
        "added_ids": sorted(new.keys() - old.keys()),
        "removed_ids": sorted(old.keys() - new.keys()),
        "modified_ids": sorted(modified),
    }


def _audit_source_records(
    root: Path, result_years: Sequence[int], fixture_years: Sequence[int]
) -> dict[str, dict[str, Any]]:
    path = root / "coverage" / "source-audit.parquet"
    connection = duckdb.connect()
    result_sql = ",".join(str(year) for year in result_years)
    fixture_sql = ",".join(str(year) for year in fixture_years)
    cursor = connection.execute(
        f"""
        SELECT * FROM read_parquet({_quoted(path)})
        WHERE (kind='matches' AND year IN ({result_sql}))
           OR (kind IN ('fixtures','tournaments') AND year IN ({fixture_sql}))
        ORDER BY kind,tour,year,source_path
        """
    )
    columns = [item[0] for item in cursor.description]
    result: dict[str, dict[str, Any]] = {}
    for row in cursor.fetchall():
        item = dict(zip(columns, row, strict=True))
        key = "|".join(
            str(item.get(field) or "")
            for field in ("kind", "tour", "year", "source_label", "source_path")
        )
        result[key] = item
    connection.close()
    return result


def _remote_audit_revisions(
    root: Path, result_years: Sequence[int], fixture_years: Sequence[int]
) -> tuple[bool, list[dict[str, Any]]]:
    """Compare lightweight upstream revisions before downloading source content."""
    from open_tennis_data.sources.wikimedia import (
        discover_pages,
        fetch_page_revisions,
    )

    old = _audit_source_records(root, result_years, fixture_years)
    archive_old = {
        str(item["revision"])
        for item in old.values()
        if item["kind"] == "matches" and item["source_label"] != "wikimedia"
    }
    archive_new = resolve_archive_revision()
    changes: list[dict[str, Any]] = []
    archive_changed = archive_old != {archive_new}
    if archive_changed:
        changes.append(
            {
                "kind": "archive",
                "source": ARCHIVE_REPOSITORY,
                "old_revision": sorted(archive_old),
                "new_revision": archive_new,
                "old_checksum": None,
                "new_checksum": None,
            }
        )

    draw_tasks: list[tuple[str, int, str]] = []
    tournament_tasks: list[tuple[str, int, str]] = []
    for tour in TOURS:
        for year in fixture_years:
            titles = discover_pages(year, tour)
            draw_tasks.extend((tour, year, title) for title in titles)
            for title in titles:
                match = re.match(r"^(\d{4})\s+(.+?)\s+[–-]", title)
                if match:
                    tournament_tasks.append((tour, year, f"{year} {match.group(2).strip()}"))
    all_titles = sorted(
        {title for _, _, title in [*draw_tasks, *tournament_tasks]}
    )
    revisions = fetch_page_revisions(all_titles)
    remote: dict[str, str] = {}
    for kind, tasks in (("fixtures", draw_tasks), ("tournaments", tournament_tasks)):
        for tour, year, title in tasks:
            revision = revisions.get(title)
            if revision is not None:
                remote[f"{kind}|{tour}|{year}|wikimedia|{title}"] = revision
    old_wiki = {
        key: str(item["revision"])
        for key, item in old.items()
        if item["kind"] in {"fixtures", "tournaments"}
    }
    for key in sorted(old_wiki.keys() | remote.keys()):
        if old_wiki.get(key) == remote.get(key):
            continue
        kind, tour, year_text, _, title = key.split("|", 4)
        changes.append(
            {
                "kind": kind,
                "source": title,
                "tour": tour,
                "year": int(year_text),
                "old_revision": old_wiki.get(key),
                "new_revision": remote.get(key),
                "old_checksum": old.get(key, {}).get("sha256"),
                "new_checksum": None,
            }
        )
    return archive_changed or old_wiki != remote, changes


def _audit_quality_snapshot(root: Path, years: Sequence[int]) -> dict[str, int]:
    connection = duckdb.connect()
    year_sql = ",".join(str(year) for year in years)
    quarantine = int(
        _required_row(
            connection.execute(
                f"SELECT count(*) FROM read_parquet({_quoted(root / 'quarantine/quarantine.parquet')}) "
                f"WHERE year IN ({year_sql})"
            )
        )[0]
    )
    audit = root / "coverage" / "source-audit.parquet"
    reconciled, source_rows, normalized_rows = _required_row(
        connection.execute(
            f"SELECT count(*) FILTER (WHERE source_rows=normalized_rows+quarantined_rows), "
            f"coalesce(sum(source_rows),0), coalesce(sum(normalized_rows),0) "
            f"FROM read_parquet({_quoted(audit)}) WHERE kind='matches' "
            f"AND year IN ({year_sql})"
        )
    )
    connection.close()
    return {
        "quarantined_rows": quarantine,
        "reconciled_sources": int(reconciled),
        "source_rows": int(source_rows),
        "normalized_rows": int(normalized_rows),
    }


def _historical_partition_paths(root: Path, before_year: int) -> list[str]:
    connection = duckdb.connect()
    rows = connection.execute(
        f"SELECT path FROM read_parquet({_quoted(root / 'catalog/catalog.parquet')}) "
        "WHERE year IS NOT NULL AND year < ? ORDER BY path",
        [before_year],
    ).fetchall()
    connection.close()
    return [str(row[0]) for row in rows]


def _write_audit_report(output: Path, report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "retroactive-audit.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    changes = report.get("changes", {})
    lines = [
        "# Retroactive data audit",
        "",
        f"- As of: {report['as_of']}",
        f"- Status: {report['status']}",
        f"- Result years: {report['result_years'][0]}–{report['result_years'][-1]}",
        f"- Fixture years: {report['fixture_years'][0]}–{report['fixture_years'][-1]}",
        f"- Upstream revision changes: {len(report.get('source_changes', []))}",
        f"- Changed files: {report.get('changed_files', 0)}",
        f"- Older partitions unchanged: {'yes' if report.get('older_partitions_unchanged') else 'no'}",
        "",
    ]
    if report.get("error"):
        lines.extend(("## Failure", "", str(report["error"]), ""))
    if report.get("source_changes"):
        lines.extend(
            (
                "## Source revisions",
                "",
                "| Source | Old revision | New revision | Old checksum | New checksum |",
                "| --- | --- | --- | --- | --- |",
            )
        )
        for item in report["source_changes"]:
            lines.append(
                f"| {item.get('source', '')} | {item.get('old_revision') or ''} | "
                f"{item.get('new_revision') or ''} | {item.get('old_checksum') or ''} | "
                f"{item.get('new_checksum') or ''} |"
            )
        lines.append("")
    if changes:
        lines.extend(
            (
                "| Entity | Before | After | Added | Removed | Modified |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            )
        )
        for table in ("matches", "fixtures", "tournaments"):
            item = changes[table]
            lines.append(
                f"| {table} | {item['before']} | {item['after']} | {item['added']} | "
                f"{item['removed']} | {item['modified']} |"
            )
    (output / "retroactive-audit.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def audit_retroactive_dataset(
    root: Path,
    output: Path,
    *,
    as_of: date,
    workers: int = 12,
) -> dict[str, Any]:
    """Audit upstream revisions in isolation and emit machine/human reports."""
    years = [as_of.year - 1, as_of.year]
    fixture_years = [as_of.year, as_of.year + 1]
    root = root.resolve()
    base_report: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "result_years": years,
        "fixture_years": fixture_years,
        "status": "failed",
        "changed_files": 0,
        "changed_bytes": 0,
        "immutable_before_year": min(years),
        "older_partitions_unchanged": False,
        "validation": "failed",
    }
    local_errors = validate_dataset(root)
    if local_errors:
        report = {**base_report, "error": "\n".join(local_errors), "source_changes": []}
        _write_audit_report(output, report)
        raise RuntimeError("existing dataset failed validation:\n" + "\n".join(local_errors))
    connection = duckdb.connect()
    old_revision = _required_row(
        connection.execute(
            f"SELECT source_revision FROM read_parquet({_quoted(root / 'catalog/catalog.parquet')}) LIMIT 1"
        )
    )[0]
    connection.close()
    try:
        upstream_changed, source_changes = _remote_audit_revisions(
            root, years, fixture_years
        )
    except Exception as exc:
        report = {**base_report, "error": str(exc), "source_changes": []}
        _write_audit_report(output, report)
        raise RuntimeError(f"upstream revision audit failed: {exc}") from exc

    empty_change = {
        "added": 0,
        "removed": 0,
        "modified": 0,
        "before": 0,
        "after": 0,
        "field_changes": {},
        "added_ids": [],
        "removed_ids": [],
        "modified_ids": [],
    }
    if not upstream_changed:
        report = {
            **base_report,
            "status": "no_change",
            "validation": "passed",
            "older_partitions_unchanged": True,
            "old_source_revision": str(old_revision),
            "new_source_revision": str(old_revision),
            "source_changes": source_changes,
            "changes": {
                table: dict(empty_change) for table in ("matches", "fixtures", "tournaments")
            },
            "quality_before": _audit_quality_snapshot(root, years),
            "quality_after": _audit_quality_snapshot(root, years),
            "historical_partitions_proven_unchanged": _historical_partition_paths(
                root, min(years)
            ),
        }
        _write_audit_report(output, report)
        return report

    entity_specs = (
        (
            "matches",
            "match_id",
            years,
            ("date", "status", "format", "player1_id", "player2_id", "winner_id", "score"),
        ),
        (
            "tournaments",
            "tournament_id",
            [*years, as_of.year + 1],
            ("start_date", "end_date", "city", "country", "surface"),
        ),
        (
            "fixtures",
            "match_id",
            fixture_years,
            ("player1_id", "player2_id", "date", "round", "format", "tournament_id"),
        ),
    )
    before = {
        table: _entity_records(root, table, identifier, entity_years)
        for table, identifier, entity_years, _ in entity_specs
    }
    quality_before = _audit_quality_snapshot(root, years)
    try:
        with tempfile.TemporaryDirectory(prefix="open-tennis-audit-") as temporary_name:
            staged = Path(temporary_name) / "data"
            shutil.copytree(root, staged)
            corrections = root.parent / "contributions/corrections.parquet"
            if corrections.exists():
                staged_contributions = staged.parent / "contributions"
                staged_contributions.mkdir(parents=True)
                shutil.copy2(
                    corrections, staged_contributions / "corrections.parquet"
                )
            refresh = _refresh_years(staged, years, as_of=as_of, workers=workers)
            assembled_errors = validate_dataset(staged)
            if assembled_errors:
                raise RuntimeError(
                    "assembled dataset failed validation:\n" + "\n".join(assembled_errors)
                )
            after = {
                table: _entity_records(staged, table, identifier, entity_years)
                for table, identifier, entity_years, _ in entity_specs
            }
            changes = {
                table: _entity_changes(before[table], after[table], tracked_fields)
                for table, _, _, tracked_fields in entity_specs
            }
            semantic_change = any(
                changes[table][field]
                for table in changes
                for field in ("added", "removed", "modified")
            )
            new_sources = _audit_source_records(staged, years, fixture_years)
            old_sources = _audit_source_records(root, years, fixture_years)
            source_changes = []
            for key in sorted(old_sources.keys() | new_sources.keys()):
                old_item, new_item = old_sources.get(key, {}), new_sources.get(key, {})
                if (
                    old_item.get("revision") == new_item.get("revision")
                    and old_item.get("sha256") == new_item.get("sha256")
                ):
                    continue
                source_changes.append(
                    {
                        "source": key,
                        "old_revision": old_item.get("revision"),
                        "new_revision": new_item.get("revision"),
                        "old_checksum": old_item.get("sha256"),
                        "new_checksum": new_item.get("sha256"),
                    }
                )
            promoted = (
                promote_dataset(staged, root)
                if semantic_change
                else {"changed_files": 0, "changed_bytes": 0}
            )
            report = {
                **base_report,
                "status": "changed" if semantic_change else "no_semantic_change",
                "validation": "passed",
                "older_partitions_unchanged": True,
                "old_source_revision": str(old_revision),
                "new_source_revision": str(refresh["source_revision"]),
                "changed_files": int(promoted["changed_files"]),
                "changed_bytes": int(promoted["changed_bytes"]),
                "source_changes": source_changes,
                "changes": changes,
                "quality_before": quality_before,
                "quality_after": _audit_quality_snapshot(staged, years),
                "historical_partitions_proven_unchanged": _historical_partition_paths(
                    staged, min(years)
                ),
            }
    except Exception as exc:
        report = {
            **base_report,
            "error": str(exc),
            "source_changes": source_changes,
            "quality_before": quality_before,
        }
        _write_audit_report(output, report)
        raise
    _write_audit_report(output, report)
    return report


def _replace_parquet(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    path: Path,
    *,
    row_group_size: int,
) -> None:
    temporary = path.with_name(path.stem + ".tmp.parquet")
    _copy_parquet(
        connection,
        query,
        temporary,
        row_group_size=row_group_size,
        match_shaped=path.name in {"matches.parquet", "current.parquet"},
    )
    os.replace(temporary, path)


def _refresh_wikimedia_dataset_legacy(  # pragma: no cover - retained compatibility path
    root: Path,
    *,
    as_of: date,
    workers: int = 12,
) -> dict[str, int]:
    """Replace only current Wikimedia rows, fixtures, and affected reports."""
    root = root.resolve()
    year = as_of.year
    catalog_path = root / "catalog" / "catalog.parquet"
    if not catalog_path.exists():
        raise ValueError("refresh requires an existing dataset")
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
                row_group_size=row_group,
            )
    for tour in TOURS:
        _replace_parquet(
            connection,
            f"SELECT * FROM players WHERE tour={_quoted(tour)} ORDER BY player_id",
            root / "players" / f"tour={tour}" / "players.parquet",
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )
        _replace_parquet(
            connection,
            f"SELECT * FROM wikimedia_fixtures WHERE tour={_quoted(tour)} ORDER BY fixture_id",
            root / "fixtures" / f"tour={tour}" / "current.parquet",
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
        _replace_parquet(
            connection,
            f"SELECT source, source_match_id, row_fingerprint, match_id, event_id, tour, year, "
            f"false AS provisional FROM observations WHERE tour={_quoted(tour)} ORDER BY source, source_match_id",
            root / "identity" / "matches" / f"tour={tour}" / f"year={year}" / "match-links.parquet",
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
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
    )
    _replace_parquet(
        connection,
        "SELECT preferred_source AS source, preferred_source_player_id AS source_player_id, "
        "player_id, tour, false AS provisional FROM players ORDER BY source, source_player_id",
        root / "identity" / "player-links.parquet",
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
    )
    _replace_parquet(
        connection,
        "SELECT * FROM wikimedia_conflicts ORDER BY conflict_id",
        root / "conflicts" / "conflicts.parquet",
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
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    connection.close()

    catalog_path.unlink()
    catalog_connection = duckdb.connect()
    _create_catalog(catalog_connection, root, as_of, revision)
    catalog_connection.close()
    errors = validate_dataset(root)
    if errors:
        raise RuntimeError("refreshed dataset failed validation:\n" + "\n".join(errors))
    return wikimedia


def refresh_wikimedia_dataset(
    root: Path, *, as_of: date, workers: int = 12
) -> dict[str, Any]:
    """Deprecated alias for the atomic fixture/current-result refresh."""
    return refresh_fixtures_dataset(root, as_of=as_of, workers=workers)


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
    as_of, revision = _required_row(
        connection.execute(
            f"SELECT as_of, source_revision FROM read_parquet({_quoted(generated_catalog)}) LIMIT 1"
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
    if changed == 0:
        connection.close()
        return {"changed_files": 0, "changed_bytes": 0}
    target_catalog = target / "catalog" / "catalog.parquet"
    if target_catalog.exists():
        target_catalog.unlink()
    catalog_connection = duckdb.connect()
    _create_catalog(catalog_connection, target, as_of, str(revision))
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
        "tournaments",
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
            f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM read_parquet("
            f"{_sql_list(files)}, union_by_name=true, hive_partitioning=false)"
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
        predicates.append("t.level IN (" + ",".join(_quoted(level) for level in levels) + ")")
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    output.parent.mkdir(parents=True, exist_ok=True)
    _copy_parquet(
        connection,
        f"SELECT m.* FROM matches m JOIN tournaments t USING(tournament_id, tour, year)"
        f"{where} ORDER BY m.date NULLS LAST,m.tournament_id,m.draw,m.round,m.match_id",
        output,
        row_group_size=MATCH_ROW_GROUP_SIZE,
        match_shaped=True,
    )
    return int(
        _required_row(connection.execute(f"SELECT count(*) FROM read_parquet({_quoted(output)})"))[
            0
        ]
    )


def add_correction(
    path: Path,
    *,
    entity_type: str,
    entity_id: str,
    field: str,
    corrected_value: str,
    source_url: str,
    contributor: str,
    contributed_on: date,
) -> str:
    if entity_type not in {"match", "tournament", "player"}:
        raise ValueError("entity_type must be match, tournament, or player")
    if not source_url.startswith(("https://", "http://")):
        raise ValueError("source_url must be an HTTP(S) URL")
    correction_id = (
        "correction:"
        + hashlib.sha256(
            "|".join([entity_type, entity_id, field, corrected_value, source_url]).encode()
        ).hexdigest()[:20]
    )
    connection = duckdb.connect()
    if path.exists():
        connection.execute(
            f"CREATE TABLE corrections AS SELECT * FROM read_parquet({_quoted(path)})"
        )
    else:
        connection.execute(
            """
            CREATE TABLE corrections (
              correction_id VARCHAR, entity_type VARCHAR, entity_id VARCHAR,
              field VARCHAR, corrected_value VARCHAR,
              source_url VARCHAR, contributor VARCHAR, contributed_on DATE,
              license VARCHAR, status VARCHAR
            )
            """
        )
    connection.execute("DELETE FROM corrections WHERE correction_id = ?", [correction_id])
    connection.execute(
        "INSERT INTO corrections VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CC0-1.0', 'proposed')",
        [
            correction_id,
            entity_type,
            entity_id,
            field,
            corrected_value,
            source_url,
            contributor,
            contributed_on,
        ],
    )
    temporary = path.with_suffix(".tmp.parquet")
    _copy_parquet(
        connection,
        "SELECT * FROM corrections ORDER BY correction_id",
        temporary,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, path)
    return correction_id


def _validate_legacy_dataset(  # pragma: no cover - superseded by the lean validator
    root: Path,
) -> list[str]:
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
    required_catalog_columns = {
        "path",
        "table_name",
        "tour",
        "year",
        "row_count",
        "byte_size",
        "sha256",
        "as_of",
        "source_revision",
    }
    missing_catalog_columns = sorted(required_catalog_columns - set(catalog_columns))
    if missing_catalog_columns:
        return ["catalog missing columns: " + ", ".join(missing_catalog_columns)]
    unexpected_catalog_columns = sorted(set(catalog_columns) - required_catalog_columns)
    if unexpected_catalog_columns:
        errors.append("catalog contains unexpected columns: " + ", ".join(unexpected_catalog_columns))
    if len(catalog_rows) != len({row[catalog_columns.index("path")] for row in catalog_rows}):
        errors.append("catalog contains duplicate paths")
    actual_inventory = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.parquet")
        if path != catalog
    }
    catalog_inventory = {row[catalog_columns.index("path")] for row in catalog_rows}
    for path in sorted(actual_inventory - catalog_inventory):
        errors.append(f"uncataloged parquet file: {path}")
    for path in sorted(catalog_inventory - actual_inventory):
        errors.append(f"missing catalog file: {path}")
    as_of_values = {row[catalog_columns.index("as_of")] for row in catalog_rows}
    revision_values = {row[catalog_columns.index("source_revision")] for row in catalog_rows}
    if len(as_of_values) != 1:
        errors.append("catalog contains inconsistent as_of dates")
    if len(revision_values) != 1:
        errors.append("catalog contains inconsistent source revisions")
    metadata_paths = list(root.rglob("*.parquet"))
    contributions = root.parent / "contributions"
    if contributions.is_dir():
        metadata_paths.extend(contributions.rglob("*.parquet"))
    for metadata_path in sorted(set(metadata_paths)):
        metadata_keys = {
            key.decode() if isinstance(key, bytes) else str(key)
            for (key,) in connection.execute(
                f"SELECT key FROM parquet_kv_metadata({_quoted(metadata_path)})"
            ).fetchall()
        }
        if metadata_keys:
            errors.append(
                f"unexpected key-value metadata in {metadata_path.relative_to(root.parent)}: "
                + ", ".join(sorted(metadata_keys))
            )
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
        if path.stat().st_size != int(row[positions["byte_size"]]):
            errors.append(f"catalog byte size mismatch: {path.relative_to(root)}")
        if sha256_file(path) != row[positions["sha256"]]:
            errors.append(f"checksum mismatch: {path.relative_to(root)}")
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
        actual_rows = int(
            _required_row(
                connection.execute(f"SELECT count(*) FROM read_parquet({_quoted(path)})")
            )[0]
        )
        if actual_rows != int(row[positions["row_count"]]):
            errors.append(f"catalog row count mismatch: {path.relative_to(root)}")
        schema_names = {name for name, _ in schema}
        partition_predicates: list[str] = []
        if row[positions["tour"]] is not None and "tour" in schema_names:
            partition_predicates.append(
                f"tour <> {_quoted(str(row[positions['tour']]))} OR tour IS NULL"
            )
        if row[positions["year"]] is not None and "year" in schema_names:
            partition_predicates.append(
                f"year <> {int(row[positions['year']])} OR year IS NULL"
            )
        if partition_predicates:
            misplaced = int(
                _required_row(
                    connection.execute(
                        f"SELECT count(*) FROM read_parquet({_quoted(path)}) WHERE "
                        + " OR ".join(f"({predicate})" for predicate in partition_predicates)
                    )
                )[0]
            )
            if misplaced:
                errors.append(
                    f"partition values mismatch in {path.relative_to(root)}: {misplaced}"
                )
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

    match_partitions = {
        (str(row[positions["tour"]]), int(row[positions["year"]]))
        for row in catalog_rows
        if row[positions["table_name"]] == "matches"
        and row[positions["tour"]] is not None
        and row[positions["year"]] is not None
    }
    for table_name in ("events", "observations", "match_links"):
        available = {
            (str(row[positions["tour"]]), int(row[positions["year"]]))
            for row in catalog_rows
            if row[positions["table_name"]] == table_name
            and row[positions["tour"]] is not None
            and row[positions["year"]] is not None
        }
        for tour, year in sorted(match_partitions - available):
            errors.append(f"{table_name} {tour}/{year}: missing partition")
        for tour, year in sorted(available - match_partitions):
            errors.append(f"{table_name} {tour}/{year}: partition has no matches partition")

    try:
        register_views(connection, root)
    except duckdb.Error as exc:
        errors.append(f"could not register dataset views: {exc}")
        return errors
    checks = {
        "duplicate match IDs": "SELECT count(*) - count(DISTINCT match_id) FROM matches",
        "duplicate event IDs": "SELECT count(*) - count(DISTINCT event_id) FROM events",
        "duplicate player IDs": "SELECT count(*) - count(DISTINCT player_id) FROM players",
        "orphan match events": "SELECT count(*) FROM matches m LEFT JOIN events e USING(event_id) WHERE e.event_id IS NULL",
        "orphan match players": "SELECT count(*) FROM matches m LEFT JOIN players p1 ON m.player1_id=p1.player_id LEFT JOIN players p2 ON m.player2_id=p2.player_id WHERE p1.player_id IS NULL OR p2.player_id IS NULL",
        "orphan statistics": "SELECT count(*) FROM match_stats s LEFT JOIN matches m USING(match_id, tour, year) WHERE m.match_id IS NULL",
        "orphan observations": "SELECT count(*) FROM observations o LEFT JOIN matches m USING(match_id, event_id, tour, year) WHERE m.match_id IS NULL",
        "invalid winners": "SELECT count(*) FROM matches WHERE winner_id NOT IN (player1_id, player2_id)",
        "false exact dates": "SELECT count(*) FROM matches WHERE played_on IS NULL AND played_on_precision='day'",
        "missing statistics": "SELECT CASE WHEN count(*) = 0 THEN 1 ELSE 0 END FROM match_stats",
    }
    for label, sql in checks.items():
        value = int(_required_row(connection.execute(sql))[0])
        if value:
            errors.append(f"{label}: {value}")

    def grouped_errors(label: str, sql: str) -> None:
        for tour, year, value in connection.execute(sql).fetchall():
            if int(value):
                errors.append(f"{label} {tour}/{year}: {int(value)}")

    grouped_errors(
        "matches invalid participants",
        """
        SELECT tour, year, count(*) FROM matches
        WHERE player1_id IS NULL OR player2_id IS NULL OR player1_id = player2_id
          OR winner_id = loser_id OR winner_id NOT IN (player1_id, player2_id)
          OR loser_id NOT IN (player1_id, player2_id) OR winner_id = loser_id
          OR winner_side NOT IN (1, 2)
          OR (winner_side = 1 AND winner_id <> player1_id)
          OR (winner_side = 2 AND winner_id <> player2_id)
        GROUP BY tour, year ORDER BY tour, year
        """,
    )
    grouped_errors(
        "matches invalid required values",
        """
        SELECT tour, year, count(*) FROM matches
        WHERE match_id IS NULL OR trim(match_id) = '' OR event_id IS NULL OR trim(event_id) = ''
          OR event_name IS NULL OR trim(event_name) = ''
          OR player1_name IS NULL OR trim(player1_name) = ''
          OR player2_name IS NULL OR trim(player2_name) = ''
          OR discipline <> 'singles' OR draw NOT IN ('main', 'qualifying')
          OR status NOT IN ('completed', 'walkover', 'retired', 'defaulted', 'abandoned')
          OR played_on_precision NOT IN ('day', 'event_only', 'unknown')
          OR round IS NULL OR trim(round) = '' OR round_order IS NULL OR round_order < 0
          OR source_count IS NULL OR source_count < 1
          OR first_observed_on > last_updated_on
        GROUP BY tour, year ORDER BY tour, year
        """,
    )
    grouped_errors(
        "matches invalid season dates",
        """
        SELECT tour, year, count(*) FROM matches
        WHERE event_start_date IS NOT NULL
          AND year(event_start_date) NOT IN (year - 1, year, year + 1)
        GROUP BY tour, year ORDER BY tour, year
        """,
    )
    grouped_errors(
        "events invalid values",
        """
        SELECT tour, year, count(*) FROM events
        WHERE event_id IS NULL OR trim(event_id) = '' OR event_name IS NULL OR trim(event_name) = ''
          OR discipline <> 'singles' OR draw NOT IN ('main', 'qualifying')
          OR surface IS NOT NULL AND surface NOT IN ('hard', 'clay', 'grass', 'carpet')
          OR draw_size < 0 OR event_end_date < event_start_date
        GROUP BY tour, year ORDER BY tour, year
        """,
    )
    grouped_errors(
        "statistics invalid values",
        """
        SELECT tour, year, count(*) FROM match_stats
        WHERE duration_minutes < 0 OR player1_aces < 0 OR player1_double_faults < 0
          OR player1_service_points < 0 OR player1_first_serves_in < 0
          OR player1_first_serves_won < 0 OR player1_second_serves_won < 0
          OR player1_service_games < 0 OR player1_break_points_saved < 0
          OR player1_break_points_faced < 0 OR player2_aces < 0
          OR player2_double_faults < 0 OR player2_service_points < 0
          OR player2_first_serves_in < 0 OR player2_first_serves_won < 0
          OR player2_second_serves_won < 0 OR player2_service_games < 0
          OR player2_break_points_saved < 0 OR player2_break_points_faced < 0
          OR player1_first_serves_in > player1_service_points
          OR player2_first_serves_in > player2_service_points
          OR player1_first_serves_won > player1_first_serves_in
          OR player2_first_serves_won > player2_first_serves_in
          OR player1_break_points_saved > player1_break_points_faced
          OR player2_break_points_saved > player2_break_points_faced
        GROUP BY tour, year ORDER BY tour, year
        """,
    )
    grouped_errors(
        "rankings invalid values",
        """
        SELECT tour, year, count(*) FROM rankings
        WHERE ranking_date IS NULL OR year(ranking_date) <> year OR player_id IS NULL
          OR rank IS NULL OR rank < 1 OR points < 0 OR tournaments_played < 0
        GROUP BY tour, year ORDER BY tour, year
        """,
    )
    grouped_errors(
        "duplicate rankings",
        """
        SELECT tour, year, count(*) - count(DISTINCT (ranking_date, player_id))
        FROM rankings GROUP BY tour, year ORDER BY tour, year
        """,
    )
    grouped_errors(
        "duplicate observations",
        """
        SELECT tour, year, count(*) - count(DISTINCT (source, source_match_id, row_fingerprint))
        FROM observations GROUP BY tour, year ORDER BY tour, year
        """,
    )

    coverage_mismatches = int(
        _required_row(
            connection.execute(
                f"""
                WITH expected AS (
                  SELECT m.tour, m.year, m.level, m.draw, count(*)::BIGINT AS row_count,
                    count(DISTINCT m.event_id)::BIGINT AS event_count,
                    count(m.played_on)::BIGINT AS exact_date_count,
                    count(m.score)::BIGINT AS score_count,
                    count(s.match_id)::BIGINT AS statistics_count,
                    min(m.event_start_date) AS minimum_date,
                    max(m.event_start_date) AS maximum_date
                  FROM matches m LEFT JOIN match_stats s USING(match_id, tour, year)
                  GROUP BY m.tour, m.year, m.level, m.draw
                ), published AS (
                  SELECT tour, year, level, draw, row_count, event_count, exact_date_count,
                    score_count, statistics_count, minimum_date, maximum_date
                  FROM read_parquet({_quoted(root / 'coverage/coverage.parquet')})
                  WHERE table_name = 'matches'
                )
                SELECT count(*) FROM (
                  (SELECT * FROM expected EXCEPT SELECT * FROM published)
                  UNION ALL
                  (SELECT * FROM published EXCEPT SELECT * FROM expected)
                ) differences
                """
            )
        )[0]
    )
    if coverage_mismatches:
        errors.append(f"coverage does not match canonical tables: {coverage_mismatches}")
    audits = connection.execute(
        f"SELECT source_path, source_rows, normalized_rows, quarantined_rows FROM read_parquet({_quoted(root / 'coverage/source-audit.parquet')}) WHERE kind='matches'"
    ).fetchall()
    for source_path, source_rows, normalized_rows, quarantined_rows in audits:
        if int(source_rows or 0) != int(normalized_rows or 0) + int(quarantined_rows or 0):
            errors.append(
                f"source reconciliation failed for {source_path}: {source_rows} != {normalized_rows}+{quarantined_rows}"
            )
    coverage_partitions = {
        (str(tour), int(year))
        for tour, year in connection.execute(
            f"SELECT DISTINCT tour, year FROM read_parquet({_quoted(root / 'coverage/coverage.parquet')}) WHERE table_name='matches'"
        ).fetchall()
    }
    audit_partitions = {
        (str(tour), int(year))
        for tour, year in connection.execute(
            f"SELECT DISTINCT tour, year FROM read_parquet({_quoted(root / 'coverage/source-audit.parquet')}) WHERE kind='matches'"
        ).fetchall()
    }
    for label, available in (
        ("coverage", coverage_partitions),
        ("source audit", audit_partitions),
    ):
        for tour, year in sorted(match_partitions - available):
            errors.append(f"{label} {tour}/{year}: missing rows")
        for tour, year in sorted(available - match_partitions):
            errors.append(f"{label} {tour}/{year}: rows have no matches partition")

    if match_partitions and min(year for _, year in match_partitions) == 1968:
        ranking_partitions = {
            (str(tour), int(year))
            for tour, year in connection.execute("SELECT DISTINCT tour, year FROM rankings").fetchall()
        }
        catalog_as_of = next(iter(as_of_values)) if len(as_of_values) == 1 else None
        if isinstance(catalog_as_of, date):
            expected_rankings = {
                (tour, year)
                for tour, first_year in (("atp", 1973), ("wta", 1984))
                for year in range(first_year, catalog_as_of.year + 1)
            }
            for tour, year in sorted(expected_rankings - ranking_partitions):
                errors.append(f"rankings {tour}/{year}: missing partition")

    fixture_issues = {
        "duplicate fixture IDs": "SELECT count(*) - count(DISTINCT fixture_id) FROM fixtures",
        "invalid fixtures": "SELECT count(*) FROM fixtures WHERE fixture_id IS NULL OR event_id IS NULL OR tour NOT IN ('atp','wta') OR draw NOT IN ('main','qualifying') OR status <> 'tentative' OR (scheduled_at IS NOT NULL AND scheduled_on IS NOT NULL AND CAST(scheduled_at AS DATE) <> scheduled_on)",
        "orphan fixture events": "SELECT count(*) FROM fixtures f LEFT JOIN events e USING(event_id) WHERE e.event_id IS NULL",
        "orphan fixture players": "SELECT count(*) FROM fixtures f LEFT JOIN players p1 ON f.player1_id=p1.player_id LEFT JOIN players p2 ON f.player2_id=p2.player_id WHERE (f.player1_id IS NOT NULL AND p1.player_id IS NULL) OR (f.player2_id IS NOT NULL AND p2.player_id IS NULL)",
    }
    for label, sql in fixture_issues.items():
        value = int(_required_row(connection.execute(sql))[0])
        if value:
            errors.append(f"{label}: {value}")

    health_mismatches = int(
        _required_row(
            connection.execute(
                f"""
                WITH expected AS (
                  SELECT m.tour, count(*)::BIGINT AS match_count,
                    count(DISTINCT m.event_id)::BIGINT AS event_count,
                    min(m.event_start_date) AS earliest_event_date,
                    max(m.event_start_date) AS latest_event_date,
                    (SELECT max(ranking_date) FROM rankings r WHERE r.tour=m.tour) AS latest_ranking_date,
                    (SELECT count(*) FROM rankings r WHERE r.tour=m.tour)::BIGINT AS ranking_row_count,
                    (SELECT count(*) FROM read_parquet({_quoted(root / 'quarantine/quarantine.parquet')}) q WHERE q.tour=m.tour)::BIGINT AS quarantined_rows
                  FROM matches m GROUP BY m.tour
                ), published AS (
                  SELECT tour, match_count, event_count, earliest_event_date, latest_event_date,
                    latest_ranking_date, ranking_row_count, quarantined_rows
                  FROM read_parquet({_quoted(root / 'health/health.parquet')})
                )
                SELECT count(*) FROM (
                  (SELECT * FROM expected EXCEPT SELECT * FROM published)
                  UNION ALL
                  (SELECT * FROM published EXCEPT SELECT * FROM expected)
                ) differences
                """
            )
        )[0]
    )
    if health_mismatches:
        errors.append(f"health does not match canonical tables: {health_mismatches}")
    unhealthy = connection.execute(
        f"SELECT tour, status FROM read_parquet({_quoted(root / 'health/health.parquet')}) WHERE status = 'unhealthy'"
    ).fetchall()
    errors.extend(f"health {tour}: {status}" for tour, status in unhealthy)
    connection.close()
    return errors


def validate_dataset(
    root: Path,
    *,
    baseline_catalog: Path | None = None,
    immutable_before_year: int | None = None,
) -> list[str]:
    """Validate the lean dataset and optionally enforce historical immutability."""
    errors: list[str] = []
    root = root.resolve()
    catalog = root / "catalog" / "catalog.parquet"
    if not catalog.exists():
        return ["missing catalog/catalog.parquet"]
    connection = duckdb.connect()
    try:
        rows = connection.execute(
            f"SELECT path, table_name, tour, year, row_count, byte_size, sha256, as_of, "
            f"source_revision FROM read_parquet({_quoted(catalog)}) ORDER BY path"
        ).fetchall()
    except duckdb.Error as exc:
        connection.close()
        return [f"invalid catalog: {exc}"]
    paths = [str(row[0]) for row in rows]
    if len(paths) != len(set(paths)):
        errors.append("catalog contains duplicate paths")
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.parquet")
        if path != catalog
    }
    catalog_paths = set(paths)
    for relative_path in sorted(actual_paths - catalog_paths):
        errors.append(f"uncatalogued parquet file: {relative_path}")
    for relative_path in sorted(catalog_paths - actual_paths):
        errors.append(f"missing catalog file: {relative_path}")

    schemas: dict[str, list[tuple[str, str]]] = {}
    partitions: dict[str, set[tuple[str, int]]] = {}
    for relative, table, tour, year, row_count, byte_size, checksum, _, _ in rows:
        parquet_path = root / str(relative)
        if not parquet_path.exists():
            continue
        if parquet_path.stat().st_size != int(byte_size):
            errors.append(f"catalog byte size mismatch: {relative}")
        if parquet_path.stat().st_size > MAX_PARQUET_BYTES:
            errors.append(f"file exceeds 75 MB: {relative}")
        if sha256_file(parquet_path) != checksum:
            errors.append(f"checksum mismatch: {relative}")
        schema = [
            (item[0], item[1])
            for item in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet({_quoted(parquet_path)}"
                + (
                    ", hive_partitioning=false)"
                    if table in {"matches", "fixtures"}
                    else ")"
                )
            ).fetchall()
        ]
        if table in schemas and schemas[table] != schema:
            errors.append(f"schema drift in {table}: {relative}")
        schemas.setdefault(str(table), schema)
        actual_rows = int(
            _required_row(
                connection.execute(
                    f"SELECT count(*) FROM read_parquet({_quoted(parquet_path)})"
                )
            )[0]
        )
        if actual_rows != int(row_count):
            errors.append(f"catalog row count mismatch: {relative}")
        if tour is not None and year is not None:
            partitions.setdefault(str(table), set()).add((str(tour), int(year)))
        if table in {"matches", "fixtures"}:
            metadata = {
                (key.decode() if isinstance(key, bytes) else str(key)): (
                    value.decode() if isinstance(value, bytes) else str(value)
                )
                for _, key, value in connection.execute(
                    f"SELECT * FROM parquet_kv_metadata({_quoted(parquet_path)})"
                ).fetchall()
            }
            if metadata != {SCHEMA_METADATA_KEY: SCHEMA_VERSION}:
                errors.append(f"invalid v3.2 schema metadata: {relative}")
            parquet_rows = connection.execute(
                f"SELECT compression,row_group_num_rows FROM parquet_metadata("
                f"{_quoted(parquet_path)})"
            ).fetchall()
            if any(codec != "ZSTD" for codec, _ in parquet_rows):
                errors.append(f"non-ZSTD match data: {relative}")
            if any(int(size) > MATCH_ROW_GROUP_SIZE + 2048 for _, size in parquet_rows):
                errors.append(f"oversized match row group: {relative}")
            created_by, format_version = _required_row(
                connection.execute(
                    f"SELECT created_by,format_version FROM parquet_file_metadata("
                    f"{_quoted(parquet_path)})"
                )
            )
            if not str(created_by).startswith("DuckDB version v1.5.4"):
                errors.append(f"unexpected match writer: {relative}: {created_by}")
            if int(format_version) != 2:
                errors.append(f"non-V2 match Parquet: {relative}")
            dictionary_columns = {
                str(path_name).split(".")[0]
                for path_name, encodings in connection.execute(
                    f"SELECT path_in_schema,encodings FROM parquet_metadata("
                    f"{_quoted(parquet_path)})"
                ).fetchall()
                if "RLE_DICTIONARY" in str(encodings)
            }
            if not {"tour", "draw", "format", "status"}.issubset(
                dictionary_columns
            ):
                errors.append(f"missing match dictionary encoding: {relative}")
            unsorted = int(
                _required_row(
                    connection.execute(
                        f"WITH physical AS (SELECT match_id,row_number() OVER () ordinal "
                        f"FROM read_parquet({_quoted(parquet_path)}, "
                        "hive_partitioning=false)), ordered AS (SELECT match_id,"
                        "row_number() OVER (ORDER BY date NULLS LAST,tournament_id,draw,"
                        f"round,match_id) ordinal FROM read_parquet({_quoted(parquet_path)}, "
                        "hive_partitioning=false)) SELECT count(*) FROM physical p "
                        "JOIN ordered o USING(match_id) WHERE p.ordinal<>o.ordinal"
                    )
                )[0]
            )
            if unsorted:
                errors.append(f"unstable match ordering: {relative}: {unsorted}")

    expected_names = {
        "matches": list(MATCH_COLUMNS),
        "fixtures": list(FIXTURE_COLUMNS),
        "tournaments": list(TOURNAMENT_COLUMNS),
        "observations": ["match_id", "tour", "year", "source_file_id", "source_match_id"],
    }
    for table, names in expected_names.items():
        actual = [name for name, _ in schemas.get(table, [])]
        if actual != names:
            errors.append(f"{table} schema mismatch: {actual}")
    for table in ("matches", "fixtures"):
        if schemas.get(table) != list(MATCH_SCHEMA):
            errors.append(f"{table} type mismatch: {schemas.get(table, [])}")
    match_partitions = partitions.get("matches", set())
    for table in ("tournaments", "observations"):
        missing = match_partitions - partitions.get(table, set())
        for tour, year in sorted(missing):
            errors.append(f"{table} {tour}/{year}: missing partition")

    try:
        register_views(connection, root)
    except (duckdb.Error, ValueError) as exc:
        connection.close()
        return [*errors, f"could not register dataset views: {exc}"]
    checks = {
        "duplicate match IDs": "SELECT count(*)-count(DISTINCT match_id) FROM matches",
        "duplicate tournament IDs": (
            "SELECT count(*)-count(DISTINCT tournament_id) FROM tournaments"
        ),
        "duplicate fixture IDs": "SELECT count(*)-count(DISTINCT match_id) FROM fixtures",
        "duplicate lifecycle IDs": (
            "SELECT count(*) FROM matches m JOIN fixtures f USING(match_id)"
        ),
        "invalid match participants": (
            "SELECT count(*) FROM matches WHERE player1_id IS NULL OR player2_id IS NULL "
            "OR list_has_any(player1_id,player2_id) "
            "OR winner_id NOT IN (player1_id,player2_id) "
            "OR len(player1_id)<>CASE format WHEN 'singles' THEN 1 ELSE 2 END "
            "OR len(player2_id)<>CASE format WHEN 'singles' THEN 1 ELSE 2 END "
            "OR len(player1_name)<>len(player1_id) OR len(player2_name)<>len(player2_id) "
            "OR list_unique(player1_id)<>len(player1_id) "
            "OR list_unique(player2_id)<>len(player2_id)"
        ),
        "invalid match values": (
            "SELECT count(*) FROM matches WHERE match_id IS NULL OR tournament_id IS NULL "
            "OR tour NOT IN ('atp','wta') OR draw NOT IN ('main','qualifying') "
            "OR tournament_name IS NULL OR round IS NULL OR format NOT IN ('singles','doubles') "
            "OR status NOT IN ('completed','walkover','retired','defaulted','abandoned','cancelled') "
            "OR best_of NOT IN (1,3,5) "
            "OR (status IN ('completed','walkover') AND winner_id IS NULL) "
            "OR (status='cancelled' AND (winner_id IS NOT NULL OR score IS NOT NULL)) "
            "OR trim(match_id)='' OR trim(tournament_id)='' OR trim(tournament_name)='' "
            "OR trim(round)='' OR (player1_seed IS NOT NULL AND trim(player1_seed)='') "
            "OR (player2_seed IS NOT NULL AND trim(player2_seed)='')"
        ),
        "invalid match participant text": (
            "SELECT count(*) FROM ("
            "SELECT unnest(player1_id) participant_value,'id' participant_kind FROM matches UNION ALL "
            "SELECT unnest(player2_id),'id' FROM matches UNION ALL "
            "SELECT unnest(player1_name),'name' FROM matches UNION ALL "
            "SELECT unnest(player2_name),'name' FROM matches) values_ "
            "WHERE participant_value IS NULL OR trim(participant_value)='' "
            "OR (participant_kind='name' AND "
            "regexp_matches(trim(participant_value), '^(?i:tbd|unknown|qualifier|lucky loser|"
            "winner of match( [0-9]+)?)$'))"
        ),
        "orphan match tournaments": (
            "SELECT count(*) FROM matches m LEFT JOIN tournaments t "
            "USING(tournament_id,tour,year) WHERE t.tournament_id IS NULL"
        ),
        "orphan match players": (
            "SELECT count(*) FROM (SELECT unnest(player1_id) player_id FROM matches "
            "UNION ALL SELECT unnest(player2_id) FROM matches) ids "
            "LEFT JOIN players p USING(player_id) WHERE p.player_id IS NULL"
        ),
        "orphan observations": (
            "SELECT count(*) FROM observations o LEFT JOIN (SELECT match_id,tour,year FROM matches "
            "UNION ALL SELECT match_id,tour,year FROM fixtures) m "
            "USING(match_id,tour,year) WHERE m.match_id IS NULL"
        ),
        "duplicate observations": (
            "SELECT count(*)-count(DISTINCT (match_id,source_file_id,source_match_id)) "
            "FROM observations"
        ),
        "ambiguous source mappings": (
            "SELECT count(*) FROM (SELECT source_file_id,source_match_id "
            "FROM observations GROUP BY ALL HAVING count(DISTINCT match_id)>1)"
        ),
        "orphan observation sources": (
            "SELECT count(*) FROM observations o LEFT JOIN read_parquet("
            + _quoted(root / "coverage/source-audit.parquet")
            + ") s USING(source_file_id) WHERE s.source_file_id IS NULL"
        ),
        "invalid tournaments": (
            "SELECT count(*) FROM tournaments WHERE tournament_id IS NULL "
            "OR tour NOT IN ('atp','wta') OR tournament_name IS NULL "
            "OR (surface IS NOT NULL AND surface NOT IN ('hard','clay','grass','carpet')) "
            "OR end_date < start_date"
        ),
        "invalid fixtures": (
            "SELECT count(*) FROM fixtures WHERE match_id IS NULL OR tournament_id IS NULL "
            "OR tour NOT IN ('atp','wta') OR draw NOT IN ('main','qualifying') OR round IS NULL "
            "OR format NOT IN ('singles','doubles') OR status<>'fixture' "
            "OR winner_id IS NOT NULL OR score IS NOT NULL "
            "OR (player1_id IS NULL)<>(player1_name IS NULL) "
            "OR (player2_id IS NULL)<>(player2_name IS NULL) "
            "OR (player1_id IS NULL AND player1_seed IS NOT NULL) "
            "OR (player2_id IS NULL AND player2_seed IS NOT NULL) "
            "OR (player1_id IS NOT NULL AND list_unique(player1_id)<>len(player1_id)) "
            "OR (player2_id IS NOT NULL AND list_unique(player2_id)<>len(player2_id)) "
            "OR (player1_id IS NOT NULL AND player2_id IS NOT NULL "
            "AND list_has_any(player1_id,player2_id)) "
            "OR (player1_id IS NOT NULL AND len(player1_id)<>CASE format WHEN 'singles' THEN 1 ELSE 2 END) "
            "OR (player2_id IS NOT NULL AND len(player2_id)<>CASE format WHEN 'singles' THEN 1 ELSE 2 END) "
            "OR (player1_id IS NOT NULL AND len(player1_name)<>len(player1_id)) "
            "OR (player2_id IS NOT NULL AND len(player2_name)<>len(player2_id)) "
            "OR trim(match_id)='' OR trim(tournament_id)='' OR trim(tournament_name)='' "
            "OR trim(round)='' OR (player1_seed IS NOT NULL AND trim(player1_seed)='') "
            "OR (player2_seed IS NOT NULL AND trim(player2_seed)='')"
        ),
        "invalid fixture participant text": (
            "SELECT count(*) FROM ("
            "SELECT unnest(player1_id) participant_value,'id' participant_kind FROM fixtures UNION ALL "
            "SELECT unnest(player2_id),'id' FROM fixtures UNION ALL "
            "SELECT unnest(player1_name),'name' FROM fixtures UNION ALL "
            "SELECT unnest(player2_name),'name' FROM fixtures) values_ "
            "WHERE participant_value IS NULL OR trim(participant_value)='' "
            "OR (participant_kind='name' AND "
            "regexp_matches(trim(participant_value), '^(?i:tbd|unknown|qualifier|lucky loser|"
            "winner of match( [0-9]+)?)$'))"
        ),
        "orphan fixture tournaments": (
            "SELECT count(*) FROM fixtures f LEFT JOIN tournaments t "
            "USING(tournament_id,tour,year) WHERE t.tournament_id IS NULL"
        ),
        "orphan statistics": (
            "SELECT count(*) FROM match_stats s LEFT JOIN matches m "
            "USING(match_id,tour,year) WHERE m.match_id IS NULL"
        ),
        "tournament name drift": (
            "SELECT count(*) FROM (SELECT tournament_id,tour,year,tournament_name FROM matches "
            "UNION ALL SELECT tournament_id,tour,year,tournament_name FROM fixtures) m "
            "JOIN tournaments t USING(tournament_id,tour,year) "
            "WHERE m.tournament_name<>t.tournament_name"
        ),
        "fixtures without provenance": (
            "SELECT count(*) FROM fixtures f LEFT JOIN observations o "
            "USING(match_id,tour,year) WHERE o.match_id IS NULL"
        ),
    }
    for label, sql in checks.items():
        value = int(_required_row(connection.execute(sql))[0])
        if value:
            errors.append(f"{label}: {value}")

    def grouped_errors(label: str, sql: str) -> None:
        for tour, year, value in connection.execute(sql).fetchall():
            if int(value):
                errors.append(f"{label} {tour}/{year}: {int(value)}")

    grouped_errors(
        "matches invalid participants",
        """
        SELECT tour, year, count(*) FROM matches
        WHERE player1_id IS NULL OR player2_id IS NULL
          OR list_has_any(player1_id,player2_id)
          OR winner_id NOT IN (player1_id,player2_id)
        GROUP BY tour, year ORDER BY tour, year
        """,
    )
    grouped_errors(
        "statistics invalid values",
        """
        SELECT tour, year, count(*) FROM match_stats
        WHERE duration_minutes < 0 OR player1_aces < 0 OR player1_double_faults < 0
          OR player1_service_points < 0 OR player1_first_serves_in < 0
          OR player1_first_serves_won < 0 OR player1_second_serves_won < 0
          OR player1_service_games < 0 OR player1_break_points_saved < 0
          OR player1_break_points_faced < 0 OR player2_aces < 0
          OR player2_double_faults < 0 OR player2_service_points < 0
          OR player2_first_serves_in < 0 OR player2_first_serves_won < 0
          OR player2_second_serves_won < 0 OR player2_service_games < 0
          OR player2_break_points_saved < 0 OR player2_break_points_faced < 0
          OR player1_first_serves_in > player1_service_points
          OR player2_first_serves_in > player2_service_points
          OR player1_first_serves_won > player1_first_serves_in
          OR player2_first_serves_won > player2_first_serves_in
          OR player1_break_points_saved > player1_break_points_faced
          OR player2_break_points_saved > player2_break_points_faced
        GROUP BY tour, year ORDER BY tour, year
        """,
    )

    coverage_path = root / "coverage" / "coverage.parquet"
    if coverage_path.exists():
        coverage_mismatches = int(
            _required_row(
                connection.execute(
                    f"""
                    WITH expected AS (
                      SELECT 'matches'::VARCHAR AS table_name, m.tour, m.year, t.level,
                        m.draw, count(*)::BIGINT AS row_count,
                        count(DISTINCT m.tournament_id)::BIGINT AS tournament_count,
                        count(m.score)::BIGINT AS score_count,
                        count(s.match_id)::BIGINT AS statistics_count,
                        min(t.start_date) AS minimum_date, max(t.end_date) AS maximum_date
                      FROM matches m
                      JOIN tournaments t USING(tournament_id,tour,year)
                      LEFT JOIN match_stats s USING(match_id,tour,year)
                      GROUP BY m.tour,m.year,t.level,m.draw
                    ), published AS (
                      SELECT table_name,tour,year,level,draw,row_count,tournament_count,
                        score_count,statistics_count,minimum_date,maximum_date
                      FROM read_parquet({_quoted(coverage_path)})
                      WHERE table_name='matches'
                    )
                    SELECT count(*) FROM (
                      (SELECT * FROM expected EXCEPT SELECT * FROM published)
                      UNION ALL
                      (SELECT * FROM published EXCEPT SELECT * FROM expected)
                    ) differences
                    """
                )
            )[0]
        )
        if coverage_mismatches:
            errors.append(
                f"coverage does not match canonical tables: {coverage_mismatches}"
            )

    source_audit_path = root / "coverage" / "source-audit.parquet"
    if source_audit_path.exists():
        for source_path, source_rows, normalized_rows, quarantined_rows in connection.execute(
            f"SELECT source_path, source_rows, normalized_rows, quarantined_rows "
            f"FROM read_parquet({_quoted(source_audit_path)}) WHERE kind='matches'"
        ).fetchall():
            if int(source_rows or 0) != int(normalized_rows or 0) + int(
                quarantined_rows or 0
            ):
                errors.append(
                    f"source reconciliation failed for {source_path}: "
                    f"{source_rows} != {normalized_rows}+{quarantined_rows}"
                )

    if baseline_catalog is not None:
        if not baseline_catalog.exists():
            errors.append(f"missing baseline catalog: {baseline_catalog}")
        elif immutable_before_year is None:
            errors.append("immutable_before_year is required with baseline_catalog")
        else:
            differences = connection.execute(
                f"""
                WITH old AS (
                  SELECT path, row_count, sha256 FROM read_parquet({_quoted(baseline_catalog)})
                  WHERE year < {int(immutable_before_year)}
                ), new AS (
                  SELECT path, row_count, sha256 FROM read_parquet({_quoted(catalog)})
                  WHERE year < {int(immutable_before_year)}
                )
                SELECT * FROM (
                  (SELECT * FROM old EXCEPT SELECT * FROM new)
                  UNION ALL
                  (SELECT * FROM new EXCEPT SELECT * FROM old)
                ) ORDER BY path
                """
            ).fetchall()
            for path, _, _ in differences:
                errors.append(f"immutable historical partition changed: {path}")
    connection.close()
    return errors


def format_rows(columns: Sequence[str], rows: Sequence[Sequence[Any]], output: Any = None) -> None:
    output = output or sys.stdout
    output.write("\t".join(columns) + "\n")
    for row in rows:
        output.write("\t".join("" if value is None else str(value) for value in row) + "\n")


def shell(root: Path) -> int:
    connection = duckdb.connect()
    register_views(connection, root)
    print("Open Tennis Data DuckDB shell. End statements with ';'. Use .quit to exit.")
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
