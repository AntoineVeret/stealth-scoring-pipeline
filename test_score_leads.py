"""Regression tests for enrichment, data quality and Notion repair."""

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("PHANTOMBUSTER_API_KEY", "test-key")
os.environ.setdefault("NOTION_API_KEY", "test-notion-key")
os.environ.setdefault("NOTION_DATABASE_ID", "test-database")
os.environ.setdefault("PB_AGENT_STEALTH_FR_BE", "agent-profile")
os.environ.setdefault("PB_AGENT_COMPANY_FOUNDERS", "agent-company")

from score_leads import (  # noqa: E402
    NotionLead,
    build_enrichment_bonus_argument,
    choose_url_column,
    extract_linkedin_url,
    is_complete_profile,
    merge_profiles_by_url,
    normalize_url,
    notion_page_needs_repair,
    profile_quality_score,
    upsert_profiles_to_notion,
    _rich_text_chunks,
    _utf16_units,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data or {}
        self.ok = 200 <= status_code < 400
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if not self.ok:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class ProfileQualityTests(unittest.TestCase):
    def test_url_only_company_row_is_rejected(self):
        row = {"salesNavigatorUrl": "https://www.linkedin.com/in/example"}
        self.assertFalse(is_complete_profile(row))

    def test_full_profile_is_accepted(self):
        row = {
            "fullName": "Example Founder",
            "profileUrl": "https://www.linkedin.com/in/example",
            "headline": "Founder at Example",
            "location": "Paris",
        }
        self.assertTrue(is_complete_profile(row))
        self.assertGreater(profile_quality_score(row), 10)

    def test_merge_keeps_rich_profile_over_url_only_row(self):
        rows = [
            {"salesNavigatorUrl": "https://linkedin.com/in/example", "_source": "urls"},
            {
                "fullName": "Example Founder",
                "profileUrl": "https://www.linkedin.com/in/example/",
                "headline": "CEO",
                "location": "Paris",
                "_source": "profile",
            },
        ]
        merged = merge_profiles_by_url(rows)
        profile = merged["https://linkedin.com/in/example"]
        self.assertEqual(profile["fullName"], "Example Founder")
        self.assertEqual(set(profile["_sources"]), {"profile", "urls"})


class EnrichmentArgumentTests(unittest.TestCase):
    def test_company_sales_navigator_column_is_detected(self):
        rows = [{"salesNavigatorUrl": "https://linkedin.com/in/example"}]
        self.assertEqual(choose_url_column(rows), "salesNavigatorUrl")

    def test_bonus_argument_overrides_input_for_one_launch(self):
        metadata = {
            "argument": json.dumps(
                {
                    "spreadsheetUrl": "https://old.example/input.csv",
                    "profileUrlColumnName": "profileUrl",
                    "sessionCookie": "secret-value",
                }
            )
        }
        bonus = build_enrichment_bonus_argument(
            metadata,
            "https://example.com/company.csv",
            "salesNavigatorUrl",
        )
        self.assertEqual(bonus["spreadsheetUrl"], "https://example.com/company.csv")
        self.assertEqual(bonus["profileUrlColumnName"], "salesNavigatorUrl")
        self.assertEqual(bonus["columnName"], "salesNavigatorUrl")
        self.assertNotIn("sessionCookie", bonus)


class NotionRepairTests(unittest.TestCase):
    def test_unknown_url_only_page_needs_repair(self):
        raw = json.dumps({"salesNavigatorUrl": "https://linkedin.com/in/example"})
        self.assertTrue(notion_page_needs_repair("Unknown", "À scorer", raw))

    def test_scored_named_page_is_not_repaired(self):
        self.assertFalse(notion_page_needs_repair("Alice Founder", "Scoré", "{}"))

    def test_raw_data_is_chunked_without_2000_character_loss(self):
        original = "x" * 5_100
        chunks = _rich_text_chunks(original)
        reconstructed = "".join(item["text"]["content"] for item in chunks)
        self.assertEqual(reconstructed, original)
        self.assertTrue(
            all(_utf16_units(item["text"]["content"]) <= 1_800 for item in chunks)
        )

    def test_raw_data_with_emoji_stays_under_notion_utf16_limit(self):
        # Python len(emoji) == 1, while UTF-16/Notion can count it as 2.
        original = "🚀" * 1_200 + " founder data " + "🧠" * 1_200
        chunks = _rich_text_chunks(original)
        reconstructed = "".join(item["text"]["content"] for item in chunks)
        self.assertEqual(reconstructed, original)
        self.assertTrue(
            all(_utf16_units(item["text"]["content"]) <= 1_800 for item in chunks)
        )

    @patch("score_leads._request")
    def test_existing_unknown_row_is_updated_not_duplicated(self, request_mock):
        request_mock.return_value = FakeResponse()
        profile = {
            "fullName": "Alice Founder",
            "profileUrl": "https://linkedin.com/in/alice",
            "headline": "Founder",
            "location": "Paris",
        }
        existing = {
            normalize_url(profile["profileUrl"]): NotionLead(
                page_id="notion-page-id",
                linkedin_url=normalize_url(profile["profileUrl"]),
                name="Unknown",
                status="À scorer",
                raw_data=json.dumps({"salesNavigatorUrl": profile["profileUrl"]}),
                needs_repair=True,
            )
        }
        summary = upsert_profiles_to_notion([profile], existing)
        self.assertEqual(summary.repaired, 1)
        self.assertEqual(summary.created, 0)
        self.assertEqual(request_mock.call_args.args[0], "PATCH")
        self.assertIn("notion-page-id", request_mock.call_args.args[1])
        properties = request_mock.call_args.kwargs["json_body"]["properties"]
        self.assertEqual(properties["Statut"], {"select": {"name": "À scorer"}})
        self.assertNotIn("Date de scoring", properties)

    @patch("score_leads._request")
    def test_incomplete_row_is_never_written(self, request_mock):
        summary = upsert_profiles_to_notion(
            [{"salesNavigatorUrl": "https://linkedin.com/in/example"}],
            {},
        )
        self.assertEqual(summary.skipped_incomplete, 1)
        request_mock.assert_not_called()


class UrlTests(unittest.TestCase):
    def test_normalization_removes_tracking_and_www(self):
        self.assertEqual(
            normalize_url("https://www.linkedin.com/in/Example/?trk=abc"),
            "https://linkedin.com/in/example",
        )

    def test_extracts_sales_navigator_named_field(self):
        self.assertEqual(
            extract_linkedin_url(
                {"salesNavigatorUrl": "https://www.linkedin.com/in/example/"}
            ),
            "https://www.linkedin.com/in/example",
        )


if __name__ == "__main__":
    unittest.main()
