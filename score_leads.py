"""
Stealth Scoring Pipeline — reliable two-list import.

The two PhantomBuster agents do not return the same schema:

* PB_AGENT_STEALTH_FR_BE is the full-profile extraction Phantom.
* PB_AGENT_COMPANY_FOUNDERS is the URL extraction Phantom.

This importer never writes URL-only rows to Notion. It uses the full-profile
Phantom as an enrichment step for company-founder URLs, merges the resulting
profile data, creates new Notion rows, and repairs existing incomplete rows
(e.g. rows named "Unknown" containing only salesNavigatorUrl).

Scoring remains downstream: repaired/new rows are set to "À scorer".
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
ENRICHMENT_TIMEOUT_SECONDS = int(os.getenv("PB_ENRICHMENT_TIMEOUT_SECONDS", "1800"))
MAX_NOTION_RICH_TEXT_CHUNKS = 95
NOTION_TEXT_CHUNK_SIZE = 1_900

PHANTOMBUSTER_API_KEY = os.environ["PHANTOMBUSTER_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
PB_AGENT_STEALTH_FR_BE = os.environ["PB_AGENT_STEALTH_FR_BE"]
PB_AGENT_COMPANY_FOUNDERS = os.environ["PB_AGENT_COMPANY_FOUNDERS"]

# Optional. When absent, the full-profile stealth Phantom is reused with a
# one-launch-only bonusArgument. Its saved setup is not modified.
PB_AGENT_PROFILE_ENRICHER = (
    os.getenv("PB_AGENT_PROFILE_ENRICHER", "").strip()
    or PB_AGENT_STEALTH_FR_BE
)

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

NAME_FIELDS = (
    "fullName",
    "name",
    "displayName",
    "profileName",
)

PROFESSIONAL_FIELDS = (
    "headline",
    "jobTitle",
    "currentJob",
    "currentCompany",
    "companyName",
    "location",
    "summary",
    "about",
    "experience",
    "experiences",
    "jobs",
    "education",
    "schools",
)

TERMINAL_STREAM_EVENTS = {"summary", "error"}


@dataclass(frozen=True)
class AgentExport:
    source: str
    agent_id: str
    metadata: dict[str, Any]
    csv_url: str
    json_url: str
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class NotionLead:
    page_id: str
    linkedin_url: str
    name: str
    status: str
    raw_data: str
    needs_repair: bool


@dataclass(frozen=True)
class UpsertSummary:
    created: int
    repaired: int
    skipped_complete: int
    skipped_incomplete: int
    failed: int


# ---------------------------------------------------------------------------
# HTTP and configuration helpers
# ---------------------------------------------------------------------------


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int | tuple[int, int] | None = None,
) -> requests.Response:
    return requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=json_body,
        timeout=timeout or REQUEST_TIMEOUT_SECONDS,
    )


def _masked_agent_id(agent_id: str) -> str:
    return "****" if len(agent_id) <= 4 else f"****{agent_id[-4:]}"


def validate_configuration() -> None:
    required = {
        "PHANTOMBUSTER_API_KEY": PHANTOMBUSTER_API_KEY,
        "NOTION_API_KEY": NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
        "PB_AGENT_STEALTH_FR_BE": PB_AGENT_STEALTH_FR_BE,
        "PB_AGENT_COMPANY_FOUNDERS": PB_AGENT_COMPANY_FOUNDERS,
        "PB_AGENT_PROFILE_ENRICHER": PB_AGENT_PROFILE_ENRICHER,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")

    if PB_AGENT_STEALTH_FR_BE == PB_AGENT_COMPANY_FOUNDERS:
        raise RuntimeError(
            "PB_AGENT_STEALTH_FR_BE and PB_AGENT_COMPANY_FOUNDERS must be "
            "different PhantomBuster agents."
        )

    log.info(
        "Agents: profile_data=%s company_urls=%s enricher=%s",
        _masked_agent_id(PB_AGENT_STEALTH_FR_BE),
        _masked_agent_id(PB_AGENT_COMPANY_FOUNDERS),
        _masked_agent_id(PB_AGENT_PROFILE_ENRICHER),
    )


# ---------------------------------------------------------------------------
# Profile parsing, quality and merging
# ---------------------------------------------------------------------------


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _extract_linkedin_url_from_text(value: str) -> str:
    match = LINKEDIN_URL_RE.search(value.strip())
    if not match:
        return ""
    return match.group(0).rstrip("/.,;)")


def extract_linkedin_url(profile: dict[str, Any]) -> str:
    for field in LINKEDIN_URL_FIELDS:
        value = profile.get(field)
        if isinstance(value, str):
            found = _extract_linkedin_url_from_text(value)
            if found:
                return found

    for value in profile.values():
        if isinstance(value, str):
            found = _extract_linkedin_url_from_text(value)
            if found:
                return found
    return ""


def normalize_url(url: str) -> str:
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


def extract_name(profile: dict[str, Any]) -> str:
    for field in NAME_FIELDS:
        value = profile.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()

    first_name = profile.get("firstName")
    last_name = profile.get("lastName")
    combined = " ".join(
        part.strip()
        for part in (first_name, last_name)
        if isinstance(part, str) and part.strip()
    )
    return combined or "Unknown"


def profile_quality_score(profile: dict[str, Any]) -> int:
    """A deterministic richness score used to reject URL-only exports."""
    score = 0
    if extract_name(profile).lower() != "unknown":
        score += 10

    for field in PROFESSIONAL_FIELDS:
        if _nonempty(profile.get(field)):
            score += 3

    ignored = set(LINKEDIN_URL_FIELDS) | {"_source", "_sources"}
    additional_fields = sum(
        1 for key, value in profile.items() if key not in ignored and _nonempty(value)
    )
    score += min(additional_fields, 10)
    return score


def is_complete_profile(profile: dict[str, Any]) -> bool:
    """Require a real name and professional/profile fields, not only a URL."""
    if not extract_linkedin_url(profile):
        return False
    if extract_name(profile).strip().lower() in {"", "unknown", "inconnu"}:
        return False

    has_professional_field = any(
        _nonempty(profile.get(field)) for field in PROFESSIONAL_FIELDS
    )
    non_url_fields = [
        value
        for key, value in profile.items()
        if key not in set(LINKEDIN_URL_FIELDS) | {"_source", "_sources"}
        and _nonempty(value)
    ]
    return has_professional_field or len(non_url_fields) >= 3


def _source_values(profile: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    source = profile.get("_source")
    if isinstance(source, str) and source:
        values.add(source)
    sources = profile.get("_sources")
    if isinstance(sources, list):
        values.update(str(item) for item in sources if item)
    return values


def merge_two_profiles(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicate records, keeping the richer value for conflicting data."""
    left_score = profile_quality_score(left)
    right_score = profile_quality_score(right)
    richer, other = (right, left) if right_score > left_score else (left, right)

    merged = dict(richer)
    for key, value in other.items():
        if key in {"_source", "_sources"}:
            continue
        if key not in merged or not _nonempty(merged[key]):
            merged[key] = value

    sources = _source_values(left) | _source_values(right)
    if sources:
        merged["_sources"] = sorted(sources)
        merged["_source"] = sorted(sources)[0]
    return merged


def merge_profiles_by_url(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        url = normalize_url(extract_linkedin_url(row))
        if not url:
            continue
        if url in merged:
            merged[url] = merge_two_profiles(merged[url], row)
        else:
            merged[url] = dict(row)
    return merged


def choose_url_column(rows: list[dict[str, Any]]) -> str:
    """Return the actual column containing LinkedIn URLs in an export."""
    if not rows:
        return "salesNavigatorUrl"

    for field in LINKEDIN_URL_FIELDS:
        if any(extract_linkedin_url({field: row.get(field)}) for row in rows):
            return field

    for field in rows[0].keys():
        if any(
            isinstance(row.get(field), str)
            and bool(_extract_linkedin_url_from_text(row[field]))
            for row in rows[:20]
        ):
            return field
    return "salesNavigatorUrl"


# ---------------------------------------------------------------------------
# PhantomBuster exports and enrichment
# ---------------------------------------------------------------------------


def _tag_rows(rows: Iterable[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.setdefault("_source", source)
        tagged.append(item)
    return tagged


def _parse_csv(text: str, source: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    return _tag_rows((dict(row) for row in reader), source)


def _find_rows_in_json(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return [dict(item) for item in value]

    if isinstance(value, dict):
        for key in (
            "data",
            "results",
            "profiles",
            "items",
            "rows",
            "leads",
            "result",
        ):
            if key in value:
                found = _find_rows_in_json(value[key])
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
    return None if rows is None else _tag_rows(rows, source)


def _download_rows(
    url: str,
    source: str,
    *,
    cache_bust: bool = False,
) -> list[dict[str, Any]] | None:
    download_url = url
    if cache_bust:
        separator = "&" if "?" in url else "?"
        download_url = f"{url}{separator}ts={int(time.time())}"

    try:
        response = _request("GET", download_url)
    except requests.RequestException as exc:
        log.warning("%s: download failed: %s", source, exc)
        return None

    if not response.ok:
        log.info("%s: result URL returned HTTP %s", source, response.status_code)
        return None

    content_type = response.headers.get("Content-Type", "").lower()
    path_without_query = url.lower().split("?", 1)[0]
    looks_json = "json" in content_type or path_without_query.endswith(".json")
    if looks_json:
        try:
            return _parse_json_payload(response.json(), source)
        except ValueError:
            return _parse_json_payload(response.text, source)
    return _parse_csv(response.text, source)


def get_agent_metadata(agent_id: str) -> dict[str, Any]:
    response = _request(
        "GET",
        "https://api.phantombuster.com/api/v2/agents/fetch",
        headers=PHANTOMBUSTER_HEADERS,
        params={"id": agent_id},
    )
    response.raise_for_status()
    return response.json()


def _agent_result_urls(metadata: dict[str, Any]) -> tuple[str, str]:
    org_s3 = metadata.get("orgS3Folder")
    s3_folder = metadata.get("s3Folder")
    if not org_s3 or not s3_folder:
        raise RuntimeError("PhantomBuster agent metadata has no result-file folders")
    base = f"https://phantombuster.s3.amazonaws.com/{org_s3}/{s3_folder}"
    return f"{base}/result.csv", f"{base}/result.json"


def fetch_agent_export(
    agent_id: str,
    source: str,
    *,
    cache_bust: bool = False,
) -> AgentExport:
    metadata = get_agent_metadata(agent_id)
    csv_url, json_url = _agent_result_urls(metadata)

    # Fetch both formats. CSV is cumulative; JSON is the latest run and can be
    # richer. Duplicates are merged later by LinkedIn URL.
    csv_rows = _download_rows(csv_url, source, cache_bust=cache_bust) or []
    json_rows = _download_rows(json_url, source, cache_bust=cache_bust) or []
    combined = list(merge_profiles_by_url([*csv_rows, *json_rows]).values())

    fields = sorted({key for row in combined[:20] for key in row.keys()})
    log.info(
        "%s: csv=%d json=%d merged=%d fields=%s",
        source,
        len(csv_rows),
        len(json_rows),
        len(combined),
        fields,
    )
    return AgentExport(source, agent_id, metadata, csv_url, json_url, combined)


def _parse_agent_argument(metadata: dict[str, Any]) -> dict[str, Any]:
    argument = metadata.get("argument") or metadata.get("arguments") or {}
    if isinstance(argument, dict):
        return dict(argument)
    if isinstance(argument, str) and argument.strip():
        try:
            parsed = json.loads(argument)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def build_enrichment_bonus_argument(
    enricher_metadata: dict[str, Any],
    input_csv_url: str,
    input_column: str,
) -> dict[str, Any]:
    """Build a single-launch override without changing the saved Phantom setup."""
    saved_argument = _parse_agent_argument(enricher_metadata)
    bonus: dict[str, Any] = {"spreadsheetUrl": input_csv_url}

    # Preserve the exact column-setting key used by this Phantom when visible.
    column_keys = [
        key
        for key in saved_argument
        if "column" in key.lower()
        and any(token in key.lower() for token in ("url", "profile", "name"))
    ]
    for key in column_keys:
        bonus[key] = input_column

    # Common PhantomBuster field. Extra keys are harmless for Phantoms that do
    # not use them, while this fixes the salesNavigatorUrl header in this repo.
    bonus.setdefault("columnName", input_column)
    return bonus


def launch_enrichment_sync(
    agent_id: str,
    metadata: dict[str, Any],
    input_csv_url: str,
    input_column: str,
) -> str:
    """Launch the profile Phantom with the company URL export as temporary input."""
    bonus_argument = build_enrichment_bonus_argument(
        metadata,
        input_csv_url,
        input_column,
    )
    log.info(
        "Launching profile enrichment agent %s with column=%s",
        _masked_agent_id(agent_id),
        input_column,
    )

    response = requests.post(
        "https://api.phantombuster.com/api/v2/agents/launch-sync",
        headers={**PHANTOMBUSTER_HEADERS, "Content-Type": "application/json"},
        json={
            "id": agent_id,
            "bonusArgument": bonus_argument,
            "saveArgument": False,
            "manualLaunch": True,
            "maxInstanceCount": 1,
            "includeLogs": True,
        },
        stream=True,
        timeout=(30, ENRICHMENT_TIMEOUT_SECONDS),
    )
    response.raise_for_status()

    container_id = ""
    summary_seen = False
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line or not raw_line.strip():
            continue
        line = raw_line.strip()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.info("PhantomBuster: %s", line[:500])
            continue

        event_type = str(event.get("type", "")).lower()
        if event_type == "start":
            container_id = str(event.get("containerId") or "")
            log.info("Enrichment container started: %s", container_id or "unknown")
        elif event_type == "logs":
            message = event.get("message") or event.get("data") or event.get("logs")
            if message:
                log.info("Enricher: %s", str(message)[:1_000])
        elif event_type == "error":
            raise RuntimeError(f"PhantomBuster enrichment failed: {event}")
        elif event_type == "summary":
            summary_seen = True
            exit_code = event.get("exitCode")
            if exit_code not in (None, 0, "0"):
                raise RuntimeError(
                    f"PhantomBuster enrichment ended with exit code {exit_code}: {event}"
                )
            log.info("Profile enrichment completed successfully")

    if not summary_seen:
        raise RuntimeError("PhantomBuster enrichment stream ended without a summary")
    return container_id


def _company_source_urls(rows: Iterable[dict[str, Any]]) -> set[str]:
    return {
        normalize_url(extract_linkedin_url(row))
        for row in rows
        if extract_linkedin_url(row)
    }


# ---------------------------------------------------------------------------
# Notion read/repair/upsert
# ---------------------------------------------------------------------------


def _plain_text(prop: dict[str, Any], kind: str) -> str:
    items = prop.get(kind, [])
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            plain = item.get("plain_text")
            if isinstance(plain, str):
                parts.append(plain)
            else:
                content = item.get("text", {}).get("content")
                if isinstance(content, str):
                    parts.append(content)
    return "".join(parts)


def _is_url_only_raw_data(raw_data: str) -> bool:
    if not raw_data.strip():
        return True
    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    return not is_complete_profile(payload)


def notion_page_needs_repair(name: str, status: str, raw_data: str) -> bool:
    normalized_name = name.strip().lower()
    if normalized_name in {"", "unknown", "inconnu"}:
        return True
    return status == "À scorer" and _is_url_only_raw_data(raw_data)


def get_notion_leads() -> dict[str, NotionLead]:
    leads: dict[str, NotionLead] = {}
    cursor: str | None = None

    while True:
        payload: dict[str, Any] = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor

        response = _request(
            "POST",
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=NOTION_HEADERS,
            json_body=payload,
        )
        response.raise_for_status()
        data = response.json()

        for page in data.get("results", []):
            props = page.get("properties", {})
            linkedin_url = props.get("URL LinkedIn", {}).get("url") or ""
            normalized = normalize_url(linkedin_url)
            if not normalized:
                continue

            name = _plain_text(props.get("Nom du fondateur", {}), "title")
            raw_data = _plain_text(props.get("Raw data", {}), "rich_text")
            status = props.get("Statut", {}).get("select") or {}
            status_name = status.get("name", "") if isinstance(status, dict) else ""
            lead = NotionLead(
                page_id=page["id"],
                linkedin_url=normalized,
                name=name,
                status=status_name,
                raw_data=raw_data,
                needs_repair=notion_page_needs_repair(name, status_name, raw_data),
            )

            # Prefer the non-repair duplicate if Notion contains the same URL twice.
            previous = leads.get(normalized)
            if previous is None or (previous.needs_repair and not lead.needs_repair):
                leads[normalized] = lead

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    repair_count = sum(1 for lead in leads.values() if lead.needs_repair)
    log.info(
        "Notion: %d unique URLs, %d incomplete rows eligible for repair",
        len(leads),
        repair_count,
    )
    return leads


def _rich_text_chunks(text: str) -> list[dict[str, Any]]:
    max_chars = MAX_NOTION_RICH_TEXT_CHUNKS * NOTION_TEXT_CHUNK_SIZE
    if len(text) > max_chars:
        text = text[: max_chars - 40] + "\n... [truncated by importer]"
    return [
        {"type": "text", "text": {"content": text[index : index + NOTION_TEXT_CHUNK_SIZE]}}
        for index in range(0, len(text), NOTION_TEXT_CHUNK_SIZE)
    ] or [{"type": "text", "text": {"content": ""}}]


def _profile_properties(profile: dict[str, Any]) -> dict[str, Any]:
    name = extract_name(profile)
    linkedin_url = extract_linkedin_url(profile)
    raw_data = json.dumps(profile, ensure_ascii=False, indent=2, default=str)
    return {
        "Nom du fondateur": {
            "title": [{"type": "text", "text": {"content": name[:2_000]}}]
        },
        "URL LinkedIn": {"url": linkedin_url},
        "Statut": {"select": {"name": "À scorer"}},
        "Raw data": {"rich_text": _rich_text_chunks(raw_data)},
        "Date de scoring": {
            "date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        },
    }


def upsert_profiles_to_notion(
    profiles: Iterable[dict[str, Any]],
    existing: dict[str, NotionLead],
) -> UpsertSummary:
    created = repaired = skipped_complete = skipped_incomplete = failed = 0

    for profile in profiles:
        if not is_complete_profile(profile):
            skipped_incomplete += 1
            log.warning(
                "Not importing incomplete profile: source=%s url=%s fields=%s",
                profile.get("_source", "unknown"),
                extract_linkedin_url(profile),
                sorted(profile.keys()),
            )
            continue

        normalized = normalize_url(extract_linkedin_url(profile))
        notion_lead = existing.get(normalized)
        properties = _profile_properties(profile)

        try:
            if notion_lead and notion_lead.needs_repair:
                response = _request(
                    "PATCH",
                    f"https://api.notion.com/v1/pages/{notion_lead.page_id}",
                    headers=NOTION_HEADERS,
                    json_body={"properties": properties},
                )
                response.raise_for_status()
                repaired += 1
                log.info("Repaired Notion row: %s", extract_name(profile))
            elif notion_lead:
                skipped_complete += 1
            else:
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
                created += 1
                log.info("Created Notion row: %s", extract_name(profile))
        except requests.RequestException as exc:
            failed += 1
            response_text = ""
            if getattr(exc, "response", None) is not None:
                response_text = exc.response.text[:500]
            log.error(
                "Notion upsert failed for %s: %s %s",
                extract_name(profile),
                exc,
                response_text,
            )

    return UpsertSummary(
        created=created,
        repaired=repaired,
        skipped_complete=skipped_complete,
        skipped_incomplete=skipped_incomplete,
        failed=failed,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_pipeline() -> None:
    log.info("=== Founder Import + Repair Pipeline Start ===")
    validate_configuration()

    notion_leads = get_notion_leads()

    # 1) Fetch the existing full-profile dataset and the company URL dataset.
    profile_export = fetch_agent_export(
        PB_AGENT_STEALTH_FR_BE,
        "profile_data",
    )
    company_export = fetch_agent_export(
        PB_AGENT_COMPANY_FOUNDERS,
        "company_founders_urls",
    )

    company_urls = _company_source_urls(company_export.rows)
    profile_map = merge_profiles_by_url(profile_export.rows)
    complete_profile_urls = {
        url for url, profile in profile_map.items() if is_complete_profile(profile)
    }

    # Only profiles missing from Notion or currently malformed need attention.
    company_urls_needing_notion_data = {
        url
        for url in company_urls
        if url not in notion_leads or notion_leads[url].needs_repair
    }
    pending_enrichment = company_urls_needing_notion_data - complete_profile_urls

    log.info(
        "Company founders: urls=%d need_notion_data=%d pending_enrichment=%d",
        len(company_urls),
        len(company_urls_needing_notion_data),
        len(pending_enrichment),
    )

    # 2) The company agent is URL-only. Enrich its output with the same profile
    # Phantom used for stealth founders. No URL-only row is written to Notion.
    if pending_enrichment:
        enricher_metadata = get_agent_metadata(PB_AGENT_PROFILE_ENRICHER)
        input_column = choose_url_column(company_export.rows)
        launch_enrichment_sync(
            PB_AGENT_PROFILE_ENRICHER,
            enricher_metadata,
            company_export.csv_url,
            input_column,
        )

        # S3 may take a few seconds to expose the updated result files.
        time.sleep(3)
        refreshed_profile_export = fetch_agent_export(
            PB_AGENT_PROFILE_ENRICHER,
            "profile_data_enriched",
            cache_bust=True,
        )
        profile_map = merge_profiles_by_url(
            [*profile_map.values(), *refreshed_profile_export.rows]
        )

    # 3) Apply source metadata after enrichment so both feeds have the same
    # actual profile schema while remaining traceable.
    final_profiles: list[dict[str, Any]] = []
    for url, profile in profile_map.items():
        item = dict(profile)
        source = "company_founders" if url in company_urls else "stealth_fr_be"
        sources = _source_values(item) | {source}
        item["_source"] = source
        item["_sources"] = sorted(sources)
        final_profiles.append(item)

    resolved_company_urls = {
        url
        for url, profile in profile_map.items()
        if url in company_urls and is_complete_profile(profile)
    }
    unresolved = company_urls_needing_notion_data - resolved_company_urls
    if unresolved:
        log.warning(
            "%d company-founder URLs still lack full profile data. They were NOT "
            "imported as Unknown and will be retried on the next run. Sample=%s",
            len(unresolved),
            sorted(unresolved)[:10],
        )

    # 4) Create new complete profiles and repair existing Unknown/URL-only rows.
    summary = upsert_profiles_to_notion(final_profiles, notion_leads)
    log.info(
        "Notion summary: created=%d repaired=%d skipped_complete=%d "
        "skipped_incomplete=%d failed=%d unresolved_company=%d",
        summary.created,
        summary.repaired,
        summary.skipped_complete,
        summary.skipped_incomplete,
        summary.failed,
        len(unresolved),
    )

    if summary.failed:
        raise RuntimeError(f"{summary.failed} Notion writes failed")

    log.info("=== Founder Import + Repair Pipeline Complete ===")


if __name__ == "__main__":
    run_pipeline()
