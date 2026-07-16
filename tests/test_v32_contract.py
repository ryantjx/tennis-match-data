from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import duckdb

from open_tennis_data.dataset import MATCH_ROW_GROUP_SIZE, _copy_parquet
from open_tennis_data.model import normalize_participant, source_slot_match_id
from open_tennis_data.schema import (
    MATCH_COLUMNS,
    MATCH_SCHEMA,
    SCHEMA_METADATA_KEY,
    SCHEMA_VERSION,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class V32ContractTests(unittest.TestCase):
    def test_completed_and_future_have_the_exact_shared_schema(self) -> None:
        connection = duckdb.connect()
        match = DATA / "matches/tour=atp/year=2026/matches.parquet"
        fixture = DATA / "fixtures/tour=atp/current.parquet"
        schemas = []
        for path in (match, fixture):
            schemas.append(
                connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{path}', hive_partitioning=false)"
                ).fetchall()
            )
            metadata = {
                key.decode(): value.decode()
                for _, key, value in connection.execute(
                    f"SELECT * FROM parquet_kv_metadata('{path}')"
                ).fetchall()
            }
            self.assertEqual(metadata, {SCHEMA_METADATA_KEY: SCHEMA_VERSION})
        self.assertEqual(schemas[0], schemas[1])
        self.assertEqual([(row[0], row[1]) for row in schemas[0]], list(MATCH_SCHEMA))
        self.assertEqual([row[0] for row in schemas[0]], list(MATCH_COLUMNS))
        self.assertEqual(
            connection.execute(
                f"SELECT count(*) FROM (SELECT * FROM read_parquet('{match}', "
                f"hive_partitioning=false) UNION ALL SELECT * FROM read_parquet('{fixture}', "
                "hive_partitioning=false))"
            ).fetchone()[0],
            connection.execute(f"SELECT count(*) FROM read_parquet('{match}')").fetchone()[0]
            + connection.execute(f"SELECT count(*) FROM read_parquet('{fixture}')").fetchone()[0],
        )

    def test_match_rows_remove_rank_country_and_source_url(self) -> None:
        removed = {
            "fixture_id",
            "scheduled_on",
            "source_url",
            "loser_id",
            "player1_country",
            "player2_country",
            "player1_rank",
            "player2_rank",
            "player1_rank_points",
            "player2_rank_points",
        }
        self.assertFalse(removed & set(MATCH_COLUMNS))
        self.assertTrue((DATA / "rankings/tour=atp/year=2026/rankings.parquet").exists())

    def test_corrections_target_any_canonical_entity(self) -> None:
        columns = [
            row[0]
            for row in duckdb.connect()
            .execute(
                f"DESCRIBE SELECT * FROM read_parquet("
                f"'{ROOT / 'contributions/corrections.parquet'}')"
            )
            .fetchall()
        ]
        self.assertEqual(
            columns,
            [
                "correction_id",
                "entity_type",
                "entity_id",
                "field",
                "corrected_value",
                "source_url",
                "contributor",
                "contributed_on",
                "license",
                "status",
            ],
        )

    def test_fixture_nullability_and_nullable_completed_scores(self) -> None:
        connection = duckdb.connect()
        fixtures = DATA / "fixtures/tour=*/current.parquet"
        fixture_invalid = connection.execute(
            f"SELECT count(*) FROM read_parquet('{fixtures}', hive_partitioning=false) "
            "WHERE status<>'fixture' OR winner_id IS NOT NULL OR score IS NOT NULL"
        ).fetchone()[0]
        scoreless_completed = connection.execute(
            f"SELECT count(*) FROM read_parquet('{DATA / 'matches/tour=*/year=*/matches.parquet'}', "
            "hive_partitioning=false) WHERE status='completed' AND score IS NULL"
        ).fetchone()[0]
        self.assertEqual(fixture_invalid, 0)
        self.assertEqual(scoreless_completed, 303)

    def test_adapter_normalizes_singles_and_preserves_doubles_order(self) -> None:
        self.assertEqual(normalize_participant("player_1"), ["player_1"])
        self.assertEqual(
            normalize_participant(["player_2", "player_1"]),
            ["player_2", "player_1"],
        )
        self.assertIsNone(normalize_participant(None))

    def test_source_slot_ids_are_stable_and_participant_independent(self) -> None:
        first = source_slot_match_id("wikimedia", "Q123|main|QF|4")
        second = source_slot_match_id("wikimedia", "Q123|main|QF|4")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^match_[0-9a-f]{24}$")

    def test_synthetic_doubles_uses_the_shared_physical_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.parquet"
            second = Path(temporary) / "second.parquet"
            connection = duckdb.connect()
            query = """
                SELECT DATE '2026-07-16' AS date, 'match_doubles'::VARCHAR match_id,
                  'tournament_atp_2026_test'::VARCHAR tournament_id,
                  'Test Open'::VARCHAR tournament_name, 'atp'::VARCHAR tour,
                  2026::SMALLINT AS year, 'main'::VARCHAR AS draw,
                  'F'::VARCHAR AS round, 'doubles'::VARCHAR AS format,
                  ['player_1','player_2']::VARCHAR[] player1_id,
                  ['One','Two']::VARCHAR[] player1_name, '1'::VARCHAR player1_seed,
                  ['player_3','player_4']::VARCHAR[] player2_id,
                  ['Three','Four']::VARCHAR[] player2_name, '2'::VARCHAR player2_seed,
                  ['player_1','player_2']::VARCHAR[] winner_id,
                  'completed'::VARCHAR status, '6-4 6-4'::VARCHAR score,
                  3::TINYINT best_of
            """
            for output in (first, second):
                _copy_parquet(
                    connection,
                    query,
                    output,
                    row_group_size=MATCH_ROW_GROUP_SIZE,
                    match_shaped=True,
                )
            schema = connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{first}')"
            ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in schema], list(MATCH_SCHEMA))
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                hashlib.sha256(second.read_bytes()).hexdigest(),
            )

    def test_migration_report_records_equivalence_and_backfill(self) -> None:
        report = __import__("json").loads(
            (ROOT / "reports/v3.2/migration-v3.2.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["match_columns"], list(MATCH_COLUMNS))
        self.assertEqual(report["old_match_rows"], report["new_match_rows"])
        self.assertEqual(report["retained_differences"], 0)
        self.assertGreater(report["backfilled_best_of"], 0)
        self.assertTrue(all(item["old_sha256"] != item["new_sha256"] for item in report["partitions"]))


if __name__ == "__main__":
    unittest.main()
