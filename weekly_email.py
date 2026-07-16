"""
Weekly Email Summary
Queries the Notion scored leads database for the past week's additions
and sends a formatted email with a link to the Notion database.
Runs every Sunday at 20:00 CET.
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
NOTION_DATABASE_URL = os.environ.get(
    "NOTION_DATABASE_URL",
    f"https://www.notion.so/{NOTION_DATABASE_ID.replace('-', '')}",
)

RESEND_API_KEY = os.environ["RESEND_API_KEY"]
EMAIL_TO = os.environ["EMAIL_TO"]
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Stealth Scoring <scoring@resend.dev>")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Notion: fetch this week's scored leads
# ---------------------------------------------------------------------------
def fetch_weekly_leads() -> list[dict]:
    """Fetch leads scored in the past 7 days from Notion."""
    one_week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    leads = []
    has_more = True
    start_cursor = None

    while has_more:
        payload = {
            "page_size": 100,
            "filter": {
                "property": "Date de scoring",
                "date": {"on_or_after": one_week_ago},
            },
            "sorts": [{"property": "Score final", "direction": "ascending"}],
        }
        if start_cursor:
            payload["start_cursor"] = start_cursor

        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=NOTION_HEADERS,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            props = page.get("properties", {})
            leads.append(
                {
                    "name": _get_title(props.get("Nom du fondateur", {})),
                    "exit": _get_rich_text(props.get("Exit détecté", {})),
                    "repeat_founder": _get_rich_text(props.get("Repeat founder", {})),
                    "top_school": _get_rich_text(props.get("Top école", {})),
                    "top_employer": _get_rich_text(props.get("Top employeur", {})),
                    "linkedin_url": props.get("URL LinkedIn", {}).get("url", ""),
                    "category": props.get("Score final", {}).get("number", "?"),
                    "date": props.get("Date de scoring", {})
                    .get("date", {})
                    .get("start", ""),
                }
            )

        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return leads


def _get_title(prop: dict) -> str:
    items = prop.get("title", [])
    return items[0]["text"]["content"] if items else ""


def _get_rich_text(prop: dict) -> str:
    items = prop.get("rich_text", [])
    return items[0]["text"]["content"] if items else ""


# ---------------------------------------------------------------------------
# Build the email
# ---------------------------------------------------------------------------
def build_email_html(leads: list[dict]) -> str:
    """Build a clean HTML email with the weekly summary."""
    today = datetime.now().strftime("%d/%m/%Y")
    total = len(leads)

    # Category breakdown
    cat_counts: dict[int | str, int] = {}
    for lead in leads:
        cat = lead.get("category", "?")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    # Priority leads (cat 1-3)
    priority = [l for l in leads if isinstance(l.get("category"), (int, float)) and l["category"] <= 3]

    summary_rows = "".join(
        f"<tr><td style='padding:4px 12px;'>Cat {cat}</td>"
        f"<td style='padding:4px 12px;font-weight:bold;'>{count}</td></tr>"
        for cat, count in sorted(cat_counts.items())
    )

    # Build table rows
    def lead_row(lead: dict, highlight: bool = False) -> str:
        bg = "#fef3c7" if highlight else "#ffffff"
        name = lead["name"]
        if lead.get("linkedin_url"):
            name = f'<a href="{lead["linkedin_url"]}" style="color:#1a56db;">{name}</a>'
        return f"""<tr style="background:{bg};">
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{name}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{lead.get('exit','')}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{lead.get('repeat_founder','')}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{lead.get('top_school','')}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{lead.get('top_employer','')}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:bold;text-align:center;">{lead.get('category','?')}</td>
        </tr>"""

    all_rows = "".join(
        lead_row(l, highlight=isinstance(l.get("category"), (int, float)) and l["category"] <= 3)
        for l in leads
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1f2937;max-width:800px;margin:0 auto;padding:20px;">

<h1 style="font-size:22px;color:#111827;margin-bottom:4px;">Stealth Scoring - Recap Hebdo</h1>
<p style="color:#6b7280;margin-top:0;">Semaine du {today} &middot; {total} nouveaux profils scorés</p>

<table style="margin:16px 0;border-collapse:collapse;">
    <tr><th style="text-align:left;padding:4px 12px;color:#6b7280;">Catégorie</th>
        <th style="text-align:left;padding:4px 12px;color:#6b7280;">Nombre</th></tr>
    {summary_rows}
</table>

{"<p style='color:#059669;font-weight:600;'>🎯 " + str(len(priority)) + " profils prioritaires (Cat 1-3) cette semaine</p>" if priority else ""}

<table style="width:100%;border-collapse:collapse;margin:16px 0;">
<thead>
    <tr style="background:#f3f4f6;">
        <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #d1d5db;">Nom</th>
        <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #d1d5db;">Exit</th>
        <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #d1d5db;">Repeat</th>
        <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #d1d5db;">École</th>
        <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #d1d5db;">Employeur</th>
        <th style="text-align:center;padding:8px 12px;border-bottom:2px solid #d1d5db;">Score</th>
    </tr>
</thead>
<tbody>
    {all_rows}
</tbody>
</table>

<p style="margin-top:24px;">
    <a href="{NOTION_DATABASE_URL}"
       style="display:inline-block;background:#1a56db;color:#ffffff;padding:10px 20px;
              border-radius:6px;text-decoration:none;font-weight:600;">
        Ouvrir la base Notion
    </a>
</p>

<p style="color:#9ca3af;font-size:12px;margin-top:32px;">
    Généré automatiquement par le Stealth Scoring Pipeline &middot; Cleo Ventures
</p>

</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
def send_email(leads: list[dict]):
    today = datetime.now().strftime("%d/%m/%Y")
    total = len(leads)
    priority = sum(
        1 for l in leads
        if isinstance(l.get("category"), (int, float)) and l["category"] <= 3
    )

    subject = f"Stealth Scoring - {today} - {total} profils"
    if priority:
        subject += f" ({priority} prioritaires)"

    html = build_email_html(leads)

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": EMAIL_FROM,
            "to": [EMAIL_TO],
            "subject": subject,
            "html": html,
        },
    )
    resp.raise_for_status()
    log.info(f"Email sent to {EMAIL_TO} (Resend ID: {resp.json().get('id')})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=== Weekly Email Summary ===")
    leads = fetch_weekly_leads()
    log.info(f"Found {len(leads)} leads scored this week")

    if not leads:
        log.info("No leads this week, sending empty summary anyway")

    send_email(leads)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
