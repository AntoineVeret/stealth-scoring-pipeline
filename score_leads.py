"""
Stealth Scoring Pipeline (semi-automated).

Fetches founder profiles from two PhantomBuster agents, validates that both
exports are reachable, deduplicates profiles against Notion and within the
current batch, then writes new profiles to Notion with status "À scorer".

Scoring itself is performed separately through the stealth-scoring skill.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 60

PHANTOMBUSTER_API_KEY = os.environ["PHANTOMBUSTER_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
PB_AGENT_STEALTH_FR_BE = os.environ["PB_AGENT_STEALTH_FR_BE"]
PB_AGENT_COMPANY_FOUNDERS = os.environ["PB_AGENT_COMPANY_FOUNDERS"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

PHANTOMBUSTER_HEADERS = {
    "X-Phantombuster-Key": PHANTOMBUSTER_API_KEY,
    "Accept": "application/json",
}

LINKEDIN_URL_FIELDS = (
    "defaultProfileUrl",
    "profileUrl",
    "linkedinUrl",
    "linkedInUrl",
    "linkedInProfileUrl",
    "linkedinProfileUrl",
    "linkedinProfile",
    "salesNavigatorUrl",
    "salesNavigatorProfileUrl",
    "profileLink",
    "linkedin",
    "url",
    "query",
)

LINKEDIN_URL_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/"
    r"(?:in|pub|sales/lead)/[^\s,;\]\[\)\(\"'<>]+",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class FetchOutcome:
    """Result of fetching one PhantomBuster export."""

    source: str
    agent_id: str
    fetched: bool
    rows: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def _masked_agent_id(agent_id: str) -> str:
    """Return a log-safe representation of an agent ID."""
    if len(agent_id) <= 4:
        return "****"
    return f"****{agent_id[-4:]}"


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> requests.Response:
    """Issue an HTTP request with a consistent timeout."""
    return requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=json_body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def validate_configuration() -> None:
    """Fail early when the two source secrets are missing or identical."""
    required_values = {
        "PHANTOMBUSTER_API_KEY": PHANTOMBUSTER_API_KEY,
        "NOTION_API_KEY": NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
        "PB_AGENT_STEALTH_FR_BE": PB_AGENT_STEALTH_FR_BE,
        "PB_AGENT_COMPANY_FOUNDERS": PB_AGENT_COMPANY_FOUNDERS,
    }
    missing = [name for name, value in required_values.items() if not value.strip()]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    if PB_AGENT_STEALTH_FR_BE.strip() == PB_AGENT_COMPANY_FOUNDERS.strip():
        raise RuntimeError(
            "PB_AGENT_STEALTH_FR_BE and PB_AGENT_COMPANY_FOUNDERS contain the "
            "same agent ID. They must reference two different PhantomBuster agents."
        )

    log.info(
        "Configured PhantomBuster agents: stealth=%s, company_founders=%s",
        _masked_agent_id(PB_AGENT_STEALTH_FR_BE),
        _masked_agent_id(PB_AGENT_COMPANY_FOUNDERS),
    )


# ---------------------------------------------------------------------------
# LinkedIn profile extraction and deduplication
# ---------------------------------------------------------------------------


def _extract_linkedin_url_from_text(value: str) -> str:
    """Extract the first LinkedIn person-profile URL from arbitrary text."""
    match = LINKEDIN_URL_RE.search(value.strip())
    if not match:
        return ""
    return match.group(0).rstrip("/.,;)")


def extract_linkedin_url(profile: dict[str, Any]) -> str:
    """
    Find a LinkedIn profile URL across known PhantomBuster fields.

    The fallback scan makes the importer resilient when a Phantom changes its
    output column name. Only person-profile and Sales Navigator lead URLs are
    accepted; LinkedIn company URLs are deliberately ignored.
    """
    for field in LINKEDIN_URL_FIELDS:
        value = profile.get(field)
        if isinstance(value, str):
            url = _extract_linkedin_url_from_text(value)
            if url:
                return url

    for value in profile.values():
        if isinstance(value, str):
            url = _extract_linkedin_url_from_text(value)
            if url:
                return url

    return ""


def normalize_url(url: str) -> str:
    """Normalize a LinkedIn URL for reliable deduplication."""
    raw = url.strip()
    if not raw:
        return ""

    if not raw.lower().startswith(("http://", "https://")):
        raw = f"https://{raw}"

    parts = urlsplit(raw)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    path = re.sub(r"/+", "/", parts.path).rstrip("/").lower()
    return urlunsplit(("https", host, path, "", ""))


def deduplicate(
    profiles: list[dict[str, Any]],
    existing_urls: set[str],
) -> list[dict[str, Any]]:
    """Remove profiles already in Notion and duplicates across both exports."""
    normalized_existing = {normalize_url(url) for url in existing_urls if url}
    seen_in_batch: set[str] = set()
    new_profiles: list[dict[str, Any]] = []
    missing_url = 0
    duplicates = 0

    for profile in profiles:
        url = extract_linkedin_url(profile)
        if not url:
            missing_url += 1
            log.warning(
                "Skipping row without a recognised LinkedIn profile URL. "
                "source=%s fields=%s",
                profile.get("_source", "unknown"),
                sorted(profile.keys()),
            )
            continue

        normalized = normalize_url(url)
        if normalized in normalized_existing or normalized in seen_in_batch:
            duplicates += 1
            continue

        seen_in_batch.add(normalized)
        new_profiles.append(profile)

    log.info(
        "Deduplication summary: total=%d new=%d duplicates=%d missing_url=%d",
        len(profiles),
        len(new_profiles),
        duplicates,
        missing_url,
    )
    return new_profiles


# ---------------------------------------------------------------------------
# PhantomBuster result parsing
# ---------------------------------------------------------------------------


def _tag_rows(rows: Iterable[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for row in rows:
        tagged_row = dict(row)
        tagged_row.setdefault("_source", source)
        tagged.append(tagged_row)
    return tagged


def _parse_csv(text: str, source: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    return _tag_rows((dict(row) for row in reader), source)


def _find_rows_in_json(value: Any) -> list[dict[str, Any]] | None:
    """Find a list of row dictionaries inside common result-object shapes."""
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return [dict(item) for item in value]

    if isinstance(value, dict):
        for key in ("data", "results", "profiles", "items", "rows"):
            nested = value.get(key)
            found = _find_rows_in_json(nested)
            if found is not None:
                return found

    return None


def _parse_json_payload(payload: Any, source: str) -> list[dict[str, Any]] | None:
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None

    rows = _find_rows_in_json(payload)
    if rows is None:
        return None
    return _tag_rows(rows, source)


def _download_result_url(url: str, source: str) -> list[dict[str, Any]] | None:
    """Download and parse a PhantomBuster CSV or JSON result URL."""
    try:
        response = _request("GET", url)
    except requests.RequestException as exc:
        log.warning("%s: result download failed for %s: %s", source, url, exc)
        return None

    if not response.ok:
        log.info("%s: result URL returned HTTP %s: %s", source, response.status_code, url)
        return None

    content_type = response.headers.get("Content-Type", "").lower()
    looks_json = "json" in content_type or url.lower().endswith(".json")

    if looks_json:
        try:
            parsed = _parse_json_payload(response.json(), source)
        except ValueError:
            parsed = _parse_json_payload(response.text, source)
        return parsed

    return _parse_csv(response.text, source)


def _extract_url_from_result_object(result_object: Any) -> str:
    """Find a downloadable URL inside a PhantomBuster result object."""
    if isinstance(result_object, str) and result_object.startswith("http"):
        return result_object

    if isinstance(result_object, dict):
        for key in ("url", "resultUrl", "resultURL", "csvUrl", "csvURL", "downloadUrl"):
            value = result_object.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value

    return ""


def _fetch_from_result_object(agent_id: str, source: str) -> list[dict[str, Any]] | None:
    """Fallback for agents that expose data through the latest container result object."""
    try:
        output_response = _request(
            "GET",
            "https://api.phantombuster.com/api/v2/agents/fetch-output",
            headers=PHANTOMBUSTER_HEADERS,
            params={"id": agent_id},
        )
        output_response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("%s: could not fetch latest container: %s", source, exc)
        return None

    container_id = output_response.json().get("containerId")
    if not container_id:
        return None

    try:
        result_response = _request(
            "GET",
            "https://api.phantombuster.com/api/v2/containers/fetch-result-object",
            headers=PHANTOMBUSTER_HEADERS,
            params={"id": container_id},
        )
        result_response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("%s: could not fetch result object: %s", source, exc)
        return None

    payload = result_response.json().get("resultObject")

    direct_rows = _parse_json_payload(payload, source)
    if direct_rows is not None:
        return direct_rows

    result_url = _extract_url_from_result_object(payload)
    if result_url:
        return _download_result_url(result_url, source)

    return None


def fetch_phantombuster_results(agent_id: str, source: str) -> FetchOutcome:
    """
    Fetch one agent's export using PhantomBuster's documented S3 result path.

    The previous implementation used cache1.phantombooster.com as its fallback.
    PhantomBuster's documented result location is phantombuster.s3.amazonaws.com.
    This function tries the official CSV and JSON paths first, then falls back
    to the latest container result object.
    """
    safe_agent_id = _masked_agent_id(agent_id)
    log.info("%s: fetching PhantomBuster agent %s", source, safe_agent_id)

    try:
        agent_response = _request(
            "GET",
            "https://api.phantombuster.com/api/v2/agents/fetch",
            headers=PHANTOMBUSTER_HEADERS,
            params={"id": agent_id},
        )
        agent_response.raise_for_status()
        agent_data = agent_response.json()
    except requests.RequestException as exc:
        log.error("%s: failed to fetch agent metadata: %s", source, exc)
        return FetchOutcome(source, agent_id, False, [])

    org_s3 = agent_data.get("orgS3Folder")
    s3_folder = agent_data.get("s3Folder")

    if org_s3 and s3_folder:
        base = f"https://phantombuster.s3.amazonaws.com/{org_s3}/{s3_folder}"
        for filename in ("result.csv", "result.json"):
            rows = _download_result_url(f"{base}/{filename}", source)
            if rows is not None:
                if rows:
                    log.info("%s: fetched %d rows", source, len(rows))
                    log.info("%s: CSV/JSON fields: %s", source, sorted(rows[0].keys()))
                else:
                    log.info("%s: export fetched successfully but contains zero rows", source)
                return FetchOutcome(source, agent_id, True, rows)
    else:
        log.warning(
            "%s: agent metadata is missing orgS3Folder or s3Folder; trying result object",
            source,
        )

    rows = _fetch_from_result_object(agent_id, source)
    if rows is not None:
        if rows:
            log.info("%s: fetched %d rows from result object", source, len(rows))
            log.info("%s: result fields: %s", source, sorted(rows[0].keys()))
        else:
            log.info("%s: result object fetched successfully but contains zero rows", source)
        return FetchOutcome(source, agent_id, True, rows)

    log.error("%s: no readable result file or result object was found", source)
    return FetchOutcome(source, agent_id, False, [])


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------


def get_existing_linkedin_urls() -> set[str]:
    """Read all existing LinkedIn URLs from the Notion database."""
    urls: set[str] = set()
    has_more = True
    start_cursor: str | None = None

    while has_more:
        payload: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        response = _request(
            "POST",
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=NOTION_HEADERS,
            json_body=payload,
        )
        response.raise_for_status()
        data = response.json()

        for page in data.get("results", []):
            properties = page.get("properties", {})
            url_value = properties.get("URL LinkedIn", {}).get("url")
            if isinstance(url_value, str) and url_value.strip():
                urls.add(normalize_url(url_value))

        has_more = bool(data.get("has_more", False))
        start_cursor = data.get("next_cursor")

    log.info("Found %d existing leads in Notion", len(urls))
    return urls


def extract_name(profile: dict[str, Any]) -> str:
    """Extract a display name from common PhantomBuster columns."""
    full_name = profile.get("fullName") or profile.get("name")
    if isinstance(full_name, str) and full_name.strip():
        return full_name.strip()

    first_name = profile.get("firstName")
    last_name = profile.get("lastName")
    combined = " ".join(
        part.strip()
        for part in (first_name, last_name)
        if isinstance(part, str) and part.strip()
    )
    return combined or "Unknown"


def write_raw_to_notion(profiles: list[dict[str, Any]]) -> int:
    """Write new profiles to Notion with the status 'À scorer'."""
    written = 0

    for profile in profiles:
        name = extract_name(profile)
        linkedin_url = extract_linkedin_url(profile)
        if not linkedin_url:
            log.error("Refusing to write %s because its LinkedIn URL is missing", name)
            continue

        raw_data = json.dumps(profile, ensure_ascii=False, indent=2)
        if len(raw_data) > 2000:
            raw_data = raw_data[:1997] + "..."

        properties: dict[str, Any] = {
            "Nom du fondateur": {"title": [{"text": {"content": name}}]},
            "URL LinkedIn": {"url": linkedin_url},
            "Statut": {"select": {"name": "À scorer"}},
            "Raw data": {"rich_text": [{"text": {"content": raw_data}}]},
            "Date de scoring": {
                "date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
            },
        }

        try:
            response = _request(
                "POST",
                "https://api.notion.com/v1/pages",
                headers=NOTION_HEADERS,
                json_body={
                    "parent": {"database_id": NOTION_DATABASE_ID},
                    "properties": properties,
                },
            )
            response.raise_for_status()
            written += 1
        except requests.RequestException as exc:
            response_text = ""
            if getattr(exc, "response", None) is not None:
                response_text = exc.response.text[:500]
            log.error("Failed to write %s: %s %s", name, exc, response_text)

    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_pipeline() -> None:
    log.info("=== Founder Import Pipeline Start ===")
    validate_configuration()

    outcomes = [
        fetch_phantombuster_results(PB_AGENT_STEALTH_FR_BE, "stealth_fr_be"),
        fetch_phantombuster_results(PB_AGENT_COMPANY_FOUNDERS, "company_founders"),
    ]

    failed_sources = [outcome.source for outcome in outcomes if not outcome.fetched]
    if failed_sources:
        raise RuntimeError(
            "The pipeline could not retrieve all required PhantomBuster exports. "
            f"Failed sources: {', '.join(failed_sources)}. "
            "No partial Notion import was performed."
        )

    all_profiles = [row for outcome in outcomes for row in outcome.rows]
    for outcome in outcomes:
        log.info("Source summary: %s=%d rows", outcome.source, len(outcome.rows))
    log.info("Combined PhantomBuster rows: %d", len(all_profiles))

    if not all_profiles:
        log.info("Both exports were fetched successfully and contain no rows. Exiting.")
        return

    existing_urls = get_existing_linkedin_urls()
    new_profiles = deduplicate(all_profiles, existing_urls)
    if not new_profiles:
        log.info("No new profiles. Exiting.")
        return

    log.info("Writing %d raw profiles to Notion...", len(new_profiles))
    written = write_raw_to_notion(new_profiles)
    log.info("Written %d/%d profiles as 'À scorer'", written, len(new_profiles))

    if written != len(new_profiles):
        raise RuntimeError(
            f"Notion import was incomplete: wrote {written} of {len(new_profiles)} profiles."
        )

    log.info("=== Founder Import Pipeline Complete ===")


if __name__ == "__main__":
    run_pipeline()
