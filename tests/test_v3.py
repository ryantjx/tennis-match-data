from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb

from open_tennis_data.v3 import (
    add_correction,
    extract_dataset,
    parse_years,
    query_dataset,
    validate_dataset,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class V3Tests(unittest.TestCase):
    def test_year_parser(self) -> None:
        self.assertEqual(parse_years("2020,2022:2024"), [2020, 2022, 2023, 2024])
        with self.assertRaises(ValueError):
            parse_years("1967")

    def test_repository_dataset_validates(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated v3 dataset is not present")
        self.assertEqual(validate_dataset(DATA), [])

    def test_query_prunes_tour_and_year(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated v3 dataset is not present")
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
            self.skipTest("generated v3 dataset is not present")
        _, rows = query_dataset(
            DATA,
            """
            SELECT count(DISTINCT event_id)
            FROM events
            WHERE tour='atp' AND year=2023 AND lower(event_name)='m15 monastir'
            """,
            tours=["atp"],
            years=[2023],
        )
        self.assertGreater(rows[0][0], 40)

    def test_statistics_and_date_semantics(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated v3 dataset is not present")
        _, rows = query_dataset(
            DATA,
            "SELECT (SELECT count(*) FROM match_stats), "
            "(SELECT count(*) FROM matches WHERE played_on IS NULL AND played_on_precision='day')",
        )
        self.assertGreater(rows[0][0], 300_000)
        self.assertEqual(rows[0][1], 0)

    def test_extract_is_parquet_v3(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated v3 dataset is not present")
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
            self.assertEqual(metadata["schema_version"], "3")

    def test_correction_is_deterministic_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrections.parquet"
            first = add_correction(
                path,
                match_id="match:atp:test",
                field="score",
                corrected_value="6-4 6-4",
                source_url="https://example.org/result",
                contributor="tester",
                contributed_on=date(2026, 7, 12),
            )
            second = add_correction(
                path,
                match_id="match:atp:test",
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


if __name__ == "__main__":
    unittest.main()
