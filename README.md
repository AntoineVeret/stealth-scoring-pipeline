# Stealth Scoring Pipeline

Reliable daily intake of founder profiles from two PhantomBuster agents into Notion, followed by a weekly email containing only profiles that have actually been scored.

## What this version fixes

- Downloads PhantomBuster results from the supported hosted-result S3 path rather than guessing `cache1/.../result.csv`.
- Tries the exact verified output filenames first, then supports `result.csv` / `result.json` and discovered custom filenames.
- Uses the actual `linkedinProfileUrl` export field, while retaining compatible fallbacks.
- Removes rows such as `error = Out of Network profile`.
- Rejects rows without a valid LinkedIn `/in/` URL or founder name.
- Canonicalizes LinkedIn URLs and deduplicates both within the current run and against all Notion pages. Duplicate profiles found in both Phantom lists are merged, using the freshest scrape and backfilling missing fields from the other list.
- Fails the action when one configured agent cannot be fetched, instead of silently importing partial data.
- Uses Notion's current data-source API and paginates all queries.
- Builds a compact, versioned scoring payload from the real export columns instead of truncating the first 2,000 characters of an arbitrary 110-column row.
- Keeps `Date de scoring` for the real scoring step; raw imports are marked `À scorer` and can optionally receive `Date d'import`.
- Sends the weekly email only for rows with both a score and a scoring date.

## Actual workflow

```text
PhantomBuster agents
        │
        ▼
score_leads.py (daily)
fetch → validate → remove error rows → canonicalize → deduplicate
        │
        ▼
Notion rows marked "À scorer" with full raw profile JSON
        │
        ▼
Your Claude/Notion scoring step fills score + rationale + Date de scoring
        │
        ▼
weekly_email.py sends only fully scored rows
```

The repository does **not** invent a scoring rubric. The prior code passed an Anthropic key but never called Anthropic; that unused secret has been removed from the daily action. Add automatic scoring only after the exact rubric and expected JSON schema are committed to the repository.

## Verified PhantomBuster schema

The implementation was validated against the two exports supplied on 21 July 2026:

- 303 raw rows in total;
- 168 explicit error rows (`Out of Network profile` or `Not a LinkedIn Profile`);
- 135 valid rows before cross-list deduplication;
- 5 profiles present in both lists;
- 130 unique profiles with usable scoring data.

For every accepted profile, `Raw data` contains this stable scoring object:

```json
{
  "schema_version": "2026-07-21",
  "source": {"lists": [], "scraped_at": ""},
  "identity": {
    "full_name": "",
    "linkedin_url": "",
    "location": "",
    "headline": "",
    "summary": ""
  },
  "current_role": {
    "title": "",
    "company_name": "",
    "company_id": "",
    "company_url": "",
    "company_location": "",
    "company_description": "",
    "company_website": "",
    "company_industry": "",
    "company_headquarters": ""
  },
  "experience": [
    {
      "company_name": "",
      "company_id": "",
      "title": "",
      "description": "",
      "location": "",
      "duration": "",
      "date_range": ""
    }
  ],
  "education": [
    {"school_name": "", "degree": "", "date_range": "", "school_url": ""}
  ],
  "network": {
    "connections": 0,
    "connection_degree": "",
    "shared_connections": 0
  },
  "contact": {"email": "", "websites": []}
}
```

Up to five jobs and five schools are retained. Empty values, profile images, company logos, backgrounds and other non-scoring noise are removed.

## Required Notion properties

### Intake fields

| Property | Type | Required |
|---|---|---|
| `Nom du fondateur` | Title | Yes |
| `URL LinkedIn` | URL | Yes |
| `Statut` | Select or Status | Yes; must allow `À scorer` |
| `Raw data` | Rich text | Yes; contains the normalized scoring payload |
| `Date d'import` | Date | Optional |
| `Source PhantomBuster` | Multi-select, select or rich text | Optional |
| `Raw source data` | Rich text | Optional; stores the full original row for debugging |

### Scoring/email fields

| Property | Type | Required for weekly email |
|---|---|---|
| `Exit détecté` | Rich text | Optional |
| `Repeat founder` | Rich text | Optional |
| `Top école` | Rich text | Optional |
| `Top employeur` | Rich text | Optional |
| `Score final` | Number | Yes |
| `Rationale` | Rich text | Optional |
| `Date de scoring` | Date | Yes |

Share the database with the Notion integration.

## GitHub Action secrets

Required for the daily action:

| Secret | Value |
|---|---|
| `PHANTOMBUSTER_API_KEY` | PhantomBuster API key |
| `PB_AGENT_STEALTH_FR_BE` | Agent ID, or the full PhantomBuster Phantom URL |
| `PB_AGENT_COMPANY_FOUNDERS` | Agent ID, or the full PhantomBuster Phantom URL |
| `NOTION_API_KEY` | Notion integration secret |
| `NOTION_DATABASE_ID` | Notion database ID or full database URL |

The value of `PB_AGENT_COMPANY_FOUNDERS` must **not** be the GitHub settings URL. For example:

```text
Correct: 1234567890123456
Correct: https://phantombuster.com/phantoms/1234567890123456/setup
Wrong:   https://github.com/.../settings/secrets/actions/PB_AGENT_COMPANY_FOUNDERS
```

Optional secrets:

| Secret | When to use |
|---|---|
| `PB_RESULT_FILE_STEALTH_FR_BE` | Override only; defaults to `result Stealth founders FR:BE - Extraction data profil.csv` |
| `PB_RESULT_FILE_COMPANY_FOUNDERS` | Override only; defaults to `result Company founders FR:BE - Extraction data profil.csv` |
| `NOTION_DATA_SOURCE_ID` | Recommended when the database contains multiple data sources |
| `NOTION_DATA_SOURCE_NAME` | Alternative selector for multiple data sources |

An override filename can be entered with or without extension, for example `Stealth founders - output.csv`.

Required for the weekly email:

| Secret | Value |
|---|---|
| `RESEND_API_KEY` | Resend API key |
| `EMAIL_TO` | One address or comma-separated addresses |
| `NOTION_DATABASE_URL` | Link used in the email button |
| `EMAIL_FROM` | Optional; default is Resend's onboarding sender |

## First deployment

Replace the repository contents with this version, then run:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

You can validate downloaded exports locally before pushing:

```bash
python validate_exports.py "company-export.csv" "stealth-export.csv"
```

Then push the files and open **Actions → Daily Stealth Intake → Run workflow**.

A healthy run prints:

- a downloaded filename and row count for each PhantomBuster agent;
- a quality report showing rejected/error/duplicate counts;
- one Notion write line per new profile.

The action exits with a red failure if an agent ID, output filename, result schema, or Notion schema is wrong. This is intentional: a loud failure is safer than a silently incomplete database.

## Scheduling

GitHub cron is UTC:

- Daily: `30 6 * * *` = 08:30 Paris during summer, 07:30 during winter.
- Weekly: `0 18 * * 0` = Sunday 20:00 Paris during summer, 19:00 during winter.
