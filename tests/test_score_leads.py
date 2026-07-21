import json
import unittest
from unittest.mock import patch

from score_leads import (
    AgentConfig,
    PhantomBusterError,
    SOURCE_FIELD,
    build_full_notion_payload,
    build_notion_properties,
    build_scoring_payload,
    canonical_linkedin_url,
    clean_profiles,
    discover_result_filenames,
    extract_linkedin_url,
    extract_name,
    normalize_agent_id,
    parse_result_file,
    split_rich_text,
    upsert_profiles,
    row_has_error,
    validate_agent_metadata,
    validate_profile_export_schema,
)


class AgentIdTests(unittest.TestCase):
    def test_raw_agent_id(self):
        self.assertEqual(normalize_agent_id("123456789"), "123456789")

    def test_phantom_url(self):
        self.assertEqual(
            normalize_agent_id("https://phantombuster.com/phantoms/987654/setup"),
            "987654",
        )

    def test_rejects_github_settings_url(self):
        with self.assertRaisesRegex(ValueError, "GitHub settings URL"):
            normalize_agent_id(
                "https://github.com/example/repo/settings/secrets/actions/PB_AGENT"
            )


class AgentMetadataTests(unittest.TestCase):
    def test_accepts_profile_extraction_agent(self):
        config = AgentConfig(
            label="Company founders FR/BE",
            agent_id="123",
            secret_name="PB_AGENT_COMPANY_FOUNDERS",
            expected_agent_name="Company founders FR:BE - Extraction data profil",
        )
        validate_agent_metadata(
            config,
            {"name": "Company founders FR/BE - Extraction data profil"},
        )

    def test_rejects_upstream_url_extraction_agent(self):
        config = AgentConfig(
            label="Company founders FR/BE",
            agent_id="123",
            secret_name="PB_AGENT_COMPANY_FOUNDERS",
            expected_agent_name="Company founders FR:BE - Extraction data profil",
        )
        with self.assertRaisesRegex(
            PhantomBusterError,
            "Extraction URL LinkedIn",
        ):
            validate_agent_metadata(
                config,
                {"name": "Company founders FR/BE - Extraction URL LinkedIn"},
            )


class ProfileCleaningTests(unittest.TestCase):
    def test_exact_current_linkedin_field(self):
        row = {"linkedinProfileUrl": "https://www.linkedin.com/in/Jane-Doe/?trk=abc"}
        self.assertEqual(
            extract_linkedin_url(row),
            "https://www.linkedin.com/in/Jane-Doe",
        )

    def test_non_profile_url_is_rejected(self):
        self.assertEqual(canonical_linkedin_url("https://linkedin.com/company/openai"), "")

    def test_name_falls_back_after_blank_first_last(self):
        self.assertEqual(
            extract_name({"firstName": "", "lastName": "", "name": "Jane Doe"}),
            "Jane Doe",
        )

    def test_error_row_is_rejected(self):
        row = {"error": "Out of Network profile"}
        self.assertTrue(row_has_error(row))
        self.assertFalse(row_has_error({"error": ""}))

    def test_dedupes_in_run_and_against_notion(self):
        rows = [
            {
                "fullName": "Jane Doe",
                "linkedinProfileUrl": "https://linkedin.com/in/jane-doe?trk=x",
            },
            {
                "fullName": "Jane Duplicate",
                "linkedinProfileUrl": "https://www.linkedin.com/in/jane-doe/",
            },
            {
                "fullName": "John Existing",
                "linkedinProfileUrl": "https://www.linkedin.com/in/john-existing",
            },
            {
                "fullName": "Broken",
                "linkedinProfileUrl": "https://www.linkedin.com/in/broken",
                "error": "Out of Network profile",
            },
        ]
        clean, stats = clean_profiles(
            rows,
            {"https://www.linkedin.com/in/john-existing"},
        )
        # Existing Notion rows are deliberately retained so they can be PATCHed
        # with the full CSV payload.
        self.assertEqual(
            [profile["name"] for profile in clean],
            ["Jane Doe", "John Existing"],
        )
        self.assertEqual(stats.duplicate_in_run, 1)
        self.assertEqual(stats.duplicate_in_notion, 1)
        self.assertEqual(stats.error_rows, 1)

    def test_duplicate_rows_are_merged_using_freshest_data(self):
        rows = [
            {
                "fullName": "Jane Doe",
                "linkedinProfileUrl": "https://linkedin.com/in/jane-doe",
                "timestamp": "2026-05-01T08:00:00Z",
                "headline": "Older headline",
                "schoolSchoolName1": "HEC Paris",
                SOURCE_FIELD: "Company founders FR/BE",
            },
            {
                "fullName": "Jane Doe",
                "linkedinProfileUrl": "https://linkedin.com/in/jane-doe",
                "timestamp": "2026-06-01T08:00:00Z",
                "headline": "New headline",
                "jobCompanyName1": "Stealth AI Startup",
                SOURCE_FIELD: "Stealth founders FR/BE",
            },
        ]
        clean, stats = clean_profiles(rows, set())
        self.assertEqual(len(clean), 1)
        self.assertEqual(stats.duplicate_in_run, 1)
        self.assertEqual(clean[0]["raw"]["headline"], "New headline")
        self.assertEqual(clean[0]["raw"]["schoolSchoolName1"], "HEC Paris")
        self.assertEqual(len(clean[0]["raw_rows"]), 2)
        self.assertEqual(
            clean[0]["raw_rows"][0]["row"]["headline"],
            "Older headline",
        )
        self.assertEqual(
            clean[0]["raw_rows"][1]["row"]["headline"],
            "New headline",
        )
        self.assertEqual(
            clean[0]["sources"],
            ["Company founders FR/BE", "Stealth founders FR/BE"],
        )

    def test_scoring_payload_contains_actual_export_fields(self):
        profile = {
            "name": "Jane Doe",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe",
            "sources": ["Stealth founders FR/BE"],
            "raw": {
                "timestamp": "2026-07-21T08:30:00Z",
                "location": "Paris, France",
                "headline": "Founder at Stealth AI",
                "summary": "Building an AI company",
                "currentJobTitle": "Founder",
                "currentCompanyName": "Stealth AI Startup",
                "companyIndustry": "Software Development",
                "jobCompanyName1": "Stealth AI Startup",
                "jobJobTitle1": "Founder",
                "jobDateRange1": "06/2026-Present",
                "jobCompanyName2": "Acme",
                "jobJobTitle2": "CEO & Co-founder",
                "jobDateRange2": "2020-2025",
                "schoolSchoolName1": "HEC Paris",
                "schoolDegree1": "Master",
                "schoolDateRange1": "2018-2020",
                "numberOfConnections": "2,642",
                "email": "jane@example.com",
                "websites": "https://example.com,https://github.com/jane",
            },
        }
        payload = build_scoring_payload(profile)
        self.assertEqual(payload["identity"]["headline"], "Founder at Stealth AI")
        self.assertEqual(payload["current_role"]["company_industry"], "Software Development")
        self.assertEqual(len(payload["experience"]), 2)
        self.assertEqual(payload["education"][0]["school_name"], "HEC Paris")
        self.assertEqual(payload["network"]["connections"], 2642)
        self.assertEqual(len(payload["contact"]["websites"]), 2)

    def test_generic_stealth_company_metadata_is_not_treated_as_real(self):
        profile = {
            "name": "Jane Doe",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe",
            "sources": ["Company founders FR/BE"],
            "raw": {
                "currentCompanyName": "Stealth Startup",
                "companyWebsite": "https://harmonic.ai/get-discovered",
                "companyIndustry": "Technology, Information and Internet",
                "companyWebsiteHeadquarters": "San Francisco, California, United States",
            },
        }
        current = build_scoring_payload(profile)["current_role"]
        self.assertTrue(current["company_page_is_generic"])
        self.assertNotIn("company_website", current)
        self.assertNotIn("company_industry", current)
        self.assertNotIn("company_headquarters", current)


    def test_full_notion_payload_preserves_every_raw_column_and_empty_value(self):
        profile = {
            "name": "Jane Doe",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe",
            "sources": ["Company founders FR/BE", "Stealth founders FR/BE"],
            "raw": {
                "fullName": "Jane Doe",
                "linkedinProfileUrl": "https://www.linkedin.com/in/jane-doe",
                "headline": "Founder",
            },
            "raw_rows": [
                {
                    "source": "Company founders FR/BE",
                    "row": {
                        "fullName": "Jane Doe",
                        "addresses": "",
                        "phoneNumbers": "+33 1 23 45 67 89",
                        "jobLogoUrl1": "https://example.com/logo.png",
                        "website3": "https://third.example",
                    },
                },
                {
                    "source": "Stealth founders FR/BE",
                    "row": {
                        "fullName": "Jane Doe",
                        "addresses": "Paris",
                        "phoneNumbers": "",
                        "backgroundUrl": "https://example.com/background.png",
                        "error": "",
                    },
                },
            ],
        }
        payload = build_full_notion_payload(profile)
        self.assertIn("scoring_input", payload)
        self.assertEqual(len(payload["raw_exports"]), 2)
        self.assertEqual(payload["raw_exports"][0]["row"]["addresses"], "")
        self.assertEqual(
            payload["raw_exports"][0]["row"]["jobLogoUrl1"],
            "https://example.com/logo.png",
        )
        self.assertEqual(
            payload["raw_exports"][1]["row"]["backgroundUrl"],
            "https://example.com/background.png",
        )
        self.assertIn("error", payload["raw_exports"][1]["row"])

    def test_rich_text_split_never_silently_truncates(self):
        value = "x" * 16000
        chunks = split_rich_text(value)
        reconstructed = "".join(chunk["text"]["content"] for chunk in chunks)
        self.assertEqual(reconstructed, value)
        with self.assertRaisesRegex(ValueError, "refusing to truncate"):
            split_rich_text("x" * (1900 * 101))

    def test_upsert_updates_existing_raw_data_without_resetting_score_status(self):
        class FakeNotion:
            def __init__(self):
                self.updated = []
                self.created = []

            def property_type(self, name):
                return {"Statut": "status"}.get(name)

            def update_page(self, page_id, properties):
                self.updated.append((page_id, properties))
                return {}

            def create_page(self, properties):
                self.created.append(properties)
                return {}

            def url_exists(self, property_name, url):
                return False

        existing = {
            "name": "Existing Founder",
            "linkedin_url": "https://www.linkedin.com/in/existing",
            "sources": ["Company founders FR/BE"],
            "raw": {"fullName": "Existing Founder"},
            "raw_rows": [
                {
                    "source": "Company founders FR/BE",
                    "row": {"fullName": "Existing Founder", "email": "x@example.com"},
                }
            ],
        }
        new = {
            "name": "New Founder",
            "linkedin_url": "https://www.linkedin.com/in/new",
            "sources": ["Stealth founders FR/BE"],
            "raw": {"fullName": "New Founder"},
            "raw_rows": [
                {
                    "source": "Stealth founders FR/BE",
                    "row": {"fullName": "New Founder"},
                }
            ],
        }
        notion = FakeNotion()
        with patch("score_leads.time.sleep", return_value=None):
            created, updated = upsert_profiles(
                notion,
                [existing, new],
                {existing["linkedin_url"]: ["1234567890abcdef1234567890abcdef"]},
            )
        self.assertEqual((created, updated), (1, 1))
        self.assertNotIn("Statut", notion.updated[0][1])
        self.assertIn("Raw data", notion.updated[0][1])
        self.assertIn("Statut", notion.created[0])

    def test_profile_export_schema_rejects_wrong_csv(self):
        with self.assertRaisesRegex(RuntimeError, "not the profile-extraction export"):
            validate_profile_export_schema(
                [{"companyName": "Acme", "companyUrl": "https://linkedin.com/company/acme"}],
                "Company founders FR/BE",
            )

    def test_profile_export_schema_accepts_actual_headers(self):
        validate_profile_export_schema(
            [
                {
                    "fullName": "Jane Doe",
                    "linkedinProfileUrl": "https://linkedin.com/in/jane",
                    "headline": "Founder",
                    "jobCompanyName1": "Stealth",
                    "schoolSchoolName1": "HEC Paris",
                }
            ],
            "Stealth founders FR/BE",
        )


class ResultFileTests(unittest.TestCase):
    def test_csv_parse_with_bom(self):
        data = "\ufefffullName,linkedinProfileUrl\nJane Doe,https://linkedin.com/in/jane\n"
        rows = parse_result_file("result.csv", data.encode(), {})
        self.assertEqual(rows[0]["fullName"], "Jane Doe")

    def test_json_parse(self):
        data = json.dumps({"results": [{"fullName": "Jane"}]}).encode()
        rows = parse_result_file("result.json", data, {"Content-Type": "application/json"})
        self.assertEqual(rows, [{"fullName": "Jane"}])

    def test_discovers_custom_output_filename_but_not_input(self):
        metadata = {
            "argument": json.dumps(
                {
                    "spreadsheetUrl": "https://example.com/source.csv",
                    "resultFileName": "Stealth founders output.csv",
                }
            )
        }
        self.assertIn("Stealth founders output.csv", discover_result_filenames(metadata))
        self.assertNotIn("source.csv", discover_result_filenames(metadata))


if __name__ == "__main__":
    unittest.main()
