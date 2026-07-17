from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

from open_tennis_data.dataset import audit_retroactive_dataset


class RetroactiveAuditWorkflowTests(unittest.TestCase):
    def make_root(self, temporary: str) -> Path:
        root = Path(temporary) / "data"
        catalog = root / "catalog/catalog.parquet"
        catalog.parent.mkdir(parents=True)
        connection = duckdb.connect()
        connection.execute(
            f"COPY (SELECT 'old-revision'::VARCHAR AS source_revision) "
            f"TO '{catalog}' (FORMAT PARQUET)"
        )
        connection.close()
        return root

    @staticmethod
    def entity_snapshots(*, changed: bool) -> list[dict[str, dict[str, object]]]:
        old_match = {
            "match-1": {
                "match_id": "match-1",
                "status": "completed",
                "player1_id": "p1",
                "player2_id": "p2",
                "winner_id": "p1",
                "score": "6-4 6-4",
            }
        }
        old_tournament = {
            "tournament-1": {
                "tournament_id": "tournament-1",
                "start_date": date(2026, 7, 13),
                "end_date": date(2026, 7, 19),
                "city": "London",
                "country": "GBR",
                "surface": "grass",
            }
        }
        old_fixture = {
            "match_fixture_1": {
                "match_id": "match_fixture_1",
                "player1_id": "p1",
                "player2_id": "p2",
                "date": None,
                "round": "QF",
                "tournament_id": "tournament-1",
            }
        }
        if not changed:
            return [
                old_match,
                old_tournament,
                old_fixture,
                old_match,
                old_tournament,
                old_fixture,
            ]
        new_match = {"match-1": {**old_match["match-1"], "score": "6-3 6-4"}}
        new_tournament = {
            "tournament-1": {
                **old_tournament["tournament-1"],
                "start_date": date(2026, 7, 14),
            }
        }
        new_fixture = {
            "match_fixture_1": {
                **old_fixture["match_fixture_1"],
                "date": date(2026, 7, 17),
            }
        }
        return [
            old_match,
            old_tournament,
            old_fixture,
            new_match,
            new_tournament,
            new_fixture,
        ]

    def common_patches(self, *, changed: bool) -> tuple[object, ...]:
        return (
            patch("open_tennis_data.dataset.validate_dataset", return_value=[]),
            patch(
                "open_tennis_data.dataset._remote_audit_revisions",
                return_value=(
                    True,
                    [
                        {
                            "source": "page-a",
                            "old_revision": "1",
                            "new_revision": "2",
                        },
                        {
                            "source": "page-b",
                            "old_revision": "3",
                            "new_revision": "4",
                        },
                    ],
                ),
            ),
            patch(
                "open_tennis_data.dataset._entity_records",
                side_effect=self.entity_snapshots(changed=changed),
            ),
            patch(
                "open_tennis_data.dataset._audit_quality_snapshot",
                return_value={
                    "quarantined_rows": 0,
                    "reconciled_sources": 2,
                    "source_rows": 2,
                    "normalized_rows": 2,
                },
            ),
            patch(
                "open_tennis_data.dataset._historical_partition_paths",
                return_value=["matches/tour=atp/year=2024/matches.parquet"],
            ),
        )

    def test_changed_revisions_without_semantic_change_do_not_promote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            output = Path(temporary) / "audit"
            patches = self.common_patches(changed=False)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
                "open_tennis_data.dataset._refresh_years",
                return_value={"source_revision": "new-revision"},
            ), patch(
                "open_tennis_data.dataset._audit_source_records", return_value={}
            ), patch("open_tennis_data.dataset.promote_dataset") as promote:
                report = audit_retroactive_dataset(
                    root, output, as_of=date(2026, 7, 16), workers=1
                )
            self.assertEqual(report["status"], "no_semantic_change")
            self.assertEqual(report["changed_files"], 0)
            promote.assert_not_called()
            self.assertTrue((output / "retroactive-audit.json").exists())

    def test_semantic_changes_report_fields_and_promote_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            output = Path(temporary) / "audit"
            patches = self.common_patches(changed=True)
            new_sources = {
                "source-a": {"revision": "2", "sha256": "new-a"},
                "source-b": {"revision": "4", "sha256": "new-b"},
            }
            old_sources = {
                "source-a": {"revision": "1", "sha256": "old-a"},
                "source-b": {"revision": "3", "sha256": "old-b"},
            }
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
                "open_tennis_data.dataset._refresh_years",
                return_value={"source_revision": "new-revision"},
            ), patch(
                "open_tennis_data.dataset._audit_source_records",
                side_effect=[new_sources, old_sources],
            ), patch(
                "open_tennis_data.dataset.promote_dataset",
                return_value={"changed_files": 6, "changed_bytes": 900},
            ) as promote:
                report = audit_retroactive_dataset(
                    root, output, as_of=date(2026, 7, 16), workers=1
                )
            self.assertEqual(report["status"], "changed")
            self.assertEqual(report["changed_files"], 6)
            self.assertEqual(report["changes"]["matches"]["field_changes"]["score"], 1)
            self.assertEqual(
                report["changes"]["fixtures"]["field_changes"]["date"], 1
            )
            self.assertEqual(
                report["changes"]["tournaments"]["field_changes"]["start_date"], 1
            )
            self.assertEqual(len(report["source_changes"]), 2)
            promote.assert_called_once()
            payload = json.loads(
                (output / "retroactive-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["validation"], "passed")

    def test_failed_rebuild_writes_report_and_never_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            output = Path(temporary) / "audit"
            patches = self.common_patches(changed=False)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
                "open_tennis_data.dataset._refresh_years",
                side_effect=RuntimeError("invalid upstream fixture"),
            ), patch("open_tennis_data.dataset.promote_dataset") as promote:
                with self.assertRaisesRegex(RuntimeError, "invalid upstream fixture"):
                    audit_retroactive_dataset(
                        root, output, as_of=date(2026, 7, 16), workers=1
                    )
            promote.assert_not_called()
            payload = json.loads(
                (output / "retroactive-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["validation"], "failed")
            self.assertIn("invalid upstream fixture", payload["error"])


if __name__ == "__main__":
    unittest.main()
