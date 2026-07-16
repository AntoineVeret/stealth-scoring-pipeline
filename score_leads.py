"""
Stealth Scoring Pipeline (semi-automated)
Fetches new founder profiles from PhantomBuster, deduplicates against
previously seen leads in Notion, and writes raw profiles marked "À scorer".
Scoring happens in Claude.ai via the stealth-scoring skill.
"""

import os
import json
import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PHANTOMBUSTER_API_KEY = os.environ["PHANTOMBUSTER_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

PB_AGENT_STEALTH_FR_BE = os.environ["PB_AGENT_STEALTH_FR_BE"]
PB_AGENT_COMPANY_FOUNDERS = os.environ["PB_AGENT_COMPANY_FOUNDERS"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# PhantomBuster: fetch latest results
# ---------------------------------------------------------------------------
def fetch_phantombuster_results(agent_id: str) -> list[dict]:
    """Fetch the latest result CSV from a PhantomBuster agent."""
    headers = {
        "X-Phantombuster-Key": PHANTOMBUSTER_API_KEY,
        "Content-Type": "application/json",
    }
    params = {"id": agent_id}

    resp = requests.get(
        "https://api.phantombuster.com/api/v2/agents/fetch-output",
        headers=headers, params=params,
    )
    resp.raise_for_status()
    data = resp.json()

    result_url = data.get("output") or data.get("resultObject")
    if not result_url:
        resp2 = requests.get(
            "https://api.phantombuster.com/api/v2/agents/fetch",
            headers=headers, params=params,
        )
        resp2.raise_for_status()
        agent_data = resp2.json()
        s3 = agent_data.get("s3Folder")
        if s3:
            result_url = f"https://cache1.phantombuster.com/{s3}/result.csv"

    if not result_url:
        log.warning(f"No results found for agent {agent_id}")
        return []

    csv_resp = requests.get(result_url)
    csv_resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(csv_resp.text))
    return list(reader)


# ---------------------------------------------------------------------------
# Notion: read existing LinkedIn URLs (dedup)
# ---------------------------------------------------------------------------
def get_existing_linkedin_urls() -> set[str]:
    urls = set()
    has_more = True
    start_cursor = None

    while has_more:
        payload: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=NOTION_HEADERS, json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            props = page.get("properties", {})
            url_prop = props.get("URL LinkedIn", {})
            if url_prop.get("url"):
                urls.add(url_prop["url"].strip().rstrip("/").lower())

        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    log.info(f"Found {len(urls)} existing leads in Notion")
    return urls


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/").lower()
    if "?" in url:
        url = url.split("?")[0]
    return url


def deduplicate(profiles: list[dict], existing_urls: set[str]) -> list[dict]:
    new = []
    for p in profiles:
        url = (
            p.get("profileUrl")
            or p.get("linkedInProfileUrl")
            or p.get("linkedin")
            or p.get("url")
            or ""
        )
        if not url:
            continue
        if normalize_url(url) not in existing_urls:
            new.append(p)
    log.info(f"Deduplicated: {len(profiles)} total, {len(new)} new")
    return new


# ---------------------------------------------------------------------------
# Notion: write raw profiles as "À scorer"
# ---------------------------------------------------------------------------
def write_raw_to_notion(profiles: list[dict]) -> int:
    written = 0
    for p in profiles:
        # Extract name and URL from PhantomBuster fields
        name = (
            p.get("fullName")
            or p.get("firstName", "") + " " + p.get("lastName", "")
            or p.get("name")
            or "Unknown"
        ).strip()

        linkedin_url = (
            p.get("profileUrl")
            or p.get("linkedInProfileUrl")
            or p.get("linkedin")
            or p.get("url")
            or ""
        )

        # Store the full profile data as JSON for Claude to score later
        raw_data = json.dumps(p, ensure_ascii=False, indent=2)
        # Notion rich_text is limited to 2000 chars
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
            resp = requests.post(
                "https://api.notion.com/v1/pages",
                headers=NOTION_HEADERS,
                json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties},
            )
            resp.raise_for_status()
            written += 1
        except requests.HTTPError as e:
            log.error(f"Failed to write {name}: {e}")

    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_pipeline():
    log.info("=== Stealth Pipeline Start ===")

    log.info("Fetching from PhantomBuster...")
    p1 = fetch_phantombuster_results(PB_AGENT_STEALTH_FR_BE)
    p2 = fetch_phantombuster_results(PB_AGENT_COMPANY_FOUNDERS)
    all_profiles = p1 + p2
    log.info(f"Fetched {len(p1)} + {len(p2)} = {len(all_profiles)} profiles")

    if not all_profiles:
        log.info("No profiles fetched. Exiting.")
        return

    existing = get_existing_linkedin_urls()
    new_profiles = deduplicate(all_profiles, existing)

    if not new_profiles:
        log.info("No new profiles. Exiting.")
        return

    log.info(f"Writing {len(new_profiles)} raw profiles to Notion...")
    written = write_raw_to_notion(new_profiles)
    log.info(f"Written {written} profiles as 'À scorer'")
    log.info("=== Pipeline Complete ===")


if __name__ == "__main__":
    run_pipeline()
