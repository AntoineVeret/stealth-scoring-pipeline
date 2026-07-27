# Stealth Scoring Pipeline

Daily import of founder profiles from two PhantomBuster agents into Notion, followed by a separate scoring workflow and a weekly email summary.

## Architecture

```text
PhantomBuster: stealth founders FR/BE ───────┐
                                             ├─> score_leads.py
PhantomBuster: company founders FR/BE ──────┘      fetch both exports
                                                    validate both sources
                                                    deduplicate
                                                    write to Notion as "À scorer"
                                                           │
                                                           ▼
                                              stealth-scoring skill/workflow
                                                    score Notion leads
                                                           │
                                                           ▼
                                                  weekly_email.py
```

The daily Python importer does not call the Anthropic API directly. It places new profiles in Notion with the status `À scorer`; scoring is handled separately.

## Required Notion properties

Create a Notion database with these property names and types:

| Property | Type |
|---|---|
| `Nom du fondateur` | Title |
| `URL LinkedIn` | URL |
| `Statut` | Select, including `À scorer` |
| `Raw data` | Rich text |
| `Date de scoring` | Date |
| `Exit détecté` | Rich text |
| `Repeat founder` | Rich text |
| `Top école` | Rich text |
| `Top employeur` | Rich text |
| `Score final` | Number |
| `Rationale` | Rich text |

Share the database with the Notion integration used by the pipeline.

## Required GitHub Actions secrets

Add these under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
|---|---|
| `PHANTOMBUSTER_API_KEY` | PhantomBuster API key |
| `NOTION_API_KEY` | Notion integration secret |
| `NOTION_DATABASE_ID` | Target Notion database ID |
| `PB_AGENT_STEALTH_FR_BE` | Agent ID for the stealth founders export |
| `PB_AGENT_COMPANY_FOUNDERS` | Agent ID for the company founders export |
| `ANTHROPIC_API_KEY` | Used by the separate scoring workflow, not the importer |
| `NOTION_DATABASE_URL` | Used by the weekly email link |
| `RESEND_API_KEY` | Weekly email delivery |
| `EMAIL_TO` | Weekly email recipient |

`PB_AGENT_STEALTH_FR_BE` and `PB_AGENT_COMPANY_FOUNDERS` must contain two different agent IDs.

## Daily importer behaviour

The importer:

1. Retrieves metadata for both PhantomBuster agents.
2. Downloads each `result.csv` from PhantomBuster's documented S3 location.
3. Falls back to `result.json` or the latest container result object when needed.
4. Stops without writing to Notion if either required source cannot be retrieved.
5. Recognises common LinkedIn columns such as `profileUrl`, `linkedinUrl`, `linkedinProfileUrl`, and Sales Navigator lead URLs.
6. Scans unknown columns for a LinkedIn person-profile URL when PhantomBuster changes an output field name.
7. Deduplicates against existing Notion URLs and across the two exports.
8. Writes new records with the source included in `Raw data` as `_source`.

## Manual test

Open **Actions → Daily Stealth Scoring → Run workflow**.

A healthy run logs both sources independently:

```text
stealth_fr_be: fetched ... rows
company_founders: fetched ... rows
Source summary: stealth_fr_be=... rows
Source summary: company_founders=... rows
Combined PhantomBuster rows: ...
Deduplication summary: total=... new=... duplicates=... missing_url=...
Written .../... profiles as 'À scorer'
```

The Action fails when:

- a required secret is empty;
- both agent secrets contain the same ID;
- one PhantomBuster export cannot be retrieved;
- only part of the intended Notion batch is written.

## Schedule

The daily workflow uses:

```yaml
cron: "30 6 * * *"
```

GitHub Actions cron is UTC, so this is 08:30 in Paris during CEST and 07:30 during CET.

## Local checks

```bash
pip install -r requirements.txt
python -m py_compile score_leads.py
python -m unittest discover -s tests -v
```
