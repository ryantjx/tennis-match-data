from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb

from open_tennis_data.dataset import (
    add_correction,
    create_direct_downloads,
    extract_dataset,
    parse_years,
    query_dataset,
    validate_dataset,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class DatasetTests(unittest.TestCase):
    def test_year_parser(self) -> None:
        self.assertEqual(parse_years("2020,2022:2024"), [2020, 2022, 2023, 2024])
        with self.assertRaises(ValueError):
            parse_years("1967")

    def test_repository_dataset_validates(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated dataset is not present")
        self.assertEqual(validate_dataset(DATA), [])

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
            self.skipTest("generated dataset is not present")
        _, rows = query_dataset(
            DATA,
            "SELECT (SELECT count(*) FROM match_stats), "
            "(SELECT count(*) FROM matches WHERE played_on IS NULL AND played_on_precision='day')",
        )
        self.assertGreater(rows[0][0], 300_000)
        self.assertEqual(rows[0][1], 0)

    def test_extract_has_no_version_metadata(self) -> None:
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
            self.assertEqual(metadata, {})

    def test_direct_downloads_include_matches_and_fixtures(self) -> None:
        if not (DATA / "catalog" / "catalog.parquet").exists():
            self.skipTest("generated dataset is not present")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            output = Path(temporary) / "downloads"
            future_output = Path(temporary) / "future-downloads"
            connection = duckdb.connect()
            for tour in ("atp", "wta"):
                match_output = root / "matches" / f"tour={tour}" / "year=2026" / "matches.parquet"
                fixture_output = root / "fixtures" / f"tour={tour}" / "current.parquet"
                match_output.parent.mkdir(parents=True, exist_ok=True)
                fixture_output.parent.mkdir(parents=True, exist_ok=True)
                connection.execute(
                    f"COPY (SELECT * FROM read_parquet('{DATA / 'matches' / f'tour={tour}' / 'year=2026' / 'matches.parquet'}') LIMIT 10) "
                    f"TO '{match_output}' (FORMAT PARQUET)"
                )
                connection.execute(
                    f"COPY (SELECT * REPLACE (NULL::DATE AS scheduled_on, NULL::TIMESTAMP AS scheduled_at) "
                    f"FROM read_parquet('{DATA / 'fixtures' / f'tour={tour}' / 'current.parquet'}') LIMIT 2) "
                    f"TO '{fixture_output}' (FORMAT PARQUET)"
                )
            (root / "catalog").mkdir(parents=True)
            shutil.copy2(DATA / "catalog" / "catalog.parquet", root / "catalog/catalog.parquet")

            summary = create_direct_downloads(root, output)
            self.assertEqual(
                set(summary),
                {
                    "mens.parquet",
                    "womens.parquet",
                    "atp.parquet",
                    "wta.parquet",
                    "all-matches.parquet",
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
            rows, fixtures, tours = connection.execute(
                f"SELECT count(*), count(*) FILTER (WHERE record_type='fixture'), "
                f"count(DISTINCT tour) FROM read_parquet('{output / 'all-matches.parquet'}')"
            ).fetchone()
            self.assertEqual(rows, 24)
            self.assertEqual(fixtures, 4)
            self.assertEqual(tours, 2)
            metadata = dict(
                (key.decode(), value.decode())
                for _, key, value in connection.execute(
                    f"SELECT * FROM parquet_kv_metadata('{output / 'all-matches.parquet'}')"
                ).fetchall()
            )
            self.assertEqual(metadata, {})

            future_summary = create_direct_downloads(root, future_output, future_only=True)
            self.assertEqual(set(future_summary), set(summary))
            future_rows, future_fixtures, future_tours, completed = connection.execute(
                f"SELECT count(*), count(*) FILTER (WHERE record_type='fixture'), "
                f"count(DISTINCT tour), count(*) FILTER (WHERE record_type='completed') "
                f"FROM read_parquet('{future_output / 'all-matches.parquet'}')"
            ).fetchone()
            self.assertEqual(future_rows, 4)
            self.assertEqual(future_fixtures, 4)
            self.assertEqual(future_tours, 2)
            self.assertEqual(completed, 0)

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
