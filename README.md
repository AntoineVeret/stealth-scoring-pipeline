# Stealth Scoring Pipeline

Automated daily scoring of founder profiles from PhantomBuster, stored in Notion, with weekly email summaries.

## Architecture

```
PhantomBuster (2 scrapers)
        │
        ▼
  score_leads.py          ← runs daily at 8:30 AM CET
  (fetch → deduplicate → score via Claude → write to Notion)
        │
        ▼
  Notion Database         ← scored leads accumulate here
        │
        ▼
  weekly_email.py         ← runs Sunday 20:00 CET
  (query week's leads → send HTML email with Notion link)
```

## Setup

### 1. Create the Notion database

Create a new Notion database with these exact property names and types:

| Property          | Type      |
|-------------------|-----------|
| Nom du fondateur  | Title     |
| Exit détecté      | Rich text |
| Repeat founder    | Rich text |
| Top école         | Rich text |
| Top employeur     | Rich text |
| URL LinkedIn      | URL       |
| Score final       | Number    |
| Rationale         | Rich text |
| Date de scoring   | Date      |

### 2. Create a Notion integration

1. Go to https://www.notion.so/my-integrations
2. Create a new integration (name it "Stealth Scoring")
3. Copy the Internal Integration Secret
4. Go to your database page, click "..." > "Connections" > add your integration

### 3. Get your PhantomBuster agent IDs

1. Go to https://phantombuster.com/phantoms
2. Open each scraper:
   - "Stealth founders FR/BE - Extraction data profil"
   - "Company founders FR/BE - Extraction URL LinkedIn"
3. The agent ID is in the URL: `phantombuster.com/phantoms/XXXXX/setup`

### 4. Set up Resend (email delivery)

1. Go to https://resend.com and sign up (free tier: 100 emails/day)
2. Create an API key
3. Optionally: add and verify your own domain to send from a custom address (otherwise emails come from onboarding@resend.dev)

### 5. Create the GitHub repo and add secrets

```bash
gh repo create stealth-scoring-pipeline --private
cd stealth-scoring-pipeline
cp -r /path/to/this/project/* .
git add .
git commit -m "Initial pipeline setup"
git push origin main
```

Then add these secrets in GitHub (Settings > Secrets and variables > Actions):

| Secret                    | Value                                           |
|---------------------------|------------------------------------------------|
| `PHANTOMBUSTER_API_KEY`   | Your PhantomBuster API key                      |
| `ANTHROPIC_API_KEY`       | Your Anthropic API key                          |
| `NOTION_API_KEY`          | Notion integration secret                       |
| `NOTION_DATABASE_ID`      | Database ID from the Notion URL                 |
| `NOTION_DATABASE_URL`     | Full Notion database URL (for the email link)   |
| `PB_AGENT_STEALTH_FR_BE`  | PhantomBuster agent ID for stealth scraper      |
| `PB_AGENT_COMPANY_FOUNDERS`| PhantomBuster agent ID for company scraper     |
| `RESEND_API_KEY`          | Resend API key                                  |
| `EMAIL_TO`                | Recipient email (your email)                    |

### 6. Test manually

In GitHub Actions, go to each workflow and click "Run workflow" to test before relying on the schedule.

## Timezone note

GitHub Actions cron uses UTC. The workflows are set to:
- Daily: `30 6 * * *` (6:30 UTC = 8:30 CEST in summer)
- Weekly: `0 18 * * 0` (18:00 UTC = 20:00 CEST on Sundays)

During winter (CET = UTC+1), these will run one hour early (7:30 and 19:00 Paris time). To fix, update the cron expressions to `30 7` and `0 19` in November, and revert in March. Alternatively, use a timezone-aware scheduler like Google Cloud Scheduler.

## Costs

- Claude API: roughly $0.02-0.05 per batch of 10 profiles (Sonnet 4.6)
- GitHub Actions: free tier covers this easily (2,000 min/month)
- PhantomBuster: depends on your plan
- Notion API: free
