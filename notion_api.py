"""Small Notion API client shared by the intake and weekly email jobs."""

from __future__ import annotations

import os
import re
import time
from typing import Any, Iterator

import requests

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"


class NotionAPIError(RuntimeError):
    """Raised when Notion returns an unusable response."""


class NotionClient:
    def __init__(
        self,
        api_key: str,
        database_id: str,
        data_source_id: str | None = None,
        data_source_name: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key.strip()
        self.database_id = normalize_notion_id(database_id)
        self._configured_data_source_id = (
            normalize_notion_id(data_source_id) if data_source_id else None
        )
        self.data_source_name = data_source_name.strip() if data_source_name else None
        self.timeout = timeout
        self.session = requests.Session()
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._data_source_id: str | None = None
        self._schema: dict[str, Any] | None = None

    @classmethod
    def from_env(cls) -> "NotionClient":
        return cls(
            api_key=require_env("NOTION_API_KEY"),
            database_id=require_env("NOTION_DATABASE_ID"),
            data_source_id=os.getenv("NOTION_DATA_SOURCE_ID"),
            data_source_name=os.getenv("NOTION_DATA_SOURCE_NAME"),
        )

    @property
    def data_source_id(self) -> str:
        if self._data_source_id:
            return self._data_source_id

        if self._configured_data_source_id:
            self._data_source_id = self._configured_data_source_id
            return self._data_source_id

        database = self._request_json("GET", f"/databases/{self.database_id}")
        sources = database.get("data_sources") or []
        if not sources:
            raise NotionAPIError(
                "The Notion database exposes no data source. Share the database with "
                "the integration and verify NOTION_DATABASE_ID."
            )

        if self.data_source_name:
            matches = [
                source
                for source in sources
                if str(source.get("name", "")).strip() == self.data_source_name
            ]
            if len(matches) != 1:
                available = ", ".join(
                    str(source.get("name") or source.get("id")) for source in sources
                )
                raise NotionAPIError(
                    f'NOTION_DATA_SOURCE_NAME="{self.data_source_name}" did not match '
                    f"exactly one data source. Available: {available}"
                )
            self._data_source_id = normalize_notion_id(matches[0]["id"])
            return self._data_source_id

        if len(sources) > 1:
            available = ", ".join(
                f'{source.get("name", "unnamed")} ({source.get("id")})'
                for source in sources
            )
            raise NotionAPIError(
                "This database has multiple data sources. Set NOTION_DATA_SOURCE_ID "
                f"or NOTION_DATA_SOURCE_NAME. Available: {available}"
            )

        self._data_source_id = normalize_notion_id(sources[0]["id"])
        return self._data_source_id

    @property
    def schema(self) -> dict[str, Any]:
        if self._schema is None:
            data = self._request_json("GET", f"/data_sources/{self.data_source_id}")
            self._schema = data.get("properties") or {}
        return self._schema

    def require_properties(self, expected: dict[str, set[str]]) -> None:
        problems: list[str] = []
        for name, allowed_types in expected.items():
            prop = self.schema.get(name)
            if not prop:
                problems.append(f'"{name}" is missing')
                continue
            actual_type = str(prop.get("type", ""))
            if actual_type not in allowed_types:
                allowed = "/".join(sorted(allowed_types))
                problems.append(f'"{name}" must be {allowed}, found {actual_type or "unknown"}')
        if problems:
            raise NotionAPIError("Invalid Notion schema: " + "; ".join(problems))

    def property_type(self, property_name: str) -> str | None:
        prop = self.schema.get(property_name)
        return str(prop.get("type")) if prop else None

    def query_pages(
        self,
        *,
        filter_: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": min(max(page_size, 1), 100)}
            if filter_:
                payload["filter"] = filter_
            if sorts:
                payload["sorts"] = sorts
            if cursor:
                payload["start_cursor"] = cursor

            data = self._request_json(
                "POST",
                f"/data_sources/{self.data_source_id}/query",
                json=payload,
                safe_to_retry=True,
            )
            yield from data.get("results", [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                raise NotionAPIError("Notion returned has_more=true without next_cursor")

    def create_page(self, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "/pages",
            json={
                "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
                "properties": properties,
            },
            safe_to_retry=False,
        )

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        """Idempotently replace selected properties on an existing page."""
        normalized_page_id = normalize_notion_id(page_id)
        return self._request_json(
            "PATCH",
            f"/pages/{normalized_page_id}",
            json={"properties": properties},
            safe_to_retry=True,
        )

    def url_exists(self, property_name: str, url: str) -> bool:
        filter_ = {"property": property_name, "url": {"equals": url}}
        return next(self.query_pages(filter_=filter_, page_size=1), None) is not None

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        safe_to_retry: bool = True,
    ) -> dict[str, Any]:
        # Mutating requests are not automatically replayed after network/5xx errors,
        # but HTTP 429 is safe to retry because Notion rejected the request.
        attempts = 5
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    NOTION_API_BASE + path,
                    headers=self.headers,
                    json=json,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if not safe_to_retry or attempt + 1 >= attempts:
                    break
                time.sleep(min(2**attempt, 8))
                continue

            if response.ok:
                try:
                    return response.json()
                except ValueError as exc:
                    raise NotionAPIError(
                        f"Notion returned non-JSON for {method} {path}: {response.text[:300]}"
                    ) from exc

            retryable_status = response.status_code == 429 or (
                safe_to_retry and response.status_code in {500, 502, 503, 504}
            )
            if retryable_status:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(2**attempt, 8)
                if attempt + 1 < attempts:
                    time.sleep(delay)
                    continue

            body = response.text[:1200]
            raise NotionAPIError(
                f"Notion {method} {path} failed with HTTP {response.status_code}: {body}"
            )

        raise NotionAPIError(f"Notion {method} {path} failed: {last_error}")


def normalize_notion_id(value: str) -> str:
    """Accept a UUID, compact Notion ID, or a full Notion URL."""
    text = value.strip()
    uuid_matches = re.findall(
        r"(?i)(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
        text,
    )
    compact_matches = re.findall(
        r"(?i)(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])",
        text,
    )
    matches = [match.replace("-", "") for match in uuid_matches] + compact_matches
    if not matches:
        raise ValueError(
            "Could not extract a 32-character Notion ID. Use a database/data-source ID "
            "or its full Notion URL."
        )
    compact = matches[-1].lower()
    return (
        f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-"
        f"{compact[16:20]}-{compact[20:32]}"
    )


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def rich_text_value(prop: dict[str, Any]) -> str:
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(str(item.get("plain_text", "")) for item in items)
