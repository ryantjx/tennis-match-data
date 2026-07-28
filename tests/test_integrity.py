from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from open_tennis_data.integrity import (
    lifecycle_match_id,
    stable_fixture_key,
    validate_material_date,
    validate_revision_chain,
)
from open_tennis_data.source_policy import SourcePolicyRegistry


class IntegrityTests(unittest.TestCase):
    def test_fixture_and_match_identity_ignore_mutable_participants(self) -> None:
        fixture = stable_fixture_key(
            tour="wta",
            event_edition_id="mubadala-dc-open:2026",
            draw="main",
            round_name="R32",
            draw_position="slot:12",
        )
        self.assertEqual(
            fixture,
            stable_fixture_key(
                tour="wta",
                event_edition_id="mubadala-dc-open:2026",
                draw="main",
                round_name="R32",
                draw_position="slot:12",
            ),
        )
        self.assertTrue(lifecycle_match_id(fixture).startswith("match_"))

    def test_material_date_guards_cover_cutoff_identity_and_no_play(self) -> None:
        errors = validate_material_date(
            played_on=date(2029, 7, 20),
            source_year=2026,
            source_cutoff_at="2026-07-24T04:17:00Z",
            event_start=date(2026, 7, 13),
            event_end=date(2026, 7, 20),
            venue_timezone="Europe/London",
            status="completed",
            has_event_or_draw_identity=False,
            evidence_dimensions={"participants"},
        )
        self.assertIn("played date is outside the declared source season", errors)
        self.assertIn("played date is after the conservative source cutoff", errors)
        self.assertIn("date reconciliation is participant-only", errors)
        self.assertIn("date reconciliation lacks event or draw identity", errors)
        self.assertEqual(
            validate_material_date(
                played_on=date(2026, 7, 20),
                source_year=2026,
                source_cutoff_at="2026-07-20T23:00:00Z",
                event_start=date(2026, 7, 13),
                event_end=date(2026, 7, 20),
                venue_timezone="Europe/London",
                status="walkover",
                has_event_or_draw_identity=True,
                evidence_dimensions={"round"},
            ),
            ["no-play outcome cannot have a played date"],
        )

    def test_revision_chain_rejects_cycles_and_scope_changes(self) -> None:
        rows = [
            {"id": "a", "previous": "b", "source": "one"},
            {"id": "b", "previous": "a", "source": "two"},
        ]
        errors = validate_revision_chain(
            rows,
            id_field="id",
            predecessor_field="previous",
            scope_fields=("source",),
        )
        self.assertTrue(any("cycle" in item for item in errors))
        self.assertTrue(any("scope field source" in item for item in errors))

    def test_official_network_policy_is_fail_closed(self) -> None:
        registry = SourcePolicyRegistry.load()
        decision = registry.evaluate_network_request(
            "atp-site",
            "https://www.atptour.com/en/tournaments/washington/418/overview",
            route="overview",
            fields={"participants"},
            at=datetime(2026, 7, 28, tzinfo=UTC),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "policy_blocked")

    def test_valid_grant_needs_domain_route_fields_and_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_text(json.dumps({
                "sources": [{
                    "source": "atp-site",
                    "policy_state": "private_evaluation",
                    "terms_url": "https://example.test/terms",
                    "allowed_uses": ["private_evaluation"],
                    "allowed_fields": ["participants"],
                    "attribution": "test",
                    "rate_limit": "one per minute",
                    "parser_version": "3.3",
                    "reviewed_at": "2026-07-28",
                    "policy_revision": "test-1",
                    "grant_id": "grant-1",
                    "grant_sha256": "a" * 64,
                    "grant_expires_at": "2026-12-31",
                    "network_collection_enabled": True,
                    "allowed_domains": ["www.atptour.com"],
                    "allowed_routes": ["overview"],
                    "retention_mode": "private",
                    "redistribution_mode": "none",
                    "minimum_request_interval": 60,
                    "maximum_requests_per_day": 10
                }]
            }), encoding="utf-8")
            registry = SourcePolicyRegistry.load(path)
            allowed = registry.evaluate_network_request(
                "atp-site",
                "https://www.atptour.com/tournament",
                route="overview",
                fields={"participants"},
                at=datetime(2026, 7, 28, tzinfo=UTC),
            )
            self.assertTrue(allowed.allowed)
            self.assertFalse(allowed.public_projection_allowed)
            denied = registry.evaluate_network_request(
                "atp-site",
                "https://evil.example/tournament",
                route="overview",
                fields={"participants"},
                at=datetime(2026, 7, 28, tzinfo=UTC),
            )
            self.assertEqual(denied.reason, "domain_not_allowlisted")


if __name__ == "__main__":
    unittest.main()
