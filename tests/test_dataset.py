from __future__ import annotations

import csv
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import duckdb

from open_tennis_data.dataset import (
    SourceFile,
    _copy_parquet,
    _create_catalog,
    _create_match_tables,
    _create_source_file_table,
    _remote_audit_revisions,
    _reuse_match_ids,
    _reuse_player_ids,
    _reuse_tournament_ids,
    _write_audit_report,
    add_correction,
    audit_retroactive_dataset,
    bootstrap_dataset,
    create_direct_downloads,
    download_sources,
    extract_dataset,
    parse_years,
    query_dataset,
    validate_dataset,
)
from open_tennis_data.schema import MATCH_COLUMNS, SCHEMA_METADATA_KEY, SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class DatasetTests(unittest.TestCase):
    def rebuild_test_catalog(self, root: Path) -> None:
        catalog = root / "catalog/catalog.parquet"
        connection = duckdb.connect()
        as_of, revision = connection.execute(
            f"SELECT as_of, source_revision FROM read_parquet('{catalog}') LIMIT 1"
        ).fetchone()
        connection.close()
        catalog.unlink()
        connection = duckdb.connect()
        _create_catalog(connection, root, as_of, revision)
        connection.close()

    def replace_test_parquet(self, path: Path, query: str) -> None:
        temporary = path.with_suffix(".replacement.parquet")
        connection = duckdb.connect()
        connection.execute(f"COPY ({query}) TO '{temporary}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        connection.close()
        os.replace(temporary, path)

    def test_year_parser(self) -> None:
        self.assertEqual(parse_years("2020,2022:2024"), [2020, 2022, 2023, 2024])
        with self.assertRaises(ValueError):
            parse_years("1967")

    def test_download_rejects_an_invalid_explicit_source_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "40-character lowercase Git SHA"):
                download_sources(
                    Path(temporary), [2026], revision="moving-main", workers=1
                )

    def test_bootstrap_refuses_an_initialized_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "existing.parquet").touch()
            with self.assertRaisesRegex(ValueError, "empty, uninitialized"):
                bootstrap_dataset(
                    root, through_year=2026, as_of=date(2026, 7, 16), workers=1
                )

    def test_incremental_refresh_reuses_established_tournament_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            existing = base / "existing"
            generated = base / "generated"
            old_sources = existing / "identity/tournament-sources.parquet"
            new_sources = generated / "identity/tournament-sources.parquet"
            old_sources.parent.mkdir(parents=True)
            new_sources.parent.mkdir(parents=True)
            tournament = generated / "tournaments/tour=atp/year=2026/tournaments.parquet"
            fixture = generated / "fixtures/tour=atp/current.parquet"
            tournament.parent.mkdir(parents=True)
            fixture.parent.mkdir(parents=True)
            connection = duckdb.connect()
            source_columns = (
                "source,source_tournament_id,tournament_id,tour,year,source_url"
            )
            connection.execute(
                f"COPY (SELECT 'wikimedia' AS \"source\", 'Q1' AS source_tournament_id, "
                f"'established' AS tournament_id, 'atp' AS tour, 2026 AS year, "
                f"'https://example.org' AS source_url) TO '{old_sources}' (FORMAT PARQUET)"
            )
            connection.execute(
                f"COPY (SELECT 'wikimedia' AS \"source\", 'Q1' AS source_tournament_id, "
                f"'generated' AS tournament_id, 'atp' AS tour, 2026 AS year, "
                f"'https://example.org' AS source_url) TO '{new_sources}' (FORMAT PARQUET)"
            )
            connection.execute(
                f"COPY (SELECT 'generated' tournament_id, 'Test' tournament_name) "
                f"TO '{tournament}' (FORMAT PARQUET)"
            )
            connection.execute(
                f"COPY (SELECT 'generated' tournament_id, 'match_fixture' match_id) "
                f"TO '{fixture}' (FORMAT PARQUET)"
            )
            connection.close()
            self.assertEqual(_reuse_tournament_ids(generated, existing, [2026]), 1)
            connection = duckdb.connect()
            self.assertEqual(
                connection.execute(
                    f"SELECT tournament_id FROM read_parquet('{tournament}')"
                ).fetchone()[0],
                "established",
            )
            self.assertEqual(
                connection.execute(
                    f"SELECT tournament_id FROM read_parquet('{fixture}')"
                ).fetchone()[0],
                "established",
            )
            self.assertEqual(
                connection.execute(
                    f"SELECT {source_columns} FROM read_parquet('{new_sources}')"
                ).fetchone()[2],
                "established",
            )
            connection.close()

    def test_incremental_refresh_reuses_fixture_lifecycle_match_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            existing = base / "existing"
            generated = base / "generated"
            old_observation = existing / "observations/tour=atp/year=2026/observations.parquet"
            new_observation = generated / "observations/tour=atp/year=2026/observations.parquet"
            old_audit = existing / "coverage/source-audit.parquet"
            new_audit = generated / "coverage/source-audit.parquet"
            fixture = generated / "fixtures/tour=atp/current.parquet"
            for path in (old_observation, new_observation, old_audit, new_audit, fixture):
                path.parent.mkdir(parents=True, exist_ok=True)
            connection = duckdb.connect()
            for path, match_id, source_file_id in (
                (old_observation, "match_established", "old-source"),
                (new_observation, "match_generated", "new-source"),
            ):
                connection.execute(
                    f"COPY (SELECT '{match_id}'::VARCHAR match_id, 'atp'::VARCHAR tour, "
                    f"2026::SMALLINT AS year, '{source_file_id}'::VARCHAR source_file_id, "
                    f"'page:slot'::VARCHAR source_match_id) TO '{path}' (FORMAT PARQUET)"
                )
            for path, source_file_id in (
                (old_audit, "old-source"),
                (new_audit, "new-source"),
            ):
                connection.execute(
                    f"COPY (SELECT '{source_file_id}'::VARCHAR source_file_id, "
                    f"'wikimedia'::VARCHAR source_label, "
                    f"'https://example.org/draw'::VARCHAR source_url) "
                    f"TO '{path}' (FORMAT PARQUET)"
                )
            connection.execute(
                f"COPY (SELECT * FROM (VALUES "
                f"(DATE '2026-07-02','tournament_2','main','R16','match_unchanged'),"
                f"(DATE '2026-07-01','tournament_1','main','R32','match_generated')) "
                f"fixture(date,tournament_id,draw,round,match_id)) "
                f"TO '{fixture}' (FORMAT PARQUET)"
            )
            connection.close()
            self.assertEqual(_reuse_match_ids(generated, existing, [2026]), 1)
            connection = duckdb.connect()
            self.assertEqual(
                connection.execute(
                    f"SELECT match_id FROM read_parquet('{fixture}', "
                    "hive_partitioning=false)"
                ).fetchall(),
                [("match_established",), ("match_unchanged",)],
            )
            self.assertEqual(
                connection.execute(
                    f"SELECT match_id FROM read_parquet('{new_observation}')"
                ).fetchone()[0],
                "match_established",
            )
            connection.close()

    def test_incremental_refresh_reuses_established_player_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            existing = base / "existing"
            generated = base / "generated"
            old_links = existing / "identity/player-links.parquet"
            new_links = generated / "identity/player-links.parquet"
            players = generated / "players/tour=atp/players.parquet"
            for path in (old_links, new_links, players):
                path.parent.mkdir(parents=True, exist_ok=True)
            connection = duckdb.connect()
            for path, player_id in (
                (old_links, "player_established"),
                (new_links, "player_generated"),
            ):
                connection.execute(
                    f"COPY (SELECT 'wikimedia'::VARCHAR AS \"source\", 'Q1'::VARCHAR "
                    f"source_player_id, '{player_id}'::VARCHAR player_id, "
                    f"'atp'::VARCHAR tour, false provisional) TO '{path}' (FORMAT PARQUET)"
                )
            connection.execute(
                f"COPY (SELECT 'player_generated'::VARCHAR AS player_id, "
                f"'atp'::VARCHAR AS tour, 'Corrected Name'::VARCHAR AS name) "
                f"TO '{players}' (FORMAT PARQUET)"
            )
            connection.close()
            self.assertEqual(_reuse_player_ids(generated, existing), 1)
            connection = duckdb.connect()
            self.assertEqual(
                connection.execute(f"SELECT player_id FROM read_parquet('{players}')").fetchone()[0],
                "player_established",
            )
            self.assertEqual(
                connection.execute(f"SELECT player_id FROM read_parquet('{new_links}')").fetchone()[0],
                "player_established",
            )
            connection.close()

    def test_match_ingestion_quarantines_each_bad_source_row_once(self) -> None:
        columns = [
            "tourney_id",
            "tourney_name",
            "surface",
            "draw_size",
            "tourney_level",
            "tourney_date",
            "match_num",
            "winner_id",
            "winner_seed",
            "winner_entry",
            "winner_name",
            "winner_ioc",
            "loser_id",
            "loser_seed",
            "loser_entry",
            "loser_name",
            "loser_ioc",
            "score",
            "best_of",
            "round",
            "minutes",
            "winner_rank",
            "winner_rank_points",
            "loser_rank",
            "loser_rank_points",
            "w_ace",
            "w_df",
            "w_svpt",
            "w_1stIn",
            "w_1stWon",
            "w_2ndWon",
            "w_SvGms",
            "w_bpSaved",
            "w_bpFaced",
            "l_ace",
            "l_df",
            "l_svpt",
            "l_1stIn",
            "l_1stWon",
            "l_2ndWon",
            "l_SvGms",
            "l_bpSaved",
            "l_bpFaced",
        ]
        valid = {
            "tourney_id": "2026-001",
            "tourney_name": "Test Open",
            "surface": "Hard",
            "draw_size": "32",
            "tourney_level": "A",
            "tourney_date": "20260712",
            "match_num": "1",
            "winner_id": "1",
            "winner_name": "Winner",
            "loser_id": "2",
            "loser_name": "Loser",
            "score": "6-4 6-4",
            "best_of": "3",
            "round": "R32",
            "w_svpt": "50",
            "w_1stIn": "30",
            "w_1stWon": "20",
            "w_bpSaved": "2",
            "w_bpFaced": "3",
            "l_svpt": "50",
            "l_1stIn": "30",
            "l_1stWon": "20",
            "l_bpSaved": "2",
            "l_bpFaced": "3",
        }
        self_match = {**valid, "match_num": "2", "loser_id": "1", "loser_name": "Winner"}
        negative = {**valid, "match_num": "3", "l_bpSaved": "-1"}
        impossible = {**valid, "match_num": "4", "w_1stWon": "31"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matches.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows([valid, valid, self_match, negative, impossible])
            source = SourceFile(
                kind="matches",
                tour="atp",
                year=2026,
                label="tour",
                source_path="atp/atp_matches_2026.csv",
                local_path=path.resolve(),
                url="https://example.org/matches.csv",
                revision="a" * 40,
                sha256="b" * 64,
            )
            connection = duckdb.connect()
            _create_source_file_table(connection, [source])
            _create_match_tables(connection, [source], date(2026, 7, 12))
            self.assertEqual(connection.execute("SELECT count(*) FROM matches").fetchone()[0], 1)
            self.assertEqual(
                dict(
                    connection.execute(
                        "SELECT reason, count(*) FROM quarantine GROUP BY reason"
                    ).fetchall()
                ),
                {
                    "duplicate_source_row": 1,
                    "invalid_participants": 1,
                    "invalid_statistics": 2,
                },
            )
            connection.close()

    def test_repository_dataset_validates(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated dataset is not present")
        self.assertEqual(validate_dataset(DATA), [])

    def test_validator_labels_corrupt_rows_by_tour_and_year(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            shutil.copytree(DATA, root, copy_function=os.link)
            matches = root / "matches/tour=atp/year=1969/matches.parquet"
            statistics = root / "match_stats/tour=atp/year=1991/match-stats.parquet"
            coverage = root / "coverage/coverage.parquet"
            self.replace_test_parquet(
                matches,
                f"SELECT * REPLACE (player1_id AS player2_id) FROM read_parquet('{matches}')",
            )
            self.replace_test_parquet(
                statistics,
                f"SELECT * REPLACE (-1 AS player1_aces) FROM read_parquet('{statistics}')",
            )
            self.replace_test_parquet(
                coverage,
                f"SELECT * REPLACE (CASE WHEN tour='atp' AND year=1969 "
                f"THEN row_count+1 ELSE row_count END AS row_count) "
                f"FROM read_parquet('{coverage}')",
            )
            self.rebuild_test_catalog(root)
            errors = validate_dataset(root)
            self.assertTrue(
                any(error.startswith("matches invalid participants atp/1969:") for error in errors)
            )
            self.assertTrue(
                any(error.startswith("statistics invalid values atp/1991:") for error in errors)
            )
            self.assertTrue(
                any(error.startswith("coverage does not match canonical tables:") for error in errors)
            )

    def test_validator_rejects_invalid_lists_placeholders_and_winners(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            shutil.copytree(DATA, root, copy_function=os.link)
            matches = root / "matches/tour=atp/year=1969/matches.parquet"
            replacement = matches.with_suffix(".replacement.parquet")
            connection = duckdb.connect()
            _copy_parquet(
                connection,
                f"""
                WITH numbered AS (
                  SELECT *, row_number() OVER (ORDER BY match_id) rn
                  FROM read_parquet('{matches}', hive_partitioning=false)
                )
                SELECT * EXCLUDE(rn) REPLACE (
                  CASE rn
                    WHEN 1 THEN []::VARCHAR[]
                    WHEN 3 THEN [player1_id[1],player1_id[1]]::VARCHAR[]
                    WHEN 4 THEN [player1_id[1],'player_overlap']::VARCHAR[]
                    ELSE player1_id END AS player1_id,
                  CASE rn
                    WHEN 1 THEN []::VARCHAR[]
                    WHEN 2 THEN ['TBD']::VARCHAR[]
                    WHEN 3 THEN [player1_name[1],player1_name[1]]::VARCHAR[]
                    WHEN 4 THEN [player1_name[1],'Overlap Partner']::VARCHAR[]
                    ELSE player1_name END AS player1_name,
                  CASE rn
                    WHEN 3 THEN [player2_id[1],'player_other']::VARCHAR[]
                    WHEN 4 THEN [player1_id[1],'player_opponent']::VARCHAR[]
                    ELSE player2_id END AS player2_id,
                  CASE rn
                    WHEN 3 THEN [player2_name[1],'Other Partner']::VARCHAR[]
                    WHEN 4 THEN [player1_name[1],'Opponent Partner']::VARCHAR[]
                    ELSE player2_name END AS player2_name,
                  CASE rn
                    WHEN 3 THEN [player1_id[1],player1_id[1]]::VARCHAR[]
                    WHEN 4 THEN [player1_id[1],'player_overlap']::VARCHAR[]
                    WHEN 5 THEN ['player_not_in_match']::VARCHAR[]
                    ELSE winner_id END AS winner_id,
                  CASE WHEN rn IN (3,4) THEN 'doubles' ELSE format END AS format
                ) FROM numbered
                ORDER BY date NULLS LAST,tournament_id,draw,round,match_id
                """,
                replacement,
                row_group_size=65_536,
                match_shaped=True,
            )
            connection.close()
            os.replace(replacement, matches)
            self.rebuild_test_catalog(root)
            errors = validate_dataset(root)
            self.assertTrue(any("invalid match participants" in error for error in errors))
            self.assertTrue(any("invalid match participant text" in error for error in errors))

    def test_validator_detects_catalog_accounting_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            shutil.copytree(DATA, root, copy_function=os.link)
            catalog = root / "catalog/catalog.parquet"
            self.replace_test_parquet(
                catalog,
                f"SELECT * REPLACE (CASE WHEN path="
                f"'matches/tour=atp/year=1968/matches.parquet' THEN row_count+1 "
                f"ELSE row_count END AS row_count) FROM read_parquet('{catalog}')",
            )
            errors = validate_dataset(root)
            self.assertIn(
                "catalog row count mismatch: matches/tour=atp/year=1968/matches.parquet",
                errors,
            )

    def test_validator_rejects_health_provenance_and_quarantine_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)

            health_root = base / "health-data"
            shutil.copytree(DATA, health_root, copy_function=os.link)
            health = health_root / "health/health.parquet"
            self.replace_test_parquet(
                health,
                f"SELECT * REPLACE (as_of + INTERVAL 1 DAY AS as_of) "
                f"FROM read_parquet('{health}')",
            )
            self.rebuild_test_catalog(health_root)
            self.assertTrue(
                any(
                    "health does not match catalog and canonical tables" in error
                    for error in validate_dataset(health_root)
                )
            )

            provenance_root = base / "provenance-data"
            shutil.copytree(DATA, provenance_root, copy_function=os.link)
            observations = (
                provenance_root
                / "observations/tour=atp/year=1968/observations.parquet"
            )
            self.replace_test_parquet(
                observations,
                f"SELECT * FROM read_parquet('{observations}', hive_partitioning=false) "
                "QUALIFY row_number() OVER (ORDER BY match_id)>1",
            )
            self.rebuild_test_catalog(provenance_root)
            self.assertTrue(
                any(
                    "canonical matches without provenance evidence" in error
                    for error in validate_dataset(provenance_root)
                )
            )

            quarantine_root = base / "quarantine-data"
            shutil.copytree(DATA, quarantine_root, copy_function=os.link)
            quarantine = quarantine_root / "quarantine/quarantine.parquet"
            self.replace_test_parquet(
                quarantine,
                f"WITH numbered AS (SELECT *,row_number() OVER (ORDER BY tour,year,"
                f"source_match_id) rn FROM read_parquet('{quarantine}')) "
                "SELECT * EXCLUDE(rn) REPLACE(CASE WHEN reason='ambiguous_source_mapping' "
                "AND rn=(SELECT min(rn) FROM numbered WHERE reason='ambiguous_source_mapping') "
                "THEN ['missing-match-id']::VARCHAR[] ELSE candidate_match_ids END "
                "AS candidate_match_ids) FROM numbered",
            )
            self.rebuild_test_catalog(quarantine_root)
            self.assertTrue(
                any(
                    "invalid quarantine candidate IDs" in error
                    for error in validate_dataset(quarantine_root)
                )
            )

    def test_validator_rejects_changes_before_incremental_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            shutil.copytree(DATA, root, copy_function=os.link)
            baseline = Path(temporary) / "baseline.parquet"
            shutil.copy2(root / "catalog/catalog.parquet", baseline)
            matches = root / "matches/tour=atp/year=2024/matches.parquet"
            self.replace_test_parquet(
                matches,
                f"SELECT * REPLACE ('migration-test' AS score) "
                f"FROM read_parquet('{matches}')",
            )
            self.rebuild_test_catalog(root)
            errors = validate_dataset(
                root, baseline_catalog=baseline, immutable_before_year=2026
            )
            self.assertIn(
                "immutable historical partition changed: "
                "matches/tour=atp/year=2024/matches.parquet",
                errors,
            )

    def test_query_prunes_tour_and_year(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated dataset is not present")
        columns, rows = query_dataset(
            DATA,
            "SELECT min(tour), max(tour), min(year), max(year), count(*) FROM matches",
            tours=["atp"],
            years=[2023],
        )
        self.assertEqual(columns[-1], "count_star()")
        self.assertEqual(rows[0][:4], ("atp", "atp", 2023, 2023))
        self.assertGreater(rows[0][4], 30_000)

    def test_repeated_futures_events_are_distinct(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated dataset is not present")
        _, rows = query_dataset(
            DATA,
            """
            SELECT count(DISTINCT tournament_id)
            FROM tournaments
            WHERE tour='atp' AND year=2023 AND lower(tournament_name)='m15 monastir'
            """,
            tours=["atp"],
            years=[2023],
        )
        self.assertGreater(rows[0][0], 40)

    def test_statistics_and_date_semantics(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated dataset is not present")
        _, rows = query_dataset(
            DATA,
            "SELECT (SELECT count(*) FROM match_stats), "
            "(SELECT count(*) FROM tournaments WHERE end_date < start_date)",
        )
        self.assertGreater(rows[0][0], 300_000)
        self.assertEqual(rows[0][1], 0)

    def test_extract_has_v32_version_metadata(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated dataset is not present")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "subset.parquet"
            count = extract_dataset(DATA, output, tours=["wta"], years=[2023], levels=["itf"])
            self.assertGreater(count, 20_000)
            connection = duckdb.connect()
            metadata = dict(
                (key.decode(), value.decode())
                for _, key, value in connection.execute(
                    f"SELECT * FROM parquet_kv_metadata('{output}')"
                ).fetchall()
            )
            self.assertEqual(metadata, {SCHEMA_METADATA_KEY: SCHEMA_VERSION})
            self.assertEqual(
                [row[0] for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{output}')"
                ).fetchall()],
                list(MATCH_COLUMNS),
            )

    def test_direct_downloads_include_matches_and_fixtures(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated dataset is not present")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            output = Path(temporary) / "downloads"
            future_output = Path(temporary) / "future-downloads"
            connection = duckdb.connect()
            as_of = connection.execute(
                f"SELECT as_of FROM read_parquet('{DATA / 'catalog/catalog.parquet'}') LIMIT 1"
            ).fetchone()[0]
            past = (as_of - timedelta(days=1)).isoformat()
            future = (as_of + timedelta(days=1)).isoformat()
            for tour in ("atp", "wta"):
                match_output = root / "matches" / f"tour={tour}" / "year=2026" / "matches.parquet"
                fixture_output = root / "fixtures" / f"tour={tour}" / "current.parquet"
                tournament_output = (
                    root
                    / "tournaments"
                    / f"tour={tour}"
                    / "year=2026"
                    / "tournaments.parquet"
                )
                observation_output = (
                    root
                    / "observations"
                    / f"tour={tour}"
                    / "year=2026"
                    / "observations.parquet"
                )
                match_output.parent.mkdir(parents=True, exist_ok=True)
                fixture_output.parent.mkdir(parents=True, exist_ok=True)
                tournament_output.parent.mkdir(parents=True, exist_ok=True)
                observation_output.parent.mkdir(parents=True, exist_ok=True)
                connection.execute(
                    f"COPY (SELECT * FROM read_parquet('{DATA / 'matches' / f'tour={tour}' / 'year=2026' / 'matches.parquet'}') LIMIT 10) "
                    f"TO '{match_output}' (FORMAT PARQUET)"
                )
                connection.execute(
                    f"COPY (WITH numbered AS (SELECT *, row_number() OVER (ORDER BY match_id) AS rn "
                    f"FROM read_parquet('{DATA / 'fixtures' / f'tour={tour}' / 'current.parquet'}') LIMIT 4) "
                    f"SELECT * EXCLUDE (rn) REPLACE (CASE rn WHEN 1 THEN DATE '{past}' "
                    f"WHEN 2 THEN DATE '{as_of}' WHEN 3 THEN DATE '{future}' "
                    f"ELSE NULL END AS date) FROM numbered) "
                    f"TO '{fixture_output}' (FORMAT PARQUET)"
                )
                connection.execute(
                    f"COPY (SELECT * FROM read_parquet("
                    f"'{DATA / 'tournaments' / f'tour={tour}' / 'year=2026' / 'tournaments.parquet'}')) "
                    f"TO '{tournament_output}' (FORMAT PARQUET)"
                )
                shutil.copy2(
                    DATA / "observations" / f"tour={tour}" / "year=2026" / "observations.parquet",
                    observation_output,
                )
            (root / "catalog").mkdir(parents=True)
            (root / "coverage").mkdir(parents=True)
            shutil.copy2(DATA / "catalog" / "catalog.parquet", root / "catalog/catalog.parquet")
            shutil.copy2(
                DATA / "coverage/source-audit.parquet",
                root / "coverage/source-audit.parquet",
            )

            summary = create_direct_downloads(root, output)
            self.assertEqual(
                set(summary),
                {
                    "mens.parquet",
                    "womens.parquet",
                    "atp.parquet",
                    "wta.parquet",
                    "all-matches.parquet",
                    "tournaments.parquet",
                    "provenance.parquet",
                    "sources.parquet",
                },
            )
            self.assertEqual(
                (output / "atp.parquet").read_bytes(),
                (output / "mens.parquet").read_bytes(),
            )
            self.assertEqual(
                (output / "wta.parquet").read_bytes(),
                (output / "womens.parquet").read_bytes(),
            )
            rows, tours = connection.execute(
                f"SELECT count(*), count(DISTINCT tour) "
                f"FROM read_parquet('{output / 'all-matches.parquet'}')"
            ).fetchone()
            self.assertEqual(rows, 20)
            self.assertEqual(tours, 2)
            metadata = dict(
                (key.decode(), value.decode())
                for _, key, value in connection.execute(
                    f"SELECT * FROM parquet_kv_metadata('{output / 'all-matches.parquet'}')"
                ).fetchall()
            )
            self.assertEqual(metadata, {SCHEMA_METADATA_KEY: SCHEMA_VERSION})

            future_summary = create_direct_downloads(root, future_output, future_only=True)
            self.assertEqual(set(future_summary), set(summary))
            future_rows, future_tours, past_dates, undated = connection.execute(
                f"SELECT count(*), count(DISTINCT tour), "
                f"count(*) FILTER (WHERE date < DATE '{as_of}'), "
                f"count(*) FILTER (WHERE date IS NULL) "
                f"FROM read_parquet('{future_output / 'all-matches.parquet'}')"
            ).fetchone()
            self.assertEqual(future_rows, 6)
            self.assertEqual(future_tours, 2)
            self.assertEqual(past_dates, 0)
            self.assertEqual(undated, 2)

    def test_correction_is_deterministic_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrections.parquet"
            first = add_correction(
                path,
                entity_type="match",
                entity_id="match:atp:test",
                field="score",
                corrected_value="6-4 6-4",
                source_url="https://example.org/result",
                contributor="tester",
                contributed_on=date(2026, 7, 12),
            )
            second = add_correction(
                path,
                entity_type="match",
                entity_id="match:atp:test",
                field="score",
                corrected_value="6-4 6-4",
                source_url="https://example.org/result",
                contributor="tester",
                contributed_on=date(2026, 7, 12),
            )
            self.assertEqual(first, second)
            self.assertEqual(
                duckdb.connect()
                .execute(f"SELECT count(*) FROM read_parquet('{path}')")
                .fetchone()[0],
                1,
            )

    def test_retroactive_audit_no_change_writes_artifacts_without_mutation(self) -> None:
        catalog = DATA / "catalog/catalog.parquet"
        before = catalog.read_bytes()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "open_tennis_data.dataset._remote_audit_revisions",
            return_value=(False, []),
        ):
            output = Path(temporary)
            report = audit_retroactive_dataset(
                DATA, output, as_of=date(2026, 7, 16), workers=1
            )
            self.assertEqual(report["status"], "no_change")
            self.assertEqual(report["changed_files"], 0)
            self.assertTrue((output / "retroactive-audit.json").exists())
            self.assertTrue((output / "retroactive-audit.md").exists())
        self.assertEqual(catalog.read_bytes(), before)

    def test_retroactive_revision_probe_detects_changed_sources(self) -> None:
        def pages(year: int, tour: str) -> list[str]:
            return [f"{year} Test Open – Singles"] if year == 2026 else []

        with patch(
            "open_tennis_data.dataset.resolve_archive_revision", return_value="f" * 40
        ), patch(
            "open_tennis_data.sources.wikimedia.discover_pages", side_effect=pages
        ), patch(
            "open_tennis_data.sources.wikimedia.fetch_page_revisions",
            return_value={
                "2026 Test Open – Singles": "999",
                "2026 Test Open": "998",
            },
        ):
            changed, sources = _remote_audit_revisions(
                DATA, [2025, 2026], [2026, 2027]
            )
        self.assertTrue(changed)
        self.assertTrue(any(item["kind"] == "archive" for item in sources))
        self.assertTrue(any(item["kind"] == "fixtures" for item in sources))
        self.assertTrue(any(item["kind"] == "tournaments" for item in sources))

    def test_failed_audit_report_includes_source_revision_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            _write_audit_report(
                output,
                {
                    "as_of": "2026-07-16",
                    "status": "failed",
                    "result_years": [2025, 2026],
                    "fixture_years": [2026, 2027],
                    "source_changes": [
                        {
                            "source": "test-page",
                            "old_revision": "1",
                            "new_revision": "2",
                            "old_checksum": "a",
                            "new_checksum": "b",
                        }
                    ],
                    "changed_files": 0,
                    "older_partitions_unchanged": False,
                    "error": "invalid staged input",
                },
            )
            markdown = (output / "retroactive-audit.md").read_text(encoding="utf-8")
            self.assertIn("invalid staged input", markdown)
            self.assertIn("test-page", markdown)
            self.assertTrue((output / "retroactive-audit.json").exists())


if __name__ == "__main__":
    unittest.main()
