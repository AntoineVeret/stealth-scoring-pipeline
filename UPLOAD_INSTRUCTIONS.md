# Upload instructions

## 1. Replace these repository files

Upload while preserving their paths:

```text
score_leads.py
requirements.txt
.github/workflows/daily-scoring.yml
tests/test_score_leads.py
```

The README and changelog are documentation only.

## 2. GitHub secrets

Keep the five existing secrets unchanged.

No new secret is mandatory. For cleaner separation, duplicate the PhantomBuster agent named approximately `Stealth founders FR/BE - Extraction data profil`, then add its agent ID as:

```text
PB_AGENT_PROFILE_ENRICHER
```

Without that optional secret, the existing full-profile agent is reused for one API launch and its saved configuration is not modified.

## 3. Run the repair

In GitHub:

1. Open **Actions**.
2. Open **Daily Stealth Scoring**.
3. Click **Run workflow**.
4. Open the job **Import, enrich and repair founder profiles**.

## 4. Validate the result

In the logs, verify:

- the company URL column is detected as `salesNavigatorUrl`;
- the enrichment launch completes;
- `repaired` is greater than zero on the first run;
- no URL-only row is imported;
- `unresolved_company` is zero, or only contains profiles PhantomBuster has not processed yet.

In Notion, the July 27 rows should keep the same pages and URLs but change from `Unknown` to actual founder names. Their `Raw data` should contain profile fields such as headline, company, experience or location, and their status should remain `À scorer` until the scoring process runs.

## 5. Important PhantomBuster setup check

The full-profile/enricher Phantom must already work manually with a list of LinkedIn profile URLs. Its LinkedIn session must be valid. The code supplies the company export as `spreadsheetUrl` and selects `salesNavigatorUrl` as the input column for that launch.
