from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb

from open_tennis_data.dataset import (
    MATCH_ROW_GROUP_SIZE,
    _copy_parquet,
    _create_catalog,
    validate_dataset,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class SemanticDedupTests(unittest.TestCase):
    @staticmethod
    def rebuild_catalog(root: Path) -> None:
        connection = duckdb.connect()
        catalog = root / "catalog/catalog.parquet"
        as_of, revision = connection.execute(
            f"SELECT as_of,source_revision FROM read_parquet('{catalog}') LIMIT 1"
        ).fetchone()
        catalog.unlink()
        _create_catalog(connection, root, as_of, revision)
        connection.close()

    def test_cleanup_report_and_alias_contract(self) -> None:
        report = json.loads(
            (ROOT / "reports/data-quality/semantic-dedup-v3.2.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["exact_duplicate_groups"], 54)
        self.assertEqual(report["removed_match_rows"], 54)
        self.assertEqual(report["preserved_surviving_ids"], 54)
        self.assertEqual(report["observation_rows_before"], report["observation_rows_after"])
        self.assertEqual(len(report["review_only_variant_groups"]), 24)

        connection = duckdb.connect()
        aliases, retired, targets = connection.execute(
            f"SELECT count(*),count(DISTINCT retired_match_id),"
            f"count(DISTINCT canonical_match_id) FROM read_parquet("
            f"'{DATA / 'identity/match-aliases.parquet'}')"
        ).fetchone()
        self.assertEqual((aliases, retired, targets), (54, 54, 54))

    def test_cleaned_repository_has_no_semantic_duplicates(self) -> None:
        self.assertEqual(validate_dataset(DATA), [])

    def test_validator_rejects_broken_aliases_and_semantic_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            alias_root = base / "alias-data"
            shutil.copytree(DATA, alias_root, copy_function=os.link)
            aliases = alias_root / "identity/match-aliases.parquet"
            replacement = aliases.with_suffix(".replacement.parquet")
            connection = duckdb.connect()
            connection.execute(
                f"COPY (WITH numbered AS (SELECT *,row_number() OVER (ORDER BY "
                f"retired_match_id) rn FROM read_parquet('{aliases}')) SELECT * EXCLUDE(rn) "
                "REPLACE(CASE WHEN rn=1 THEN 'missing-match-id' ELSE canonical_match_id END "
                f"AS canonical_match_id) FROM numbered) TO '{replacement}' (FORMAT PARQUET)"
            )
            connection.close()
            os.replace(replacement, aliases)
            self.rebuild_catalog(alias_root)
            self.assertTrue(
                any("match alias targets missing" in error for error in validate_dataset(alias_root))
            )

            duplicate_root = base / "duplicate-data"
            shutil.copytree(DATA, duplicate_root, copy_function=os.link)
            matches = duplicate_root / "matches/tour=atp/year=1971/matches.parquet"
            replacement = matches.with_suffix(".replacement.parquet")
            connection = duckdb.connect()
            _copy_parquet(
                connection,
                f"SELECT * FROM read_parquet('{matches}',hive_partitioning=false) UNION ALL "
                f"(SELECT * REPLACE('semantic-duplicate-test' AS match_id) FROM "
                f"read_parquet('{matches}',hive_partitioning=false) LIMIT 1)",
                replacement,
                row_group_size=MATCH_ROW_GROUP_SIZE,
                match_shaped=True,
            )
            connection.close()
            os.replace(replacement, matches)
            self.rebuild_catalog(duplicate_root)
            self.assertTrue(
                any(
                    "exact semantic duplicate matches" in error
                    for error in validate_dataset(duplicate_root)
                )
            )


if __name__ == "__main__":
    unittest.main()
