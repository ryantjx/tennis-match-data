from __future__ import annotations

import os
import unittest
from datetime import date
from pathlib import Path

import duckdb

from open_tennis_data.dataset import validate_dataset

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("OPEN_TENNIS_DATA_ROOT", ROOT / "data")).resolve()


class YearlyDataQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.connection = duckdb.connect()
        cls.catalog = DATA / "catalog" / "catalog.parquet"
        if not cls.catalog.exists():
            raise AssertionError("checked-in dataset catalog is required")
        cls.as_of = cls.connection.execute(
            f"SELECT as_of FROM read_parquet('{cls.catalog}') LIMIT 1"
        ).fetchone()[0]
        if not isinstance(cls.as_of, date):
            raise AssertionError("catalog as_of must be a date")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    def test_repository_dataset_passes_complete_validation(self) -> None:
        self.assertEqual(validate_dataset(DATA), [])

    def test_every_tour_year_is_present_and_clean(self) -> None:
        expected = {
            (tour, year)
            for tour in ("atp", "wta")
            for year in range(1968, self.as_of.year + 1)
        }
        found = set(
            self.connection.execute(
                f"SELECT tour, year FROM read_parquet('{self.catalog}') "
                "WHERE table_name='matches'"
            ).fetchall()
        )
        self.assertEqual(found, expected)

        for tour, year in sorted(expected):
            with self.subTest(tour=tour, year=year):
                files = {
                    "matches": DATA
                    / "matches"
                    / f"tour={tour}"
                    / f"year={year}"
                    / "matches.parquet",
                    "tournaments": DATA
                    / "tournaments"
                    / f"tour={tour}"
                    / f"year={year}"
                    / "tournaments.parquet",
                    "observations": DATA
                    / "observations"
                    / f"tour={tour}"
                    / f"year={year}"
                    / "observations.parquet",
                }
                for table, path in files.items():
                    self.assertTrue(path.exists(), f"missing {table} partition")
                    count, wrong_partition = self.connection.execute(
                        f"SELECT count(*), count(*) FILTER "
                        f"(WHERE tour<>'{tour}' OR year<>{year}) "
                        f"FROM read_parquet('{path}')"
                    ).fetchone()
                    self.assertGreater(count, 0, f"empty {table} partition")
                    self.assertEqual(wrong_partition, 0, f"mispartitioned {table} rows")

                invalid_matches = self.connection.execute(
                    f"""
                    SELECT count(*) FROM read_parquet('{files['matches']}')
                    WHERE player1_id IS NULL OR player2_id IS NULL
                      OR player1_id=player2_id OR winner_id=loser_id
                      OR winner_id NOT IN (player1_id, player2_id)
                      OR loser_id NOT IN (player1_id, player2_id)
                      OR tournament_id IS NULL OR match_id IS NULL
                      OR draw NOT IN ('main','qualifying') OR round IS NULL
                      OR status NOT IN ('completed','walkover','retired','defaulted','abandoned')
                    """
                ).fetchone()[0]
                self.assertEqual(invalid_matches, 0)

    def test_ranking_years_follow_documented_coverage(self) -> None:
        expected = {
            (tour, year)
            for tour, first_year in (("atp", 1973), ("wta", 1984))
            for year in range(first_year, self.as_of.year + 1)
        }
        found = set(
            self.connection.execute(
                f"SELECT tour, year FROM read_parquet('{self.catalog}') "
                "WHERE table_name='rankings'"
            ).fetchall()
        )
        self.assertEqual(found, expected)

    def test_quarantine_reasons_are_explicit(self) -> None:
        reasons = {
            row[0]
            for row in self.connection.execute(
                f"SELECT DISTINCT reason FROM read_parquet('{DATA / 'quarantine/quarantine.parquet'}')"
            ).fetchall()
        }
        self.assertLessEqual(
            reasons,
            {"duplicate_source_row", "invalid_participants", "invalid_statistics"},
        )


if __name__ == "__main__":
    unittest.main()
