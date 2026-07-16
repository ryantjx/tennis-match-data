"""One-time, offline v3.1 to v3.2 dataset migration."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from open_tennis_data.dataset import (
    MATCH_ROW_GROUP_SIZE,
    OBSERVATION_ROW_GROUP_SIZE,
    _copy_parquet,
    _create_catalog,
    _quoted,
    _required_row,
    _sql_list,
    sha256_file,
    validate_dataset,
)
from open_tennis_data.schema import MATCH_COLUMNS


def _match_projection(old_path: Path, tournament_path: Path) -> str:
    return f"""
        SELECT NULL::DATE AS date, m.match_id, m.tournament_id, t.tournament_name,
          m.tour, m.year::SMALLINT AS year, m.draw, m.round, 'singles'::VARCHAR AS format,
          [m.player1_id]::VARCHAR[] AS player1_id,
          [m.player1_name]::VARCHAR[] AS player1_name, m.player1_seed,
          [m.player2_id]::VARCHAR[] AS player2_id,
          [m.player2_name]::VARCHAR[] AS player2_name, m.player2_seed,
          CASE WHEN m.winner_id IS NULL THEN NULL ELSE [m.winner_id]::VARCHAR[] END AS winner_id,
          m.status, m.score,
          coalesce(m.best_of, CASE
            WHEN m.tour='atp' AND t.level='grand_slam' AND m.draw='main' THEN 5
            ELSE 3 END)::TINYINT AS best_of
        FROM read_parquet({_quoted(old_path)}, hive_partitioning=false) m
        JOIN read_parquet({_quoted(tournament_path)}, hive_partitioning=false) t
          USING(tournament_id,tour,year)
        ORDER BY date NULLS LAST,tournament_id,draw,round,match_id
    """


def _fixture_projection(
    fixture_path: Path, tournament_paths: list[Path]
) -> str:
    return f"""
        SELECT f.scheduled_on AS date,
          regexp_replace(f.fixture_id, '^fixture-', 'match_') AS match_id,
          f.tournament_id, t.tournament_name, f.tour, f.year::SMALLINT AS year,
          f.draw, f.round, 'singles'::VARCHAR AS format,
          CASE WHEN f.player1_id IS NULL THEN NULL ELSE [f.player1_id]::VARCHAR[] END AS player1_id,
          CASE WHEN f.player1_name IS NULL THEN NULL ELSE [f.player1_name]::VARCHAR[] END AS player1_name,
          NULL::VARCHAR AS player1_seed,
          CASE WHEN f.player2_id IS NULL THEN NULL ELSE [f.player2_id]::VARCHAR[] END AS player2_id,
          CASE WHEN f.player2_name IS NULL THEN NULL ELSE [f.player2_name]::VARCHAR[] END AS player2_name,
          NULL::VARCHAR AS player2_seed, NULL::VARCHAR[] AS winner_id,
          'fixture'::VARCHAR AS status, NULL::VARCHAR AS score,
          CASE WHEN f.tour='atp' AND t.level='grand_slam' AND f.draw='main'
            THEN 5 ELSE 3 END::TINYINT AS best_of
        FROM read_parquet({_quoted(fixture_path)}, hive_partitioning=false) f
        JOIN read_parquet({_sql_list(tournament_paths)}, union_by_name=true,
                          hive_partitioning=false) t
          USING(tournament_id,tour,year)
        ORDER BY date NULLS LAST,tournament_id,draw,round,match_id
    """


def migrate_v31_to_v32(source: Path, output: Path, report_dir: Path) -> dict[str, Any]:
    """Build and validate v3.2 in ``output`` without mutating ``source``."""
    source = source.resolve()
    output = output.resolve()
    report_dir = report_dir.resolve()
    if not (source / "catalog/catalog.parquet").exists():
        raise ValueError("v3.1 source dataset requires catalog/catalog.parquet")
    if output.exists() and any(output.iterdir()):
        raise ValueError("migration output must be empty")
    shutil.copytree(source, output, dirs_exist_ok=True)
    connection = duckdb.connect()
    old_catalog = source / "catalog/catalog.parquet"
    as_of, revision = _required_row(
        connection.execute(
            f"SELECT as_of,source_revision FROM read_parquet({_quoted(old_catalog)}) LIMIT 1"
        )
    )
    if not isinstance(as_of, date):
        raise ValueError("v3.1 catalog as_of must be a date")

    partition_reports: list[dict[str, Any]] = []
    retained_differences = 0
    backfilled_best_of = 0
    old_match_rows = 0
    old_match_schema: list[tuple[str, str]] = []
    new_match_schema: list[tuple[str, str]] = []
    for old_path in sorted(source.glob("matches/tour=*/year=*/matches.parquet")):
        relative = old_path.relative_to(source)
        tournament_path = source / str(relative).replace(
            "matches/", "tournaments/"
        ).replace("matches.parquet", "tournaments.parquet")
        destination = output / relative
        old_rows, null_best_of = _required_row(
            connection.execute(
                f"SELECT count(*),count(*) FILTER(WHERE best_of IS NULL) "
                f"FROM read_parquet({_quoted(old_path)}, hive_partitioning=false)"
            )
        )
        old_match_rows += int(old_rows)
        backfilled_best_of += int(null_best_of)
        old_checksum = sha256_file(old_path)
        if not old_match_schema:
            old_match_schema = [
                (row[0], row[1])
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet({_quoted(old_path)}, "
                    "hive_partitioning=false)"
                ).fetchall()
            ]
        _copy_parquet(
            connection,
            _match_projection(old_path, tournament_path),
            destination,
            row_group_size=MATCH_ROW_GROUP_SIZE,
            match_shaped=True,
        )
        if not new_match_schema:
            new_match_schema = [
                (row[0], row[1])
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet({_quoted(destination)}, "
                    "hive_partitioning=false)"
                ).fetchall()
            ]
        difference = int(
            _required_row(
                connection.execute(
                    f"""
                    WITH old_rows AS (
                      SELECT match_id,tournament_id,tour,year::SMALLINT AS year,draw,round,
                        player1_id,player1_name,player1_seed,player2_id,player2_name,
                        player2_seed,winner_id,status,score,best_of
                      FROM read_parquet({_quoted(old_path)}, hive_partitioning=false)
                    ), new_rows AS (
                      SELECT match_id,tournament_id,tour,year,draw,round,
                        player1_id[1] player1_id,player1_name[1] player1_name,player1_seed,
                        player2_id[1] player2_id,player2_name[1] player2_name,player2_seed,
                        winner_id[1] winner_id,status,score,best_of
                      FROM read_parquet({_quoted(destination)}, hive_partitioning=false)
                    )
                    SELECT count(*) FROM (
                      (SELECT * FROM old_rows WHERE best_of IS NOT NULL
                       EXCEPT SELECT * FROM new_rows)
                      UNION ALL
                      (SELECT * FROM new_rows WHERE match_id IN
                        (SELECT match_id FROM old_rows WHERE best_of IS NOT NULL)
                       EXCEPT SELECT * FROM old_rows)
                    ) differences
                    """
                )
            )[0]
        )
        retained_differences += difference
        partition_reports.append(
            {
                "path": relative.as_posix(),
                "rows": int(old_rows),
                "old_sha256": old_checksum,
                "new_sha256": sha256_file(destination),
                "retained_differences": difference,
            }
        )

    fixture_rows = 0
    old_fixtures: list[tuple[str, str, int, str, str]] = []
    old_fixture_schema: list[tuple[str, str]] = []
    new_fixture_schema: list[tuple[str, str]] = []
    tournament_paths = sorted(source.glob("tournaments/tour=*/year=*/tournaments.parquet"))
    for old_path in sorted(source.glob("fixtures/tour=*/current.parquet")):
        relative = old_path.relative_to(source)
        destination = output / relative
        if not old_fixture_schema:
            old_fixture_schema = [
                (row[0], row[1])
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet({_quoted(old_path)}, "
                    "hive_partitioning=false)"
                ).fetchall()
            ]
        old_fixtures.extend(
            (
                str(row[0]),
                str(row[1]),
                int(row[2]),
                str(row[3]),
                str(row[4]),
            )
            for row in connection.execute(
                f"SELECT fixture_id,tour,year,source_url,tournament_id "
                f"FROM read_parquet({_quoted(old_path)}, hive_partitioning=false)"
            ).fetchall()
        )
        count = int(
            _required_row(
                connection.execute(
                    f"SELECT count(*) FROM read_parquet({_quoted(old_path)}, "
                    "hive_partitioning=false)"
                )
            )[0]
        )
        fixture_rows += count
        _copy_parquet(
            connection,
            _fixture_projection(old_path, tournament_paths),
            destination,
            row_group_size=MATCH_ROW_GROUP_SIZE,
            match_shaped=True,
        )
        if not new_fixture_schema:
            new_fixture_schema = [
                (row[0], row[1])
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet({_quoted(destination)}, "
                    "hive_partitioning=false)"
                ).fetchall()
            ]

    source_audit = source / "coverage/source-audit.parquet"
    observation_paths = sorted(
        source.glob("observations/tour=*/year=*/observations.parquet")
    )
    connection.execute(
        f"CREATE TABLE migrated_observations AS SELECT * FROM read_parquet("
        f"{_sql_list(observation_paths)}, union_by_name=true, hive_partitioning=false)"
    )
    ambiguous_source_rows = int(
        _required_row(
            connection.execute(
                "SELECT count(*) FROM migrated_observations WHERE "
                "(source_file_id,source_match_id) IN (SELECT source_file_id,source_match_id "
                "FROM migrated_observations GROUP BY ALL HAVING count(DISTINCT match_id)>1)"
            )
        )[0]
    )
    connection.execute(
        "DELETE FROM migrated_observations WHERE (source_file_id,source_match_id) IN "
        "(SELECT source_file_id,source_match_id FROM migrated_observations "
        "GROUP BY ALL HAVING count(DISTINCT match_id)>1)"
    )
    fixture_observations: list[tuple[str, str, int, str, str]] = []
    for fixture_id, tour, year, source_url, _ in old_fixtures:
        source_rows = connection.execute(
            f"SELECT source_file_id FROM read_parquet({_quoted(source_audit)}) "
            "WHERE kind='fixtures' AND source_url=? ORDER BY source_file_id LIMIT 1",
            [source_url],
        ).fetchall()
        if not source_rows:
            raise RuntimeError(f"fixture has no source-file mapping: {fixture_id}")
        fixture_observations.append(
            (
                fixture_id.replace("fixture-", "match_", 1),
                tour,
                year,
                str(source_rows[0][0]),
                fixture_id,
            )
        )
    if fixture_observations:
        connection.executemany(
            "INSERT INTO migrated_observations VALUES (?,?,?,?,?)", fixture_observations
        )
    remaining_ambiguity = int(
        _required_row(
            connection.execute(
                "SELECT count(*) FROM (SELECT source_file_id,source_match_id FROM "
                "migrated_observations GROUP BY ALL HAVING count(DISTINCT match_id)>1)"
            )
        )[0]
    )
    if remaining_ambiguity:
        raise RuntimeError("fixture provenance collided with an established source mapping")
    for tour, year in connection.execute(
        "SELECT DISTINCT tour,year FROM migrated_observations ORDER BY tour,year"
    ).fetchall():
        path = output / "observations" / f"tour={tour}" / f"year={year}" / "observations.parquet"
        old_path = source / path.relative_to(output)
        semantic_differences = int(
            _required_row(
                connection.execute(
                    f"""
                    SELECT count(*) FROM (
                      (SELECT * FROM read_parquet({_quoted(old_path)}, hive_partitioning=false)
                       EXCEPT SELECT * FROM migrated_observations
                       WHERE tour={_quoted(str(tour))} AND year={int(year)})
                      UNION ALL
                      (SELECT * FROM migrated_observations
                       WHERE tour={_quoted(str(tour))} AND year={int(year)}
                       EXCEPT SELECT * FROM read_parquet({_quoted(old_path)},
                                                        hive_partitioning=false))
                    ) differences
                    """
                )
            )[0]
        )
        if not semantic_differences:
            continue
        _copy_parquet(
            connection,
            f"SELECT * FROM migrated_observations WHERE tour={_quoted(str(tour))} "
            f"AND year={int(year)} "
            "ORDER BY tour,year,source_file_id,source_match_id,match_id",
            path,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )

    catalog_path = output / "catalog/catalog.parquet"
    catalog_path.unlink()
    _create_catalog(connection, output, as_of, str(revision))
    errors = validate_dataset(output)
    if retained_differences:
        errors.append(f"retained-field migration differences: {retained_differences}")
    report: dict[str, Any] = {
        "schema_version": "3.2",
        "as_of": as_of.isoformat(),
        "status": "passed" if not errors else "failed",
        "old_match_rows": old_match_rows,
        "new_match_rows": sum(item["rows"] for item in partition_reports),
        "fixture_rows": fixture_rows,
        "backfilled_best_of": backfilled_best_of,
        "quarantined_ambiguous_source_rows": ambiguous_source_rows,
        "retained_differences": retained_differences,
        "preserved_completed_match_ids": old_match_rows,
        "fixture_id_mappings": [
            {
                "fixture_id": fixture_id,
                "match_id": fixture_id.replace("fixture-", "match_", 1),
            }
            for fixture_id, _, _, _, _ in old_fixtures
        ],
        "match_columns": list(MATCH_COLUMNS),
        "old_match_schema": old_match_schema,
        "new_match_schema": new_match_schema,
        "old_fixture_schema": old_fixture_schema,
        "new_fixture_schema": new_fixture_schema,
        "old_catalog_sha256": sha256_file(old_catalog),
        "new_catalog_sha256": sha256_file(catalog_path),
        "partitions": partition_reports,
        "validation_errors": errors,
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "migration-v3.2.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# Open Tennis Data v3.2 migration",
        "",
        f"- Status: {report['status']}",
        f"- As of: {report['as_of']}",
        f"- Match rows retained: {old_match_rows}",
        f"- Fixture rows migrated: {fixture_rows}",
        f"- Missing `best_of` values backfilled: {backfilled_best_of}",
        f"- Ambiguous provenance rows quarantined: {ambiguous_source_rows}",
        f"- Retained-field differences: {retained_differences}",
        f"- Completed match IDs preserved: {old_match_rows}",
        f"- Rewritten match partitions: {len(partition_reports)}",
        f"- Old catalog checksum: `{report['old_catalog_sha256']}`",
        f"- New catalog checksum: `{report['new_catalog_sha256']}`",
        "",
        "## Schemas",
        "",
        f"- Old completed schema: `{old_match_schema}`",
        f"- New completed schema: `{new_match_schema}`",
        f"- Old fixture schema: `{old_fixture_schema}`",
        f"- New fixture schema: `{new_fixture_schema}`",
        "",
    ]
    if errors:
        lines.extend(("## Validation errors", "", *[f"- {error}" for error in errors], ""))
    (report_dir / "migration-v3.2.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    connection.close()
    if errors:
        raise RuntimeError("v3.2 migration failed validation:\n" + "\n".join(errors))
    return report
