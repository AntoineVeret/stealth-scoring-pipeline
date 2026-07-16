"""
Stealth Scoring Pipeline
Fetches new founder profiles from PhantomBuster, deduplicates against
previously scored leads in Notion, scores them via Claude API,
and writes results to a Notion database.
"""

import os
import json
import csv
import io
import time
import logging
from datetime import datetime, timezone
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Config (all sourced from environment / GitHub Secrets)
# ---------------------------------------------------------------------------
PHANTOMBUSTER_API_KEY = os.environ["PHANTOMBUSTER_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

# PhantomBuster agent IDs for the two scrapers
PB_AGENT_STEALTH_FR_BE = os.environ["PB_AGENT_STEALTH_FR_BE"]
PB_AGENT_COMPANY_FOUNDERS = os.environ["PB_AGENT_COMPANY_FOUNDERS"]

CLAUDE_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 10  # profiles per Claude API call

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PhantomBuster: fetch latest results
# ---------------------------------------------------------------------------
def fetch_phantombuster_results(agent_id: str) -> list[dict]:
    """Fetch the latest result CSV from a PhantomBuster agent."""
    url = f"https://api.phantombuster.com/api/v2/agents/fetch-output"
    headers = {
        "X-Phantombuster-Key": PHANTOMBUSTER_API_KEY,
        "Content-Type": "application/json",
    }
    params = {"id": agent_id}

    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()

    # The output contains a URL to the result CSV
    result_url = data.get("output") or data.get("resultObject")
    if not result_url:
        # Try fetching from the container's result store
        container_url = f"https://api.phantombuster.com/api/v2/agents/fetch"
        resp2 = requests.get(container_url, headers=headers, params=params)
        resp2.raise_for_status()
        agent_data = resp2.json()
        result_url = agent_data.get("s3Folder")
        if result_url:
            result_url = f"https://cache1.phantombuster.com/{result_url}/result.csv"

    if not result_url:
        log.warning(f"No results found for agent {agent_id}")
        return []

    # Download and parse the CSV
    csv_resp = requests.get(result_url)
    csv_resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(csv_resp.text))
    return list(reader)


# ---------------------------------------------------------------------------
# Notion: read existing scored leads (for deduplication)
# ---------------------------------------------------------------------------
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def get_existing_linkedin_urls() -> set[str]:
    """Query the Notion database and return all LinkedIn URLs already scored."""
    urls = set()
    has_more = True
    start_cursor = None

    while has_more:
        payload: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=NOTION_HEADERS,
            json=payload,
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
def normalize_linkedin_url(url: str) -> str:
    """Normalize a LinkedIn URL for comparison."""
    url = url.strip().rstrip("/").lower()
    # Remove query params
    if "?" in url:
        url = url.split("?")[0]
    return url


def deduplicate(profiles: list[dict], existing_urls: set[str]) -> list[dict]:
    """Remove profiles that have already been scored."""
    new_profiles = []
    for p in profiles:
        # PhantomBuster Sales Navigator scraper uses various column names
        url = (
            p.get("profileUrl")
            or p.get("linkedInProfileUrl")
            or p.get("linkedin")
            or p.get("url")
            or ""
        )
        if not url:
            continue
        if normalize_linkedin_url(url) not in existing_urls:
            new_profiles.append(p)

    log.info(f"Deduplicated: {len(profiles)} total, {len(new_profiles)} new")
    return new_profiles


# ---------------------------------------------------------------------------
# Claude API: score profiles
# ---------------------------------------------------------------------------
SCORING_SYSTEM_PROMPT = """You are a VC deal flow scoring engine. Score each founder profile into categories 1-6 using these EXACT rules:

DEFINITIONS:
- Top background = at least one match from TOP ÉCOLES or TOP EMPLOYEURS (school OR employer)
- Exceptional background = matches in BOTH lists (top school AND top employer)
- Repeat founder = founded/co-founded at least 2 companies (including current stealth venture), with at least one prior company existing 18+ months

SCORING RULES (apply in order, stop at first match):
- Cat 1: Already sold a company for > 50M€
- Cat 2: Already sold a company for < 50M€ or amount unknown
- Cat 3: No exit + repeat founder + top background
- Cat 4: No exit + repeat founder + no top background, OR no exit + first-time founder + exceptional background (top school AND top employer)
- Cat 5: No exit + first-time founder + top background but NOT exceptional (school OR employer, not both)
- Cat 6: No exit + first-time founder + no top background

TOP ÉCOLES include (non-exhaustive, match fuzzy variants):
France: Polytechnique, Mines ParisTech, CentraleSupélec, ENS Paris, Télécom Paris, HEC, ESSEC, ESCP, EDHEC, EM Lyon
Suisse: ETH Zurich, EPFL, IMD, HSG
UK: Oxford, Cambridge, Imperial College, UCL, LBS
USA: MIT, Stanford, Caltech, Carnegie Mellon, Harvard, Wharton, Booth, Kellogg, Columbia
Israel: Technion, Tel Aviv University, Hebrew University, Weizmann
Germany: TU Munich, RWTH Aachen, Mannheim, WHU
Nordics: KTH, Aalto, DTU, Stockholm School of Economics
Plus: all AI research labs (INRIA, MILA, DeepMind, etc.)
And many more European schools (Bocconi, KU Leuven, TU Delft, UPC, etc.)

TOP EMPLOYEURS include:
AI labs: OpenAI, Anthropic, DeepMind, Meta FAIR, Mistral AI, etc.
Big Tech: Google, Meta, Apple, Microsoft, Amazon, Nvidia, Stripe, etc.
Scale-ups: Hugging Face, Dataiku, Alan, Pennylane, Qonto, etc.
Finance: McKinsey, BCG, Bain, Goldman Sachs, JP Morgan, KKR, Sequoia, a16z, etc.
Unicorns: Doctolib, Contentsquare, Revolut, Palantir, SpaceX, Databricks, etc.
Defence: Thales, Airbus, Safran, Helsing, Anduril, etc.

Respond ONLY with a JSON array. For each profile:
{
  "name": "Full Name",
  "exit": "OUI (montant)" or "NON",
  "repeat_founder": "OUI (Company A, Company B)" or "NON",
  "top_school": "OUI (school name)" or "NON",
  "top_employer": "OUI (employer name)" or "NON",
  "linkedin_url": "URL",
  "category": 1-6,
  "rationale": "Brief justification"
}

If info is missing, use "Information non trouvée". Never invent data."""


def score_batch(profiles: list[dict]) -> list[dict]:
    """Send a batch of profiles to Claude for scoring."""
    # Build a readable summary of each profile from the CSV fields
    profile_texts = []
    for i, p in enumerate(profiles):
        lines = [f"--- Profile {i+1} ---"]
        for key, value in p.items():
            if value and value.strip():
                lines.append(f"{key}: {value}")
        profile_texts.append("\n".join(lines))

    user_message = (
        "Score the following founder profiles. Apply the scoring rules exactly.\n\n"
        + "\n\n".join(profile_texts)
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 4096,
            "system": SCORING_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        },
    )
    resp.raise_for_status()
    data = resp.json()

    # Extract text content
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block["text"]

    # Parse JSON from response (handle markdown fences)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.error(f"Failed to parse Claude response: {text[:500]}")
        return []


# ---------------------------------------------------------------------------
# Notion: write scored leads
# ---------------------------------------------------------------------------
def write_to_notion(scored_leads: list[dict]) -> int:
    """Write scored leads to the Notion database. Returns count written."""
    written = 0
    for lead in scored_leads:
        properties: dict[str, Any] = {
            "Nom du fondateur": {"title": [{"text": {"content": lead.get("name", "Unknown")}}]},
            "Exit détecté": {
                "rich_text": [{"text": {"content": str(lead.get("exit", "NON"))}}]
            },
            "Repeat founder": {
                "rich_text": [{"text": {"content": str(lead.get("repeat_founder", "NON"))}}]
            },
            "Top école": {
                "rich_text": [{"text": {"content": str(lead.get("top_school", "NON"))}}]
            },
            "Top employeur": {
                "rich_text": [{"text": {"content": str(lead.get("top_employer", "NON"))}}]
            },
            "URL LinkedIn": {"url": lead.get("linkedin_url", "")},
            "Score final": {"number": lead.get("category", 6)},
        }

        # Optional: add rationale
        if lead.get("rationale"):
            properties["Rationale"] = {
                "rich_text": [{"text": {"content": lead["rationale"][:2000]}}]
            }

        # Add scoring date
        properties["Date de scoring"] = {
            "date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
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
            log.error(f"Failed to write {lead.get('name')}: {e}")

    return written


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run_pipeline():
    log.info("=== Stealth Scoring Pipeline Start ===")

    # 1. Fetch from PhantomBuster
    log.info("Fetching from PhantomBuster...")
    profiles_stealth = fetch_phantombuster_results(PB_AGENT_STEALTH_FR_BE)
    profiles_company = fetch_phantombuster_results(PB_AGENT_COMPANY_FOUNDERS)
    all_profiles = profiles_stealth + profiles_company
    log.info(f"Fetched {len(profiles_stealth)} stealth + {len(profiles_company)} company = {len(all_profiles)} total")

    if not all_profiles:
        log.info("No profiles fetched. Exiting.")
        return

    # 2. Deduplicate against Notion
    log.info("Checking Notion for existing leads...")
    existing_urls = get_existing_linkedin_urls()
    new_profiles = deduplicate(all_profiles, existing_urls)

    if not new_profiles:
        log.info("No new profiles to score. Exiting.")
        return

    # 3. Score in batches via Claude
    log.info(f"Scoring {len(new_profiles)} new profiles...")
    all_scored = []
    for i in range(0, len(new_profiles), BATCH_SIZE):
        batch = new_profiles[i : i + BATCH_SIZE]
        log.info(f"  Scoring batch {i // BATCH_SIZE + 1} ({len(batch)} profiles)...")
        scored = score_batch(batch)
        all_scored.extend(scored)
        if i + BATCH_SIZE < len(new_profiles):
            time.sleep(1)  # rate limiting courtesy

    log.info(f"Scored {len(all_scored)} profiles")

    # 4. Write to Notion
    log.info("Writing to Notion...")
    written = write_to_notion(all_scored)
    log.info(f"Written {written} leads to Notion")

    # 5. Summary
    cat_counts = {}
    for lead in all_scored:
        cat = lead.get("category", "?")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    log.info(f"Category breakdown: {json.dumps(cat_counts, sort_keys=True)}")
    log.info("=== Pipeline Complete ===")


if __name__ == "__main__":
    run_pipeline()
