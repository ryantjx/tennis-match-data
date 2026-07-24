from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from open_tennis_data.exact_dates import (
    CanonicalMatch,
    DateRow,
    DateSource,
    fetch_live_completed_sources,
    fetch_tennis_data_file,
    parse_excel_date,
    parse_tennis_tv_completed,
    parse_wta_api_completed,
    quarantine_conflicting_dates,
    reconcile_date_rows,
)


def source(tour: str = "wta") -> DateSource:
    return DateSource(
        kind="match_dates",
        tour=tour,
        year=2024,
        source_label="wta-api" if tour == "wta" else "tennis-tv",
        source_path="snapshot.json",
        source_url="https://example.test/snapshot.json",
        revision="sha256:abc",
        sha256="abc",
        license="terms",
        source_rows=1,
    )


class ExactDateSourceTests(unittest.TestCase):
    def test_excel_serial_string_and_null_dates(self) -> None:
        self.assertEqual(parse_excel_date(45291), date(2023, 12, 31))
        self.assertEqual(parse_excel_date("31/12/2023"), date(2023, 12, 31))
        self.assertEqual(parse_excel_date("2024-01-01"), date(2024, 1, 1))
        self.assertIsNone(parse_excel_date(None))
        self.assertIsNone(parse_excel_date("malformed"))

    def test_source_id_is_contextual_and_stable(self) -> None:
        first = source()
        same = source()
        changed = DateSource(**{**first.__dict__, "tour": "atp"})
        self.assertEqual(first.source_file_id, same.source_file_id)
        self.assertNotEqual(first.source_file_id, changed.source_file_id)

    def test_fetch_supports_xlsx_and_legacy_xls_magic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source"
            with mock.patch(
                "open_tennis_data.exact_dates._download", return_value=b"PK\x03\x04content"
            ):
                path, _ = fetch_tennis_data_file("atp", 2024, destination)
                self.assertEqual(path.suffix, ".xlsx")
            with mock.patch(
                "open_tennis_data.exact_dates._download",
                return_value=b"\xd0\xcf\x11\xe0content",
            ):
                path, _ = fetch_tennis_data_file("wta", 2007, destination)
                self.assertEqual(path.suffix, ".xls")

    def test_cross_year_exact_date_and_compound_names_reconcile(self) -> None:
        match = CanonicalMatch(
            "match:1",
            "atp",
            2024,
            "Brisbane International",
            date(2024, 1, 1),
            date(2024, 1, 7),
            "R32",
            "Alex de Minaur",
            "Jan-Lennard Struff",
            "6-4 6-4",
        )
        row = DateRow(
            "atp",
            2024,
            date(2023, 12, 31),
            "Brisbane International",
            "Brisbane",
            "1st Round",
            "De Minaur A.",
            "Struff J.L.",
            "6-4 6-4",
            "source:1",
            "row:1",
            "fingerprint",
        )
        result = reconcile_date_rows([row], [match])
        self.assertEqual(result[0].match_id, "match:1")
        self.assertEqual(result[0].row.played_on, date(2023, 12, 31))

    def test_rematches_are_ambiguous_and_conflicting_dates_are_quarantined(self) -> None:
        base = dict(
            tour="wta",
            year=2024,
            tournament_name="Test Open",
            tournament_start=date(2024, 1, 1),
            tournament_end=date(2024, 1, 7),
            round="R32",
            player1_name="Iga Swiatek",
            player2_name="Coco Gauff",
            score=None,
        )
        matches = [CanonicalMatch(match_id=f"match:{index}", **base) for index in (1, 2)]
        row = DateRow(
            "wta", 2024, date(2024, 1, 2), "Test Open", "Test", "1st Round",
            "Swiatek I.", "Gauff C.", None, "source:1", "row:1", "fingerprint:1",
        )
        ambiguous = reconcile_date_rows([row], matches)[0]
        self.assertEqual(ambiguous.reason, "ambiguous_exact_date")
        self.assertEqual(ambiguous.candidate_match_ids, ("match:1", "match:2"))

        unique = reconcile_date_rows([row], matches[:1])[0]
        later = DateRow(**{**row.__dict__, "played_on": date(2024, 1, 3), "source_match_id": "row:2"})
        unique_later = reconcile_date_rows([later], matches[:1])[0]
        kept, rejected = quarantine_conflicting_dates([unique, unique_later])
        self.assertFalse(kept)
        self.assertEqual({item.reason for item in rejected}, {"conflicting_exact_date"})

    def test_wta_and_tennis_tv_completed_parsers_require_match_dates(self) -> None:
        wta_rows, wta_rejects = parse_wta_api_completed(
            [
                {
                    "DrawMatchType": "S",
                    "MatchState": "F",
                    "Winner": "1",
                    "MatchTimeStamp": "2024-06-08T12:00:00Z",
                    "PlayerNameFirstA": "Iga",
                    "PlayerNameLastA": "Swiatek",
                    "PlayerNameFirstB": "Coco",
                    "PlayerNameLastB": "Gauff",
                    "MatchID": "wta-1",
                }
            ],
            tournament={"name": "Test Open", "city": "London"},
            source=source("wta"),
        )
        self.assertEqual(wta_rows[0].played_on, date(2024, 6, 8))
        self.assertFalse(wta_rejects)

        tv_rows, tv_rejects = parse_tennis_tv_completed(
            [
                {
                    "MatchId": "atp-1",
                    "Status": "Completed",
                    "MatchDate": "2024-06-09T13:00:00Z",
                    "ResultString": "6-4 6-4",
                    "WinningPlayerId": "1",
                    "PlayerTeam1": {
                        "PlayerId": "1",
                        "PlayerFirstNameFull": "Carlos",
                        "PlayerLastName": "Alcaraz",
                    },
                    "PlayerTeam2": {
                        "PlayerId": "2",
                        "PlayerFirstNameFull": "Jannik",
                        "PlayerLastName": "Sinner",
                    },
                }
            ],
            tournament={"Name": "Test Open"},
            source=source("atp"),
        )
        self.assertEqual(tv_rows[0].played_on, date(2024, 6, 9))
        self.assertFalse(tv_rejects)

    def test_live_completed_fetch_catalogs_wta_and_tennis_tv_responses(self) -> None:
        def response(base: str, path: str, params: dict, headers: dict):
            del params, headers
            if path == "/tournaments/":
                return (
                    {
                        "content": [
                            {
                                "year": 2024,
                                "name": "WTA Test",
                                "tournamentGroup": {"id": 11},
                            }
                        ]
                    },
                    base + path,
                    "a" * 64,
                )
            if path == "/tournaments/11/2024/matches":
                return (
                    {
                        "matches": [
                            {
                                "DrawMatchType": "S",
                                "MatchState": "F",
                                "Winner": "1",
                                "MatchTimeStamp": "2024-06-08T12:00:00Z",
                                "PlayerNameFirstA": "Iga",
                                "PlayerNameLastA": "Swiatek",
                                "PlayerNameFirstB": "Coco",
                                "PlayerNameLastB": "Gauff",
                            }
                        ]
                    },
                    base + path,
                    "b" * 64,
                )
            if path == "/tournaments":
                return (
                    [{"id": 22, "year": 2024, "gender": "ATP", "Name": "ATP Test"}],
                    base + path,
                    "c" * 64,
                )
            return (
                {
                    "matches": [
                        {
                            "Status": "Completed",
                            "MatchDate": "2024-06-09T13:00:00Z",
                            "ResultString": "6-4 6-4",
                            "WinningPlayerId": "1",
                            "PlayerTeam1": {
                                "PlayerId": "1",
                                "PlayerFirstName": "Carlos",
                                "PlayerLastName": "Alcaraz",
                            },
                            "PlayerTeam2": {
                                "PlayerId": "2",
                                "PlayerFirstName": "Jannik",
                                "PlayerLastName": "Sinner",
                            },
                        }
                    ]
                },
                base + path,
                "d" * 64,
            )

        with mock.patch("open_tennis_data.exact_dates._json_request", side_effect=response):
            results = fetch_live_completed_sources(2024, date(2024, 7, 1))
        self.assertEqual([item[0].source_label for item in results], ["wta-api", "tennis-tv"])
        self.assertEqual([len(item[1]) for item in results], [1, 1])
        self.assertTrue(all(item[0].source_rows == 1 for item in results))


if __name__ == "__main__":
    unittest.main()
