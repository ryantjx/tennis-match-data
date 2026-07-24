from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb

from open_tennis_data.dataset import (
    SourceFile,
    _create_match_tables,
    _create_source_file_table,
    _reuse_match_ids,
    _reuse_player_ids,
    _reuse_tournament_ids,
    _source_specs,
    _write_audit_report,
    add_correction,
    bootstrap_dataset,
    download_sources,
    parse_years,
)


class DatasetTests(unittest.TestCase):
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

    def test_v3_source_plan_is_top_level_matches_and_players_only(self) -> None:
        sources = _source_specs(
            [2025],
            top_level_only=True,
        )
        self.assertEqual(
            {
                (kind, tour, label)
                for kind, tour, _, label, _ in sources
            },
            {
                ("players", "atp", "players"),
                ("players", "wta", "players"),
                ("matches", "atp", "tour"),
                ("matches", "wta", "tour"),
            },
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
        semantic_duplicate = {**valid, "match_num": "5"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matches.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows([valid, valid, self_match, semantic_duplicate])
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
            self.assertEqual(connection.execute("SELECT count(*) FROM observations").fetchone()[0], 2)
            observation_source_id = connection.execute(
                "SELECT 'source_file_' || substr(sha256(concat_ws('|', source_label, "
                "source_url, revision, source_sha256, 'matches', tour)), 1, 20) "
                "FROM observations LIMIT 1"
            ).fetchone()[0]
            raw_source_id = connection.execute(
                "SELECT source_file_id FROM raw_matches LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(observation_source_id, raw_source_id)
            self.assertEqual(
                dict(
                    connection.execute(
                        "SELECT reason, count(*) FROM quarantine GROUP BY reason"
                    ).fetchall()
                ),
                {
                    "duplicate_source_row": 1,
                    "invalid_participants": 1,
                },
            )
            connection.close()

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
