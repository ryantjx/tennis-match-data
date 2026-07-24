#!/usr/bin/env python3
"""Atomically replace tournament fallbacks with verified match-level dates."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import duckdb

from open_tennis_data.dataset import (
    MATCH_ROW_GROUP_SIZE,
    OBSERVATION_ROW_GROUP_SIZE,
    _copy_parquet,
    _create_catalog,
    _quoted,
    _rebuild_health,
    _replace_parquet,
    promote_dataset,
    sha256_file,
    validate_dataset,
)
from open_tennis_data.exact_dates import (
    CanonicalMatch,
    DateSource,
    fetch_tennis_data_file,
    parse_tennis_data_file,
    quarantine_conflicting_dates,
    reconcile_date_rows,
)

EXACT_REASONS = (
    "invalid_exact_date_source_row",
    "unmatched_exact_date",
    "ambiguous_exact_date",
    "conflicting_exact_date",
)


def _remove_provider_duplicates(root: Path) -> list[str]:
    """Remove only byte-identical ``name 2.parquet`` provider conflict copies."""
    removed: list[str] = []
    for path in sorted(root.rglob("* [0-9].parquet")):
        stem, ordinal = path.stem.rsplit(" ", 1)
        if not ordinal.isdigit():
            continue
        canonical = path.with_name(stem + path.suffix)
        if not canonical.exists() or sha256_file(path) != sha256_file(canonical):
            raise RuntimeError(f"non-identical provider conflict requires review: {path}")
        path.unlink()
        removed.append(path.relative_to(root).as_posix())
    return removed


def _files(root: Path, table: str, filename: str) -> list[Path]:
    return sorted((root / table).glob(f"tour=*/year=*/{filename}"))


def _load_canonical(root: Path) -> list[CanonicalMatch]:
    matches = _files(root, "matches", "matches.parquet")
    tournaments = _files(root, "tournaments", "tournaments.parquet")
    connection = duckdb.connect()
    rows = connection.execute(
        f"""
        SELECT m.match_id,m.tour,m.year,m.tournament_name,t.start_date,t.end_date,
          m.round,m.player1_name[1],m.player2_name[1],m.score
        FROM read_parquet({[str(path) for path in matches]},union_by_name=true,
                          hive_partitioning=false) m
        JOIN read_parquet({[str(path) for path in tournaments]},union_by_name=true,
                          hive_partitioning=false) t USING(tournament_id,tour,year)
        WHERE (m.tour='atp' AND m.year>=2000) OR (m.tour='wta' AND m.year>=2007)
        """
    ).fetchall()
    connection.close()
    return [CanonicalMatch(*row) for row in rows]


def _fetch_source(
    temporary: Path, tour: str, year: int
) -> tuple[DateSource, list[Any], list[dict[str, Any]]]:
    local, url = fetch_tennis_data_file(
        tour, year, temporary / f"tennis-data-{tour}-{year}"
    )
    return parse_tennis_data_file(local, tour, year, url)


def _source_years(root: Path) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    for path in _files(root, "matches", "matches.parquet"):
        tour = path.parts[-3].split("=", 1)[1]
        year = int(path.parts[-2].split("=", 1)[1])
        if year >= (2000 if tour == "atp" else 2007):
            result.append((tour, year))
    return sorted(set(result))


def _write_date_observations(
    connection: duckdb.DuckDBPyConnection,
    staged: Path,
    accepted: list[Any],
    match_year: dict[str, int],
) -> None:
    existing = staged / "date_observations"
    if existing.exists():
        shutil.rmtree(existing)
    connection.execute(
        """
        CREATE TABLE restored_date_observations (
          match_id VARCHAR,tour VARCHAR,year SMALLINT,played_on DATE,
          source_file_id VARCHAR,source_match_id VARCHAR,date_precision VARCHAR,
          match_method VARCHAR,row_fingerprint VARCHAR
        )
        """
    )
    connection.executemany(
        "INSERT INTO restored_date_observations VALUES (?,?,?,?,?,?,'day',?,?)",
        [
            (
                item.match_id,
                item.row.tour,
                match_year[str(item.match_id)],
                item.row.played_on,
                item.row.source_file_id,
                item.row.source_match_id,
                item.match_method,
                item.row.row_fingerprint,
            )
            for item in accepted
        ],
    )
    for tour, year in connection.execute(
        "SELECT DISTINCT tour,year FROM restored_date_observations ORDER BY tour,year"
    ).fetchall():
        _copy_parquet(
            connection,
            f"SELECT * FROM restored_date_observations WHERE tour={_quoted(tour)} "
            f"AND year={int(year)} ORDER BY ALL",
            staged
            / "date_observations"
            / f"tour={tour}"
            / f"year={year}"
            / "date-observations.parquet",
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )


def _rewrite_matches(
    connection: duckdb.DuckDBPyConnection, staged: Path, accepted: list[Any]
) -> None:
    connection.execute("CREATE TABLE restored_dates (match_id VARCHAR,played_on DATE)")
    connection.executemany(
        "INSERT INTO restored_dates VALUES (?,?)",
        [(item.match_id, item.row.played_on) for item in accepted],
    )
    connection.execute(
        "CREATE TABLE agreed_dates AS SELECT match_id,min(played_on) played_on "
        "FROM restored_dates GROUP BY match_id HAVING count(DISTINCT played_on)=1"
    )
    for path in _files(staged, "matches", "matches.parquet"):
        _replace_parquet(
            connection,
            f"SELECT m.* REPLACE(d.played_on AS date) FROM read_parquet({_quoted(path)},"
            "hive_partitioning=false) m LEFT JOIN agreed_dates d USING(match_id) "
            "ORDER BY date NULLS LAST,tournament_id,draw,round,match_id",
            path,
            row_group_size=MATCH_ROW_GROUP_SIZE,
        )


def _rewrite_evidence_tables(
    connection: duckdb.DuckDBPyConnection,
    staged: Path,
    sources: list[DateSource],
    accepted: list[Any],
    rejected: list[dict[str, Any]],
) -> None:
    accepted_counts = Counter(item.row.source_file_id for item in accepted)
    rejected_counts = Counter(str(item["source_file_id"]) for item in rejected)
    audit = staged / "coverage/source-audit.parquet"
    connection.execute(
        f"CREATE TABLE retained_audit AS SELECT * FROM read_parquet({_quoted(audit)}) "
        "WHERE kind<>'match_dates'"
    )
    connection.execute("CREATE TABLE exact_audit AS SELECT * FROM retained_audit LIMIT 0")
    connection.executemany(
        "INSERT INTO exact_audit VALUES (?,'match_dates',?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                source.source_file_id,
                source.tour,
                source.year,
                source.source_label,
                source.source_path,
                source.source_url,
                source.revision,
                source.sha256,
                source.license,
                source.source_rows,
                accepted_counts[source.source_file_id],
                rejected_counts[source.source_file_id],
            )
            for source in sources
        ],
    )
    _replace_parquet(
        connection,
        "SELECT * FROM retained_audit UNION ALL SELECT * FROM exact_audit "
        "ORDER BY kind,tour,year,source_label,source_file_id",
        audit,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )

    quarantine = staged / "quarantine/quarantine.parquet"
    reason_sql = ",".join(_quoted(reason) for reason in EXACT_REASONS)
    connection.execute(
        f"CREATE TABLE restored_quarantine AS SELECT * FROM read_parquet("
        f"{_quoted(quarantine)}) WHERE reason NOT IN ({reason_sql})"
    )
    if rejected:
        connection.executemany(
            "INSERT INTO restored_quarantine VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    item["tour"],
                    item["year"],
                    item["source_label"],
                    item["source_path"],
                    item["source_file_id"],
                    item["source_match_id"],
                    item["row_fingerprint"],
                    item["candidate_match_ids"],
                    item["reason"],
                )
                for item in rejected
            ],
        )
    _replace_parquet(
        connection,
        "SELECT * FROM restored_quarantine "
        "ORDER BY tour,year,source_label,source_match_id,row_fingerprint",
        quarantine,
        row_group_size=MATCH_ROW_GROUP_SIZE,
    )


def _report_metrics(root: Path) -> dict[str, Any]:
    connection = duckdb.connect()
    matches = _files(root, "matches", "matches.parquet")
    observations = _files(root, "date_observations", "date-observations.parquet")
    metrics: dict[str, Any] = {
        "match_rows": connection.execute(
            f"SELECT count(*) FROM read_parquet({[str(path) for path in matches]},"
            "union_by_name=true,hive_partitioning=false)"
        ).fetchone()[0],
        "exact_match_dates": connection.execute(
            f"SELECT count(date) FROM read_parquet({[str(path) for path in matches]},"
            "union_by_name=true,hive_partitioning=false)"
        ).fetchone()[0],
    }
    metrics["null_match_dates"] = metrics["match_rows"] - metrics["exact_match_dates"]
    metrics["date_observations"] = (
        connection.execute(
            f"SELECT count(*) FROM read_parquet({[str(path) for path in observations]},"
            "union_by_name=true,hive_partitioning=false)"
        ).fetchone()[0]
        if observations
        else 0
    )
    metrics["by_tour_year"] = [
        {"tour": tour, "year": year, "rows": rows, "exact_dates": exact}
        for tour, year, rows, exact in connection.execute(
            f"SELECT tour,year,count(*),count(date) FROM read_parquet("
            f"{[str(path) for path in matches]},union_by_name=true,hive_partitioning=false) "
            "GROUP BY tour,year ORDER BY tour,year"
        ).fetchall()
    ]
    connection.close()
    return metrics


def _retained_match_field_differences(before: Path, after: Path) -> int:
    connection = duckdb.connect()
    differences = 0
    for old_path in _files(before, "matches", "matches.parquet"):
        relative = old_path.relative_to(before)
        new_path = after / relative
        differences += int(
            connection.execute(
                f"SELECT count(*) FROM ((SELECT * EXCLUDE(date) FROM read_parquet("
                f"{_quoted(old_path)},hive_partitioning=false) EXCEPT SELECT * EXCLUDE(date) "
                f"FROM read_parquet({_quoted(new_path)},hive_partitioning=false)) UNION ALL "
                f"(SELECT * EXCLUDE(date) FROM read_parquet({_quoted(new_path)},"
                f"hive_partitioning=false) EXCEPT SELECT * EXCLUDE(date) FROM read_parquet("
                f"{_quoted(old_path)},hive_partitioning=false)))"
            ).fetchone()[0]
        )
    connection.close()
    return differences


def _migration(root: Path, report_path: Path, *, check: bool) -> dict[str, Any]:
    root = root.resolve()
    removed_provider_duplicates = _remove_provider_duplicates(root)
    before = _report_metrics(root)
    baseline_hashes = {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in root.rglob("*.parquet")
    }
    canonical = _load_canonical(root)
    match_year = {item.match_id: item.year for item in canonical}
    with tempfile.TemporaryDirectory(prefix="exact-date-remediation-") as temporary_name:
        temporary = Path(temporary_name)
        source_dir = temporary / "sources"
        source_results = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(_fetch_source, source_dir, tour, year): (tour, year)
                for tour, year in _source_years(root)
            }
            for future in as_completed(futures):
                source_results.append(future.result())
        source_results.sort(key=lambda item: (item[0].tour, item[0].year))
        sources = [item[0] for item in source_results]
        rows = [row for _, parsed, _ in source_results for row in parsed]
        rejected = [row for _, _, invalid in source_results for row in invalid]
        source_by_id = {source.source_file_id: source for source in sources}
        reconciled, conflicts = quarantine_conflicting_dates(
            reconcile_date_rows(rows, canonical)
        )
        accepted = [item for item in reconciled if item.match_id is not None]
        for item in (item for item in (*reconciled, *conflicts) if item.reason):
            source = source_by_id[item.row.source_file_id]
            rejected.append(
                {
                    "tour": item.row.tour,
                    "year": item.row.source_year,
                    "source_label": source.source_label,
                    "source_path": source.source_path,
                    "source_file_id": item.row.source_file_id,
                    "source_match_id": item.row.source_match_id,
                    "row_fingerprint": item.row.row_fingerprint,
                    "candidate_match_ids": list(item.candidate_match_ids) or None,
                    "reason": item.reason,
                }
            )

        staged = temporary / "staged"
        # Real copies are intentional: hard-linking files managed by a cloud-file
        # provider can materialize conflict copies in the canonical directory.
        shutil.copytree(root, staged)
        removed_provider_duplicates.extend(_remove_provider_duplicates(staged))
        connection = duckdb.connect()
        _write_date_observations(connection, staged, accepted, match_year)
        _rewrite_matches(connection, staged, accepted)
        _rewrite_evidence_tables(connection, staged, sources, accepted, rejected)
        catalog = staged / "catalog/catalog.parquet"
        catalog_row = connection.execute(
            f"SELECT as_of,source_revision FROM read_parquet({_quoted(catalog)}) LIMIT 1"
        ).fetchone()
        _rebuild_health(staged, catalog_row[0])
        catalog.unlink()
        _create_catalog(connection, staged, catalog_row[0], str(catalog_row[1]))
        connection.close()
        errors = validate_dataset(staged)
        if errors:
            raise RuntimeError("staged exact-date migration failed:\n" + "\n".join(errors))
        after = _report_metrics(staged)
        retained_match_field_differences = _retained_match_field_differences(root, staged)
        if retained_match_field_differences:
            raise RuntimeError(
                "migration changed non-date match fields: "
                f"{retained_match_field_differences} differences"
            )
        changed = sorted(
            relative
            for relative, checksum in {
                path.relative_to(staged).as_posix(): sha256_file(path)
                for path in staged.rglob("*.parquet")
            }.items()
            if baseline_hashes.get(relative) != checksum
        )
        removed = sorted(set(baseline_hashes) - {path.relative_to(staged).as_posix() for path in staged.rglob("*.parquet")})
        if removed:
            raise RuntimeError(f"migration unexpectedly removed files: {removed}")
        report = {
            "schema_version": "3.2",
            "status": "passed",
            "source_policy": "day-precision match evidence only; no tournament fallback",
            "before": before,
            "after": after,
            "retrieved_on": str(catalog_row[0]),
            "source_files": [
                {
                    "tour": source.tour,
                    "year": source.year,
                    "url": source.source_url,
                    "sha256": source.sha256,
                    "source_file_id": source.source_file_id,
                    "source_rows": source.source_rows,
                }
                for source in sources
            ],
            "reconciliation": {
                "source_rows": sum(source.source_rows for source in sources),
                "accepted_observations": len(accepted),
                "unmatched": sum(item["reason"] == "unmatched_exact_date" for item in rejected),
                "ambiguous": sum(item["reason"] == "ambiguous_exact_date" for item in rejected),
                "conflicting": sum(item["reason"] == "conflicting_exact_date" for item in rejected),
                "invalid_source_rows": sum(
                    item["reason"] == "invalid_exact_date_source_row" for item in rejected
                ),
            },
            "regression_2024": {
                tour: {
                    "source_rows": sum(
                        source.source_rows
                        for source in sources
                        if source.tour == tour and source.year == 2024
                    ),
                    "accepted": sum(
                        item.row.tour == tour and item.row.source_year == 2024
                        for item in accepted
                    ),
                    "ambiguous": sum(
                        item["tour"] == tour
                        and item["year"] == 2024
                        and item["reason"] == "ambiguous_exact_date"
                        for item in rejected
                    ),
                }
                for tour in ("atp", "wta")
            },
            "preservation": {
                "match_rows_unchanged": before["match_rows"] == after["match_rows"],
                "intended_match_field": "date",
                "all_other_match_fields_unchanged": retained_match_field_differences == 0,
                "retained_match_field_differences": retained_match_field_differences,
            },
            "changed_data_files": changed,
            "validation_errors": errors,
        }
        for tour, values in report["regression_2024"].items():
            if values["accepted"] / values["source_rows"] < 0.95:
                raise RuntimeError(f"2024 {tour} unique reconciliation fell below 95%")
            if values["ambiguous"] / values["source_rows"] >= 0.01:
                raise RuntimeError(f"2024 {tour} ambiguity reached 1%")
        if check:
            if changed:
                raise RuntimeError(f"migration is not a no-op: {changed}")
        else:
            removed_provider_duplicates.extend(_remove_provider_duplicates(root))
            promote_dataset(staged, root)
            removed_provider_duplicates.extend(_remove_provider_duplicates(root))
            report["removed_byte_identical_provider_duplicates"] = sorted(
                set(removed_provider_duplicates)
            )
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/data-quality/exact-date-restoration-v3.2.json"),
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    report = _migration(args.data, args.report, check=args.check)
    print(
        f"exact-date remediation passed: {report['after']['exact_match_dates']} dated; "
        f"{report['after']['null_match_dates']} unresolved"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
