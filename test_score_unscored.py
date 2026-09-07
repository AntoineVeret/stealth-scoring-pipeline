"""Regression tests for automatic Claude scoring and Notion output."""

import unittest

from score_unscored import (
    FounderSignals,
    build_final_rationale,
    build_notion_score_properties,
    derive_final_score,
    extract_submit_tool_input,
    notion_status_name,
    parse_founder_signals,
    _rich_text,
    _utf16_units,
)


def signals(**overrides):
    values = {
        "exit_detected": False,
        "exit_company": "",
        "exit_evidence": "",
        "repeat_founder": False,
        "repeat_count": 1,
        "repeat_companies": tuple(),
        "top_employer": False,
        "top_employer_name": "",
        "top_employer_evidence": "",
        "top_school": False,
        "top_school_name": "",
        "top_school_evidence": "",
        "ai_relevance": "none",
        "claude_score": 6,
        "claude_rationale": "Pas de signal fort.",
    }
    values.update(overrides)
    return FounderSignals(**values)


class FinalScoreTests(unittest.TestCase):
    def test_score_one_for_exit_repeat_and_both_elite_backgrounds(self):
        value = signals(
            exit_detected=True,
            repeat_founder=True,
            top_employer=True,
            top_school=True,
        )
        self.assertEqual(derive_final_score(value), 1)

    def test_score_two_for_exit_and_repeat(self):
        value = signals(exit_detected=True, repeat_founder=True)
        self.assertEqual(derive_final_score(value), 2)

    def test_score_three_for_exit_without_repeat(self):
        self.assertEqual(derive_final_score(signals(exit_detected=True)), 3)

    def test_score_three_for_repeat_plus_top_background(self):
        value = signals(repeat_founder=True, top_employer=True)
        self.assertEqual(derive_final_score(value), 3)

    def test_score_four_for_repeat_only(self):
        self.assertEqual(derive_final_score(signals(repeat_founder=True)), 4)

    def test_score_five_for_top_background_only(self):
        self.assertEqual(derive_final_score(signals(top_school=True)), 5)

    def test_score_six_without_verified_signal(self):
        self.assertEqual(derive_final_score(signals()), 6)


class StructuredOutputTests(unittest.TestCase):
    def test_extracts_submit_tool_input(self):
        response = {
            "content": [
                {"type": "text", "text": "checked"},
                {
                    "type": "tool_use",
                    "name": "submit_founder_signals",
                    "input": {"claude_score": 4},
                },
            ]
        }
        self.assertEqual(extract_submit_tool_input(response), {"claude_score": 4})

    def test_parse_normalises_non_repeat_founder(self):
        parsed = parse_founder_signals(
            {
                "exit_detected": False,
                "exit_company": "",
                "exit_evidence": "",
                "repeat_founder": False,
                "repeat_count": 4,
                "repeat_companies": ["Should be removed"],
                "top_employer": True,
                "top_employer_name": "Microsoft",
                "top_employer_evidence": "Engineer at Microsoft",
                "top_school": False,
                "top_school_name": "",
                "top_school_evidence": "",
                "ai_relevance": "weak",
                "claude_score": 5,
                "claude_rationale": "Top employeur, faible connexion IA.",
            }
        )
        self.assertFalse(parsed.repeat_founder)
        self.assertEqual(parsed.repeat_count, 1)
        self.assertEqual(parsed.repeat_companies, tuple())


class NotionOutputTests(unittest.TestCase):
    def test_status_reader_supports_select_and_status(self):
        self.assertEqual(notion_status_name({"select": {"name": "À scorer"}}), "À scorer")
        self.assertEqual(notion_status_name({"status": {"name": "Scoré"}}), "Scoré")

    def test_builds_all_scoring_properties_and_marks_scored(self):
        value = signals(
            exit_detected=True,
            exit_company="Sevenhugs",
            repeat_founder=True,
            repeat_count=2,
            repeat_companies=("Sevenhugs",),
            top_school=True,
            top_school_name="HEC Paris",
            ai_relevance="moderate",
            claude_score=2,
            claude_rationale="Exit vérifié et expérience produit IA crédible.",
        )
        properties = build_notion_score_properties(
            value,
            {"Statut": "select"},
            date="2026-07-27",
        )
        self.assertEqual(properties["Score final"]["number"], 2)
        self.assertEqual(properties["Score Claude"]["number"], 2)
        self.assertEqual(properties["Statut"], {"select": {"name": "Scoré"}})
        self.assertEqual(
            properties["Date de scoring"],
            {"date": {"start": "2026-07-27"}},
        )
        self.assertIn("Sevenhugs", properties["Exit détecté"]["rich_text"][0]["text"]["content"])
        self.assertIn("Repeat founder x2", build_final_rationale(value))

    def test_supports_notion_status_property_type(self):
        properties = build_notion_score_properties(
            signals(), {"Statut": "status"}, date="2026-07-27"
        )
        self.assertEqual(properties["Statut"], {"status": {"name": "Scoré"}})

    def test_scoring_rich_text_is_utf16_safe_for_notion(self):
        payload = _rich_text("🚀" * 1_500)
        content = payload["rich_text"][0]["text"]["content"]
        self.assertLessEqual(_utf16_units(content), 1_800)


if __name__ == "__main__":
    unittest.main()
