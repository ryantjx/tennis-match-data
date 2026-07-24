from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from open_tennis_data.release import (
    V3_RELEASE_ASSETS,
    create_v3_release,
    extract_release,
    format_release_rows,
    load_release_manifest,
    query_release,
    validate_v3_release,
)
from open_tennis_data.schema import MATCH_COLUMNS
from open_tennis_data.source_policy import SourcePolicyRegistry
from tests.fixture_dataset import write_release_input


class V3ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temporary.name) / "data"
        cls.release = Path(cls.temporary.name) / "release"
        write_release_input(cls.data)
        cls.manifest = create_v3_release(
            cls.data,
            cls.release,
            as_of="2026-07-24T04:17:00Z",
            repository="example/open-tennis-data",
            release_tag="data-v3-test",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def local_manifest(self) -> dict:
        manifest = json.loads(json.dumps(self.manifest))
        for asset in manifest["assets"]:
            asset["url"] = str(self.release / asset["name"])
        return manifest

    def test_release_contract_and_lifecycle(self) -> None:
        self.assertFalse(validate_v3_release(self.release))
        self.assertEqual(
            set(V3_RELEASE_ASSETS),
            {path.name for path in self.release.iterdir()},
        )
        connection = duckdb.connect()
        columns = [
            row[0]
            for row in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{self.release / 'matches.parquet'}')"
            ).fetchall()
        ]
        self.assertEqual(columns, list(MATCH_COLUMNS))
        self.assertEqual(columns[-1], "source")
        self.assertEqual(
            connection.execute(
                f"SELECT source FROM read_parquet('{self.release / 'completed.parquet'}')"
            ).fetchone()[0],
            ["sackmann", "tennis-data.co.uk"],
        )
        counts = connection.execute(
            f"""
            SELECT
              (SELECT count(*) FROM read_parquet('{self.release / "matches.parquet"}')),
              (SELECT count(*) FROM read_parquet('{self.release / "completed.parquet"}')),
              (SELECT count(*) FROM read_parquet('{self.release / "fixtures.parquet"}'))
            """
        ).fetchone()
        self.assertEqual(counts[0], counts[1] + counts[2])
        self.assertEqual(
            connection.execute(
                f"SELECT count(*) FROM read_parquet('{self.release / 'matches.parquet'}') "
                "WHERE year<2020 OR draw<>'main' OR format<>'singles'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute(
                f"SELECT count(*) FROM read_parquet('{self.release / 'fixtures.parquet'}') "
                "WHERE status<>'fixture' OR winner_id IS NOT NULL OR score IS NOT NULL"
            ).fetchone()[0],
            0,
        )
        connection.close()

    def test_complete_gate_fails_closed_while_inventory_is_preview(self) -> None:
        errors = validate_v3_release(self.release, require_complete=True)
        self.assertTrue(any("coverage gate" in error for error in errors))
        self.assertTrue(any("retrieval time" in error for error in errors))
        stale = validate_v3_release(
            self.release,
            max_age_hours=30,
            now=datetime(2026, 7, 25, 11, 18, tzinfo=UTC),
        )
        self.assertTrue(any("31.0 hours" in error for error in stale))

    def test_identical_pinned_inputs_produce_identical_assets(self) -> None:
        repeated = Path(self.temporary.name) / "repeated"
        create_v3_release(
            self.data,
            repeated,
            as_of="2026-07-24T04:17:00Z",
            repository="example/open-tennis-data",
            release_tag="data-v3-test",
        )
        for filename in V3_RELEASE_ASSETS:
            self.assertEqual(
                (self.release / filename).read_bytes(),
                (repeated / filename).read_bytes(),
                filename,
            )

    def test_manifest_resolution_remote_views_and_extract(self) -> None:
        local = self.local_manifest()
        manifest_path = Path(self.temporary.name) / "local-manifest.json"
        manifest_path.write_text(json.dumps(local), encoding="utf-8")
        loaded = load_release_manifest(
            "test",
            url=manifest_path.as_uri(),
        )
        columns, rows = query_release(
            loaded,
            "SELECT count(*) AS rows FROM matches",
        )
        self.assertEqual(columns, ["rows"])
        self.assertGreater(rows[0][0], 0)
        extract = Path(self.temporary.name) / "atp-2025.parquet"
        extracted = extract_release(
            loaded,
            extract,
            tours=["atp"],
            years=[2025],
            levels=[],
        )
        self.assertGreater(extracted, 0)
        self.assertEqual(
            duckdb.connect()
            .execute(
                f"SELECT count(*) FROM read_parquet('{extract}') "
                "WHERE tour<>'atp' OR year<>2025"
            )
            .fetchone()[0],
            0,
        )

    def test_source_policy_rejects_blocked_and_unknown_sources(self) -> None:
        registry = SourcePolicyRegistry.load()
        registry.require_publishable({"tennis-data.co.uk", "wikimedia", "tour"})
        with self.assertRaisesRegex(ValueError, "not publishable"):
            registry.require_publishable({"wta-api"})
        with self.assertRaisesRegex(ValueError, "lack policy"):
            registry.require_publishable({"unregistered"})

    def test_every_cli_output_format_is_machine_readable(self) -> None:
        columns = ["date", "players"]
        rows = [(None, ["A", "B"])]
        self.assertIn("date\tplayers", format_release_rows(columns, rows, output_format="table"))
        self.assertIn("date,players", format_release_rows(columns, rows, output_format="csv"))
        self.assertEqual(
            json.loads(format_release_rows(columns, rows, output_format="json"))[0][
                "players"
            ],
            ["A", "B"],
        )
        self.assertEqual(
            json.loads(format_release_rows(columns, rows, output_format="jsonl"))[
                "date"
            ],
            None,
        )


if __name__ == "__main__":
    unittest.main()
