"""Open Tennis Data v3 release generation and remote access."""

from __future__ import annotations

import csv
import io
import json
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from open_tennis_data.dataset import (
    DOWNLOAD_COMPRESSION_LEVEL,
    DOWNLOAD_ROW_GROUP_SIZE,
    MATCH_ROW_GROUP_SIZE,
    OBSERVATION_ROW_GROUP_SIZE,
    _copy_parquet,
    _quoted,
    _required_row,
    _sql_list,
    sha256_file,
)
from open_tennis_data.schema import MATCH_COLUMNS, SCHEMA_VERSION, TOURS
from open_tennis_data.source_policy import SourcePolicyRegistry

DEFAULT_REPOSITORY = "ryantjx/tennis-match-data"
RELEASE_START_YEAR = 2020
PUBLIC_LEVELS = (
    "grand_slam",
    "masters_1000",
    "tour_finals",
    "olympics",
    "team",
    "wta_1000",
    "wta_500",
    "other",
)
V3_PARQUET_ASSETS = (
    "matches.parquet",
    "completed.parquet",
    "fixtures.parquet",
    "tournaments.parquet",
    "players.parquet",
    "provenance.parquet",
    "sources.parquet",
    "coverage.parquet",
    "health.parquet",
    "quarantine.parquet",
    "catalog.parquet",
)
V3_RELEASE_ASSETS = (*V3_PARQUET_ASSETS, "manifest.json", "SHA256SUMS")


def _utc_timestamp(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC).replace(microsecond=0)


def _release_url(repository: str, release: str, filename: str) -> str:
    quoted_release = "latest" if release == "latest" else urllib.parse.quote(release, safe="")
    if quoted_release == "latest":
        return f"https://github.com/{repository}/releases/latest/download/{filename}"
    return (
        f"https://github.com/{repository}/releases/download/"
        f"{quoted_release}/{filename}"
    )


def manifest_url(release: str, repository: str = DEFAULT_REPOSITORY) -> str:
    """Return the public manifest URL for ``latest`` or an immutable tag."""
    return _release_url(repository, release, "manifest.json")


def load_release_manifest(
    release: str,
    *,
    repository: str = DEFAULT_REPOSITORY,
    url: str | None = None,
) -> dict[str, Any]:
    """Load and minimally validate a v3 release manifest."""
    target = url or manifest_url(release, repository)
    request = urllib.request.Request(
        target,
        headers={"User-Agent": f"open-tennis-data/{SCHEMA_VERSION}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read())
    if payload.get("product_version") != "3":
        raise ValueError("release manifest is not an Open Tennis Data v3 release")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported release schema {payload.get('schema_version')!r}; "
            f"expected {SCHEMA_VERSION}"
        )
    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("release manifest has no assets")
    names = {item.get("name") for item in assets if isinstance(item, dict)}
    required = {"matches.parquet", "completed.parquet", "fixtures.parquet"}
    if not required.issubset(names):
        raise ValueError(f"release manifest is missing assets: {sorted(required - names)}")
    return payload


def _asset_locations(manifest: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in manifest.get("assets", []):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("url") or "")
        if name and url:
            result[name] = url
    return result


def register_release_views(
    connection: duckdb.DuckDBPyConnection,
    manifest: Mapping[str, Any],
) -> None:
    """Register compatibility-preserving views over a remote v3 release."""
    assets = _asset_locations(manifest)
    table_assets = {
        "matches": "completed.parquet",
        "fixtures": "fixtures.parquet",
        "all_matches": "matches.parquet",
        "tournaments": "tournaments.parquet",
        "players": "players.parquet",
        "provenance": "provenance.parquet",
        "sources": "sources.parquet",
        "coverage": "coverage.parquet",
        "health": "health.parquet",
        "quarantine": "quarantine.parquet",
        "catalog": "catalog.parquet",
    }
    for table, filename in table_assets.items():
        location = assets.get(filename)
        if not location:
            continue
        connection.execute(
            f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM "
            f"read_parquet({_quoted(location)}, hive_partitioning=false)"
        )


def query_release(
    manifest: Mapping[str, Any],
    sql: str,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    connection = duckdb.connect()
    register_release_views(connection, manifest)
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    rows = cursor.fetchall()
    connection.close()
    return columns, rows


def extract_release(
    manifest: Mapping[str, Any],
    output: Path,
    *,
    tours: Sequence[str],
    years: Sequence[int] | None,
    levels: Sequence[str],
) -> int:
    connection = duckdb.connect()
    register_release_views(connection, manifest)
    predicates: list[str] = []
    if tours:
        predicates.append("m.tour IN (" + ",".join(_quoted(item) for item in tours) + ")")
    if years:
        predicates.append("m.year IN (" + ",".join(str(item) for item in years) + ")")
    if levels:
        predicates.append("t.level IN (" + ",".join(_quoted(item) for item in levels) + ")")
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    _copy_parquet(
        connection,
        "SELECT m.* FROM matches m JOIN tournaments t "
        f"USING(tournament_id,tour,year){where} "
        "ORDER BY m.date NULLS LAST,m.tournament_id,m.draw,m.round,m.match_id",
        output,
        row_group_size=MATCH_ROW_GROUP_SIZE,
        match_shaped=True,
    )
    rows = int(
        _required_row(connection.execute(
            f"SELECT count(*) FROM read_parquet({_quoted(output)})"
        ))[0]
    )
    connection.close()
    return rows


def query_matches(
    connection: duckdb.DuckDBPyConnection,
    *,
    tours: Sequence[str],
    years: Sequence[int] | None,
    date_from: date | None,
    date_to: date | None,
    player: str | None,
    tournament: str | None,
    statuses: Sequence[str],
    limit: int,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    predicates: list[str] = []
    parameters: list[Any] = []
    if tours:
        predicates.append("tour IN (" + ",".join("?" for _ in tours) + ")")
        parameters.extend(tours)
    if years:
        predicates.append("year IN (" + ",".join("?" for _ in years) + ")")
        parameters.extend(years)
    if date_from:
        predicates.append("date >= ?")
        parameters.append(date_from)
    if date_to:
        predicates.append("date <= ?")
        parameters.append(date_to)
    if player:
        predicates.append(
            "(lower(array_to_string(player1_name,' / ')) LIKE ? "
            "OR lower(array_to_string(player2_name,' / ')) LIKE ?)"
        )
        value = f"%{player.casefold()}%"
        parameters.extend((value, value))
    if tournament:
        predicates.append("lower(tournament_name) LIKE ?")
        parameters.append(f"%{tournament.casefold()}%")
    if statuses:
        predicates.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        parameters.extend(statuses)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    cursor = connection.execute(
        f"SELECT {','.join(MATCH_COLUMNS)} FROM all_matches{where} "
        "ORDER BY date NULLS LAST,tour,tournament_name,round,match_id LIMIT ?",
        [*parameters, limit],
    )
    return [item[0] for item in cursor.description], cursor.fetchall()


def _write_match_assets(
    connection: duckdb.DuckDBPyConnection,
    root: Path,
    output: Path,
    as_of_date: date,
) -> None:
    match_files = sorted(root.glob("matches/tour=*/year=*/matches.parquet"))
    fixture_files = sorted(root.glob("fixtures/tour=*/current.parquet"))
    tournament_files = sorted(root.glob("tournaments/tour=*/year=*/tournaments.parquet"))
    date_files = sorted(
        root.glob("date_observations/tour=*/year=*/date-observations.parquet")
    )
    if not all((match_files, fixture_files, tournament_files, date_files)):
        raise ValueError("v3 release requires match, fixture, tournament, and date evidence")
    levels = ",".join(_quoted(item) for item in PUBLIC_LEVELS)
    tournaments = (
        f"read_parquet({_sql_list(tournament_files)},"
        "union_by_name=true,hive_partitioning=false)"
    )
    matches = (
        f"read_parquet({_sql_list(match_files)},"
        "union_by_name=true,hive_partitioning=false)"
    )
    fixtures = (
        f"read_parquet({_sql_list(fixture_files)},"
        "union_by_name=true,hive_partitioning=false)"
    )
    evidence = (
        f"read_parquet({_sql_list(date_files)},"
        "union_by_name=true,hive_partitioning=false)"
    )
    connection.execute(
        f"""
        CREATE TEMP VIEW v3_tournaments AS
        SELECT * FROM {tournaments}
        WHERE year >= {RELEASE_START_YEAR} AND level IN ({levels})
        """
    )
    connection.execute(
        f"""
        CREATE TEMP VIEW v3_completed AS
        SELECT m.*
        FROM {matches} m
        JOIN v3_tournaments t USING(tournament_id,tour,year)
        SEMI JOIN {evidence} e USING(match_id,tour,year)
        WHERE m.year >= {RELEASE_START_YEAR}
          AND m.draw='main' AND m.format='singles'
          AND m.status<>'fixture' AND m.date IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM {evidence} verified
            WHERE verified.match_id=m.match_id AND verified.tour=m.tour
              AND verified.year=m.year AND verified.played_on=m.date
              AND verified.date_precision='day'
          )
        """
    )
    connection.execute(
        f"""
        CREATE TEMP VIEW v3_fixtures AS
        SELECT f.*
        FROM {fixtures} f
        JOIN v3_tournaments t USING(tournament_id,tour,year)
        WHERE f.year >= {RELEASE_START_YEAR}
          AND f.draw='main' AND f.format='singles' AND f.status='fixture'
          AND (f.date IS NULL OR f.date >= DATE {_quoted(as_of_date.isoformat())})
        """
    )
    for table, filename in (
        ("v3_completed", "completed.parquet"),
        ("v3_fixtures", "fixtures.parquet"),
    ):
        _copy_parquet(
            connection,
            f"SELECT * FROM {table} "
            "ORDER BY date NULLS LAST,tournament_id,draw,round,match_id",
            output / filename,
            row_group_size=DOWNLOAD_ROW_GROUP_SIZE,
            compression_level=DOWNLOAD_COMPRESSION_LEVEL,
            match_shaped=True,
        )
    _copy_parquet(
        connection,
        "SELECT * FROM v3_completed UNION ALL SELECT * FROM v3_fixtures "
        "ORDER BY date NULLS LAST,tournament_id,draw,round,match_id",
        output / "matches.parquet",
        row_group_size=DOWNLOAD_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
        match_shaped=True,
    )
    _copy_parquet(
        connection,
        "WITH referenced AS (SELECT tournament_id,tour,year FROM v3_completed "
        "UNION SELECT tournament_id,tour,year FROM v3_fixtures) "
        "SELECT t.* FROM v3_tournaments t JOIN referenced r "
        "USING(tournament_id,tour,year) ORDER BY tour,year,start_date,tournament_id",
        output / "tournaments.parquet",
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )


def _write_supporting_assets(
    connection: duckdb.DuckDBPyConnection,
    root: Path,
    output: Path,
    as_of_date: date,
    registry: SourcePolicyRegistry,
) -> None:
    player_files = sorted(root.glob("players/tour=*/players.parquet"))
    observation_files = sorted(
        root.glob("observations/tour=*/year=*/observations.parquet")
    )
    date_files = sorted(
        root.glob("date_observations/tour=*/year=*/date-observations.parquet")
    )
    source_audit = root / "coverage/source-audit.parquet"
    quarantine = root / "quarantine/quarantine.parquet"
    connection.execute(
        """
        CREATE TEMP TABLE v3_source_policy (
          source_label VARCHAR,
          policy_source VARCHAR,
          policy_state VARCHAR,
          terms_url VARCHAR,
          allowed_uses VARCHAR[],
          allowed_fields VARCHAR[],
          attribution VARCHAR,
          rate_limit VARCHAR,
          parser_version VARCHAR,
          reviewed_at DATE,
          policy_revision VARCHAR
        )
        """
    )
    connection.executemany(
        "INSERT INTO v3_source_policy VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        registry.policy_rows(),
    )
    _copy_parquet(
        connection,
        f"WITH ids AS (SELECT unnest(player1_id) player_id,tour FROM v3_completed "
        "UNION SELECT unnest(player2_id),tour FROM v3_completed "
        "UNION SELECT unnest(player1_id),tour FROM v3_fixtures "
        "UNION SELECT unnest(player2_id),tour FROM v3_fixtures), players AS ("
        f"SELECT * FROM read_parquet({_sql_list(player_files)},union_by_name=true)) "
        "SELECT DISTINCT p.* FROM players p JOIN ids i USING(player_id,tour) "
        "ORDER BY tour,player_id",
        output / "players.parquet",
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )
    _copy_parquet(
        connection,
        "WITH released AS (SELECT * FROM v3_completed "
        "UNION ALL BY NAME SELECT * FROM v3_fixtures), result_observations AS ("
        "SELECT match_id,tour,year,source_file_id,source_match_id,"
        "'result_crosscheck'::VARCHAR observation_kind,NULL::DATE played_on,"
        "'none'::VARCHAR date_role,'unknown'::VARCHAR date_precision,"
        "NULL::VARCHAR match_method,NULL::VARCHAR row_fingerprint FROM "
        f"read_parquet({_sql_list(observation_files)},"
        "union_by_name=true,hive_partitioning=false)), date_observations AS ("
        "SELECT match_id,tour,year,source_file_id,source_match_id,"
        "'match_date'::VARCHAR observation_kind,played_on,"
        "'played'::VARCHAR date_role,date_precision,match_method,row_fingerprint FROM "
        f"read_parquet({_sql_list(date_files)},union_by_name=true,"
        "hive_partitioning=false)), observations AS ("
        "SELECT * FROM result_observations UNION ALL BY NAME "
        "SELECT * FROM date_observations), source_files AS ("
        f"SELECT * FROM read_parquet({_quoted(source_audit)})) "
        "SELECT DISTINCT o.match_id,o.tour,o.year,o.source_file_id,o.source_match_id,"
        "o.observation_kind,NULL::TIMESTAMPTZ retrieved_at,s.sha256 content_sha256,"
        "o.played_on,o.date_role,o.date_precision,NULL::VARCHAR source_timezone,"
        "NULL::VARCHAR venue_timezone,r.player1_name participants_side_1,"
        "r.player2_name participants_side_2,r.round,r.score,o.match_method,"
        "o.row_fingerprint,p.parser_version,p.policy_revision "
        "FROM observations o JOIN released r USING(match_id,tour,year) "
        "JOIN source_files s USING(source_file_id) "
        "JOIN v3_source_policy p USING(source_label) "
        "ORDER BY o.tour,o.year,o.source_file_id,o.source_match_id,o.observation_kind",
        output / "provenance.parquet",
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )
    _copy_parquet(
        connection,
        f"SELECT s.*,p.policy_source,p.policy_state,p.terms_url,p.allowed_uses,"
        "p.allowed_fields,p.attribution,p.rate_limit,p.parser_version,"
        "p.reviewed_at,p.policy_revision "
        f"FROM read_parquet({_quoted(source_audit)}) s "
        "JOIN v3_source_policy p USING(source_label) "
        f"SEMI JOIN read_parquet({_quoted(output / 'provenance.parquet')}) pr "
        "USING(source_file_id) ORDER BY kind,tour,year,source_label,source_file_id",
        output / "sources.parquet",
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )
    _copy_parquet(
        connection,
        "SELECT * FROM read_parquet("
        f"{_quoted(quarantine)}) "
        f"WHERE year >= {RELEASE_START_YEAR} ORDER BY tour,year,source_label,source_match_id",
        output / "quarantine.parquet",
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )
    _copy_parquet(
        connection,
        """
        WITH records AS (
          SELECT c.*, 'completed' lifecycle FROM v3_completed c
          UNION ALL BY NAME
          SELECT f.*, 'fixture' lifecycle FROM v3_fixtures f
        )
        SELECT r.tour,r.year,t.level,r.lifecycle,
          count(*)::BIGINT match_rows,
          count(r.date)::BIGINT dated_rows,
          count(*) FILTER(WHERE r.date IS NULL)::BIGINT undated_rows,
          count(DISTINCT r.tournament_id)::BIGINT tournament_rows,
          NULL::BIGINT expected_tournament_rows,
          NULL::BIGINT expected_match_rows,
          NULL::BIGINT missing_tournament_rows,
          NULL::BIGINT missing_match_rows,
          count(*) FILTER(
            WHERE r.lifecycle='completed' AND r.date IS NULL
          )::BIGINT missing_date_rows,
          (SELECT count(*) FROM read_parquet("""
        + _quoted(quarantine)
        + """) q
            WHERE q.tour=r.tour AND q.year=r.year
              AND q.reason='conflicting_exact_date')::BIGINT source_conflicts,
          'preview'::VARCHAR coverage_status
        FROM records r JOIN v3_tournaments t USING(tournament_id,tour,year)
        GROUP BY r.tour,r.year,t.level,r.lifecycle
        ORDER BY r.tour,r.year,t.level,r.lifecycle
        """,
        output / "coverage.parquet",
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )
    _copy_parquet(
        connection,
        f"""
        WITH completed AS (
          SELECT tour,count(*) row_count,max(date) latest FROM v3_completed GROUP BY tour
        ), fixtures AS (
          SELECT tour,count(*) row_count,max(date) latest FROM v3_fixtures GROUP BY tour
        )
        SELECT tours.tour,DATE {_quoted(as_of_date.isoformat())} as_of,
          coalesce(c.row_count,0)::BIGINT completed_rows,
          coalesce(f.row_count,0)::BIGINT fixture_rows,
          c.latest latest_match_date,f.latest latest_fixture_date,
          'preview'::VARCHAR status
        FROM (SELECT unnest({list(TOURS)!r}::VARCHAR[]) tour) tours
        LEFT JOIN completed c USING(tour) LEFT JOIN fixtures f USING(tour)
        ORDER BY tour
        """,
        output / "health.parquet",
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )


def _write_catalog_and_manifest(
    connection: duckdb.DuckDBPyConnection,
    output: Path,
    *,
    as_of: datetime,
    repository: str,
    release_tag: str,
    policy_revisions: Sequence[str],
) -> dict[str, Any]:
    asset_rows: list[dict[str, Any]] = []
    for path in sorted(output.glob("*.parquet")):
        rows = int(
            _required_row(connection.execute(
                f"SELECT count(*) FROM read_parquet({_quoted(path)})"
            ))[0]
        )
        asset_rows.append(
            {
                "name": path.name,
                "table": path.stem,
                "rows": rows,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    values = ",".join(
        "("
        + ",".join(
            (
                _quoted(item["name"]),
                _quoted(item["table"]),
                str(item["rows"]),
                str(item["bytes"]),
                _quoted(item["sha256"]),
                _quoted(as_of.isoformat().replace("+00:00", "Z")),
            )
        )
        + ")"
        for item in asset_rows
    )
    _copy_parquet(
        connection,
        "SELECT col0::VARCHAR path,col1::VARCHAR table_name,"
        "col2::BIGINT row_count,col3::BIGINT byte_size,"
        "col4::VARCHAR sha256,col5::TIMESTAMPTZ as_of "
        f"FROM (VALUES {values}) ORDER BY path",
        output / "catalog.parquet",
        row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        compression_level=DOWNLOAD_COMPRESSION_LEVEL,
    )
    catalog = output / "catalog.parquet"
    asset_rows.append(
        {
            "name": catalog.name,
            "table": "catalog",
            "rows": int(
                _required_row(connection.execute(
                    f"SELECT count(*) FROM read_parquet({_quoted(catalog)})"
                ))[0]
            ),
            "bytes": catalog.stat().st_size,
            "sha256": sha256_file(catalog),
        }
    )
    for item in asset_rows:
        item["url"] = _release_url(repository, release_tag, str(item["name"]))
    manifest = {
        "product": "Open Tennis Data",
        "product_version": "3",
        "collector_version": "3.2.0",
        "schema_version": SCHEMA_VERSION,
        "source_policy_revisions": list(policy_revisions),
        "release_status": "preview",
        "release_tag": release_tag,
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "scope": {
            "start_year": RELEASE_START_YEAR,
            "tours": list(TOURS),
            "draw": "main",
            "format": "singles",
            "levels": list(PUBLIC_LEVELS),
            "terminal_date_requirement": "accepted match-level day evidence",
            "fixture_dates_nullable": True,
        },
        "preview_reasons": [
            "expected closed-event tournament/draw inventory is not populated",
            "legacy observations do not record their original retrieval timestamps",
        ],
        "assets": sorted(asset_rows, key=lambda item: str(item["name"])),
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksum_paths = [*sorted(output.glob("*.parquet")), manifest_path]
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in checksum_paths),
        encoding="utf-8",
    )
    return manifest


def create_v3_release(
    root: Path,
    output: Path,
    *,
    as_of: str | datetime | None = None,
    repository: str = DEFAULT_REPOSITORY,
    release_tag: str | None = None,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    """Build the deterministic, backend-only v3 release directory."""
    root = root.resolve()
    output = output.resolve()
    timestamp = _utc_timestamp(as_of)
    tag = release_tag or f"data-v3-{timestamp:%Y%m%dT%H%M%SZ}"
    output.mkdir(parents=True, exist_ok=True)
    for filename in V3_RELEASE_ASSETS:
        path = output / filename
        if path.exists():
            path.unlink()
    connection = duckdb.connect()
    registry = SourcePolicyRegistry.load(policy_path)
    _write_match_assets(connection, root, output, timestamp.date())
    _write_supporting_assets(
        connection,
        root,
        output,
        timestamp.date(),
        registry,
    )
    source_labels = {
        str(row[0])
        for row in connection.execute(
            f"SELECT DISTINCT source_label FROM read_parquet("
            f"{_quoted(output / 'sources.parquet')})"
        ).fetchall()
    }
    registry.require_publishable(source_labels)
    manifest = _write_catalog_and_manifest(
        connection,
        output,
        as_of=timestamp,
        repository=repository,
        release_tag=tag,
        policy_revisions=registry.revisions,
    )
    terminal_errors = int(
        _required_row(connection.execute(
            f"SELECT count(*) FROM read_parquet({_quoted(output / 'completed.parquet')}) "
            "WHERE date IS NULL OR status='fixture'"
        ))[0]
    )
    fixture_errors = int(
        _required_row(connection.execute(
            f"SELECT count(*) FROM read_parquet({_quoted(output / 'fixtures.parquet')}) "
            "WHERE status<>'fixture' OR winner_id IS NOT NULL OR score IS NOT NULL"
        ))[0]
    )
    connection.close()
    if terminal_errors or fixture_errors:
        raise RuntimeError(
            f"invalid v3 lifecycle rows: terminal={terminal_errors}, fixture={fixture_errors}"
        )
    errors = validate_v3_release(output)
    if errors:
        raise RuntimeError("generated v3 release failed validation:\n" + "\n".join(errors))
    return manifest


def validate_v3_release(
    directory: Path,
    *,
    require_complete: bool = False,
    max_age_hours: float | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Validate a staged or redownloaded v3 release without network access."""
    directory = directory.resolve()
    errors: list[str] = []
    missing = [
        filename
        for filename in V3_RELEASE_ASSETS
        if not (directory / filename).is_file()
    ]
    if missing:
        return [f"missing release assets: {', '.join(missing)}"]
    try:
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid manifest.json: {exc}"]
    if manifest.get("product_version") != "3":
        errors.append("manifest product_version must be 3")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"manifest schema_version must be {SCHEMA_VERSION}")
    status = manifest.get("release_status")
    if status not in {"preview", "stable"}:
        errors.append("manifest release_status must be preview or stable")
    if require_complete and status != "stable":
        errors.append("release is preview; the closed-event coverage gate is not complete")
    try:
        as_of = _utc_timestamp(str(manifest["as_of"]))
        if max_age_hours is not None:
            reference = _utc_timestamp(now)
            age_hours = (reference - as_of).total_seconds() / 3600
            if age_hours < 0 or age_hours > max_age_hours:
                errors.append(
                    f"release age is {age_hours:.1f} hours; limit is {max_age_hours:.1f}"
                )
    except (KeyError, ValueError) as exc:
        errors.append(f"invalid manifest as_of: {exc}")

    manifest_assets = {
        str(item.get("name")): item
        for item in manifest.get("assets", [])
        if isinstance(item, Mapping)
    }
    expected_parquet = set(V3_PARQUET_ASSETS)
    if set(manifest_assets) != expected_parquet:
        errors.append(
            "manifest Parquet assets differ from the v3 contract: "
            f"missing={sorted(expected_parquet - set(manifest_assets))}, "
            f"extra={sorted(set(manifest_assets) - expected_parquet)}"
        )
    for name, item in manifest_assets.items():
        path = directory / name
        if not path.is_file():
            continue
        digest = sha256_file(path)
        if item.get("sha256") != digest:
            errors.append(f"{name}: manifest checksum mismatch")
        if item.get("bytes") != path.stat().st_size:
            errors.append(f"{name}: manifest byte size mismatch")

    checksum_entries: dict[str, str] = {}
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or "/" in parts[1] or parts[1] in checksum_entries:
            errors.append(f"invalid SHA256SUMS entry: {line!r}")
            continue
        checksum_entries[parts[1]] = parts[0]
    checksum_expected = {*V3_PARQUET_ASSETS, "manifest.json"}
    if set(checksum_entries) != checksum_expected:
        errors.append("SHA256SUMS does not enumerate every release payload")
    for name, expected in checksum_entries.items():
        path = directory / name
        if path.is_file() and sha256_file(path) != expected:
            errors.append(f"{name}: SHA256SUMS mismatch")

    connection = duckdb.connect()
    try:
        for name in V3_PARQUET_ASSETS:
            path = directory / name
            if not path.is_file():
                continue
            try:
                rows = int(
                    _required_row(connection.execute(
                        f"SELECT count(*) FROM read_parquet({_quoted(path)})"
                    ))[0]
                )
            except duckdb.Error as exc:
                errors.append(f"{name}: unreadable Parquet: {exc}")
                continue
            if name in manifest_assets and manifest_assets[name].get("rows") != rows:
                errors.append(f"{name}: manifest row count mismatch")

        for name in ("matches.parquet", "completed.parquet", "fixtures.parquet"):
            path = directory / name
            columns = [
                str(row[0])
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet({_quoted(path)})"
                ).fetchall()
            ]
            if columns != list(MATCH_COLUMNS):
                errors.append(f"{name}: incompatible 19-column match schema")
            metadata = {
                (
                    key.decode() if isinstance(key, bytes) else str(key)
                ): value.decode() if isinstance(value, bytes) else str(value)
                for _, key, value in connection.execute(
                    f"SELECT * FROM parquet_kv_metadata({_quoted(path)})"
                ).fetchall()
            }
            if metadata.get("open_tennis_data_schema_version") != SCHEMA_VERSION:
                errors.append(f"{name}: missing schema 3.2 Parquet metadata")

        matches = _quoted(directory / "matches.parquet")
        completed = _quoted(directory / "completed.parquet")
        fixtures = _quoted(directory / "fixtures.parquet")
        provenance = _quoted(directory / "provenance.parquet")
        lifecycle_errors = int(
            _required_row(connection.execute(
                f"""
                SELECT
                  (SELECT count(*) FROM read_parquet({completed})
                   WHERE date IS NULL OR status='fixture')
                + (SELECT count(*) FROM read_parquet({fixtures})
                   WHERE status<>'fixture' OR winner_id IS NOT NULL OR score IS NOT NULL)
                """
            ))[0]
        )
        if lifecycle_errors:
            errors.append(f"invalid lifecycle rows: {lifecycle_errors}")
        duplicate_ids = int(
            _required_row(connection.execute(
                f"SELECT count(*)-count(DISTINCT match_id) FROM read_parquet({matches})"
            ))[0]
        )
        if duplicate_ids:
            errors.append(f"duplicate match IDs: {duplicate_ids}")
        projection_differences = int(
            _required_row(connection.execute(
                f"""
                SELECT count(*) FROM (
                  (SELECT * FROM read_parquet({matches})
                   EXCEPT ALL
                   (SELECT * FROM read_parquet({completed})
                    UNION ALL SELECT * FROM read_parquet({fixtures})))
                  UNION ALL
                  ((SELECT * FROM read_parquet({completed})
                    UNION ALL SELECT * FROM read_parquet({fixtures}))
                   EXCEPT ALL SELECT * FROM read_parquet({matches}))
                )
                """
            ))[0]
        )
        if projection_differences:
            errors.append("matches.parquet is not the completed/fixture union")
        evidence_errors = int(
            _required_row(connection.execute(
                f"""
                SELECT count(*) FROM read_parquet({completed}) c
                WHERE NOT EXISTS (
                  SELECT 1 FROM read_parquet({provenance}) p
                  WHERE p.match_id=c.match_id AND p.tour=c.tour AND p.year=c.year
                    AND p.observation_kind='match_date'
                    AND p.played_on=c.date AND p.date_role='played'
                    AND p.date_precision='day'
                )
                """
            ))[0]
        )
        if evidence_errors:
            errors.append(
                f"terminal rows without accepted match-level day evidence: {evidence_errors}"
            )
        scope_errors = int(
            _required_row(connection.execute(
                f"SELECT count(*) FROM read_parquet({matches}) "
                f"WHERE year<{RELEASE_START_YEAR} OR draw<>'main' OR format<>'singles'"
            ))[0]
        )
        if scope_errors:
            errors.append(f"out-of-scope match rows: {scope_errors}")
        policy_errors = int(
            _required_row(connection.execute(
                f"SELECT count(*) FROM read_parquet("
                f"{_quoted(directory / 'sources.parquet')}) "
                "WHERE policy_state NOT IN ('public_research','approved') "
                "OR NOT list_contains(allowed_uses,'public_research_release')"
            ))[0]
        )
        if policy_errors:
            errors.append(f"non-publishable source rows: {policy_errors}")
        if require_complete:
            coverage_errors = int(
                _required_row(connection.execute(
                    f"SELECT count(*) FROM read_parquet("
                    f"{_quoted(directory / 'coverage.parquet')}) "
                    "WHERE coverage_status<>'complete'"
                ))[0]
            )
            retrieval_errors = int(
                _required_row(connection.execute(
                    f"SELECT count(*) FROM read_parquet({provenance}) "
                    "WHERE retrieved_at IS NULL"
                ))[0]
            )
            if coverage_errors:
                errors.append(f"incomplete coverage rows: {coverage_errors}")
            if retrieval_errors:
                errors.append(
                    f"observations without recorded retrieval time: {retrieval_errors}"
                )
    except duckdb.Error as exc:
        errors.append(f"release validation query failed: {exc}")
    finally:
        connection.close()
    return errors


def format_release_rows(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    output_format: str,
) -> str:
    """Serialize CLI rows without leaking Python-specific values."""

    def plain(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, tuple):
            return [plain(item) for item in value]
        if isinstance(value, list):
            return [plain(item) for item in value]
        return value

    records = [
        {column: plain(value) for column, value in zip(columns, row, strict=True)}
        for row in rows
    ]
    if output_format == "json":
        return json.dumps(records, ensure_ascii=False, indent=2) + "\n"
    if output_format == "jsonl":
        return "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        )
    if output_format == "csv":
        stream = io.StringIO()
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(
            [
                json.dumps(plain(value), ensure_ascii=False)
                if isinstance(value, (list, tuple))
                else plain(value)
                for value in row
            ]
            for row in rows
        )
        return stream.getvalue()
    if output_format != "table":
        raise ValueError("format must be table, csv, json, or jsonl")
    lines = ["\t".join(columns)]
    lines.extend(
        "\t".join(
            json.dumps(plain(value), ensure_ascii=False)
            if isinstance(value, (list, tuple))
            else "" if value is None else str(plain(value))
            for value in row
        )
        for row in rows
    )
    return "\n".join(lines) + "\n"
