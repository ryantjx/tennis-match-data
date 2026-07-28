"""Closed-bundle acquisition and Tennis-Data v3.2 reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
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
    sha256_file,
)
from open_tennis_data.exact_dates import (
    TENNIS_DATA_BASE,
    _score_from_row,
    fetch_tennis_data_file,
    parse_excel_date,
)
from open_tennis_data.integrity import canonical_json, content_id
from open_tennis_data.schema import MATCH_COLUMNS, SCHEMA_VERSION, TOURS
from open_tennis_data.source_policy import SourcePolicyRegistry

BUNDLE_VERSION = "1"
ACQUISITION_VERSION = "3.2"
def parse_year_range(value: str) -> list[int]:
    start_text, separator, end_text = value.partition(":")
    start = int(start_text)
    end = int(end_text) if separator else start
    if start < 2003 or end < start:
        raise ValueError("history years must be an ascending range from 2003")
    return list(range(start, end + 1))


def _normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _status(record: Mapping[str, Any]) -> str:
    comment = str(record.get("Comment") or "").strip().upper()
    if comment in {"WALKOVER", "W/O", "WO"}:
        return "walkover"
    if comment in {"DEFAULTED", "DEF"}:
        return "defaulted"
    if comment in {"RETIRED", "RET"}:
        return "retired"
    if comment in {"ABANDONED", "ABD", "ABN"}:
        return "abandoned"
    return "completed"


def _level(record: Mapping[str, Any], tour: str) -> str:
    value = _normalized(record.get("Series") or record.get("Tier"))
    if "grand slam" in value:
        return "grand_slam"
    if tour == "atp" and ("masters 1000" in value or value == "masters"):
        return "masters_1000"
    if "masters cup" in value or "tour finals" in value:
        return "tour_finals"
    if tour == "wta" and ("1000" in value or "premier mandatory" in value):
        return "wta_1000"
    if tour == "wta" and ("500" in value or value == "premier"):
        return "wta_500"
    return "other"


@dataclass(frozen=True)
class HistoryObservation:
    tour: str
    source_year: int
    source_match_id: str
    source_file_id: str
    row_fingerprint: str
    played_on: str
    tournament: str
    location: str
    surface: str | None
    level: str
    round: str
    winner: str
    loser: str
    winner_seed: str | None
    loser_seed: str | None
    score: str | None
    best_of: int | None
    status: str


def parse_history_artifact(
    path: Path,
    *,
    tour: str,
    year: int,
    source_url: str,
) -> tuple[dict[str, Any], list[HistoryObservation], list[dict[str, Any]]]:
    """Parse one immutable Tennis-Data workbook into complete terminal observations."""
    from python_calamine import CalamineWorkbook

    workbook = CalamineWorkbook.from_path(str(path))
    values = workbook.get_sheet_by_index(0).to_python()
    workbook.close()
    if not values:
        raise ValueError(f"empty spreadsheet: {path}")
    headers = [str(value).strip() for value in values[0]]
    digest = sha256_file(path)
    source_file_id = content_id(
        "source_file",
        {
            "source": "tennis-data.co.uk",
            "url": source_url,
            "sha256": digest,
            "tour": tour,
            "year": year,
            "kind": "matches",
        },
        length=20,
    )
    observations: list[HistoryObservation] = []
    rejected: list[dict[str, Any]] = []
    for ordinal, row_values in enumerate(values[1:], 2):
        record = {
            header: row_values[index] if index < len(row_values) else None
            for index, header in enumerate(headers)
        }
        source_match_id = f"tennis-data:{tour}:{year}:{ordinal}"
        fingerprint = hashlib.sha256(canonical_json(record)).hexdigest()
        played_on = parse_excel_date(record.get("Date"))
        required = [
            str(record.get(name) or "").strip()
            for name in ("Tournament", "Winner", "Loser", "Round")
        ]
        reason: str | None = None
        if tour == "atp" and year < 2003:
            reason = "tournament_date_not_match_date"
        elif played_on is None or not all(required):
            reason = "invalid_terminal_source_row"
        elif played_on.year != year and not (
            played_on.year == year - 1 and played_on.month == 12
        ):
            reason = "date_outside_source_year"
        if reason:
            rejected.append(
                {
                    "tour": tour,
                    "year": year,
                    "source_label": "tennis-data.co.uk",
                    "source_path": path.name,
                    "source_file_id": source_file_id,
                    "source_match_id": source_match_id,
                    "row_fingerprint": fingerprint,
                    "candidate_match_ids": None,
                    "reason": reason,
                }
            )
            continue
        assert played_on is not None
        best_of_value = record.get("Best of")
        try:
            best_of = int(float(str(best_of_value))) if best_of_value not in (None, "") else None
        except ValueError:
            best_of = None
        observations.append(
            HistoryObservation(
                tour=tour,
                source_year=year,
                source_match_id=source_match_id,
                source_file_id=source_file_id,
                row_fingerprint=fingerprint,
                played_on=played_on.isoformat(),
                tournament=required[0],
                location=str(record.get("Location") or "").strip(),
                surface=str(record.get("Surface") or "").strip().casefold() or None,
                level=_level(record, tour),
                round=required[3].upper(),
                winner=required[1],
                loser=required[2],
                # Tennis-Data publishes rankings in WRank/LRank, not draw seeds.
                # Keeping these nullable avoids laundering a ranking into a seed.
                winner_seed=None,
                loser_seed=None,
                score=_score_from_row(dict(record)),
                best_of=best_of,
                status=_status(record),
            )
        )
    source = {
        "source_file_id": source_file_id,
        "kind": "matches",
        "tour": tour,
        "year": year,
        "source_label": "tennis-data.co.uk",
        "source_path": path.name,
        "source_url": source_url,
        "revision": f"sha256:{digest}",
        "sha256": digest,
        "license": "Tennis-Data redistribution terms",
        "source_rows": max(0, len(values) - 1),
        "normalized_rows": len(observations),
        "quarantined_rows": len(rejected),
    }
    return source, observations, rejected


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str)
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _artifact_path(root: Path, tour: str, year: int) -> Path | None:
    candidates = [
        *root.glob(f"{tour}-{year}.xls*"),
        *root.glob(f"{year}{'w' if tour == 'wta' else ''}/{year}.xls*"),
        *root.glob(f"**/{tour}/{year}.xls*"),
    ]
    return sorted(set(candidates))[0] if candidates else None


def _parent_manifest_digest(
    parent_release: str, parent_manifest: Path | None
) -> str | None:
    if parent_manifest is None:
        return None
    payload = json.loads(parent_manifest.read_text(encoding="utf-8"))
    if payload.get("release_tag") != parent_release:
        raise ValueError("parent manifest release tag does not match --parent-release")
    return sha256_file(parent_manifest)


def refresh_history_bundle(
    *,
    years: Sequence[int],
    parent_release: str,
    output: Path,
    offline_fixture_root: Path | None = None,
    parent_manifest: Path | None = None,
    collected_at: datetime | None = None,
) -> dict[str, Any]:
    """Fetch or load annual history and close an immutable source bundle."""
    if output.exists() and any(output.iterdir()):
        raise ValueError("source bundle output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    artifacts = output / "artifacts"
    artifacts.mkdir()
    instant = (collected_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    sources: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    artifact_rows: list[dict[str, Any]] = []
    for tour in TOURS:
        for year in sorted(set(years)):
            local = (
                _artifact_path(offline_fixture_root, tour, year)
                if offline_fixture_root
                else None
            )
            if local is None:
                local, url = fetch_tennis_data_file(
                    tour, year, artifacts / f"{tour}-{year}"
                )
            else:
                url = (
                    f"{TENNIS_DATA_BASE}/"
                    f"{year if tour == 'atp' else str(year) + 'w'}/{year}{local.suffix}"
                )
                destination = artifacts / f"{tour}-{year}{local.suffix.lower()}"
                shutil.copy2(local, destination)
                local = destination
            if local.parent != artifacts:
                destination = artifacts / f"{tour}-{year}{local.suffix.lower()}"
                shutil.copy2(local, destination)
                local = destination
            source, parsed, invalid = parse_history_artifact(
                local, tour=tour, year=year, source_url=url
            )
            sources.append(source)
            observations.extend(asdict(item) for item in parsed)
            rejected.extend(invalid)
            artifact_rows.append(
                {
                    "path": local.relative_to(output).as_posix(),
                    "source_label": "tennis-data.co.uk",
                    "source_url": url,
                    "tour": tour,
                    "year": year,
                    "media_type": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        if local.suffix.lower() == ".xlsx"
                        else "application/vnd.ms-excel"
                    ),
                    "bytes": local.stat().st_size,
                    "sha256": sha256_file(local),
                    "retrieved_at": instant.isoformat().replace("+00:00", "Z"),
                    "storage_mode": "private",
                }
            )
    _write_jsonl(output / "sources.jsonl", sources)
    _write_jsonl(output / "observations.jsonl", observations)
    _write_jsonl(output / "rejections.jsonl", rejected)
    manifest: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "acquisition_version": ACQUISITION_VERSION,
        "parent_release": parent_release,
        "parent_manifest_sha256": _parent_manifest_digest(
            parent_release, parent_manifest
        ),
        "collection_started_at": instant.isoformat().replace("+00:00", "Z"),
        "collection_closed_at": instant.isoformat().replace("+00:00", "Z"),
        "source_cutoff_at": instant.isoformat().replace("+00:00", "Z"),
        "artifacts": sorted(artifact_rows, key=lambda item: item["path"]),
        "files": {},
    }
    for name in ("sources.jsonl", "observations.jsonl", "rejections.jsonl"):
        path = output / name
        manifest["files"][name] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    manifest["bundle_id"] = content_id("bundle", manifest)
    (output / "bundle.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def collect_live_bundle(
    *,
    parent_release: str,
    permission_policy: Path | None,
    output: Path,
    offline_fixture_root: Path | None = None,
    parent_manifest: Path | None = None,
    collected_at: datetime | None = None,
) -> dict[str, Any]:
    """Close a live-event bundle while keeping blocked official adapters offline."""
    from open_tennis_data.official import parse_official_fixture_tree

    if output.exists() and any(output.iterdir()):
        raise ValueError("source bundle output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    instant = (collected_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    registry = SourcePolicyRegistry.load(permission_policy)
    artifact_rows: list[dict[str, Any]] = []
    fixture_root: Path | None = None
    if offline_fixture_root:
        fixture_root = output / "artifacts"
        for source_path in sorted(offline_fixture_root.rglob("*")):
            if not source_path.is_file() or source_path.suffix.casefold() not in {
                ".html",
                ".pdf",
            }:
                continue
            relative = source_path.relative_to(offline_fixture_root)
            destination = fixture_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            artifact_rows.append(
                {
                    "path": destination.relative_to(output).as_posix(),
                    "source_url": f"offline://{relative.as_posix()}",
                    "media_type": (
                        "application/pdf"
                        if destination.suffix.casefold() == ".pdf"
                        else "text/html"
                    ),
                    "bytes": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                    "retrieved_at": instant.isoformat().replace("+00:00", "Z"),
                    "storage_mode": "private",
                }
            )
    parsed = (
        parse_official_fixture_tree(fixture_root)
        if fixture_root
        else {"observations": [], "rejections": []}
    )
    telemetry: list[dict[str, Any]] = []
    for source, url, route in (
        ("atp-site", "https://www.atptour.com/", "overview"),
        ("wta-site", "https://www.wtatennis.com/", "overview"),
        ("atp-oop", "https://www.protennislive.com/", "order-of-play-pdf"),
        ("wta-oop", "https://www.wtatennis.com/", "order-of-play-pdf"),
    ):
        decision = registry.evaluate_network_request(
            source,
            url,
            route=route,
            fields={"participants"},
            use="private_evaluation",
            at=instant,
        )
        telemetry.append(
            {
                "source": source,
                "route": route,
                "network_attempted": False,
                "status": "offline_fixture" if offline_fixture_root else decision.reason,
                "policy_revision": decision.policy_revision,
            }
        )
    _write_jsonl(output / "official-observations.jsonl", parsed["observations"])
    _write_jsonl(output / "rejections.jsonl", parsed["rejections"])
    _write_jsonl(output / "telemetry.jsonl", telemetry)
    manifest: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "acquisition_version": ACQUISITION_VERSION,
        "parent_release": parent_release,
        "parent_manifest_sha256": _parent_manifest_digest(
            parent_release, parent_manifest
        ),
        "collection_started_at": instant.isoformat().replace("+00:00", "Z"),
        "collection_closed_at": instant.isoformat().replace("+00:00", "Z"),
        "source_cutoff_at": instant.isoformat().replace("+00:00", "Z"),
        "artifacts": artifact_rows,
        "files": {},
    }
    for name in ("official-observations.jsonl", "rejections.jsonl", "telemetry.jsonl"):
        path = output / name
        manifest["files"][name] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    manifest["bundle_id"] = content_id("bundle", manifest)
    (output / "bundle.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def verify_bundle(root: Path) -> list[str]:
    errors: list[str] = []
    try:
        manifest = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid bundle manifest: {exc}"]
    expected_id = manifest.pop("bundle_id", None)
    if expected_id != content_id("bundle", manifest):
        errors.append("bundle_id does not match the closed manifest")
    for name, expected in manifest.get("files", {}).items():
        path = root / name
        if not path.is_file():
            errors.append(f"missing bundle file: {name}")
        elif path.stat().st_size != expected.get("bytes") or sha256_file(path) != expected.get(
            "sha256"
        ):
            errors.append(f"bundle file changed after close: {name}")
    for artifact in manifest.get("artifacts", []):
        path = root / str(artifact.get("path"))
        if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
            errors.append(f"artifact changed after close: {artifact.get('path')}")
    return errors


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_table(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
    schema: Sequence[tuple[str, str]],
    rows: Sequence[Sequence[Any]],
    *,
    match_shaped: bool = False,
) -> None:
    connection.execute(
        "CREATE OR REPLACE TEMP TABLE staged ("
        + ",".join(f'"{name}" {kind}' for name, kind in schema)
        + ")"
    )
    if rows:
        connection.executemany(
            "INSERT INTO staged VALUES (" + ",".join("?" for _ in schema) + ")",
            rows,
        )
    _copy_parquet(
        connection,
        "SELECT * FROM staged ORDER BY ALL",
        path,
        row_group_size=(
            MATCH_ROW_GROUP_SIZE if match_shaped else OBSERVATION_ROW_GROUP_SIZE
        ),
        match_shaped=match_shaped,
    )


def reconcile_history_bundle(
    *,
    source_bundle: Path,
    parent_release: str,
    output: Path,
    parent_directory: Path | None = None,
) -> dict[str, Any]:
    """Build a release-ready 2020+ dataset from a verified closed history bundle."""
    errors = verify_bundle(source_bundle)
    if errors:
        raise ValueError("invalid source bundle:\n" + "\n".join(errors))
    bundle = json.loads((source_bundle / "bundle.json").read_text(encoding="utf-8"))
    if bundle.get("parent_release") != parent_release:
        raise ValueError("source bundle parent does not match --parent-release")
    if output.exists() and any(output.iterdir()):
        raise ValueError("reconcile output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    observations = _read_jsonl(source_bundle / "observations.jsonl")
    quarantined = _read_jsonl(source_bundle / "rejections.jsonl")
    sources = _read_jsonl(source_bundle / "sources.jsonl")
    seen: dict[tuple[str, ...], str] = {}
    accepted: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    for row in observations:
        semantic = (
            row["tour"],
            row["played_on"],
            _normalized(row["tournament"]),
            _normalized(row["round"]),
            *_normalized_pair(row["winner"], row["loser"]),
        )
        match_id = content_id("match", semantic)
        reason = None
        if semantic in seen:
            reason = "duplicate_terminal_identity"
        if reason:
            quarantined.append(
                {
                    "tour": row["tour"],
                    "year": row["source_year"],
                    "source_label": "tennis-data.co.uk",
                    "source_path": None,
                    "source_file_id": row["source_file_id"],
                    "source_match_id": row["source_match_id"],
                    "row_fingerprint": row["row_fingerprint"],
                    "candidate_match_ids": [seen[semantic]] if semantic in seen else None,
                    "reason": reason,
                }
            )
            dispositions.append(_disposition(row, "quarantined", None, reason))
            continue
        seen[semantic] = match_id
        row["match_id"] = match_id
        accepted.append(row)
        dispositions.append(_disposition(row, "replaced", row["source_match_id"], "eligible"))
    for row in quarantined:
        if not any(item["baseline_identity"] == row["source_match_id"] for item in dispositions):
            dispositions.append(
                {
                    "baseline_identity": row["source_match_id"],
                    "disposition": "quarantined",
                    "replacement_observation_id": None,
                    "reason": row["reason"],
                    "reviewer": "automated-v3.2-gate",
                    "reviewed_at": bundle["source_cutoff_at"],
                }
            )
    connection = duckdb.connect()
    _write_dataset_tables(
        connection,
        output,
        accepted=accepted,
        quarantined=quarantined,
        sources=sources,
        dispositions=dispositions,
        bundle=bundle,
    )
    if parent_directory is not None:
        expected_parent_digest = bundle.get("parent_manifest_sha256")
        actual_parent_digest = _parent_manifest_digest(
            parent_release, parent_directory / "manifest.json"
        )
        if (
            expected_parent_digest is not None
            and actual_parent_digest != expected_parent_digest
        ):
            raise ValueError("parent manifest digest does not match the source bundle")
        _seed_parent_active_data(connection, output, parent_directory)
    _create_catalog(
        connection,
        output,
        date.fromisoformat(str(bundle["source_cutoff_at"])[:10]),
        str(bundle["bundle_id"]),
    )
    connection.close()
    manifest = {
        "acquisition_version": ACQUISITION_VERSION,
        "schema_version": SCHEMA_VERSION,
        "parent_release": parent_release,
        "source_bundle_id": bundle["bundle_id"],
        "source_cutoff_at": bundle["source_cutoff_at"],
        "accepted_rows": len(accepted),
        "quarantined_rows": len(quarantined),
        "disposition_rows": len(dispositions),
    }
    (output / "acquisition-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    validation = validate_acquisition_dataset(output)
    if validation:
        raise RuntimeError("reconciled dataset failed validation:\n" + "\n".join(validation))
    return manifest


def _seed_parent_active_data(
    connection: duckdb.DuckDBPyConnection,
    output: Path,
    parent: Path,
) -> None:
    """Preserve active fixtures and referenced identities from the pinned parent."""
    for name in ("fixtures.parquet", "players.parquet", "tournaments.parquet"):
        if not (parent / name).is_file():
            raise ValueError(f"pinned parent is missing {name}")
    fixtures_path = parent / "fixtures.parquet"
    players_path = parent / "players.parquet"
    tournaments_path = parent / "tournaments.parquet"
    for tour in TOURS:
        fixture_output = output / f"fixtures/tour={tour}/current.parquet"
        _copy_parquet(
            connection,
            f"SELECT * FROM read_parquet({_quoted(fixtures_path)}) "
            f"WHERE tour={_quoted(tour)} ORDER BY ALL",
            fixture_output,
            row_group_size=MATCH_ROW_GROUP_SIZE,
            match_shaped=True,
        )
        player_output = output / f"players/tour={tour}/players.parquet"
        player_temporary = player_output.with_name("players.parent-seed.parquet")
        _copy_parquet(
            connection,
            "SELECT * EXCLUDE(priority) FROM ("
            f"SELECT *,0 AS priority FROM read_parquet({_quoted(player_output)}) "
            "UNION ALL "
            f"SELECT *,1 AS priority FROM read_parquet({_quoted(players_path)}) "
            f"WHERE tour={_quoted(tour)}) "
            "QUALIFY row_number() OVER (PARTITION BY player_id ORDER BY priority)=1 "
            "ORDER BY ALL",
            player_temporary,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )
        player_temporary.replace(player_output)
    fixture_partitions = connection.execute(
        f"SELECT DISTINCT tour,year FROM read_parquet({_quoted(fixtures_path)}) "
        "ORDER BY tour,year"
    ).fetchall()
    for tour_value, year_value in fixture_partitions:
        tour = str(tour_value)
        year = int(year_value)
        tournament_output = (
            output
            / f"tournaments/tour={tour}/year={year}/tournaments.parquet"
        )
        parent_query = (
            f"SELECT t.* FROM read_parquet({_quoted(tournaments_path)}) t "
            f"WHERE t.tour={_quoted(tour)} AND t.year={year} AND EXISTS ("
            f"SELECT 1 FROM read_parquet({_quoted(fixtures_path)}) f "
            "WHERE f.tournament_id=t.tournament_id)"
        )
        if tournament_output.is_file():
            query = (
                "SELECT * EXCLUDE(priority) FROM ("
                f"SELECT *,0 AS priority FROM read_parquet({_quoted(tournament_output)}) "
                "UNION ALL "
                f"SELECT *,1 AS priority FROM ({parent_query})) "
                "QUALIFY row_number() OVER "
                "(PARTITION BY tournament_id ORDER BY priority)=1 ORDER BY ALL"
            )
        else:
            query = parent_query + " ORDER BY ALL"
        tournament_temporary = tournament_output.with_name(
            "tournaments.parent-seed.parquet"
        )
        _copy_parquet(
            connection,
            query,
            tournament_temporary,
            row_group_size=OBSERVATION_ROW_GROUP_SIZE,
        )
        tournament_temporary.replace(tournament_output)


def _normalized_pair(first: str, second: str) -> tuple[str, str]:
    values = sorted((_normalized(first), _normalized(second)))
    return values[0], values[1]


def _disposition(
    row: Mapping[str, Any], disposition: str, replacement: str | None, reason: str
) -> dict[str, Any]:
    return {
        "baseline_identity": row["source_match_id"],
        "disposition": disposition,
        "replacement_observation_id": replacement,
        "reason": reason,
        "reviewer": "automated-v3.2-gate",
        "reviewed_at": None,
    }


def _write_dataset_tables(
    connection: duckdb.DuckDBPyConnection,
    output: Path,
    *,
    accepted: Sequence[Mapping[str, Any]],
    quarantined: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    dispositions: Sequence[Mapping[str, Any]],
    bundle: Mapping[str, Any],
) -> None:
    by_partition: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for row in accepted:
        by_partition[(str(row["tour"]), int(row["source_year"]))].append(row)
    match_schema = list(zip(MATCH_COLUMNS, (
        "DATE", "VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR", "SMALLINT", "VARCHAR",
        "VARCHAR", "VARCHAR", "VARCHAR[]", "VARCHAR[]", "VARCHAR", "VARCHAR[]",
        "VARCHAR[]", "VARCHAR", "VARCHAR[]", "VARCHAR", "VARCHAR", "TINYINT",
        "VARCHAR[]",
    ), strict=True))
    player_rows: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    tournament_rows: dict[tuple[str, int], dict[str, tuple[Any, ...]]] = defaultdict(dict)
    observation_rows: dict[tuple[str, int], list[tuple[Any, ...]]] = defaultdict(list)
    date_rows: dict[tuple[str, int], list[tuple[Any, ...]]] = defaultdict(list)
    for (tour, year), rows in sorted(by_partition.items()):
        match_rows: list[tuple[Any, ...]] = []
        for row in rows:
            tournament_id = content_id(
                "tournament",
                {"tour": tour, "year": year, "name": _normalized(row["tournament"])},
                length=20,
            )
            winner_id = content_id(
                "player", {"tour": tour, "name": _normalized(row["winner"])}, length=20
            )
            loser_id = content_id(
                "player", {"tour": tour, "name": _normalized(row["loser"])}, length=20
            )
            player_rows[tour].update(
                {(winner_id, tour, row["winner"]), (loser_id, tour, row["loser"])}
            )
            played = date.fromisoformat(str(row["played_on"]))
            current_tournament = tournament_rows[(tour, year)].get(tournament_id)
            start_date = (
                min(current_tournament[7], played) if current_tournament else played
            )
            end_date = (
                max(current_tournament[8], played) if current_tournament else played
            )
            tournament_rows[(tour, year)][tournament_id] = (
                tournament_id,
                tour,
                year,
                row["tournament"],
                row["level"],
                row["surface"],
                False,
                start_date,
                end_date,
                row["location"] or None,
                None,
                next(
                    item["source_url"]
                    for item in sources
                    if item["source_file_id"] == row["source_file_id"]
                ),
            )
            match_rows.append(
                (
                    played,
                    row["match_id"],
                    tournament_id,
                    row["tournament"],
                    tour,
                    year,
                    "main",
                    row["round"],
                    "singles",
                    [winner_id],
                    [row["winner"]],
                    row["winner_seed"],
                    [loser_id],
                    [row["loser"]],
                    row["loser_seed"],
                    [winner_id],
                    row["status"],
                    row["score"],
                    row["best_of"],
                    ["tennis-data.co.uk"],
                )
            )
            observation_rows[(tour, year)].append(
                (
                    row["match_id"],
                    tour,
                    year,
                    row["source_file_id"],
                    row["source_match_id"],
                )
            )
            date_rows[(tour, year)].append(
                (
                    row["match_id"],
                    tour,
                    year,
                    played,
                    row["source_file_id"],
                    row["source_match_id"],
                    "day",
                    "event_round_participants_score",
                    row["row_fingerprint"],
                )
            )
        _write_table(
            connection,
            output / f"matches/tour={tour}/year={year}/matches.parquet",
            match_schema,
            match_rows,
            match_shaped=True,
        )
    for tour in TOURS:
        _write_table(
            connection,
            output / f"fixtures/tour={tour}/current.parquet",
            match_schema,
            [],
            match_shaped=True,
        )
        _write_table(
            connection,
            output / f"players/tour={tour}/players.parquet",
            (
                ("player_id", "VARCHAR"),
                ("tour", "VARCHAR"),
                ("name", "VARCHAR"),
                ("country", "VARCHAR"),
                ("birth_date", "DATE"),
                ("hand", "VARCHAR"),
                ("height_cm", "SMALLINT"),
                ("preferred_source", "VARCHAR"),
                ("preferred_source_player_id", "VARCHAR"),
            ),
            [
                (*row, None, None, None, None, None, None)
                for row in sorted(player_rows[tour])
            ],
        )
    tournament_schema = (
        ("tournament_id", "VARCHAR"), ("tour", "VARCHAR"), ("year", "SMALLINT"),
        ("tournament_name", "VARCHAR"), ("level", "VARCHAR"), ("surface", "VARCHAR"),
        ("indoor", "BOOLEAN"), ("start_date", "DATE"), ("end_date", "DATE"),
        ("city", "VARCHAR"), ("country", "VARCHAR"), ("source_url", "VARCHAR"),
    )
    observation_schema = (
        ("match_id", "VARCHAR"), ("tour", "VARCHAR"), ("year", "SMALLINT"),
        ("source_file_id", "VARCHAR"), ("source_match_id", "VARCHAR"),
    )
    date_schema = (
        ("match_id", "VARCHAR"), ("tour", "VARCHAR"), ("year", "SMALLINT"),
        ("played_on", "DATE"), ("source_file_id", "VARCHAR"),
        ("source_match_id", "VARCHAR"), ("date_precision", "VARCHAR"),
        ("match_method", "VARCHAR"), ("row_fingerprint", "VARCHAR"),
    )
    for partition, values in sorted(tournament_rows.items()):
        tour, year = partition
        _write_table(
            connection,
            output / f"tournaments/tour={tour}/year={year}/tournaments.parquet",
            tournament_schema,
            sorted(values.values()),
        )
        _write_table(
            connection,
            output / f"observations/tour={tour}/year={year}/observations.parquet",
            observation_schema,
            observation_rows[partition],
        )
        _write_table(
            connection,
            output
            / f"date_observations/tour={tour}/year={year}/date-observations.parquet",
            date_schema,
            date_rows[partition],
        )
    audit_schema = (
        ("source_file_id", "VARCHAR"), ("kind", "VARCHAR"), ("tour", "VARCHAR"),
        ("year", "SMALLINT"), ("source_label", "VARCHAR"), ("source_path", "VARCHAR"),
        ("source_url", "VARCHAR"), ("revision", "VARCHAR"), ("sha256", "VARCHAR"),
        ("license", "VARCHAR"), ("source_rows", "BIGINT"),
        ("normalized_rows", "BIGINT"), ("quarantined_rows", "BIGINT"),
    )
    _write_table(
        connection,
        output / "coverage/source-audit.parquet",
        audit_schema,
        [tuple(row[name] for name, _ in audit_schema) for row in sources],
    )
    quarantine_schema = (
        ("tour", "VARCHAR"), ("year", "SMALLINT"), ("source_label", "VARCHAR"),
        ("source_path", "VARCHAR"), ("source_file_id", "VARCHAR"),
        ("source_match_id", "VARCHAR"), ("row_fingerprint", "VARCHAR"),
        ("candidate_match_ids", "VARCHAR[]"), ("reason", "VARCHAR"),
    )
    _write_table(
        connection,
        output / "quarantine/quarantine.parquet",
        quarantine_schema,
        [tuple(row.get(name) for name, _ in quarantine_schema) for row in quarantined],
    )
    integrity = output / "integrity"
    artifact_schema = (
        ("evidence_artifact_id", "VARCHAR"), ("source_label", "VARCHAR"),
        ("source_url", "VARCHAR"), ("publisher_revision", "VARCHAR"),
        ("retrieved_at", "TIMESTAMPTZ"), ("publisher_modified_at", "TIMESTAMPTZ"),
        ("media_type", "VARCHAR"), ("byte_length", "BIGINT"), ("sha256", "VARCHAR"),
        ("storage_mode", "VARCHAR"), ("retention_basis", "VARCHAR"),
    )
    artifacts = []
    for item in bundle["artifacts"]:
        artifact_id = content_id("artifact", item)
        artifacts.append((
            artifact_id, item["source_label"], item["source_url"], None,
            item["retrieved_at"], None, item["media_type"], item["bytes"],
            item["sha256"], item["storage_mode"], "source-policy",
        ))
    _write_table(connection, integrity / "evidence_artifacts.parquet", artifact_schema, artifacts)
    evaluation_schema = (
        ("evaluation_id", "VARCHAR"), ("evidence_artifact_id", "VARCHAR"),
        ("policy_revision", "VARCHAR"), ("evaluated_at", "TIMESTAMPTZ"),
        ("eligible_for_public_projection", "BOOLEAN"), ("allowed_fields", "VARCHAR[]"),
        ("allowed_roles", "VARCHAR[]"), ("reason", "VARCHAR"),
        ("supersedes_evaluation_id", "VARCHAR"),
    )
    evaluations = [
        (
            content_id("evaluation", {"artifact": row[0], "policy": "v3.2"}),
            row[0], "v3.2-acquisition-2026-07-28", bundle["source_cutoff_at"], True,
            ["date", "participants", "round", "result", "score", "tournament"],
            ["historical_terminal"], "eligible_tennis_data_observation", None,
        )
        for row in artifacts
    ]
    _write_table(
        connection,
        integrity / "evidence_artifact_evaluations.parquet",
        evaluation_schema,
        evaluations,
    )
    disposition_schema = (
        ("baseline_identity", "VARCHAR"), ("disposition", "VARCHAR"),
        ("replacement_observation_id", "VARCHAR"), ("reason", "VARCHAR"),
        ("reviewer", "VARCHAR"), ("reviewed_at", "TIMESTAMPTZ"),
    )
    _write_table(
        connection,
        integrity / "migration_dispositions.parquet",
        disposition_schema,
        [tuple(row.get(name) for name, _ in disposition_schema) for row in dispositions],
    )


def validate_acquisition_dataset(root: Path) -> list[str]:
    required = [
        root / "acquisition-manifest.json",
        root / "catalog/catalog.parquet",
        root / "coverage/source-audit.parquet",
        root / "quarantine/quarantine.parquet",
        root / "integrity/evidence_artifacts.parquet",
        root / "integrity/evidence_artifact_evaluations.parquet",
        root / "integrity/migration_dispositions.parquet",
    ]
    errors = [f"missing acquisition dataset file: {path.relative_to(root)}" for path in required if not path.is_file()]
    matches = sorted(root.glob("matches/tour=*/year=*/matches.parquet"))
    dates = sorted(root.glob("date_observations/tour=*/year=*/date-observations.parquet"))
    if not matches:
        errors.append("acquisition dataset contains no accepted matches")
    if errors:
        return errors
    connection = duckdb.connect()
    try:
        match_expr = "[" + ",".join("'" + str(path).replace("'", "''") + "'" for path in matches) + "]"
        date_expr = "[" + ",".join("'" + str(path).replace("'", "''") + "'" for path in dates) + "]"
        duplicate_ids = _required_row(connection.execute(
            f"SELECT count(*)-count(DISTINCT match_id) FROM read_parquet({match_expr})"
        ))[0]
        if duplicate_ids:
            errors.append(f"duplicate acquisition match IDs: {duplicate_ids}")
        missing_dates = _required_row(connection.execute(
            f"SELECT count(*) FROM read_parquet({match_expr}) m WHERE NOT EXISTS "
            f"(SELECT 1 FROM read_parquet({date_expr}) d WHERE d.match_id=m.match_id "
            "AND d.played_on=m.date)"
        ))[0]
        if missing_dates:
            errors.append(f"accepted matches without date evidence: {missing_dates}")
        source_rows, normalized_rows, rejected_rows = _required_row(connection.execute(
            f"SELECT sum(source_rows),sum(normalized_rows),sum(quarantined_rows) "
            f"FROM read_parquet('{str(root / 'coverage/source-audit.parquet')}')"
        ))
        if source_rows != normalized_rows + rejected_rows:
            errors.append("source row accounting is incomplete")
        disposition_count = _required_row(connection.execute(
            f"SELECT count(*) FROM read_parquet("
            f"'{str(root / 'integrity/migration_dispositions.parquet')}')"
        ))[0]
        if disposition_count != source_rows:
            errors.append("migration disposition count does not equal source rows")
    except duckdb.Error as exc:
        errors.append(f"acquisition validation query failed: {exc}")
    finally:
        connection.close()
    return errors
