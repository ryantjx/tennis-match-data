from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from open_tennis_data.acquisition import (
    HistoryObservation,
    reconcile_history_bundle,
    refresh_history_bundle,
    validate_acquisition_dataset,
    verify_bundle,
)
from open_tennis_data.official import (
    capture_rendered_page,
    parse_official_html,
    parse_official_pdf,
    parse_oop_text,
)
from open_tennis_data.release import create_v3_release, validate_v3_release
from open_tennis_data.source_policy import SourcePolicyRegistry


class AcquisitionTests(unittest.TestCase):
    def test_combined_event_requires_and_preserves_explicit_tour(self) -> None:
        payload = b"""
        <main>
          <article data-match-id="shared-1" data-tour="wta" data-round="R32"
            data-player1="Player A" data-player2="Player B"
            data-date="2026-07-28" data-court="Stadium"></article>
          <article data-match-id="atp-1" data-tour="atp" data-round="R32"
            data-player1="Player C" data-player2="Player D"></article>
          <article data-match-id="bad-1" data-player1="A" data-player2="B"></article>
        </main>
        """
        observations, rejected = parse_official_html(
            payload,
            source_label="atp-site",
            source_event_id="mubadala-dc-open:2026",
            route="schedule",
            source_url="offline://dc.html",
        )
        self.assertEqual({item.tour for item in observations}, {"atp", "wta"})
        self.assertIn("missing_explicit_tour_marker", {item["reason"] for item in rejected})

    def test_policy_blocks_before_playwright_is_imported(self) -> None:
        with self.assertRaisesRegex(ValueError, "policy_blocked"):
            capture_rendered_page(
                source_label="atp-site",
                url="https://www.atptour.com/",
                route="overview",
                fields={"participants"},
                policy=SourcePolicyRegistry.load(),
            )

    def test_oop_parser_preserves_ordering_and_rejects_bad_pdf(self) -> None:
        observations, rejected = parse_oop_text(
            (
                "MATCH|dc-1|wta|R32|Player A|Player B|2026-07-28|"
                "13:00|Not Before 1 PM|Stadium\n"
                "MATCH|dc-2|atp|R32|Player C|Player D|2026-07-28|"
                "|Followed By|Grandstand"
            ),
            source_label="wta-oop",
            source_event_id="mubadala-dc-open:2026",
            source_url="offline://dc.pdf",
            artifact_sha256="a" * 64,
        )
        self.assertEqual(rejected, [])
        self.assertEqual({item.tour for item in observations}, {"atp", "wta"})
        self.assertEqual(observations[0].ordering_label, "Not Before 1 PM")
        parsed, malformed = parse_official_pdf(
            b"not a PDF",
            source_label="atp-oop",
            source_event_id="test:2026",
            source_url="offline://bad.pdf",
        )
        self.assertEqual(parsed, [])
        self.assertEqual(malformed[0]["reason"], "malformed_pdf")

    def test_closed_history_bundle_reconciles_to_deterministic_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            (fixtures / "atp-2026.xlsx").write_bytes(b"PK\x03\x04fixture")
            (fixtures / "wta-2026.xlsx").write_bytes(b"PK\x03\x04fixture")

            def parsed(path: Path, *, tour: str, year: int, source_url: str):
                source_file_id = f"source_file_{tour}_{year}"
                source = {
                    "source_file_id": source_file_id,
                    "kind": "matches",
                    "tour": tour,
                    "year": year,
                    "source_label": "tennis-data.co.uk",
                    "source_path": path.name,
                    "source_url": source_url,
                    "revision": "sha256:test",
                    "sha256": "a" * 64,
                    "license": "Tennis-Data redistribution terms",
                    "source_rows": 2,
                    "normalized_rows": 2,
                    "quarantined_rows": 0,
                }
                common = dict(
                    tour=tour,
                    source_year=year,
                    source_file_id=source_file_id,
                    tournament="Test Open",
                    location="London",
                    surface="hard",
                    level="other",
                    round="F",
                    winner="Winner One",
                    loser="Loser Two",
                    winner_seed="1",
                    loser_seed="2",
                    score="6-4 6-4",
                    best_of=3,
                )
                return source, [
                    HistoryObservation(
                        **common,
                        source_match_id=f"{tour}:accepted",
                        row_fingerprint=f"{tour}-accepted",
                        played_on="2026-07-20",
                        status="completed",
                    ),
                    HistoryObservation(
                        **common,
                        source_match_id=f"{tour}:walkover",
                        row_fingerprint=f"{tour}-walkover",
                        played_on="2026-07-21",
                        status="walkover",
                    ),
                ], []

            bundle = root / "bundle"
            with patch(
                "open_tennis_data.acquisition.parse_history_artifact",
                side_effect=parsed,
            ):
                refresh_history_bundle(
                    years=[2026],
                    parent_release="data-v3-test",
                    output=bundle,
                    offline_fixture_root=fixtures,
                    collected_at=datetime(2026, 7, 28, tzinfo=UTC),
                )
            self.assertEqual(verify_bundle(bundle), [])
            dataset = root / "dataset"
            result = reconcile_history_bundle(
                source_bundle=bundle,
                parent_release="data-v3-test",
                output=dataset,
            )
            self.assertEqual(result["accepted_rows"], 4)
            self.assertEqual(result["quarantined_rows"], 0)
            self.assertEqual(validate_acquisition_dataset(dataset), [])
            first = root / "release-one"
            second = root / "release-two"
            create_v3_release(
                dataset,
                first,
                as_of="2026-07-28T04:17:00Z",
                release_tag="data-v3-20260728T041700Z",
            )
            create_v3_release(
                dataset,
                second,
                as_of="2026-07-28T04:17:00Z",
                release_tag="data-v3-20260728T041700Z",
            )
            self.assertEqual(validate_v3_release(first), [])
            self.assertEqual(
                {path.name: path.read_bytes() for path in first.iterdir()},
                {path.name: path.read_bytes() for path in second.iterdir()},
            )


if __name__ == "__main__":
    unittest.main()
