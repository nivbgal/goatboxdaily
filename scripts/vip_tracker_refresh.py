"""
vip_tracker_refresh.py
Refreshes the GOATBox VIP roster in Google Sheets and posts a heat summary to #vip.

Runs daily as a step in vip_report.yml, after the new-entrants report.

Env vars required:
  GCP_SERVICE_ACCOUNT_JSON   full JSON of the GCP service account key
  SLACK_BOT_TOKEN            xoxb-... token with chat:write scope

Sheet: 1OE2X6Cbg3a0CGO2oRy_uZ28GFt1yFZQmHdjSRbeX-nk (Sheet1)
Slack: C0BAJM1T8LE (#vip)
"""

import json
import os
import sys
from datetime import date

import gspread
from google.cloud import bigquery
from google.oauth2 import service_account
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SHEET_ID = "1OE2X6Cbg3a0CGO2oRy_uZ28GFt1yFZQmHdjSRbeX-nk"
SLACK_CHANNEL = "C0BAJM1T8LE"  # #vip
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"

# Column groups - must stay in sync with COLUMNS order below
AUTO_COLS = [
    "user_id", "tier", "ltv_usd", "net_spend_usd", "last_purchase_date",
    "days_since_last_purchase", "days_since_last_session", "lifecycle_state",
    "total_purchases", "avg_transaction_usd", "country", "favorite_box_category",
]
IDENTITY_COLS = ["first_name", "last_name", "email"]
FREYA_COLS = [
    "preferred_channel", "relationship_stage", "last_contacted_at",
    "conversation_note_1", "conversation_note_2", "conversation_note_3", "manager_notes",
]
CALC_COLS = ["recency_score", "context_score", "hot_cold_score", "hot_cold_label", "score_updated"]

COLUMNS = [
    "user_id", "first_name", "last_name", "email", "tier", "ltv_usd", "net_spend_usd",
    "last_purchase_date", "days_since_last_purchase", "days_since_last_session",
    "lifecycle_state", "total_purchases", "avg_transaction_usd", "country",
    "favorite_box_category", "preferred_channel", "relationship_stage", "last_contacted_at",
    "conversation_note_1", "conversation_note_2", "conversation_note_3", "manager_notes",
    "recency_score", "context_score", "hot_cold_score", "hot_cold_label", "score_updated",
]

BQ_SQL = """
SELECT
  crm.user_id,
  CASE
    WHEN crm.lifetime_purchases_usd >= 1000 THEN 'GOAT'
    WHEN crm.lifetime_purchases_usd >= 250  THEN 'Gold'
    WHEN crm.lifetime_purchases_usd >= 100  THEN 'VIP'
  END AS tier,
  CAST(crm.lifetime_purchases_usd AS FLOAT64) AS ltv_usd,
  CAST(crm.lifetime_purchases_usd AS FLOAT64) - COALESCE(oc.total_procurement_cost, 0) AS net_spend_usd,
  DATE(crm.last_purchase_at) AS last_purchase_date,
  crm.days_since_last_purchase,
  crm.days_since_last_session,
  crm.lifecycle_state,
  crm.total_purchases,
  ROUND(CAST(crm.lifetime_purchases_usd AS FLOAT64) / NULLIF(crm.total_purchases, 0), 2) AS avg_transaction_usd,
  crm.country,
  crm.favorite_box_category
FROM `goatbox-prod.processing_data.user_fact_crm` crm
LEFT JOIN (
  SELECT
    o.user_id,
    SUM(COALESCE(vp.vendor_price, 0) + COALESCE(vp.vendor_fee, 0)) AS total_procurement_cost
  FROM `goatbox-prod.processing_data.flat_order_created_events` o
  LEFT JOIN (
    SELECT order_id, vendor_price, vendor_fee
    FROM `goatbox-prod.processing_data.flat_order_vendor_pricing_events`
    QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY event_timestamp DESC) = 1
  ) vp ON vp.order_id = o.order_id
  GROUP BY o.user_id
) oc ON oc.user_id = crm.user_id
WHERE crm.lifetime_purchases_usd >= 100
  AND NOT EXISTS (
    SELECT 1 FROM `goatbox-prod.processing_data.internal_users` iu
    WHERE iu.user_id = crm.user_id
  )
ORDER BY crm.lifetime_purchases_usd DESC
"""


# --- Scoring ---

def calc_recency_score(days_since_last_session):
    # [Certain] Higher = more recent. Hits 0 at 30 days of inactivity.
    if days_since_last_session is None:
        return 0.0
    raw = 100.0 - (float(days_since_last_session) / 30.0) * 100.0
    return round(max(0.0, raw), 1)


def calc_context_score(row):
    # [Certain] 70 if Freya left any conversation note, else 50.
    has_notes = any(
        str(row.get(f"conversation_note_{i}", "") or "").strip()
        for i in range(1, 4)
    )
    return 70 if has_notes else 50


def calc_hot_cold_label(score):
    # [Certain] Bucket boundaries from spec.
    if score >= 70:
        return "Hot"
    if score >= 40:
        return "Warm"
    if score >= 15:
        return "Cold"
    return "Inactive"


def compute_scores(row, today_str):
    rs = calc_recency_score(row.get("days_since_last_session"))
    cs = calc_context_score(row)
    hcs = round((rs * 0.5) + (cs * 0.5), 1)
    return {
        "recency_score": rs,
        "context_score": cs,
        "hot_cold_score": hcs,
        "hot_cold_label": calc_hot_cold_label(hcs),
        "score_updated": today_str,
    }


# --- Helpers ---

def safe_str(val):
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def row_to_list(row_dict):
    return [str(row_dict.get(col, "") if row_dict.get(col, "") is not None else "") for col in COLUMNS]


def post_error(slack, msg):
    try:
        slack.chat_postMessage(
            channel=SLACK_CHANNEL,
            text=f":warning: VIP tracker refresh FAILED: {msg}",
        )
    except Exception as exc:
        print(f"Also failed to post error to Slack: {exc}", file=sys.stderr)


# --- Slack message ---

def build_slack_message(merged_rows, today_str):
    """Build the heat summary. merged_rows are pre-sorted ascending by hot_cold_score."""
    counts = {"Hot": 0, "Warm": 0, "Cold": 0, "Inactive": 0}
    for r in merged_rows:
        label = r.get("hot_cold_label", "Inactive")
        counts[label] = counts.get(label, 0) + 1

    # Top 5 Cold/Inactive VIPs where Freya has not yet made contact
    needs_attention = [
        r for r in merged_rows
        if r.get("hot_cold_label") in ("Cold", "Inactive")
        and str(r.get("relationship_stage", "")).strip() == "new"
    ][:5]

    # All Hot VIPs sorted desc by score for readability
    hot_vips = sorted(
        [r for r in merged_rows if r.get("hot_cold_label") == "Hot"],
        key=lambda r: float(r.get("hot_cold_score") or 0),
        reverse=True,
    )

    def fmt_ltv(r):
        try:
            return f"${float(r.get('ltv_usd', 0)):,.0f}"
        except (ValueError, TypeError):
            return str(r.get("ltv_usd", ""))

    lines = [
        f":thermometer: *VIP Heat Report - {today_str}*",
        "",
        (
            f":fire: Hot: {counts['Hot']} | "
            f":yellow_circle: Warm: {counts['Warm']} | "
            f":snowflake: Cold: {counts['Cold']} | "
            f":black_square_button: Inactive: {counts['Inactive']}"
        ),
        f"Total VIPs: {len(merged_rows)}",
        "",
        "*Needs attention (Cold/Inactive, not yet contacted):*",
    ]

    if needs_attention:
        for r in needs_attention:
            lines.append(
                f"- `{r['user_id']}` | {r.get('tier', '')} | "
                f"{fmt_ltv(r)} | {r.get('days_since_last_session', '?')}d since last session"
            )
    else:
        lines.append("- None")

    lines += ["", "*Hot VIPs (score >= 70):*"]

    if hot_vips:
        for r in hot_vips:
            lines.append(
                f"- `{r['user_id']}` | {r.get('tier', '')} | "
                f"{fmt_ltv(r)} | score {r.get('hot_cold_score', '')}"
            )
    else:
        lines.append("- None")

    lines += ["", f"Tracker updated. Sheet: {SHEET_URL}"]

    return "\n".join(lines)


# --- Main ---

def main():
    sa_json_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    today_str = date.today().isoformat()

    if not sa_json_str or not slack_token:
        print("ERROR: GCP_SERVICE_ACCOUNT_JSON and SLACK_BOT_TOKEN must be set", file=sys.stderr)
        sys.exit(1)

    slack = WebClient(token=slack_token)
    sa_info = json.loads(sa_json_str)

    # --- 1. Query BigQuery [Certain: if this fails we abort before touching the Sheet] ---
    print("Querying BigQuery for full VIP roster...")
    bq_creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    bq = bigquery.Client(credentials=bq_creds, project=sa_info["project_id"])

    try:
        bq_rows = [dict(r) for r in bq.query(BQ_SQL).result()]
    except Exception as exc:
        msg = str(exc)
        print(f"BigQuery error: {msg}", file=sys.stderr)
        post_error(slack, msg)
        sys.exit(1)

    print(f"  BQ returned {len(bq_rows)} VIP rows")

    # Guard: a 0-row result almost certainly means a query/data problem, not a real empty VIP list.
    # Clearing the sheet on 0 rows would wipe all of Freya's notes.
    if not bq_rows:
        msg = "BQ returned 0 VIP rows - refusing to overwrite Sheet data"
        print(msg, file=sys.stderr)
        post_error(slack, msg)
        sys.exit(1)

    # --- 2. Read existing Sheet to preserve Freya columns ---
    print("Reading existing Google Sheet...")
    sheets_creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(sheets_creds)
    ws = gc.open_by_key(SHEET_ID).get_worksheet(0)

    existing_data = ws.get_all_records(default_blank="")
    existing_by_uid = {
        str(r.get("user_id", "")): r
        for r in existing_data
        if r.get("user_id")
    }
    print(f"  Sheet has {len(existing_by_uid)} existing rows")

    # --- 3. Merge BQ data with existing Sheet rows ---
    merged_rows = []

    for bq_row in bq_rows:
        uid = str(bq_row.get("user_id", ""))
        existing = existing_by_uid.get(uid, {})
        is_new = uid not in existing_by_uid

        row = {}

        for col in AUTO_COLS:
            row[col] = safe_str(bq_row.get(col))

        # Identity cols: preserved from Sheet; blank for VIPs not yet in Sheet
        for col in IDENTITY_COLS:
            row[col] = str(existing.get(col, "") or "")

        # Freya cols: NEVER overwrite. New VIPs get relationship_stage="new", rest blank.
        for col in FREYA_COLS:
            if is_new and col == "relationship_stage":
                row[col] = "new"
            else:
                row[col] = str(existing.get(col, "") or "")

        row.update(compute_scores(row, today_str))
        merged_rows.append(row)

    # Sort ascending by hot_cold_score so coldest VIPs appear first in Sheet
    merged_rows.sort(key=lambda r: float(r.get("hot_cold_score") or 0))

    # --- 4. Write back to Sheet ---
    print(f"Writing {len(merged_rows)} rows to Sheet...")
    output = [COLUMNS] + [row_to_list(r) for r in merged_rows]
    try:
        ws.clear()
        ws.update(output, "A1", value_input_option="USER_ENTERED")
        print("  Sheet updated.")
    except Exception as exc:
        msg = f"Sheet write failed (may be partially cleared - check Sheet): {exc}"
        print(msg, file=sys.stderr)
        post_error(slack, msg)
        sys.exit(1)

    # --- 5. Post heat summary to Slack #vip ---
    print("Posting heat summary to #vip...")
    msg = build_slack_message(merged_rows, today_str)

    try:
        resp = slack.chat_postMessage(
            channel=SLACK_CHANNEL,
            text=msg,
            unfurl_links=False,
            unfurl_media=False,
        )
        print(f"  Posted. ts={resp['ts']}")
    except SlackApiError as exc:
        err = exc.response.get("error", str(exc))
        print(f"Slack error: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
