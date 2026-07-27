"""Regression tests for the two-source PhantomBuster importer."""

import os
import unittest
from unittest.mock import patch

# score_leads reads its configuration at import time. Dummy values are enough
# for these pure unit tests; no network requests are made.
os.environ.setdefault("PHANTOMBUSTER_API_KEY", "test-key")
os.environ.setdefault("NOTION_API_KEY", "test-notion-key")
os.environ.setdefault("NOTION_DATABASE_ID", "test-database")
os.environ.setdefault("PB_AGENT_STEALTH_FR_BE", "agent-stealth")
os.environ.setdefault("PB_AGENT_COMPANY_FOUNDERS", "agent-company")

from score_leads import (  # noqa: E402
    deduplicate,
    extract_linkedin_url,
    fetch_phantombuster_results,
    normalize_url,
)


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data=None,
        text: str = "",
        content_type: str = "text/csv",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = {"Content-Type": content_type}
        self.ok = 200 <= status_code < 400

    def json(self):
        return self._json_data

    def raise_for_status(self) -> None:
        if not self.ok:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class PhantomBusterFetchTests(unittest.TestCase):
    @patch("score_leads._request")
    def test_uses_documented_phantombuster_s3_url(self, request_mock) -> None:
        request_mock.side_effect = [
            FakeResponse(
                json_data={
                    "orgS3Folder": "workspace-folder",
                    "s3Folder": "agent-folder",
                },
                content_type="application/json",
            ),
            FakeResponse(
                text=(
                    "fullName,linkedinUrl\n"
                    "Example Founder,https://www.linkedin.com/in/example-founder\n"
                )
            ),
        ]

        outcome = fetch_phantombuster_results("agent-company", "company_founders")

        self.assertTrue(outcome.fetched)
        self.assertEqual(len(outcome.rows), 1)
        self.assertEqual(
            request_mock.call_args_list[1].args[1],
            "https://phantombuster.s3.amazonaws.com/"
            "workspace-folder/agent-folder/result.csv",
        )


class LinkedInExtractionTests(unittest.TestCase):
    def test_company_founders_linkedin_url_field(self) -> None:
        profile = {"linkedinUrl": "https://www.linkedin.com/in/antoine-veret/"}
        self.assertEqual(
            extract_linkedin_url(profile),
            "https://www.linkedin.com/in/antoine-veret",
        )

    def test_alternative_linkedin_profile_url_field(self) -> None:
        profile = {"linkedinProfileUrl": "https://linkedin.com/in/example-founder"}
        self.assertEqual(
            extract_linkedin_url(profile),
            "https://linkedin.com/in/example-founder",
        )

    def test_fallback_scans_unknown_column(self) -> None:
        profile = {
            "unexpectedColumn": "Founder: https://fr.linkedin.com/in/example-founder/?trk=test"
        }
        self.assertEqual(
            extract_linkedin_url(profile),
            "https://fr.linkedin.com/in/example-founder/?trk=test",
        )

    def test_company_url_is_not_accepted_as_person_profile(self) -> None:
        profile = {"url": "https://www.linkedin.com/company/example"}
        self.assertEqual(extract_linkedin_url(profile), "")


class DeduplicationTests(unittest.TestCase):
    def test_normalize_removes_query_www_and_trailing_slash(self) -> None:
        self.assertEqual(
            normalize_url("https://www.linkedin.com/in/Example-Founder/?trk=abc"),
            "https://linkedin.com/in/example-founder",
        )

    def test_deduplicates_between_two_sources(self) -> None:
        profiles = [
            {
                "_source": "stealth_fr_be",
                "profileUrl": "https://www.linkedin.com/in/example-founder/",
            },
            {
                "_source": "company_founders",
                "linkedinUrl": "https://linkedin.com/in/example-founder?trk=other",
            },
        ]
        result = deduplicate(profiles, set())
        self.assertEqual(len(result), 1)

    def test_excludes_existing_notion_profile(self) -> None:
        profiles = [
            {"linkedinUrl": "https://www.linkedin.com/in/existing-founder/?trk=abc"}
        ]
        existing = {"https://linkedin.com/in/existing-founder"}
        self.assertEqual(deduplicate(profiles, existing), [])


if __name__ == "__main__":
    unittest.main()
