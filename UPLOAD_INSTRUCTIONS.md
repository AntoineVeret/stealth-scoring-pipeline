# Upload instructions — two PhantomBuster sources fix

## Files to replace or add

Upload these files while preserving their paths:

1. Replace `score_leads.py` at the repository root.
2. Replace `.github/workflows/daily-scoring.yml`.
3. Add `tests/test_score_leads.py`.

No change is required to `requirements.txt`.

## Why this patch is needed

The previous importer had two independent failure modes:

- Its fallback result URL used `cache1.phantombooster.com`. PhantomBuster's documented result-file location is `phantombuster.s3.amazonaws.com/{orgS3Folder}/{s3Folder}/result.csv`.
- Its LinkedIn URL mapper did not recognise common fields such as `linkedinUrl` and `linkedinProfileUrl`, so rows could be silently discarded even after the export was downloaded.

The replacement importer fixes both issues and also:

- identifies each source separately in the GitHub Actions log;
- verifies the two GitHub secrets do not contain the same agent ID;
- refuses to perform a partial Notion import when one source cannot be fetched;
- deduplicates profiles across the two PhantomBuster exports;
- logs rows discarded because they have no recognised LinkedIn profile URL;
- runs regression tests before the live import.

## GitHub upload steps

1. Open the repository's **Code** tab.
2. Upload or edit each file at the path listed above.
3. Commit the changes to `main`.
4. Open **Actions → Daily Stealth Scoring → Run workflow**.
5. Open the run and inspect the `Run founder import pipeline` step.

## Expected successful log

A healthy run should contain lines similar to:

```text
Configured PhantomBuster agents: stealth=****1234, company_founders=****5678
stealth_fr_be: fetched 42 rows
company_founders: fetched 31 rows
Source summary: stealth_fr_be=42 rows
Source summary: company_founders=31 rows
Combined PhantomBuster rows: 73
Deduplication summary: total=73 new=... duplicates=... missing_url=...
Written .../... profiles as 'À scorer'
```

Zero rows is not automatically an error: an export can be fetched successfully and contain zero rows. The Action fails only when a required export cannot be retrieved or when the Notion write is incomplete.

## Secrets to verify

In **Settings → Secrets and variables → Actions**, confirm these repository secrets exist and are non-empty:

- `PHANTOMBUSTER_API_KEY`
- `NOTION_API_KEY`
- `NOTION_DATABASE_ID`
- `PB_AGENT_STEALTH_FR_BE`
- `PB_AGENT_COMPANY_FOUNDERS`

The last two values must be different PhantomBuster agent IDs.

## Important scoring note

This repository's current `score_leads.py` imports profiles into Notion with the status `À scorer`. It does not call the Anthropic API itself. The subsequent score is produced by the separate `stealth-scoring` skill/workflow.
