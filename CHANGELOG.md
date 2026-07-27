# Changelog

## v2 — full-profile enrichment and Notion repair

- Identified the schema mismatch between the full-profile stealth agent and the URL-only company-founders agent.
- Added automatic PhantomBuster chaining through `spreadsheetUrl`.
- Added optional `PB_AGENT_PROFILE_ENRICHER`; defaults to the existing full-profile agent.
- Uses one-launch-only `bonusArgument` and does not overwrite the Phantom's saved setup.
- Fetches and merges CSV plus JSON results, preferring richer records.
- Prevents URL-only profiles from being inserted into Notion.
- Repairs existing `Unknown` / URL-only Notion rows in place.
- Preserves full Raw data using multiple rich-text chunks.
- Adds unresolved-profile warnings instead of silently creating unusable rows.
- Adds regression tests for enrichment arguments, profile completeness, merging and Notion repairs.
