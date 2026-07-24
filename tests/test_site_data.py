from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any


def load_site_data_module() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "prepare-site-data.py"
    spec = importlib.util.spec_from_file_location("prepare_site_data", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load prepare-site-data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


site_data = load_site_data_module()


def release(tag: str, published_at: str, *, draft: bool = False) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "published_at": published_at,
        "draft": draft,
        "assets": [
            {
                "name": name,
                "browser_download_url": f"https://example.test/{tag}/{name}",
            }
            for name in site_data.REQUIRED_ASSETS
        ],
    }


def manifest(tag: str, payloads: dict[str, bytes]) -> dict[str, Any]:
    return {
        "product": "Open Tennis Data",
        "product_version": "3",
        "schema_version": "3.3",
        "release_status": "preview",
        "release_tag": tag,
        "assets": [
            {
                "name": name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in payloads.items()
        ],
    }


class SiteDataTests(unittest.TestCase):
    def test_selects_newest_published_v3_release(self) -> None:
        releases = [
            release("data-v3-older", "2026-07-23T10:00:00Z"),
            release("data-v3-draft", "2026-07-25T10:00:00Z", draft=True),
            release("future-latest", "2026-07-26T10:00:00Z"),
            release("data-v3-newer", "2026-07-24T10:00:00Z"),
        ]
        self.assertEqual(
            site_data.select_release(releases)["tag_name"],
            "data-v3-newer",
        )

    def test_tag_override_is_exact(self) -> None:
        releases = [
            release("data-v3-one", "2026-07-23T10:00:00Z"),
            release("data-v3-two", "2026-07-24T10:00:00Z"),
        ]
        self.assertEqual(
            site_data.select_release(releases, tag="data-v3-one")["tag_name"],
            "data-v3-one",
        )
        with self.assertRaisesRegex(site_data.SnapshotError, "not found"):
            site_data.select_release(releases, tag="data-v3-missing")

    def test_manifest_contract_is_enforced(self) -> None:
        payloads = {
            "matches.parquet": b"matches",
            "tournaments.parquet": b"tournaments",
            "players.parquet": b"players",
        }
        valid = manifest("data-v3-test", payloads)
        inventory = site_data.validate_manifest(valid, expected_tag="data-v3-test")
        self.assertEqual(set(inventory), set(payloads))

        wrong_schema = dict(valid, schema_version="3.2")
        with self.assertRaisesRegex(site_data.SnapshotError, "schema version"):
            site_data.validate_manifest(wrong_schema, expected_tag="data-v3-test")

        missing = dict(valid, assets=valid["assets"][:-1])
        with self.assertRaisesRegex(site_data.SnapshotError, "players.parquet"):
            site_data.validate_manifest(missing, expected_tag="data-v3-test")

    def test_checksum_mismatch_is_rejected(self) -> None:
        payloads = {
            "matches.parquet": b"matches",
            "tournaments.parquet": b"tournaments",
            "players.parquet": b"players",
        }
        inventory = site_data.validate_manifest(
            manifest("data-v3-test", payloads),
            expected_tag="data-v3-test",
        )
        with self.assertRaisesRegex(site_data.SnapshotError, "checksum mismatch"):
            site_data.verify_payload("matches.parquet", b"matchez", inventory)

    def test_snapshot_replacement_is_atomic_at_directory_level(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "data"
            output.mkdir()
            (output / "old.txt").write_text("old", encoding="utf-8")
            payloads = {
                "manifest.json": json.dumps({"release_tag": "data-v3-test"}).encode(),
                "matches.parquet": b"matches",
                "tournaments.parquet": b"tournaments",
                "players.parquet": b"players",
            }
            site_data.replace_snapshot(output, payloads)
            self.assertFalse((output / "old.txt").exists())
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                sorted(payloads),
            )
            self.assertEqual((output / "matches.parquet").read_bytes(), b"matches")


if __name__ == "__main__":
    unittest.main()
