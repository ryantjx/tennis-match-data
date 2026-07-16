#!/usr/bin/env bash
set -euo pipefail

directory=${1:?download directory required}
mode=${2:-all}

files=(mens.parquet womens.parquet atp.parquet wta.parquet all-matches.parquet tournaments.parquet provenance.parquet sources.parquet)
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
source_columns = [row[0] for row in connection.execute(
    f"DESCRIBE SELECT * FROM read_parquet('{directory / 'sources.parquet'}')"
).fetchall()]
for required in ("source_file_id", "source_url", "revision", "sha256", "license"):
    if required not in source_columns:
        raise SystemExit(f"sources.parquet is missing {required}")
connection.close()
PY
