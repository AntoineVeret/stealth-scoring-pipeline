"""Send a weekly email containing only fully scored Notion profiles."""

from __future__ import annotations

import html
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

from notion_api import NotionClient, require_env, rich_text_value

PARIS_TZ = ZoneInfo("Europe/Paris")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weekly-email")


def title_value(prop: dict[str, Any]) -> str:
    return "".join(str(item.get("plain_text", "")) for item in prop.get("title", []))


def fetch_weekly_leads(notion: NotionClient) -> list[dict[str, Any]]:
    notion.require_properties(
        {
            "Nom du fondateur": {"title"},
            "URL LinkedIn": {"url"},
            "Score final": {"number"},
            "Date de scoring": {"date"},
        }
    )
    one_week_ago = (datetime.now(PARIS_TZ).date() - timedelta(days=7)).isoformat()
    filter_ = {
        "and": [
            {"property": "Date de scoring", "date": {"on_or_after": one_week_ago}},
            {"property": "Score final", "number": {"is_not_empty": True}},
        ]
    }
    sorts = [{"property": "Score final", "direction": "ascending"}]

    leads: list[dict[str, Any]] = []
    for page in notion.query_pages(filter_=filter_, sorts=sorts):
        props = page.get("properties", {})
        leads.append(
            {
                "name": title_value(props.get("Nom du fondateur", {})),
                "exit": rich_text_value(props.get("Exit détecté", {})),
                "repeat_founder": rich_text_value(props.get("Repeat founder", {})),
                "top_school": rich_text_value(props.get("Top école", {})),
                "top_employer": rich_text_value(props.get("Top employeur", {})),
                "linkedin_url": props.get("URL LinkedIn", {}).get("url", ""),
                "category": props.get("Score final", {}).get("number"),
                "date": props.get("Date de scoring", {}).get("date", {}).get("start", ""),
            }
        )
    return leads


def build_email_html(leads: list[dict[str, Any]], notion_database_url: str) -> str:
    today = datetime.now(PARIS_TZ).strftime("%d/%m/%Y")
    category_counts: dict[int | float | str, int] = {}
    for lead in leads:
        category = lead.get("category") if lead.get("category") is not None else "?"
        category_counts[category] = category_counts.get(category, 0) + 1

    priority = [
        lead
        for lead in leads
        if isinstance(lead.get("category"), (int, float)) and lead["category"] <= 3
    ]
    summary_rows = "".join(
        f"<tr><td style='padding:4px 12px;'>Cat {html.escape(str(category))}</td>"
        f"<td style='padding:4px 12px;font-weight:bold;'>{count}</td></tr>"
        for category, count in sorted(category_counts.items(), key=lambda item: str(item[0]))
    )

    def cell(value: Any) -> str:
        return html.escape(str(value or ""))

    def lead_row(lead: dict[str, Any]) -> str:
        category = lead.get("category")
        highlight = isinstance(category, (int, float)) and category <= 3
        background = "#fef3c7" if highlight else "#ffffff"
        escaped_name = cell(lead.get("name"))
        linkedin_url = str(lead.get("linkedin_url") or "")
        if linkedin_url:
            escaped_name = (
                f'<a href="{html.escape(linkedin_url, quote=True)}" '
                f'style="color:#1a56db;">{escaped_name}</a>'
            )
        values = [
            escaped_name,
            cell(lead.get("exit")),
            cell(lead.get("repeat_founder")),
            cell(lead.get("top_school")),
            cell(lead.get("top_employer")),
            cell(category if category is not None else "?"),
        ]
        cells = "".join(
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{value}</td>'
            for value in values[:-1]
        )
        cells += (
            '<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;'
            f'font-weight:bold;text-align:center;">{values[-1]}</td>'
        )
        return f'<tr style="background:{background};">{cells}</tr>'

    all_rows = "".join(lead_row(lead) for lead in leads)
    priority_banner = (
        f"<p style='color:#059669;font-weight:600;'>🎯 {len(priority)} profils prioritaires "
        "(Cat 1-3) cette semaine</p>"
        if priority
        else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1f2937;max-width:800px;margin:0 auto;padding:20px;">
<h1 style="font-size:22px;color:#111827;margin-bottom:4px;">Stealth Scoring - Recap Hebdo</h1>
<p style="color:#6b7280;margin-top:0;">Semaine du {today} &middot; {len(leads)} nouveaux profils scorés</p>
<table style="margin:16px 0;border-collapse:collapse;">
<tr><th style="text-align:left;padding:4px 12px;color:#6b7280;">Catégorie</th><th style="text-align:left;padding:4px 12px;color:#6b7280;">Nombre</th></tr>
{summary_rows}</table>
{priority_banner}
<table style="width:100%;border-collapse:collapse;margin:16px 0;">
<thead><tr style="background:#f3f4f6;">
<th style="text-align:left;padding:8px 12px;">Nom</th><th style="text-align:left;padding:8px 12px;">Exit</th><th style="text-align:left;padding:8px 12px;">Repeat</th><th style="text-align:left;padding:8px 12px;">École</th><th style="text-align:left;padding:8px 12px;">Employeur</th><th style="text-align:center;padding:8px 12px;">Score</th>
</tr></thead><tbody>{all_rows}</tbody></table>
<p style="margin-top:24px;"><a href="{html.escape(notion_database_url, quote=True)}" style="display:inline-block;background:#1a56db;color:#ffffff;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600;">Ouvrir la base Notion</a></p>
<p style="color:#9ca3af;font-size:12px;margin-top:32px;">Généré automatiquement par le Stealth Scoring Pipeline &middot; Cleo Ventures</p>
</body></html>"""


def send_email(leads: list[dict[str, Any]], notion_database_url: str) -> None:
    resend_api_key = require_env("RESEND_API_KEY")
    email_to = require_env("EMAIL_TO")
    email_from = os.getenv("EMAIL_FROM", "").strip() or "Stealth Scoring <onboarding@resend.dev>"
    today = datetime.now(PARIS_TZ).strftime("%d/%m/%Y")
    priority = sum(
        1
        for lead in leads
        if isinstance(lead.get("category"), (int, float)) and lead["category"] <= 3
    )
    subject = f"Stealth Scoring - {today} - {len(leads)} profils"
    if priority:
        subject += f" ({priority} prioritaires)"

    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
        json={
            "from": email_from,
            "to": [address.strip() for address in email_to.split(",") if address.strip()],
            "subject": subject,
            "html": build_email_html(leads, notion_database_url),
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Resend returned HTTP {response.status_code}: {response.text[:1000]}"
        )
    log.info("Email sent (Resend ID: %s)", response.json().get("id"))


def main() -> None:
    try:
        notion = NotionClient.from_env()
        database_url = os.getenv("NOTION_DATABASE_URL", "").strip() or (
            f"https://www.notion.so/{notion.database_id.replace('-', '')}"
        )
        leads = fetch_weekly_leads(notion)
        log.info("Found %d fully scored profile(s) in the past seven days", len(leads))
        send_email(leads, database_url)
    except Exception as exc:
        log.exception("Weekly email failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
