import unittest

from notion_api import NotionClient, normalize_notion_id, rich_text_value


class NotionHelpersTests(unittest.TestCase):
    def test_normalize_compact_id(self):
        self.assertEqual(
            normalize_notion_id("1234567890abcdef1234567890abcdef"),
            "12345678-90ab-cdef-1234-567890abcdef",
        )

    def test_normalize_full_url(self):
        value = "https://www.notion.so/Workspace-1234567890abcdef1234567890abcdef?v=abc"
        self.assertEqual(
            normalize_notion_id(value),
            "12345678-90ab-cdef-1234-567890abcdef",
        )


    def test_update_page_uses_patch_and_preserves_unspecified_properties(self):
        client = NotionClient(
            api_key="secret",
            database_id="1234567890abcdef1234567890abcdef",
        )
        calls = []

        def fake_request(method, path, *, json=None, safe_to_retry=True):
            calls.append((method, path, json, safe_to_retry))
            return {"id": "page"}

        client._request_json = fake_request  # type: ignore[method-assign]
        client.update_page(
            "abcdefabcdefabcdefabcdefabcdefab",
            {"Raw data": {"rich_text": []}},
        )
        self.assertEqual(calls[0][0], "PATCH")
        self.assertEqual(
            calls[0][1],
            "/pages/abcdefab-cdef-abcd-efab-cdefabcdefab",
        )
        self.assertEqual(
            calls[0][2],
            {"properties": {"Raw data": {"rich_text": []}}},
        )
        self.assertTrue(calls[0][3])

    def test_rich_text_uses_plain_text_and_all_chunks(self):
        self.assertEqual(
            rich_text_value(
                {"rich_text": [{"plain_text": "Hello "}, {"plain_text": "world"}]}
            ),
            "Hello world",
        )


if __name__ == "__main__":
    unittest.main()
