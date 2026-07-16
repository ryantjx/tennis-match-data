import unittest

from open_tennis_data.model import (
    canonical_player_id,
    canonical_round,
    match_id,
    new_event,
    normalize_text,
    semantic_match,
    slugify,
)
from open_tennis_data.scores import parse_bracket_scores, parse_sackmann_score


class ModelAndScoreTests(unittest.TestCase):
    def test_normalization_and_fallback_identifiers(self):
        self.assertEqual(normalize_text("Émilie's Open"), "emilie s open")
        self.assertEqual(slugify("Émilie's Open"), "emilie-s-open")
        self.assertEqual(canonical_player_id("atp", "123", "Player"), "atp:123")
        self.assertTrue(canonical_player_id("atp", "", "Player").startswith("player_"))

    def test_transient_wikimedia_event_and_semantic_score(self):
        event = new_event(
            tour="wta",
            year=2026,
            event_name="Example",
            draw="main",
            event_id="wikimedia:1",
            event_start_date=None,
            surface=None,
            level=None,
        )
        self.assertEqual(set(event), {"kind", "event", "source_catalog", "matches"})
        self.assertEqual(
            semantic_match(
                {"winner_id": "wta:1", "status": "completed", "round": "F", "score": "7-6(4) RET"}
            ),
            ("wta:1", "completed", "F", "7-6"),
        )

    def test_match_id_is_winner_and_source_independent(self):
        first = match_id("atp", 2026, "Wimbledon Championships", "main", "QF", ["A", "B"])
        second = match_id(
            "atp", 2026, "Wimbledon Championships", "main", "Quarterfinals", ["B", "A"]
        )
        self.assertEqual(first, second)

    def test_parses_tiebreak_and_retirement(self):
        score = parse_sackmann_score("7-6(4) 3-6 4-0 RET")
        self.assertEqual(len(score["sets"]), 3)
        self.assertEqual(score["sets"][0]["tiebreak_loser_points"], 4)
        self.assertEqual(score["termination"], "retired")

    def test_round_aliases(self):
        self.assertEqual(canonical_round("Quarterfinals"), "QF")
        self.assertEqual(canonical_round("R64"), "R64")

    def test_bracket_scores_and_empty_values(self):
        score = parse_bracket_scores(["6", "4", "<sup>r</sup>"], ["3", "6", ""], 0)
        self.assertEqual(score["raw"], "6-3 4-6")
        self.assertEqual(score["termination"], "retired")
        self.assertEqual(parse_sackmann_score("")["sets"], [])


if __name__ == "__main__":
    unittest.main()
