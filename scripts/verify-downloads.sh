#!/usr/bin/env bash
set -euo pipefail

directory=${1:?download directory required}
mode=${2:-all}

manifest="$(cd "$(dirname "$0")" && pwd)/release-assets.txt"
files=()
while IFS= read -r filename; do files+=("$filename"); done < "$manifest"
test "${#files[@]}" -eq 9
for filename in "${files[@]}"; do
  test -f "$directory/$filename"
done
cmp "$directory/atp.parquet" "$directory/mens.parquet"
cmp "$directory/wta.parquet" "$directory/womens.parquet"

"${PYTHON:-python3}" - "$directory" "$mode" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

directory = Path(sys.argv[1]).resolve()
future_only = sys.argv[2] == "future"
connection = duckdb.connect()
as_of = connection.execute(
    "SELECT as_of FROM read_parquet('data/catalog/catalog.parquet') LIMIT 1"
).fetchone()[0]
expected_schema = None
expected_columns = [
    "date", "match_id", "tournament_id", "tournament_name", "tour", "year",
    "draw", "round", "format", "player1_id", "player1_name", "player1_seed",
    "player2_id", "player2_name", "player2_seed", "winner_id", "status",
    "score", "best_of",
]
match_paths = [directory / name for name in (
    "mens.parquet", "womens.parquet", "atp.parquet", "wta.parquet", "all-matches.parquet"
)]
for path in match_paths:
    if path.stat().st_size > 75 * 1024 * 1024:
        raise SystemExit(f"download exceeds 75 MB: {path.name}")
    schema = connection.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    if [row[0] for row in schema] != expected_columns:
        raise SystemExit(f"unexpected release columns: {path.name}")
    typed_schema = [(row[0], row[1]) for row in schema]
    if expected_schema is None:
        expected_schema = typed_schema
    elif typed_schema != expected_schema:
        raise SystemExit(f"download schema drift: {path.name}")
    metadata = {
        key.decode(): value.decode()
        for _, key, value in connection.execute(
            f"SELECT * FROM parquet_kv_metadata('{path}')"
        ).fetchall()
    }
    if metadata != {"open_tennis_data_schema_version": "3.2"}:
        raise SystemExit(f"unexpected Parquet metadata: {path.name}: {metadata}")
    if future_only:
        invalid_past = connection.execute(
            f"SELECT count(*) FROM read_parquet('{path}') "
            "WHERE date IS NOT NULL AND date < ?",
            [as_of],
        ).fetchone()[0]
        if invalid_past:
            raise SystemExit(f"past fixtures in {path.name}: {invalid_past}")
    else:
        undated = connection.execute(
            f"SELECT count(*) FROM read_parquet('{path}') WHERE date IS NULL"
        ).fetchone()[0]
        if undated:
            raise SystemExit(f"completed matches without dates in {path.name}: {undated}")
tournaments = directory / "tournaments.parquet"
if tournaments.stat().st_size > 75 * 1024 * 1024:
    raise SystemExit("download exceeds 75 MB: tournaments.parquet")
if connection.execute(
    f"SELECT count(*) FROM parquet_kv_metadata('{tournaments}')"
).fetchone()[0]:
    raise SystemExit("unexpected Parquet metadata: tournaments.parquet")
if [
    row[0]
    for row in connection.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{tournaments}')"
    ).fetchall()
] != [
    "tournament_id", "tour", "year", "tournament_name", "level", "surface",
    "indoor", "start_date", "end_date", "city", "country", "source_url",
]:
    raise SystemExit("unexpected tournaments.parquet schema")
if [row[0] for row in connection.execute(
    f"DESCRIBE SELECT * FROM read_parquet('{directory / 'provenance.parquet'}')"
).fetchall()] != ["match_id", "tour", "year", "source_file_id", "source_match_id"]:
    raise SystemExit("unexpected provenance.parquet schema")
if [(row[0], row[1]) for row in connection.execute(
    f"DESCRIBE SELECT * FROM read_parquet('{directory / 'ambiguities.parquet'}')"
).fetchall()] != [
    ("tour", "VARCHAR"), ("year", "SMALLINT"), ("source_file_id", "VARCHAR"),
    ("source_match_id", "VARCHAR"), ("candidate_match_ids", "VARCHAR[]"),
    ("reason", "VARCHAR"),
]:
    raise SystemExit("unexpected ambiguities.parquet schema")
source_columns = [row[0] for row in connection.execute(
    f"DESCRIBE SELECT * FROM read_parquet('{directory / 'sources.parquet'}')"
).fetchall()]
for required in ("source_file_id", "source_url", "revision", "sha256", "license"):
    if required not in source_columns:
        raise SystemExit(f"sources.parquet is missing {required}")
records = directory / "all-matches.parquet"
provenance = directory / "provenance.parquet"
ambiguities = directory / "ambiguities.parquet"
sources = directory / "sources.parquet"
duplicate_sources = connection.execute(
    f"SELECT count(*)-count(DISTINCT source_file_id) FROM read_parquet('{sources}')"
).fetchone()[0]
if duplicate_sources:
    raise SystemExit(f"sources contains {duplicate_sources} duplicate source IDs")
missing_matches = connection.execute(
    f"SELECT count(*) FROM read_parquet('{provenance}') p ANTI JOIN "
    f"read_parquet('{records}') m USING(match_id,tour,year)"
).fetchone()[0]
if missing_matches:
    raise SystemExit(f"provenance references {missing_matches} missing matches")
missing_sources = connection.execute(
    f"SELECT count(*) FROM read_parquet('{provenance}') p ANTI JOIN "
    f"read_parquet('{sources}') s USING(source_file_id)"
).fetchone()[0]
if missing_sources:
    raise SystemExit(f"provenance references {missing_sources} missing sources")
invalid_ambiguities = connection.execute(
    f"SELECT count(*) FROM read_parquet('{ambiguities}') "
    "WHERE reason<>'ambiguous_source_mapping' OR candidate_match_ids IS NULL "
    "OR len(candidate_match_ids)=0"
).fetchone()[0]
if invalid_ambiguities:
    raise SystemExit(f"ambiguities contains {invalid_ambiguities} invalid rows")
missing_ambiguity_matches = connection.execute(
    f"SELECT count(*) FROM (SELECT tour,year,unnest(candidate_match_ids) match_id "
    f"FROM read_parquet('{ambiguities}')) a ANTI JOIN read_parquet('{records}') m "
    "USING(match_id,tour,year)"
).fetchone()[0]
if missing_ambiguity_matches:
    raise SystemExit(f"ambiguities references {missing_ambiguity_matches} missing matches")
missing_ambiguity_sources = connection.execute(
    f"SELECT count(*) FROM read_parquet('{ambiguities}') a ANTI JOIN "
    f"read_parquet('{sources}') s USING(source_file_id)"
).fetchone()[0]
if missing_ambiguity_sources:
    raise SystemExit(f"ambiguities references {missing_ambiguity_sources} missing sources")
matches_without_evidence = connection.execute(
    f"WITH ambiguity_candidates AS (SELECT tour,year,unnest(candidate_match_ids) match_id "
    f"FROM read_parquet('{ambiguities}')) SELECT count(*) FROM read_parquet('{records}') m "
    f"WHERE NOT EXISTS (SELECT 1 FROM read_parquet('{provenance}') p "
    "WHERE (p.match_id,p.tour,p.year)=(m.match_id,m.tour,m.year)) "
    "AND NOT EXISTS (SELECT 1 FROM ambiguity_candidates a "
    "WHERE (a.match_id,a.tour,a.year)=(m.match_id,m.tour,m.year))"
).fetchone()[0]
if matches_without_evidence:
    raise SystemExit(f"release contains {matches_without_evidence} matches without evidence")
if not future_only:
    without_exact_date_evidence = connection.execute(
        f"SELECT count(*) FROM read_parquet('{records}') m WHERE NOT EXISTS ("
        f"SELECT 1 FROM read_parquet('{provenance}') p JOIN read_parquet('{sources}') s "
        "USING(source_file_id) WHERE (p.match_id,p.tour,p.year)=(m.match_id,m.tour,m.year) "
        "AND s.kind='match_dates')"
    ).fetchone()[0]
    if without_exact_date_evidence:
        raise SystemExit(
            f"completed release contains {without_exact_date_evidence} rows without exact-date evidence"
        )
    nonterminal = connection.execute(
        f"SELECT count(*) FROM read_parquet('{records}') WHERE status NOT IN "
        "('completed','walkover','retired','defaulted','abandoned')"
    ).fetchone()[0]
    if nonterminal:
        raise SystemExit(f"completed release contains {nonterminal} nonterminal rows")
unused_sources = connection.execute(
    f"SELECT count(*) FROM read_parquet('{sources}') s ANTI JOIN "
    f"(SELECT source_file_id FROM read_parquet('{provenance}') UNION "
    f"SELECT source_file_id FROM read_parquet('{ambiguities}')) p "
    "USING(source_file_id)"
).fetchone()[0]
if unused_sources:
    raise SystemExit(f"sources contains {unused_sources} unreferenced rows")
tournament_mismatch = connection.execute(
    f"WITH referenced AS (SELECT DISTINCT tournament_id,tour,year FROM read_parquet('{records}')), "
    f"released AS (SELECT tournament_id,tour,year FROM read_parquet('{tournaments}')) "
    "SELECT (SELECT count(*) FROM (TABLE referenced EXCEPT TABLE released)) + "
    "(SELECT count(*) FROM (TABLE released EXCEPT TABLE referenced))"
).fetchone()[0]
if tournament_mismatch:
    raise SystemExit(f"tournaments asset differs from referenced editions: {tournament_mismatch}")
connection.close()
PY
