# Stealth Scoring Pipeline

Daily PhantomBuster-to-Notion intake for the Cleo Ventures stealth-founder scoring workflow.

## What this version does

The pipeline reads the two profile-data extraction Phantoms:

- `Stealth founders FR:BE - Extraction data profil`
- `Company founders FR:BE - Extraction data profil`

It then:

1. downloads every available `result.json`/`result.csv` candidate and selects the most complete export;
2. rejects explicit PhantomBuster error rows;
3. requires a founder name and a canonical LinkedIn `/in/` URL;
4. deduplicates profiles across both lists;
5. creates new Notion rows;
6. **updates existing Notion rows instead of skipping them**;
7. writes all source data into the existing `Raw data` property.

This last point is the important migration fix. Running the action once backfills the complete CSV data into the rows already visible in the final scoring table.

## Exact `Raw data` structure

Each accepted founder receives one JSON object in `Raw data`:

```json
{
  "schema_version": "2026-07-21-full-export-v1",
  "scoring_input": {
    "schema_version": "2026-07-21",
    "source": {},
    "identity": {},
    "current_role": {},
    "experience": [],
    "education": [],
    "network": {},
    "contact": {}
  },
  "raw_exports": [
    {
      "source": "Company founders FR/BE",
      "row": {
        "salesNavigatorUrl": "",
        "firstName": "",
        "lastName": "",
        "...": "all remaining CSV columns, including empty values"
      }
    }
  ]
}
```

`scoring_input` gives Claude a normalized view. `raw_exports` preserves **every one of the 110 CSV columns and its exact value** for every valid row.

When the same person appears in both PhantomBuster lists, both complete source rows are retained in `raw_exports`; they are not collapsed or discarded.

## Validation against the supplied exports

Validated on the two exports supplied on 21 July 2026:

- 303 raw rows;
- 168 explicit error rows rejected;
- 135 valid source rows;
- 5 cross-list duplicates;
- 130 unique founder profiles;
- 110 fields preserved for every valid source row;
- largest complete Notion payload: 29,674 characters.

The pipeline splits `Raw data` into Notion rich-text objects of at most **1,800 UTF-8 bytes**. This is intentionally stricter than Python character counting: accented text and emoji caused Notion to reject a nominal 1,900-character chunk as 2,236 units. The complete payload is reconstructed exactly, and the pipeline fails loudly instead of truncating if it would require more than 100 objects. The supplied exports require at most 20 objects for one founder.

## Existing rows are now backfilled

Earlier versions skipped a profile whenever its LinkedIn URL already existed in Notion. That left the current 130 rows with only the compact payload shown in the screenshot.

This version performs an upsert:

- **new LinkedIn URL:** create the Notion row and set `Statut = À scorer`;
- **existing LinkedIn URL:** PATCH `Nom du fondateur`, `URL LinkedIn`, `Raw data`, and optional import/source fields.

Updates do **not** overwrite `Score final`, `Rationale`, `Date de scoring`, `Exit détecté`, `Repeat founder`, `Top employeur`, `Top école`, or the existing `Statut`. Claude's scoring output is therefore preserved.

## Required Notion properties

| Property | Type | Required |
|---|---|---|
| `Nom du fondateur` | Title | Yes |
| `URL LinkedIn` | URL | Yes |
| `Raw data` | Rich text | Yes |
| `Statut` | Select or Status | Yes; new profiles use `À scorer` |
| `Date d'import` | Date | Optional |
| `Source PhantomBuster` | Multi-select, select or rich text | Optional |

Scoring properties used by the final table and weekly email:

| Property | Type |
|---|---|
| `Score final` | Number |
| `Rationale` | Rich text |
| `Date de scoring` | Date |
| `Exit détecté` | Rich text |
| `Repeat founder` | Rich text |
| `Top employeur` | Rich text |
| `Top école` | Rich text |

## Required GitHub secrets

| Secret | Value |
|---|---|
| `PHANTOMBUSTER_API_KEY` | PhantomBuster API key |
| `PB_AGENT_STEALTH_FR_BE` | Agent ID of `Stealth founders FR:BE - Extraction data profil` |
| `PB_AGENT_COMPANY_FOUNDERS` | Agent ID of `Company founders FR:BE - Extraction data profil` |
| `NOTION_API_KEY` | Notion integration secret |
| `NOTION_DATABASE_ID` | Notion database ID or full URL |

Optional when the Notion database contains multiple data sources:

- `NOTION_DATA_SOURCE_ID`
- `NOTION_DATA_SOURCE_NAME`

No `PB_RESULT_FILE_*` secret is required.

## Deploy and backfill

Replace the repository contents with this version, commit, and run:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Then open GitHub:

**Actions → Daily Stealth Scoring → Run workflow**

The logs first show each downloadable PhantomBuster candidate and the selected one. When the full CSV objects are available, the successful run should report approximately:

```text
Stealth founders FR/BE: selected result.csv with 103 row(s) and 55 unique usable profile(s)
Company founders FR/BE: selected result.csv with 200 row(s) and 80 unique usable profile(s)
Quality report: input=303 accepted_unique=130 errors=168 ... duplicate_in_run=5 existing_in_notion=130
...
Stealth intake complete: 0 created, 130 existing page(s) updated
```

The exact existing count depends on the current contents of the Notion table. Open any row afterward and inspect `Raw data`: it should contain both `scoring_input` and `raw_exports`.

## Local export validation

```bash
python validate_exports.py \
  "result Company founders FR:BE - Extraction data profil.csv" \
  "result Stealth founders FR:BE - Extraction data profil.csv"
```

Expected result for the supplied files:

```text
Quality: input=303 errors=168 duplicates=5 unique=130
Full Notion payload chars: min=4209 median=11976 max=29674
Raw export preservation: rows=135 fields_per_row_min=110 fields_per_row_max=110
```
