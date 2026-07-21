"""Reliable PhantomBuster -> Notion intake pipeline.

The job downloads each PhantomBuster agent's hosted result file, rejects error
rows, canonicalizes LinkedIn profile URLs, deduplicates profiles across both
exports, then upserts complete CSV-backed records into Notion. Existing rows are
backfilled without overwriting Claude's scoring fields or status.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from notion_api import NotionAPIError, NotionClient, require_env

PB_API_BASE = "https://api.phantombuster.com/api/v2"
PB_S3_BASE = "https://phantombuster.s3.amazonaws.com"
PB_LEGACY_CACHE_BASE = "https://cache1.phantombooster.com"
PARIS_TZ = ZoneInfo("Europe/Paris")
SCORING_PAYLOAD_VERSION = "2026-07-21"
FULL_RAW_PAYLOAD_VERSION = "2026-07-21-full-export-v1"
NOTION_RICH_TEXT_CHUNK_SIZE = 1800  # maximum UTF-8 bytes per text object
NOTION_RICH_TEXT_MAX_OBJECTS = 100
EXPECTED_AGENT_NAMES = {
    "Stealth founders FR/BE": "Stealth founders FR:BE - Extraction data profil",
    "Company founders FR/BE": "Company founders FR:BE - Extraction data profil",
}
SOURCE_FIELD = "_phantombuster_source"

LINKEDIN_FIELD_NAMES = (
    "linkedinProfileUrl",  # exact field used by the current Phantom exports
    "linkedInProfileUrl",
    "defaultProfileUrl",
    "profileUrl",
    "salesNavigatorUrl",
    "linkedinUrl",
    "linkedin",
    "url",
    "query",
)

NAME_FIELD_NAMES = ("fullName", "fullname", "displayName", "name")
ERROR_FIELD_NAMES = (
    "error",
    "errorMessage",
    "error_message",
    "extractionError",
    "scrapingError",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stealth-intake")


@dataclass(frozen=True)
class AgentConfig:
    label: str
    agent_id: str
    secret_name: str
    expected_agent_name: str
    result_filename: str | None = None


@dataclass
class CleanStats:
    input_rows: int = 0
    error_rows: int = 0
    missing_url: int = 0
    missing_name: int = 0
    duplicate_in_run: int = 0
    duplicate_in_notion: int = 0
    accepted: int = 0


class PhantomBusterError(RuntimeError):
    """Raised when an agent result cannot be fetched reliably."""


class PhantomBusterClient:
    def __init__(self, api_key: str, timeout: int = 45) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.headers = {"X-Phantombuster-Key": self.api_key}

    def fetch_results(self, config: AgentConfig) -> list[dict[str, Any]]:
        metadata = self._get_json(
            "/agents/fetch",
            {
                "id": config.agent_id,
                "withManifest": "true",
                "withAgentObject": "true",
            },
        )
        validate_agent_metadata(config, metadata)

        org_folder = str(metadata.get("orgS3Folder") or "").strip("/")
        agent_folder = str(metadata.get("s3Folder") or "").strip("/")
        if not org_folder or not agent_folder:
            raise PhantomBusterError(
                f"{config.label}: /agents/fetch did not return orgS3Folder and s3Folder. "
                "Verify the agent ID and API-key permissions."
            )

        # PhantomBuster's persistent storage normally uses result.json/result.csv.
        # A browser download can be renamed to "result <agent name>.csv" even though
        # that longer display name is not the object key in S3. Try canonical names first.
        filenames: list[str] = ["result.json", "result.csv"]
        filenames.extend(expand_result_filename(config.result_filename))
        filenames.extend(discover_result_filenames(metadata))

        try:
            output = self._get_json("/agents/fetch-output", {"id": config.agent_id})
            filenames.extend(discover_result_filenames(output, require_result_context=True))
        except PhantomBusterError as exc:
            log.warning("%s: unable to inspect latest output: %s", config.label, exc)

        filenames = stable_unique(filenames)
        attempts: list[str] = []
        downloaded: list[tuple[tuple[int, int, int], str, list[dict[str, Any]]]] = []

        # Do not return the first downloadable object. PhantomBuster can expose a
        # compact result.json for the latest execution while result.csv retains the
        # complete export. Download every discoverable result and keep the one with
        # the most unique usable profiles, then the most rows/columns as tie-breakers.
        for filename in filenames:
            safe_filename = quote(filename.lstrip("/"), safe="/")
            urls = [
                f"{PB_S3_BASE}/{quote(org_folder, safe='/')}/{quote(agent_folder, safe='/')}/{safe_filename}",
                # Last-resort compatibility for older agents. The official S3 URL is always tried first.
                f"{PB_LEGACY_CACHE_BASE}/{quote(org_folder, safe='/')}/{quote(agent_folder, safe='/')}/{safe_filename}",
            ]
            for url in urls:
                try:
                    response = self.session.get(url, timeout=self.timeout)
                except requests.RequestException as exc:
                    attempts.append(f"{filename}: network error {exc}")
                    continue

                if response.status_code in {403, 404}:
                    attempts.append(f"{filename}: HTTP {response.status_code}")
                    continue
                if not response.ok:
                    attempts.append(f"{filename}: HTTP {response.status_code}")
                    continue

                try:
                    rows = parse_result_file(filename, response.content, response.headers)
                except ValueError as exc:
                    attempts.append(f"{filename}: invalid result ({exc})")
                    continue

                quality = result_file_quality(rows)
                downloaded.append((quality, filename, rows))
                log.info(
                    "%s: candidate %s has %d row(s), %d unique usable profile(s), %d column(s)",
                    config.label,
                    filename,
                    len(rows),
                    quality[0],
                    quality[2],
                )
                # The same object is mirrored on the legacy host; once one URL for
                # this filename succeeds there is no value in downloading its mirror.
                break

        if downloaded:
            quality, filename, rows = max(downloaded, key=lambda item: item[0])
            log.info(
                "%s: selected %s with %d row(s) and %d unique usable profile(s)",
                config.label,
                filename,
                len(rows),
                quality[0],
            )
            return rows

        attempted = "; ".join(attempts[-12:]) or "no candidate URL was attempted"
        custom_hint = (
            f" Current explicit filename override: {config.result_filename}."
            if config.result_filename
            else " Verify that the secret contains the Agent ID of the profile-data extraction Phantom."
        )
        raise PhantomBusterError(
            f"{config.label}: no valid hosted result file could be downloaded. "
            f"Last attempts: {attempted}.{custom_hint}"
        )

    def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.session.get(
                PB_API_BASE + path,
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PhantomBusterError(f"GET {path} failed: {exc}") from exc

        if not response.ok:
            raise PhantomBusterError(
                f"GET {path} returned HTTP {response.status_code}: {response.text[:800]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise PhantomBusterError(
                f"GET {path} returned non-JSON: {response.text[:300]}"
            ) from exc
        if not isinstance(data, dict):
            raise PhantomBusterError(f"GET {path} returned an unexpected JSON shape")
        return data


def normalized_words(value: str) -> set[str]:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return set(re.findall(r"[a-z0-9]+", ascii_text.casefold()))


def agent_display_name(metadata: dict[str, Any]) -> str:
    for key in ("name", "agentName", "displayName"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def validate_agent_metadata(config: AgentConfig, metadata: dict[str, Any]) -> None:
    """Catch an upstream URL-collector ID before constructing impossible S3 URLs."""
    actual_name = agent_display_name(metadata)
    if not actual_name:
        log.warning(
            "%s: PhantomBuster metadata did not include an agent name; ID=%s",
            config.label,
            config.agent_id,
        )
        return

    log.info(
        "%s: resolved PhantomBuster agent '%s' (ID %s)",
        config.label,
        actual_name,
        config.agent_id,
    )

    actual = normalized_words(actual_name)
    expected = normalized_words(config.expected_agent_name)
    family = "stealth" if "stealth" in expected else "company"
    is_expected_family = family in actual and any(word.startswith("founder") for word in actual)
    is_profile_extractor = (
        "extraction" in actual
        and "data" in actual
        and any(word.startswith("profil") for word in actual)
    )

    if is_expected_family and is_profile_extractor:
        return

    raise PhantomBusterError(
        f"{config.label}: {config.secret_name} points to Phantom '{actual_name}' "
        f"(ID {config.agent_id}), but this pipeline needs '{config.expected_agent_name}'. "
        "Open that profile-extraction Phantom in PhantomBuster and replace the GitHub "
        f"secret {config.secret_name} with its Agent ID. Do not use the upstream "
        "'Extraction URL LinkedIn' Phantom."
    )


def normalize_agent_id(value: str) -> str:
    """Accept a raw PhantomBuster agent ID or a full Phantom URL."""
    text = value.strip()
    if not text:
        raise ValueError("Empty PhantomBuster agent ID")
    if "github.com" in text.lower() and "/settings/secrets/" in text.lower():
        raise ValueError(
            "The secret value is a GitHub settings URL. Store the PhantomBuster agent ID "
            "(or the Phantom's URL), not the GitHub secret-settings page."
        )

    parsed = urlparse(text if "://" in text else "https://placeholder.invalid/" + text)
    if parsed.netloc and parsed.netloc != "placeholder.invalid":
        path = unquote(parsed.path)
        match = re.search(r"/(?:phantoms|agents)/([^/?#]+)", path, flags=re.I)
        if not match:
            raise ValueError(f"Could not extract a PhantomBuster agent ID from URL: {text}")
        return match.group(1).strip()

    if any(char.isspace() for char in text):
        raise ValueError("A PhantomBuster agent ID cannot contain whitespace")
    return text


def expand_result_filename(value: str | None) -> list[str]:
    if not value:
        return []
    filename = value.strip().lstrip("/")
    if not filename:
        return []
    lower = filename.lower()
    if lower.endswith((".csv", ".json")):
        base, ext = filename.rsplit(".", 1)
        other = "json" if ext.lower() == "csv" else "csv"
        return [filename, f"{base}.{other}"]
    return [f"{filename}.json", f"{filename}.csv"]


def discover_result_filenames(
    value: Any,
    *,
    require_result_context: bool = False,
) -> list[str]:
    """Discover plausible custom result filenames in metadata or console output."""
    found: list[str] = []
    positive = re.compile(r"result|output|export|written|saved|generated", re.I)
    negative = re.compile(r"input|source|spreadsheet|argument", re.I)

    def walk(node: Any, key_path: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, key_path + (str(key),))
            return
        if isinstance(node, list):
            for child in node:
                walk(child, key_path)
            return
        if not isinstance(node, str):
            return

        text = node.strip()
        if not text:
            return

        # Agent arguments are often JSON serialized into one metadata field.
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if parsed is not None:
                walk(parsed, key_path)

        context = " ".join(key_path) + " " + text
        key_context = " ".join(key_path)
        if negative.search(key_context) and not positive.search(key_context):
            return
        if require_result_context and not positive.search(context):
            return

        for url_match in re.findall(r"https?://[^\s\"'<>]+?\.(?:csv|json)(?:\?[^\s\"'<>]*)?", text, re.I):
            basename = unquote(urlparse(url_match).path.rsplit("/", 1)[-1])
            if basename:
                found.append(basename)

        for file_match in re.findall(r"([\w .()'&+\-]{1,180}\.(?:csv|json))", text, re.I):
            candidate = file_match.strip()
            if candidate and (positive.search(context) or not require_result_context):
                found.append(candidate)

    walk(value)
    return stable_unique(found)


def parse_result_file(
    filename: str,
    content: bytes,
    headers: requests.structures.CaseInsensitiveDict[str] | dict[str, str],
) -> list[dict[str, Any]]:
    if not content:
        return []

    content_type = str(headers.get("Content-Type", "")).lower()
    prefix = content[:200].lstrip().lower()
    if prefix.startswith((b"<html", b"<!doctype html", b"<?xml")):
        raise ValueError("received an HTML/XML error document")

    is_json = filename.lower().endswith(".json") or "application/json" in content_type
    if is_json:
        try:
            payload = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON") from exc
        return rows_from_json(payload)

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header")
    return [dict(row) for row in reader]


def rows_from_json(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "profiles", "rows"):
            child = payload.get(key)
            if isinstance(child, list):
                return [row for row in child if isinstance(row, dict)]
        if payload and all(isinstance(value, dict) for value in payload.values()):
            return list(payload.values())
    raise ValueError("JSON does not contain a row list")


def stable_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def result_file_quality(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Rank PhantomBuster outputs by usable profiles, row count and field coverage."""
    usable_urls: set[str] = set()
    columns: set[str] = set()
    for row in rows:
        columns.update(str(key) for key in row)
        if row_has_error(row):
            continue
        url = extract_linkedin_url(row)
        if url and extract_name(row):
            usable_urls.add(url)
    return len(usable_urls), len(rows), len(columns)


def case_insensitive_value(row: dict[str, Any], field_names: Iterable[str]) -> Any:
    lookup = {str(key).casefold(): value for key, value in row.items()}
    for field_name in field_names:
        value = lookup.get(field_name.casefold())
        if value is not None:
            return value
    return None


def row_has_error(row: dict[str, Any]) -> bool:
    lookup = {str(key).casefold(): value for key, value in row.items()}
    benign = {"", "0", "false", "none", "null", "no", "no error", "success", "ok"}
    for field in ERROR_FIELD_NAMES:
        if field.casefold() not in lookup:
            continue
        value = lookup[field.casefold()]
        if value is None or value is False or value == 0:
            continue
        if str(value).strip().casefold() not in benign:
            return True
    return False


def extract_linkedin_url(row: dict[str, Any]) -> str:
    for field in LINKEDIN_FIELD_NAMES:
        value = case_insensitive_value(row, (field,))
        if not isinstance(value, str):
            continue
        canonical = canonical_linkedin_url(value)
        if canonical:
            return canonical
    return ""


def canonical_linkedin_url(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if text.startswith("www.") or text.lower().startswith("linkedin.com"):
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""

    host = parsed.netloc.casefold().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    if host not in {"linkedin.com", "m.linkedin.com", "fr.linkedin.com"} and not host.endswith(
        ".linkedin.com"
    ):
        return ""

    path = re.sub(r"/+", "/", unquote(parsed.path)).rstrip("/")
    match = re.match(r"^/in/([^/?#]+)$", path, flags=re.I)
    if not match:
        return ""
    slug = quote(match.group(1).strip(), safe="-._~%")
    if not slug:
        return ""
    return urlunparse(("https", "www.linkedin.com", f"/in/{slug}", "", "", ""))


def extract_name(row: dict[str, Any]) -> str:
    for field in NAME_FIELD_NAMES:
        value = case_insensitive_value(row, (field,))
        if isinstance(value, str) and value.strip():
            return normalize_spaces(value)

    first = case_insensitive_value(row, ("firstName", "first_name", "first"))
    last = case_insensitive_value(row, ("lastName", "last_name", "last"))
    combined = " ".join(
        part.strip() for part in (str(first or ""), str(last or "")) if part.strip()
    )
    return normalize_spaces(combined)


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def nonempty(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def text_field(row: dict[str, Any], name: str, *, max_length: int = 12000) -> str:
    value = row.get(name)
    if not nonempty(value):
        return ""
    return normalize_spaces(str(value))[:max_length]


def int_field(row: dict[str, Any], name: str) -> int | None:
    value = text_field(row, name, max_length=100)
    if not value:
        return None
    digits = re.sub(r"[^0-9-]", "", value)
    try:
        return int(digits)
    except (TypeError, ValueError):
        return None


def compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: child
        for key, child in value.items()
        if child not in (None, "", [], {})
    }


def collect_websites(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for field in ("websites", "website", "website2", "website3"):
        raw = text_field(row, field, max_length=12000)
        if raw:
            values.extend(part.strip() for part in raw.split(",") if part.strip())
    return stable_unique(values)


def extract_experiences(row: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    experiences: list[dict[str, Any]] = []
    for index in range(1, limit + 1):
        experience = compact_dict(
            {
                "company_name": text_field(row, f"jobCompanyName{index}"),
                "company_id": text_field(row, f"jobCompanyId{index}", max_length=200),
                "title": text_field(row, f"jobJobTitle{index}"),
                "description": text_field(row, f"jobDescription{index}", max_length=6000),
                "location": text_field(row, f"jobLocation{index}"),
                "duration": text_field(row, f"jobDuration{index}", max_length=300),
                "date_range": text_field(row, f"jobDateRange{index}", max_length=300),
            }
        )
        if experience:
            experiences.append(experience)
    return experiences


def extract_education(row: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    education: list[dict[str, Any]] = []
    for index in range(1, limit + 1):
        item = compact_dict(
            {
                "school_name": text_field(row, f"schoolSchoolName{index}"),
                "degree": text_field(row, f"schoolDegree{index}"),
                "date_range": text_field(row, f"schoolDateRange{index}", max_length=300),
                "school_url": text_field(row, f"schoolSchoolUrl{index}", max_length=2000),
            }
        )
        if item:
            education.append(item)
    return education


def source_timestamp(row: dict[str, Any]) -> str:
    return text_field(row, "timestamp", max_length=100)


def is_generic_stealth_company(name: str) -> bool:
    normalized = normalize_spaces(name).casefold()
    return bool(re.search(r"\bstealth\b", normalized))


def clean_current_company_metadata(row: dict[str, Any]) -> dict[str, Any]:
    company_name = text_field(row, "currentCompanyName")
    generic_page = is_generic_stealth_company(company_name)
    website = text_field(row, "companyWebsite", max_length=2000)
    industry = text_field(row, "companyIndustry")
    headquarters = text_field(row, "companyWebsiteHeadquarters")

    if website.casefold().rstrip("/") in {
        "https://harmonic.ai/get-discovered",
        "http://stealthstartup.com",
        "https://stealthstartup.com",
    }:
        website = ""
    if generic_page and industry.casefold() == "technology, information and internet":
        industry = ""
    if generic_page and headquarters.casefold() in {
        "san francisco, california, united states",
        "worldwide, united states",
    }:
        headquarters = ""

    return compact_dict(
        {
            "company_page_is_generic": True if generic_page else None,
            "company_website": website,
            "company_industry": industry,
            "company_headquarters": headquarters,
        }
    )


def merge_raw_rows(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Keep the freshest scrape, then backfill its blank fields from the other row."""
    left_ts = source_timestamp(left)
    right_ts = source_timestamp(right)
    primary, secondary = (right, left) if right_ts > left_ts else (left, right)
    merged = dict(primary)
    for key, value in secondary.items():
        if key == SOURCE_FIELD:
            continue
        if not nonempty(merged.get(key)) and nonempty(value):
            merged[key] = value
    return merged


def validate_profile_export_schema(rows: list[dict[str, Any]], label: str) -> None:
    """Fail before import when a Phantom points to the wrong CSV/output file."""
    if not rows:
        return
    keys = {str(key).casefold() for row in rows for key in row}
    required_groups = {
        "LinkedIn profile URL": {name.casefold() for name in LINKEDIN_FIELD_NAMES},
        "founder name": {"fullname", "firstname", "lastname", "name"},
        "career or education data": {
            "headline",
            "currentcompanyname",
            "jobcompanyname1",
            "schoolschoolname1",
        },
    }
    missing = [name for name, candidates in required_groups.items() if not keys.intersection(candidates)]
    if missing:
        raise RuntimeError(
            f"{label}: the downloaded file is not the profile-extraction export; "
            f"missing {', '.join(missing)}. Check that the configured Phantom is the profile-data extraction agent."
        )


def build_scoring_payload(profile: dict[str, Any]) -> dict[str, Any]:
    """Create the compact, stable JSON object consumed by the scoring step."""
    row = profile["raw"]
    payload = {
        "schema_version": SCORING_PAYLOAD_VERSION,
        "source": {
            "lists": profile.get("sources", []),
            "scraped_at": source_timestamp(row),
        },
        "identity": compact_dict(
            {
                "full_name": profile["name"],
                "linkedin_url": profile["linkedin_url"],
                "location": text_field(row, "location"),
                "headline": text_field(row, "headline"),
                "summary": text_field(row, "summary", max_length=8000),
            }
        ),
        "current_role": compact_dict(
            {
                "title": text_field(row, "currentJobTitle"),
                "company_name": text_field(row, "currentCompanyName"),
                "company_id": text_field(row, "currentCompanyId", max_length=200),
                "company_url": text_field(row, "currentCompanyUrl", max_length=2000),
                "company_location": text_field(row, "currentCompanyLocation"),
                "company_description": text_field(
                    row, "currentCompanyDescription", max_length=8000
                ),
                **clean_current_company_metadata(row),
            }
        ),
        "experience": extract_experiences(row),
        "education": extract_education(row),
        "network": compact_dict(
            {
                "connections": int_field(row, "numberOfConnections"),
                "connection_degree": text_field(row, "connectionDegree", max_length=100),
                "shared_connections": int_field(row, "numberOfSharedConnections"),
            }
        ),
        "contact": compact_dict(
            {
                "email": text_field(row, "email", max_length=500),
                "websites": collect_websites(row),
            }
        ),
    }
    return compact_dict(payload)


def build_full_notion_payload(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the complete scoring record written to Notion.

    ``scoring_input`` is normalized for Claude, while ``raw_exports`` preserves
    every original CSV column and value for every valid source row. Nothing is
    truncated or discarded when a profile appears in both PhantomBuster lists.
    """
    raw_exports = profile.get("raw_rows") or [
        {
            "source": source,
            "row": {key: value for key, value in profile["raw"].items() if key != SOURCE_FIELD},
        }
        for source in (profile.get("sources") or [""])
    ]
    return {
        "schema_version": FULL_RAW_PAYLOAD_VERSION,
        "scoring_input": build_scoring_payload(profile),
        "raw_exports": raw_exports,
    }


def clean_profiles(
    rows: list[dict[str, Any]],
    existing_urls: set[str] | None = None,
) -> tuple[list[dict[str, Any]], CleanStats]:
    """Clean and deduplicate profiles without dropping existing Notion rows.

    Existing profiles are returned as well so the pipeline can PATCH their
    ``Raw data`` property. This is required to backfill complete CSV data into
    rows that were created by earlier versions of the pipeline.
    """
    existing_urls = existing_urls or set()
    stats = CleanStats(input_rows=len(rows))
    profiles_by_url: dict[str, dict[str, Any]] = {}
    ordered_urls: list[str] = []

    for annotated_row in rows:
        if row_has_error(annotated_row):
            stats.error_rows += 1
            continue
        url = extract_linkedin_url(annotated_row)
        if not url:
            stats.missing_url += 1
            continue
        name = extract_name(annotated_row)
        if not name:
            stats.missing_name += 1
            continue

        source = text_field(annotated_row, SOURCE_FIELD, max_length=300)
        # Preserve exactly the columns returned by PhantomBuster. The synthetic
        # source marker is stored alongside the row, not inside it.
        raw_row = {
            key: value for key, value in annotated_row.items() if key != SOURCE_FIELD
        }
        raw_export = {"source": source, "row": raw_row}

        if url in profiles_by_url:
            stats.duplicate_in_run += 1
            current = profiles_by_url[url]
            current["raw"] = merge_raw_rows(current["raw"], raw_row)
            current["raw_rows"].append(raw_export)
            if source and source not in current["sources"]:
                current["sources"].append(source)
            current["name"] = extract_name(current["raw"]) or current["name"]
            continue

        profiles_by_url[url] = {
            "name": name,
            "linkedin_url": url,
            "raw": raw_row,
            "raw_rows": [raw_export],
            "sources": [source] if source else [],
        }
        ordered_urls.append(url)

    clean = [profiles_by_url[url] for url in ordered_urls]
    stats.duplicate_in_notion = sum(1 for url in ordered_urls if url in existing_urls)
    stats.accepted = len(clean)
    return clean, stats


def get_existing_linkedin_pages(notion: NotionClient) -> dict[str, list[str]]:
    """Map each canonical LinkedIn URL to all matching Notion page IDs."""
    pages_by_url: dict[str, list[str]] = {}
    for page in notion.query_pages():
        prop = page.get("properties", {}).get("URL LinkedIn", {})
        value = prop.get("url")
        page_id = str(page.get("id") or "").strip()
        if not isinstance(value, str) or not page_id:
            continue
        canonical = canonical_linkedin_url(value)
        if canonical:
            pages_by_url.setdefault(canonical, []).append(page_id)
    return pages_by_url


def split_rich_text(
    value: str,
    chunk_size: int = NOTION_RICH_TEXT_CHUNK_SIZE,
    max_chunks: int = NOTION_RICH_TEXT_MAX_OBJECTS,
) -> list[dict[str, Any]]:
    """Split text into Notion-safe UTF-8 chunks without truncating data.

    Notion documents the limit as 2,000 characters, but its validation can count
    multi-byte Unicode content more aggressively than Python's ``len``. French
    accents, typographic punctuation and emoji therefore made a 1,900-codepoint
    chunk fail as 2,236 units. Limiting each object by UTF-8 byte length is stricter
    and safe for ASCII, accented text and non-BMP characters alike.
    """
    if chunk_size <= 0 or chunk_size > 2000:
        raise ValueError("Notion rich-text chunks must contain 1 to 2,000 UTF-8 bytes")

    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0

    for char in value:
        char_bytes = len(char.encode("utf-8"))
        if char_bytes > chunk_size:
            raise ValueError("A single character exceeds the configured Notion chunk size")
        if current and current_bytes + char_bytes > chunk_size:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(char)
        current_bytes += char_bytes

    if current or not chunks:
        chunks.append("".join(current))

    if len(chunks) > max_chunks:
        raise ValueError(
            f"Raw data requires {len(chunks)} rich-text objects, exceeding the Notion "
            f"limit of {max_chunks}; refusing to truncate any CSV data"
        )
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]

def status_property(property_type: str) -> dict[str, Any]:
    if property_type == "status":
        return {"status": {"name": "À scorer"}}
    return {"select": {"name": "À scorer"}}


def build_notion_properties(
    profile: dict[str, Any],
    notion: NotionClient,
    *,
    include_status: bool,
) -> dict[str, Any]:
    full_json = json.dumps(
        build_full_notion_payload(profile),
        ensure_ascii=False,
        sort_keys=False,
        indent=2,
        default=str,
    )
    properties: dict[str, Any] = {
        "Nom du fondateur": {
            "title": [{"type": "text", "text": {"content": profile["name"][:2000]}}]
        },
        "URL LinkedIn": {"url": profile["linkedin_url"]},
        # This single field contains both a normalized Claude input and every
        # original column/value from every valid CSV source row.
        "Raw data": {"rich_text": split_rich_text(full_json)},
    }
    if include_status:
        properties["Statut"] = status_property(notion.property_type("Statut") or "select")

    if notion.property_type("Date d'import") == "date":
        properties["Date d'import"] = {
            "date": {"start": datetime.now(PARIS_TZ).date().isoformat()}
        }

    source_type = notion.property_type("Source PhantomBuster")
    sources = profile.get("sources", [])
    if source_type == "multi_select":
        properties["Source PhantomBuster"] = {
            "multi_select": [{"name": source[:100]} for source in sources]
        }
    elif source_type == "select" and sources:
        source_name = "Both lists" if len(sources) > 1 else sources[0]
        properties["Source PhantomBuster"] = {"select": {"name": source_name[:100]}}
    elif source_type == "rich_text":
        properties["Source PhantomBuster"] = {
            "rich_text": split_rich_text(", ".join(sources), max_chunks=2)
        }
    return properties


def upsert_profiles(
    notion: NotionClient,
    profiles: list[dict[str, Any]],
    existing_pages: dict[str, list[str]],
) -> tuple[int, int]:
    """Create new profiles and backfill every existing matching Notion row."""
    created = 0
    updated = 0
    for index, profile in enumerate(profiles, start=1):
        page_ids = existing_pages.get(profile["linkedin_url"], [])
        if page_ids:
            properties = build_notion_properties(profile, notion, include_status=False)
            for page_id in page_ids:
                notion.update_page(page_id, properties)
                updated += 1
            log.info(
                "Notion upsert %d/%d: updated %d existing page(s) for %s",
                index,
                len(profiles),
                len(page_ids),
                profile["name"],
            )
        else:
            properties = build_notion_properties(profile, notion, include_status=True)
            try:
                notion.create_page(properties)
            except NotionAPIError as exc:
                # A transient error can be ambiguous after a create. Check by URL
                # before declaring failure, but never create a second copy.
                if notion.url_exists("URL LinkedIn", profile["linkedin_url"]):
                    log.warning(
                        "Notion returned an error for %s, but the URL now exists; "
                        "treating it as created: %s",
                        profile["name"],
                        exc,
                    )
                else:
                    raise
            created += 1
            log.info(
                "Notion upsert %d/%d: created %s",
                index,
                len(profiles),
                profile["name"],
            )
        time.sleep(0.35)
    return created, updated

def read_agent_configs() -> list[AgentConfig]:
    stealth_label = "Stealth founders FR/BE"
    company_label = "Company founders FR/BE"
    return [
        AgentConfig(
            label=stealth_label,
            agent_id=normalize_agent_id(require_env("PB_AGENT_STEALTH_FR_BE")),
            secret_name="PB_AGENT_STEALTH_FR_BE",
            expected_agent_name=EXPECTED_AGENT_NAMES[stealth_label],
            result_filename=None,
        ),
        AgentConfig(
            label=company_label,
            agent_id=normalize_agent_id(require_env("PB_AGENT_COMPANY_FOUNDERS")),
            secret_name="PB_AGENT_COMPANY_FOUNDERS",
            expected_agent_name=EXPECTED_AGENT_NAMES[company_label],
            result_filename=None,
        ),
    ]


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def run_pipeline() -> int:
    log.info("=== Stealth intake started ===")
    phantom = PhantomBusterClient(require_env("PHANTOMBUSTER_API_KEY"))
    notion = NotionClient.from_env()
    notion.require_properties(
        {
            "Nom du fondateur": {"title"},
            "URL LinkedIn": {"url"},
            "Statut": {"select", "status"},
            "Raw data": {"rich_text"},
        }
    )

    allow_partial = env_bool("ALLOW_PARTIAL_FETCH", default=False)
    all_rows: list[dict[str, Any]] = []
    failures: list[str] = []
    per_agent = Counter()

    for config in read_agent_configs():
        try:
            rows = phantom.fetch_results(config)
        except Exception as exc:  # collect both failures before exiting
            failures.append(f"{config.label}: {exc}")
            log.error("%s failed: %s", config.label, exc)
            continue
        validate_profile_export_schema(rows, config.label)
        per_agent[config.label] = len(rows)
        for row in rows:
            annotated = dict(row)
            annotated[SOURCE_FIELD] = config.label
            all_rows.append(annotated)

    if failures and not allow_partial:
        raise RuntimeError(
            "One or more configured PhantomBuster agents failed; refusing a partial import. "
            + " | ".join(failures)
        )
    if failures:
        log.warning("Continuing with partial data because ALLOW_PARTIAL_FETCH=true")
    if not all_rows:
        log.info("Both result files were valid but contained zero rows; nothing to import")
        return 0

    log.info("Fetched rows by agent: %s", dict(per_agent))
    existing_pages = get_existing_linkedin_pages(notion)
    existing_page_count = sum(len(page_ids) for page_ids in existing_pages.values())
    log.info(
        "Notion currently contains %d canonical LinkedIn URL(s) across %d page(s)",
        len(existing_pages),
        existing_page_count,
    )

    clean, stats = clean_profiles(all_rows, set(existing_pages))
    log.info(
        "Quality report: input=%d accepted_unique=%d errors=%d missing_url=%d "
        "missing_name=%d duplicate_in_run=%d existing_in_notion=%d",
        stats.input_rows,
        stats.accepted,
        stats.error_rows,
        stats.missing_url,
        stats.missing_name,
        stats.duplicate_in_run,
        stats.duplicate_in_notion,
    )

    if not clean and stats.input_rows > 0:
        raise RuntimeError(
            "PhantomBuster returned rows, but none had both a valid LinkedIn /in/ URL and a name. "
            "Stopping to avoid silently accepting the wrong export schema."
        )
    if not clean:
        log.info("Both files contained zero usable profiles; nothing to upsert")
        return 0

    created, updated = upsert_profiles(notion, clean, existing_pages)
    log.info(
        "=== Stealth intake complete: %d created, %d existing page(s) updated ===",
        created,
        updated,
    )
    return created + updated


def main() -> None:
    try:
        run_pipeline()
    except Exception as exc:
        log.exception("Pipeline failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
