# Stealth Scoring Pipeline — full-profile import and repair

This version fixes the `Unknown` rows shown in Notion.

## Root cause

The two PhantomBuster secrets represent two different stages:

- `PB_AGENT_STEALTH_FR_BE`: **full LinkedIn profile extraction**.
- `PB_AGENT_COMPANY_FOUNDERS`: **LinkedIn URL extraction only**.

The previous importer appended both result files and sent them directly to Notion. A company-founder row containing only `salesNavigatorUrl` therefore became:

- `Nom du fondateur = Unknown`
- an almost-empty `Raw data`
- `Statut = À scorer`

Those rows cannot be scored correctly because the founder background is absent.

## New flow

```text
Full-profile Phantom (stealth) ───────────────┐
                                               ├─ merge complete profile data
Company-founders URL Phantom ─ profile enrich ┘
                                                      │
                                                      ▼
                                    Create new Notion rows
                                    Repair existing Unknown rows
                                    Never import URL-only rows
```

The importer now:

1. Fetches both CSV and JSON result files.
2. Merges duplicate records, retaining the richest version.
3. Detects company rows that contain only a LinkedIn URL.
4. Sends the company result CSV through the full-profile Phantom using a one-launch-only `bonusArgument`.
5. Leaves the full-profile Phantom's saved setup unchanged.
6. Rejects any row that still lacks a founder name and profile details.
7. Updates existing `Unknown` Notion pages in place instead of creating duplicates.
8. Resets repaired rows to `À scorer` so the normal scoring workflow can process them.
9. Stores Raw data in multiple Notion rich-text chunks instead of truncating everything at 2,000 characters.

## Required secrets

Existing secrets remain required:

- `PHANTOMBUSTER_API_KEY`
- `NOTION_API_KEY`
- `NOTION_DATABASE_ID`
- `PB_AGENT_STEALTH_FR_BE`
- `PB_AGENT_COMPANY_FOUNDERS`

### Optional but recommended

`PB_AGENT_PROFILE_ENRICHER`

This can point to a duplicate of the full-profile Phantom. When it is not set, the importer safely reuses `PB_AGENT_STEALTH_FR_BE` for a single launch. The API request uses `bonusArgument` with `saveArgument: false`, so the Phantom's normal saved input is not changed.

## Expected first repair run

The first run should include logs similar to:

```text
Company founders: urls=... need_notion_data=... pending_enrichment=...
Launching profile enrichment agent ... with column=salesNavigatorUrl
Profile enrichment completed successfully
Repaired Notion row: <founder name>
Notion summary: created=... repaired=... unresolved_company=...
```

The July 27 `Unknown` pages are updated in place when their enriched profiles are returned. Do not delete them before running the workflow.

Rows that PhantomBuster has not enriched yet are deliberately not imported. They remain pending and are retried on the next run.

## Local validation

```bash
pip install -r requirements.txt
python -m py_compile score_leads.py
python -m unittest discover -s tests -v
```
