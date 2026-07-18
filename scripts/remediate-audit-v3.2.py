#!/usr/bin/env python3
"""Atomically remediate the v3.2 date and source-identity audit findings."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from open_tennis_data.dataset import (
    MATCH_ROW_GROUP_SIZE,
    OBSERVATION_ROW_GROUP_SIZE,
    _create_catalog,
    _rebuild_health,
    _replace_parquet,
    _required_row,
    _source_file_id_expression,
    sha256_file,
    validate_dataset,
)
from open_tennis_data.sources.wikimedia import fetch_pages_at_revisions, parse_tournament_page


def _sql_list(paths: list[Path]) -> str:
    return "[" + ",".join("'" + str(path).replace("'", "''") + "'" for path in paths) + "]"


def _quoted(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _catalog_context(root: Path) -> tuple[date, str]:
    connection = duckdb.connect()
    result = _required_row(
        connection.execute(
            f"SELECT as_of,source_revision FROM read_parquet("
            f"{_quoted(root / 'catalog/catalog.parquet')}) LIMIT 1"
        )
    )
    connection.close()
    if not isinstance(result[0], date):
        raise RuntimeError("catalog as_of is not a date")
    return result[0], str(result[1])


def _quality(root: Path) -> dict[str, Any]:
    connection = duckdb.connect()
    matches = sorted(root.glob("matches/tour=*/year=*/matches.parquet"))
    observations = sorted(root.glob("observations/tour=*/year=*/observations.parquet"))
    sources = root / "coverage/source-audit.parquet"
    quarantine = root / "quarantine/quarantine.parquet"
    match_rows, null_dates = _required_row(
        connection.execute(
            f"SELECT count(*),count(*) FILTER(WHERE date IS NULL) FROM read_parquet("
            f"{_sql_list(matches)},union_by_name=true,hive_partitioning=false)"
        )
    )
    observation_rows = _required_row(
        connection.execute(
            f"SELECT count(*) FROM read_parquet({_sql_list(observations)},"
            "union_by_name=true,hive_partitioning=false)"
        )
    )[0]
    source_rows, distinct_source_ids = _required_row(
        connection.execute(
            f"SELECT count(*),count(DISTINCT source_file_id) FROM read_parquet({_quoted(sources)})"
        )
    )
    quarantine_rows, ambiguity_rows = _required_row(
        connection.execute(
            f"SELECT count(*),count(*) FILTER(WHERE reason='ambiguous_source_mapping') "
            f"FROM read_parquet({_quoted(quarantine)})"
        )
    )
    matches_without_direct_provenance = _required_row(
        connection.execute(
            f"WITH matches AS (SELECT * FROM read_parquet({_sql_list(matches)},"
            "union_by_name=true,hive_partitioning=false)), observations AS (SELECT * FROM "
            f"read_parquet({_sql_list(observations)},union_by_name=true,hive_partitioning=false)) "
            "SELECT count(*) FROM matches m WHERE NOT EXISTS (SELECT 1 FROM observations o "
            "WHERE (o.match_id,o.tour,o.year)=(m.match_id,m.tour,m.year))"
        )
    )[0]
    ambiguity_candidate_matches = _required_row(
        connection.execute(
            f"SELECT count(DISTINCT candidate) FROM (SELECT unnest(candidate_match_ids) candidate "
            f"FROM read_parquet({_quoted(quarantine)}) WHERE reason='ambiguous_source_mapping')"
        )
    )[0]
    matches_without_any_evidence = _required_row(
        connection.execute(
            f"WITH matches AS (SELECT * FROM read_parquet({_sql_list(matches)},"
            "union_by_name=true,hive_partitioning=false)), observations AS (SELECT * FROM "
            f"read_parquet({_sql_list(observations)},union_by_name=true,hive_partitioning=false)), "
            f"candidates AS (SELECT unnest(candidate_match_ids) match_id FROM read_parquet("
            f"{_quoted(quarantine)}) WHERE reason='ambiguous_source_mapping') SELECT count(*) "
            "FROM matches m WHERE NOT EXISTS (SELECT 1 FROM observations o WHERE "
            "(o.match_id,o.tour,o.year)=(m.match_id,m.tour,m.year)) AND NOT EXISTS "
            "(SELECT 1 FROM candidates c WHERE c.match_id=m.match_id)"
        )
    )[0]
    health = [
        {"tour": tour, "latest_ranking_date": str(latest), "status": status}
        for tour, latest, status in connection.execute(
            f"SELECT tour,latest_ranking_date,status FROM read_parquet("
            f"{_quoted(root / 'health/health.parquet')}) ORDER BY tour"
        ).fetchall()
    ]
    connection.close()
    return {
        "match_rows": int(match_rows),
        "null_match_dates": int(null_dates),
        "observation_rows": int(observation_rows),
        "source_rows": int(source_rows),
        "distinct_source_ids": int(distinct_source_ids),
        "quarantine_rows": int(quarantine_rows),
        "ambiguous_source_rows": int(ambiguity_rows),
        "ambiguity_candidate_matches": int(ambiguity_candidate_matches),
        "matches_without_direct_provenance": int(matches_without_direct_provenance),
        "matches_without_any_evidence": int(matches_without_any_evidence),
        "health": health,
    }


def _rebuild_coverage(root: Path) -> None:
    connection = duckdb.connect()
    matches = sorted(root.glob("matches/tour=*/year=*/matches.parquet"))
    tournaments = sorted(root.glob("tournaments/tour=*/year=*/tournaments.parquet"))
    statistics = sorted(root.glob("match_stats/tour=*/year=*/match-stats.parquet"))
    _replace_parquet(
        connection,
        f"WITH matches AS (SELECT * FROM read_parquet({_sql_list(matches)},"
        "union_by_name=true,hive_partitioning=false)), tournaments AS (SELECT * FROM "
        f"read_parquet({_sql_list(tournaments)},union_by_name=true,hive_partitioning=false)), "
        f"statistics AS (SELECT * FROM read_parquet({_sql_list(statistics)},union_by_name=true)) "
        "SELECT 'matches'::VARCHAR AS table_name,m.tour,m.year,t.level,m.draw,"
        "count(*)::BIGINT AS row_count,count(DISTINCT m.tournament_id)::BIGINT AS tournament_count,"
        "count(m.score)::BIGINT AS score_count,count(s.match_id)::BIGINT AS statistics_count,"
        "min(t.start_date) AS minimum_date,max(t.end_date) AS maximum_date FROM matches m "
        "JOIN tournaments t USING(tournament_id,tour,year) LEFT JOIN statistics s "
        "USING(match_id,tour,year) GROUP BY m.tour,m.year,t.level,m.draw "
        "ORDER BY m.tour,m.year,t.level,m.draw",
        root / "coverage/coverage.parquet",
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    connection.close()


def _refresh_pinned_tournament_metadata(root: Path) -> dict[str, int]:
    """Fill missing current tournament metadata from recorded immutable revisions."""
    connection = duckdb.connect()
    tournaments = sorted(root.glob("tournaments/tour=*/year=*/tournaments.parquet"))
    rows = connection.execute(
        f"WITH tournaments AS (SELECT * FROM read_parquet({_sql_list(tournaments)},"
        "union_by_name=true,hive_partitioning=false)), pages AS (SELECT * FROM read_parquet("
        f"{_quoted(root / 'coverage/source-audit.parquet')}) WHERE kind='tournaments'), "
        f"links AS (SELECT * FROM read_parquet({_quoted(root / 'identity/tournament-sources.parquet')}) "
        "WHERE source='wikimedia') SELECT DISTINCT t.tour,t.year,t.tournament_id,"
        "p.source_path,p.source_url,p.revision FROM tournaments t JOIN links l "
        "USING(tournament_id,tour,year) JOIN pages p ON p.source_url=l.source_url "
        "AND p.tour=l.tour AND p.year=l.year WHERE t.start_date IS NULL "
        "ORDER BY t.tour,t.year,t.tournament_id,p.source_path"
    ).fetchall()
    if not rows:
        connection.close()
        return {"pages": 0, "tournament_editions": 0}
    revisions: dict[str, str] = {}
    for _, _, _, title, _, revision in rows:
        previous = revisions.setdefault(str(title), str(revision))
        if previous != str(revision):
            raise RuntimeError(f"conflicting pinned revisions for {title}")
    pages = fetch_pages_at_revisions(revisions)
    metadata: dict[tuple[str, int, str], tuple[Any, ...]] = {}
    for tour, year, tournament_id, title, source_url, _ in rows:
        page = pages.get(str(title))
        if page is None:
            raise RuntimeError(f"pinned Wikimedia page is unavailable: {title}")
        parsed = parse_tournament_page(page, str(tour), int(year))
        if parsed["start_date"] is None:
            raise RuntimeError(f"pinned Wikimedia tournament date is unparseable: {title}")
        metadata[(str(tour), int(year), str(tournament_id))] = (
            str(tour),
            int(year),
            str(tournament_id),
            parsed["start_date"],
            parsed["end_date"],
            parsed["surface"],
            parsed["city"],
            parsed["country"],
            str(source_url),
        )
    connection.execute(
        "CREATE TABLE pinned_tournament_metadata(tour VARCHAR,year SMALLINT,"
        "tournament_id VARCHAR,start_date DATE,end_date DATE,surface VARCHAR,city VARCHAR,"
        "country VARCHAR,source_url VARCHAR)"
    )
    connection.executemany(
        "INSERT INTO pinned_tournament_metadata VALUES (?,?,?,?,?,?,?,?,?)",
        list(metadata.values()),
    )
    for path in tournaments:
        tour = path.parent.parent.name.removeprefix("tour=")
        year = int(path.parent.name.removeprefix("year="))
        if not any(key[:2] == (tour, year) for key in metadata):
            continue
        _replace_parquet(
            connection,
            f"SELECT t.* REPLACE(coalesce(t.start_date,p.start_date) AS start_date,"
            "coalesce(t.end_date,p.end_date) AS end_date,coalesce(t.surface,p.surface) AS surface,"
            "coalesce(t.city,p.city) AS city,coalesce(t.country,p.country) AS country,"
            "coalesce(t.source_url,p.source_url) AS source_url) FROM read_parquet("
            f"{_quoted(path)},hive_partitioning=false) t LEFT JOIN pinned_tournament_metadata p "
            "USING(tournament_id,tour,year) ORDER BY t.tour,t.year,"
            "coalesce(t.start_date,p.start_date),t.tournament_id",
            path,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
    connection.close()
    return {"pages": len(revisions), "tournament_editions": len(metadata)}


def _migrate(staged: Path, baseline: Path) -> tuple[int, int]:
    connection = duckdb.connect()
    source_audit = staged / "coverage/source-audit.parquet"
    fixtures = sorted(staged.glob("fixtures/tour=*/current.parquet"))
    source_id = _source_file_id_expression(
        "source_label", "source_url", "revision", "sha256", "kind", "tour"
    )
    connection.execute(
        f"CREATE TABLE source_map AS SELECT source_file_id old_id,kind,tour,year,"
        f"{source_id} new_id FROM read_parquet({_quoted(source_audit)})"
    )
    connection.execute(
        f"CREATE TABLE fixture_ids AS SELECT match_id,tour,year FROM read_parquet("
        f"{_sql_list(fixtures)},union_by_name=true,hive_partitioning=false)"
    )

    retained_match_differences = 0
    for path in sorted(staged.glob("matches/tour=*/year=*/matches.parquet")):
        relative = path.relative_to(staged)
        old_path = baseline / relative
        tournament = staged / "tournaments" / relative.parent.relative_to("matches") / "tournaments.parquet"
        _replace_parquet(
            connection,
            f"SELECT m.* REPLACE(coalesce(m.date,t.start_date) AS date) FROM read_parquet("
            f"{_quoted(path)},hive_partitioning=false) m JOIN read_parquet({_quoted(tournament)},"
            "hive_partitioning=false) t USING(tournament_id,tour,year) "
            "ORDER BY date NULLS LAST,tournament_id,draw,round,match_id",
            path,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )
        retained_match_differences += int(
            _required_row(
                connection.execute(
                    f"SELECT count(*) FROM ((SELECT * EXCLUDE(date) FROM read_parquet("
                    f"{_quoted(old_path)},hive_partitioning=false) EXCEPT SELECT * EXCLUDE(date) "
                    f"FROM read_parquet({_quoted(path)},hive_partitioning=false)) UNION ALL "
                    f"(SELECT * EXCLUDE(date) FROM read_parquet({_quoted(path)},hive_partitioning=false) "
                    f"EXCEPT SELECT * EXCLUDE(date) FROM read_parquet({_quoted(old_path)},"
                    "hive_partitioning=false)))"
                )
            )[0]
        )

    retained_observation_differences = 0
    for path in sorted(staged.glob("observations/tour=*/year=*/observations.parquet")):
        relative = path.relative_to(staged)
        old_path = baseline / relative
        _replace_parquet(
            connection,
            f"SELECT o.* REPLACE(s.new_id AS source_file_id) FROM read_parquet({_quoted(path)},"
            "hive_partitioning=false) o LEFT JOIN fixture_ids f USING(match_id,tour,year) "
            "JOIN source_map s ON o.source_file_id=s.old_id AND o.tour=s.tour AND o.year=s.year "
            "AND s.kind=CASE WHEN f.match_id IS NULL THEN 'matches' ELSE 'fixtures' END "
            "ORDER BY o.tour,o.year,s.new_id,o.source_match_id,o.match_id",
            path,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )
        retained_observation_differences += int(
            _required_row(
                connection.execute(
                    f"SELECT count(*) FROM ((SELECT * EXCLUDE(source_file_id) FROM read_parquet("
                    f"{_quoted(old_path)}) EXCEPT SELECT * EXCLUDE(source_file_id) FROM read_parquet("
                    f"{_quoted(path)})) UNION ALL (SELECT * EXCLUDE(source_file_id) FROM read_parquet("
                    f"{_quoted(path)}) EXCEPT SELECT * EXCLUDE(source_file_id) FROM read_parquet("
                    f"{_quoted(old_path)})))"
                )
            )[0]
        )

    quarantine = staged / "quarantine/quarantine.parquet"
    _replace_parquet(
        connection,
        f"SELECT q.* REPLACE(s.new_id AS source_file_id) FROM read_parquet({_quoted(quarantine)}) q "
        "JOIN source_map s ON q.source_file_id=s.old_id AND q.tour=s.tour AND q.year=s.year "
        "AND s.kind='matches' ORDER BY q.tour,q.year,q.source_label,s.new_id,q.source_match_id",
        quarantine,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    _replace_parquet(
        connection,
        f"SELECT * REPLACE({source_id} AS source_file_id) FROM read_parquet("
        f"{_quoted(source_audit)}) ORDER BY kind,tour,year,source_label,source_file_id",
        source_audit,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )
    connection.close()
    return retained_match_differences, retained_observation_differences


def remediate(data: Path, report_path: Path, *, check: bool = False) -> dict[str, Any]:
    data = data.resolve()
    report_path = report_path.resolve()
    before_errors = validate_dataset(data)
    permitted = (
        "matches without dates:",
        "duplicate source file IDs:",
        "non-canonical source file IDs:",
    )
    unexpected = [error for error in before_errors if not error.startswith(permitted)]
    if unexpected:
        raise RuntimeError("baseline has unrelated validation failures:\n" + "\n".join(unexpected))
    as_of, revision = _catalog_context(data)
    before = _quality(data)
    baseline_hashes = {
        path.relative_to(data).as_posix(): sha256_file(path)
        for path in data.rglob("*.parquet")
    }
    temporary_root = Path(tempfile.mkdtemp(prefix="audit-remediation-", dir=data.parent))
    staged = temporary_root / "data"
    try:
        shutil.copytree(data, staged, copy_function=os.link)
        pinned_metadata = _refresh_pinned_tournament_metadata(staged)
        retained_matches, retained_observations = _migrate(staged, data)
        _rebuild_coverage(staged)
        _rebuild_health(staged, as_of)
        catalog = staged / "catalog/catalog.parquet"
        catalog.unlink()
        connection = duckdb.connect()
        _create_catalog(connection, staged, as_of, revision)
        connection.close()
        errors = validate_dataset(staged)
        if errors:
            raise RuntimeError("remediated dataset failed validation:\n" + "\n".join(errors))
        after = _quality(staged)
        changed = sorted(
            path.relative_to(staged).as_posix()
            for path in staged.rglob("*.parquet")
            if baseline_hashes.get(path.relative_to(staged).as_posix()) != sha256_file(path)
        )
        if check:
            if changed:
                raise RuntimeError("remediation is not a no-op:\n" + "\n".join(changed))
            return {"status": "passed", "changed_files": []}
        if after["null_match_dates"]:
            raise RuntimeError(f"{after['null_match_dates']} terminal matches remain undated")
        if after["source_rows"] != after["distinct_source_ids"]:
            raise RuntimeError("source_file_id remains non-unique")
        if retained_matches or retained_observations:
            raise RuntimeError("retained match or observation fields changed")
        report = {
            "status": "passed",
            "schema_version": "3.2",
            "baseline_revision": revision,
            "as_of": as_of.isoformat(),
            "source_file_id_components": [
                "source_label",
                "source_url",
                "revision",
                "content_sha256",
                "kind",
                "tour",
            ],
            "pinned_wikimedia_metadata": pinned_metadata,
            "before": before,
            "after": after,
            "retained_match_field_differences": retained_matches,
            "retained_observation_field_differences": retained_observations,
            "affected_match_partitions": sorted(
                path.removeprefix("matches/").removesuffix("/matches.parquet")
                for path in changed
                if path.startswith("matches/")
            ),
            "changed_data_files": changed,
            "validation_errors": [],
        }
        backup = temporary_root / "baseline-data"
        promoted: list[str] = []
        try:
            for relative in changed:
                source = staged / relative
                destination = data / relative
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
                temporary = destination.with_name(destination.name + ".audit-remediation.tmp")
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)
                promoted.append(relative)
        except BaseException:
            for relative in reversed(promoted):
                shutil.copy2(backup / relative, data / relative)
            raise
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return report
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data")
    parser.add_argument(
        "--report", default="reports/data-quality/audit-remediation-v3.2.json"
    )
    parser.add_argument("--check", action="store_true", help="require remediation to be a no-op")
    args = parser.parse_args()
    result = remediate(Path(args.data), Path(args.report), check=args.check)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
