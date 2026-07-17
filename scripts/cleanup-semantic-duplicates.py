#!/usr/bin/env python3
"""One-time, review-only cleanup of exact semantic match duplicates."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb

from open_tennis_data.dataset import (
    MATCH_ROW_GROUP_SIZE,
    OBSERVATION_ROW_GROUP_SIZE,
    _copy_parquet,
    _create_catalog,
    _quoted,
    _rebuild_health,
    _replace_parquet,
    _sql_list,
    validate_dataset,
)


def main() -> None:
    root = Path("data").resolve()
    report_dir = Path("reports/data-quality").resolve()
    changed_on = date.fromisoformat("2026-07-17")
    matches = sorted(root.glob("matches/tour=*/year=*/matches.parquet"))
    statistics = sorted(root.glob("match_stats/tour=*/year=*/match-stats.parquet"))
    observations = sorted(root.glob("observations/tour=*/year=*/observations.parquet"))
    connection = duckdb.connect()
    connection.execute(
        f"CREATE TABLE all_matches AS SELECT * FROM read_parquet({_sql_list(matches)}, "
        "union_by_name=true,hive_partitioning=false)"
    )
    connection.execute(
        f"CREATE TABLE all_statistics AS SELECT * FROM read_parquet({_sql_list(statistics)}, "
        "union_by_name=true,hive_partitioning=false)"
    )
    connection.execute(
        f"CREATE TABLE all_observations AS SELECT * FROM read_parquet({_sql_list(observations)}, "
        "union_by_name=true,hive_partitioning=false)"
    )
    connection.execute(
        """
        CREATE TABLE exact_groups AS
        SELECT row_number() OVER (ORDER BY ids[1]) group_id,* FROM (
          SELECT m.* EXCLUDE(match_id),s.* EXCLUDE(match_id,tour,year),
            count(*) member_count,list(m.match_id ORDER BY m.match_id) ids
          FROM all_matches m LEFT JOIN all_statistics s USING(match_id,tour,year)
          GROUP BY ALL HAVING member_count>1
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE duplicate_members AS
        SELECT group_id,unnest(ids) match_id FROM exact_groups
        """
    )
    connection.execute(
        """
        CREATE TABLE member_order AS
        SELECT d.group_id,d.match_id,
          coalesce(min(o.source_file_id || '|' || o.source_match_id),
                   'zz|' || d.match_id) source_order
        FROM duplicate_members d LEFT JOIN all_observations o USING(match_id)
        GROUP BY d.group_id,d.match_id
        """
    )
    connection.execute(
        f"""
        CREATE TABLE aliases AS
        WITH resolved AS (
          SELECT group_id,match_id,
            first_value(match_id) OVER (
              PARTITION BY group_id ORDER BY source_order,match_id
            ) canonical_match_id
          FROM member_order
        )
        SELECT match_id retired_match_id,canonical_match_id,
          'semantic_duplicate'::VARCHAR reason,DATE {_quoted(changed_on.isoformat())} changed_on
        FROM resolved WHERE match_id<>canonical_match_id
        ORDER BY retired_match_id
        """
    )
    groups, removed = connection.execute(
        "SELECT count(*),sum(member_count-1) FROM exact_groups"
    ).fetchone()
    if (groups, removed) != (54, 54):
        raise RuntimeError(f"expected 54 exact groups/rows, found {groups}/{removed}")
    observation_count = connection.execute("SELECT count(*) FROM all_observations").fetchone()[0]
    affected = connection.execute(
        "SELECT DISTINCT m.tour,m.year FROM all_matches m JOIN aliases a "
        "ON m.match_id=a.retired_match_id ORDER BY ALL"
    ).fetchall()

    alias_path = root / "identity/match-aliases.parquet"
    _copy_parquet(
        connection,
        "SELECT * FROM aliases ORDER BY retired_match_id",
        alias_path,
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
    )
    for tour, year in affected:
        match_path = root / "matches" / f"tour={tour}" / f"year={year}" / "matches.parquet"
        _replace_parquet(
            connection,
            f"SELECT m.* FROM read_parquet({_quoted(match_path)},hive_partitioning=false) m "
            "ANTI JOIN aliases a ON m.match_id=a.retired_match_id "
            "ORDER BY date NULLS LAST,tournament_id,draw,round,match_id",
            match_path,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
        observation_path = (
            root / "observations" / f"tour={tour}" / f"year={year}" / "observations.parquet"
        )
        _replace_parquet(
            connection,
            f"SELECT o.* REPLACE(coalesce(a.canonical_match_id,o.match_id) AS match_id) "
            f"FROM read_parquet({_quoted(observation_path)},hive_partitioning=false) o "
            "LEFT JOIN aliases a ON o.match_id=a.retired_match_id "
            "ORDER BY tour,year,source_file_id,source_match_id,match_id",
            observation_path,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )
        statistic_path = (
            root / "match_stats" / f"tour={tour}" / f"year={year}" / "match-stats.parquet"
        )
        if statistic_path.exists():
            _replace_parquet(
                connection,
                f"SELECT s.* FROM read_parquet({_quoted(statistic_path)},hive_partitioning=false) s "
                "ANTI JOIN aliases a ON s.match_id=a.retired_match_id ORDER BY tour,year,match_id",
                statistic_path,
                row_group_size=OBSERVATION_ROW_GROUP_SIZE,
            )

    match_files = sorted(root.glob("matches/tour=*/year=*/matches.parquet"))
    tournament_files = sorted(root.glob("tournaments/tour=*/year=*/tournaments.parquet"))
    statistic_files = sorted(root.glob("match_stats/tour=*/year=*/match-stats.parquet"))
    _replace_parquet(
        connection,
        f"""
        WITH matches AS (SELECT * FROM read_parquet({_sql_list(match_files)},union_by_name=true)),
        tournaments AS (SELECT * FROM read_parquet({_sql_list(tournament_files)},union_by_name=true)),
        statistics AS (SELECT * FROM read_parquet({_sql_list(statistic_files)},union_by_name=true))
        SELECT 'matches'::VARCHAR table_name,m.tour,m.year,t.level,m.draw,
          count(*)::BIGINT row_count,count(DISTINCT m.tournament_id)::BIGINT tournament_count,
          count(m.score)::BIGINT score_count,count(s.match_id)::BIGINT statistics_count,
          min(t.start_date) minimum_date,max(t.end_date) maximum_date
        FROM matches m JOIN tournaments t USING(tournament_id,tour,year)
        LEFT JOIN statistics s USING(match_id,tour,year)
        GROUP BY m.tour,m.year,t.level,m.draw ORDER BY ALL
        """,
        root / "coverage/coverage.parquet",
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    quarantine_path = root / "quarantine/quarantine.parquet"
    _replace_parquet(
        connection,
        f"SELECT q.* REPLACE(CASE WHEN candidate_match_ids IS NULL THEN NULL "
        "ELSE list_transform(candidate_match_ids,id->coalesce(a.alias_map[id],id)) END "
        "AS candidate_match_ids) "
        f"FROM read_parquet({_quoted(quarantine_path)}) q CROSS JOIN ("
        "SELECT map(list(retired_match_id),list(canonical_match_id)) alias_map FROM aliases) a "
        "ORDER BY tour,year,source_label,source_match_id,row_fingerprint NULLS LAST",
        quarantine_path,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    as_of, revision = connection.execute(
        f"SELECT as_of,source_revision FROM read_parquet("
        f"{_quoted(root / 'catalog/catalog.parquet')}) LIMIT 1"
    ).fetchone()
    _rebuild_health(root, as_of)
    catalog = root / "catalog/catalog.parquet"
    catalog.unlink()
    _create_catalog(connection, root, as_of, revision)

    remaining_observations = connection.execute(
        f"SELECT count(*) FROM read_parquet({_sql_list(sorted(root.glob('observations/tour=*/year=*/observations.parquet')))},"
        "union_by_name=true,hive_partitioning=false)"
    ).fetchone()[0]
    if remaining_observations != observation_count:
        raise RuntimeError("observation accounting changed")
    errors = validate_dataset(root)
    if errors:
        raise RuntimeError("cleanup validation failed:\n" + "\n".join(errors))

    variants = connection.execute(
        f"""
        SELECT tournament_id,tour,year,draw,round,
          least(player1_id[1],player2_id[1]) participant1_id,
          greatest(player1_id[1],player2_id[1]) participant2_id,
          list(match_id ORDER BY match_id) match_ids
        FROM read_parquet({_sql_list(match_files)},union_by_name=true,hive_partitioning=false)
        GROUP BY ALL HAVING count(*)>1 ORDER BY tour,year,tournament_id,draw,round
        """
    ).fetchall()
    if len(variants) != 24:
        raise RuntimeError(f"expected 24 review groups, found {len(variants)}")
    report = {
        "status": "passed",
        "changed_on": changed_on.isoformat(),
        "exact_duplicate_groups": groups,
        "removed_match_rows": removed,
        "preserved_surviving_ids": groups,
        "observation_rows_before": observation_count,
        "observation_rows_after": remaining_observations,
        "affected_partitions": [f"{tour}/{year}" for tour, year in affected],
        "review_only_variant_groups": [
            {
                "tournament_id": row[0], "tour": row[1], "year": row[2],
                "draw": row[3], "round": row[4], "participant1_id": row[5],
                "participant2_id": row[6], "match_ids": row[7],
            }
            for row in variants
        ],
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "semantic-dedup-v3.2.json").write_text(
        json.dumps(report,indent=2,sort_keys=True)+"\n",encoding="utf-8"
    )
    lines = [
        "# Semantic duplicate cleanup review", "",
        f"- Status: {report['status']}",
        f"- Exact duplicate groups: {groups}",
        f"- Retired match rows: {removed}",
        f"- Observation rows preserved: {observation_count}",
        f"- Non-identical review groups left unchanged: {len(variants)}", "",
        "The remaining groups share participants, tournament, draw, and round, but differ in",
        "published match facts or available statistics. They are retained for human review.", "",
    ]
    (report_dir / "semantic-dedup-v3.2.md").write_text("\n".join(lines),encoding="utf-8")
    connection.close()


if __name__ == "__main__":
    main()
