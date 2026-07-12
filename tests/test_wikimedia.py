import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from open_tennis_data.sources.wikimedia import discover_pages, parse_page

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
