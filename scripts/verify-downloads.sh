#!/usr/bin/env bash
set -euo pipefail

directory=${1:?download directory required}
mode=${2:-all}

files=(mens.parquet womens.parquet atp.parquet wta.parquet all-matches.parquet tournaments.parquet)
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
expected_columns = (
    [
        "fixture_id", "tournament_id", "tour", "year", "draw", "round",
        "player1_id", "player1_name", "player2_id", "player2_name",
        "scheduled_on", "source_url",
    ]
    if future_only
    else [
        "match_id", "tournament_id", "tour", "year", "draw", "round",
        "player1_id", "player1_name", "player1_country", "player2_id",
        "player2_name", "player2_country", "winner_id", "loser_id",
        "player1_seed", "player2_seed", "player1_entry", "player2_entry",
        "player1_rank", "player2_rank", "player1_rank_points",
        "player2_rank_points", "status", "score", "best_of",
    ]
)
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
    metadata = connection.execute(
        f"SELECT count(*) FROM parquet_kv_metadata('{path}')"
    ).fetchone()[0]
    if metadata:
        raise SystemExit(f"unexpected Parquet metadata: {path.name}")
    if future_only:
        invalid_past = connection.execute(
            f"SELECT count(*) FROM read_parquet('{path}') "
            "WHERE scheduled_on IS NOT NULL AND scheduled_on < ?",
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
connection.close()
PY
