import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import patch

from open_tennis_data.fixtures import parse_wikimedia_fixture_page
from open_tennis_data.sources.wikimedia import (
    api,
    discover_pages,
    fetch_page_revision,
    fetch_pages_at_revisions,
    parse_page,
    parse_tournament_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def page(title, fixture, revision=123):
    return {
        "title": title,
        "page_id": revision,
        "wikidata_id": None,
        "revision_id": revision,
        "revision_timestamp": "2026-07-10T12:00:00Z",
        "content": (FIXTURES / fixture).read_text(encoding="utf-8"),
    }


class WikimediaTests(unittest.TestCase):
    def test_mediawiki_rate_limits_back_off_before_failing(self):
        limited = urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limited",
            {"Retry-After": "7"},
            None,
        )
        with patch(
            "open_tennis_data.sources.wikimedia.urllib.request.urlopen",
            side_effect=limited,
        ), patch("open_tennis_data.sources.wikimedia.time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                api({"action": "query"}, attempts=2)
        sleep.assert_called_once_with(7.0)

    @patch("open_tennis_data.sources.wikimedia.api")
    def test_fetch_page_revision_requires_the_recorded_revision(self, request):
        request.return_value = {
            "query": {
                "pages": [
                    {
                        "title": "Renamed page",
                        "pageid": 10,
                        "pageprops": {"wikibase_item": "Q10"},
                        "revisions": [
                            {
                                "revid": 123,
                                "timestamp": "2026-07-10T12:00:00Z",
                                "slots": {"main": {"content": "draw"}},
                            }
                        ],
                    }
                ]
            }
        }
        result = fetch_page_revision("Recorded page", "123")
        self.assertEqual(result["title"], "Recorded page")
        self.assertEqual(request.call_args.args[0]["revids"], "123")

        request.return_value["query"]["pages"][0]["revisions"][0]["revid"] = 124
        with self.assertRaisesRegex(RuntimeError, "revisions are unavailable"):
            fetch_page_revision("Recorded page", "123")

    @patch("open_tennis_data.sources.wikimedia.api")
    def test_fetch_pages_at_revisions_batches_requests(self, request):
        request.return_value = {
            "query": {
                "pages": [
                    {
                        "pageid": 10,
                        "revisions": [
                            {
                                "revid": revision,
                                "timestamp": "2026-07-10T12:00:00Z",
                                "slots": {"main": {"content": title}},
                            }
                        ],
                    }
                    for title, revision in (("First", 123), ("Second", 456))
                ]
            }
        }
        pages = fetch_pages_at_revisions({"First": "123", "Second": "456"})
        self.assertEqual(set(pages), {"First", "Second"})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0]["revids"], "123|456")

    def test_tournament_page_extracts_annual_date_window(self):
        tournament_page = {
            "title": "2026 Iași Open",
            "page_id": 2063,
            "wikidata_id": "Q2063",
            "content": """
                {{Infobox tennis tournament event
                | date = 13–19 July 2026
                | location = Iași, Romania
                | surface = Clay / outdoor
                }}
            """,
        }
        parsed = parse_tournament_page(tournament_page, "wta", 2026)
        self.assertEqual(parsed["start_date"], date(2026, 7, 13))
        self.assertEqual(parsed["end_date"], date(2026, 7, 19))
        self.assertEqual((parsed["city"], parsed["country"]), ("Iași", "Romania"))
        self.assertEqual(parsed["surface"], "clay")

    def test_tennis_event_info_extracts_cross_month_date_window(self):
        tournament_page = {
            "title": "2026 Wimbledon Championships",
            "page_id": 2026,
            "wikidata_id": "Q2026",
            "content": """
                {{TennisEventInfo|2026|Wimbledon Championships
                | date = 29 June – 12 July 2026
                | location = Wimbledon, London, England
                | surface = Grass / outdoor
                }}
            """,
        }
        parsed = parse_tournament_page(tournament_page, "wta", 2026)
        self.assertEqual(parsed["start_date"], date(2026, 6, 29))
        self.assertEqual(parsed["end_date"], date(2026, 7, 12))
        self.assertEqual(parsed["surface"], "grass")

    def test_tournament_page_extracts_cross_year_date_window(self):
        tournament_page = {
            "title": "2026 United Cup",
            "page_id": 2027,
            "wikidata_id": "Q2027",
            "content": "{{TennisEventInfo|date=28 December – 4 January 2026}}",
        }
        parsed = parse_tournament_page(tournament_page, "atp", 2026)
        self.assertEqual(parsed["start_date"], date(2025, 12, 28))
        self.assertEqual(parsed["end_date"], date(2026, 1, 4))

    def test_tournament_page_keeps_explicit_years_on_both_range_ends(self):
        tournament = {
            "title": "2025 Brisbane International",
            "page_id": 2025,
            "wikidata_id": "Q2025",
            "content": (
                "{{TennisEventInfo|date=30 December 2024 – 5 January 2025"
                "|location=Tennyson, Australia|surface=Hard}}"
            ),
        }
        parsed = parse_tournament_page(tournament, "atp", 2025)
        self.assertEqual(parsed["start_date"], date(2024, 12, 30))
        self.assertEqual(parsed["end_date"], date(2025, 1, 5))

    def test_mens_page_keeps_completed_and_walkover_not_live(self):
        players = {}
        event = parse_page(
            page("2026 Wimbledon Championships – Men's singles", "wimbledon_men.wiki"),
            "atp",
            date(2026, 7, 10),
            players,
        )
        self.assertEqual(len(event["matches"]), 2)
        self.assertEqual({match["status"] for match in event["matches"]}, {"completed", "walkover"})
        self.assertTrue(any(player["name"] == "Jannik Sinner" for player in players.values()))

    def test_womens_unicode_names(self):
        players = {}
        event = parse_page(
            page("2026 Wimbledon Championships – Women's singles", "wimbledon_women.wiki"),
            "wta",
            date(2026, 7, 10),
            players,
        )
        names = {player["name"] for match in event["matches"] for player in match["players"]}
        self.assertIn("Karolína Muchová", names)
        self.assertEqual(len(event["matches"]), 2)

    def test_qualifying_draw(self):
        event = parse_page(
            page(
                "2026 Wimbledon Championships – Men's singles qualifying",
                "wimbledon_qualifying.wiki",
            ),
            "atp",
            date(2026, 6, 29),
            {},
        )
        self.assertEqual(event["event"]["draw"], "qualifying")
        self.assertEqual(event["matches"][0]["round"], "Q1")

    def test_future_draw_parser_keeps_tentative_slots_without_claiming_dates(self):
        players = {}
        event = parse_wikimedia_fixture_page(
            page("2026 Wimbledon Championships – Men's singles", "wimbledon_future.wiki"),
            "atp",
            date(2026, 7, 10),
            players,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(len(event["fixtures"]), 2)
        self.assertEqual({fixture["status"] for fixture in event["fixtures"]}, {"tentative"})
        self.assertTrue(
            all(
                fixture["scheduled_on"] is None and fixture["scheduled_at"] is None
                for fixture in event["fixtures"]
            )
        )

    def test_future_draw_parser_rejects_unsupported_title(self):
        document = page("2026 Wimbledon Championships – Men's singles", "wimbledon_future.wiki")
        document["title"] = "Unsupported page"
        self.assertIsNone(
            parse_wikimedia_fixture_page(document, "atp", date(2026, 7, 10), {})
        )

    @patch("open_tennis_data.sources.wikimedia.api")
    def test_discovers_only_relevant_tour_singles(self, mocked_api):
        mocked_api.return_value = {
            "query": {
                "categorymembers": [
                    {"title": "2026 Wimbledon Championships – Men's singles"},
                    {"title": "2026 Wimbledon Championships – Men's singles qualifying"},
                    {"title": "2026 Wimbledon Championships – Women's singles"},
                    {"title": "2026 Wimbledon Championships – Men's doubles"},
                ]
            }
        }
        titles = discover_pages(2026, "atp")
        self.assertEqual(len(titles), 2)


if __name__ == "__main__":
    unittest.main()
