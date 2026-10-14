"""
vip_tracker_refresh.py
Refreshes the GOATBox VIP roster in Google Sheets and posts a heat summary to #vip.

Runs daily as a step in vip_report.yml, after the new-entrants report.

Hot/Cold engine (spec: "Hot/Cold Engine" in docs/vip-program-spec.md):
  recency_score  = days since Freya's last_contacted_at, 100 at day 0 down to 0 at day 30
  context_score  = what the VIP did after that contact (see calc_context_score)
  hot_cold_score = 0.5 x recency + 0.5 x context
Never-contacted VIPs score 0 and are labelled Inactive. Rows whose first_name is
DELETED stay in the Sheet (Freya's notes live there) but are left out of the report.

Tier movement: each run compares the tier BigQuery reports now against the `tier`
the Sheet recorded last run. A difference writes `previous_tier` + `tier_changed_at`
and shows up in the "Tier moves today" section of the heat summary.

Identity: first_name / last_name / email are carried forward from the Sheet by user_id.
A blank email is filled from source_data.events; an email already in the Sheet is never
overwritten. BigQuery has no name source, so names stay as entered by hand.

Env vars required:
  GCP_SERVICE_ACCOUNT_JSON   full JSON of the GCP service account key
  SLACK_BOT_TOKEN            xoxb-... token with chat:write scope
Optional:
  DRY_RUN=1                  query and merge, print counts, but don't write the Sheet or post

Sheet: 1OE2X6Cbg3a0CGO2oRy_uZ28GFt1yFZQmHdjSRbeX-nk (Sheet1)
Slack: C0BAJM1T8LE (#vip)
"""

import json
import os
import sys
from datetime import date, datetime

import gspread
from google.cloud import bigquery
from google.oauth2 import service_account
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SHEET_ID = "1OE2X6Cbg3a0CGO2oRy_uZ28GFt1yFZQmHdjSRbeX-nk"
SLACK_CHANNEL = "C0BAJM1T8LE"  # #vip
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"

# Voucher = this share of spend_last_30_days, never below 0. No caps yet; add them in
# calc_voucher_usd.
VOUCHER_RATE = 0.10

# Hot/Cold engine. last_contacted_at is Freya's column, entered as MM/DD/YYYY.
CONTACT_DATE_FORMAT = "%m/%d/%Y"
RECENCY_WINDOW_DAYS = 30  # recency_score reaches 0 this many days after the last contact
# context_score points, by what the VIP did after the last contact. Freya may retune these.
CONTEXT_PURCHASED = 100
CONTEXT_ACTIVE = 60  # logged in or opened a box, no purchase
CONTEXT_NOTHING = 20
CONTEXT_NEVER_CONTACTED = 0

# Heat Report lists
REPORT_LIST_LIMIT = 10
NEEDS_ATTENTION_DAYS = 14  # contacted longer ago than this = needs attention
BACK_ON_SITE_DAYS = 3  # on site within this many days...
BACK_ON_SITE_NO_PURCHASE_DAYS = 30  # ...and no purchase for at least this many

# Column groups - must stay in sync with COLUMNS order below
AUTO_COLS = [
    "user_id", "tier", "ltv_usd", "net_spend_usd", "last_purchase_date",
    "days_since_last_purchase", "days_since_last_session", "lifecycle_state",
    "total_purchases", "avg_transaction_usd", "country", "favorite_box_category",
    "spend_last_30_days",
]
VOUCHER_COLS = ["voucher_usd"]  # derived from spend_last_30_days, not from BQ
IDENTITY_COLS = ["first_name", "last_name", "email"]
FREYA_COLS = [
    "preferred_channel", "relationship_stage", "last_contacted_at",
    "conversation_note_1", "conversation_note_2", "conversation_note_3", "manager_notes",
]
CALC_COLS = ["recency_score", "context_score", "hot_cold_score", "hot_cold_label", "score_updated"]
# Tier history is carried forward from the Sheet run-to-run, not recomputed from BQ.
# Appended at the end of COLUMNS so existing column positions don't shift.
TIER_HISTORY_COLS = ["previous_tier", "tier_changed_at"]

COLUMNS = [
    "user_id", "first_name", "last_name", "email", "tier", "ltv_usd", "net_spend_usd",
    "spend_last_30_days", "voucher_usd",
    "last_purchase_date", "days_since_last_purchase", "days_since_last_session",
    "lifecycle_state", "total_purchases", "avg_transaction_usd", "country",
    "favorite_box_category", "preferred_channel", "relationship_stage", "last_contacted_at",
    "conversation_note_1", "conversation_note_2", "conversation_note_3", "manager_notes",
    "recency_score", "context_score", "hot_cold_score", "hot_cold_label", "score_updated",
    "previous_tier", "tier_changed_at",
]

TIER_RANK = {"VIP": 1, "Gold": 2, "GOAT": 3}

BQ_SQL = """
WITH crm_dedup AS (
  -- user_fact_crm contains exact duplicate rows for ~82 user_ids; without this
  -- those VIPs get two rows in the Sheet and are double-counted everywhere.
  SELECT *
  FROM `goatbox-prod.processing_data.user_fact_crm`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY last_purchase_at DESC) = 1
),
vip_activity AS (
  -- From raw events because user_fact_crm.days_since_last_session lags: on 2026-10-12
  -- it was stale for 41 of 149 VIPs, by up to 86 days.
  SELECT
    user_id,
    DATE(MAX(event_timestamp)) AS last_session_date,
    DATE(MAX(IF(event_name IN ('login_completed', 'boxes_opened'), event_timestamp, NULL))) AS last_active_date,
    DATE(MAX(IF(event_name = 'purchase_completed', event_timestamp, NULL))) AS last_purchase_event_date
  FROM `goatbox-prod.source_data.events`
  WHERE event_name IN ('login_completed', 'boxes_opened', 'shop_opened', 'purchase_completed')
    AND user_id IN (SELECT user_id FROM crm_dedup WHERE lifetime_purchases_usd >= 100)
  GROUP BY user_id
)
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
  DATE_DIFF(CURRENT_DATE(), va.last_session_date, DAY) AS days_since_last_session,
  va.last_active_date,
  va.last_purchase_event_date,
  crm.lifecycle_state,
  crm.total_purchases,
  ROUND(CAST(crm.lifetime_purchases_usd AS FLOAT64) / NULLIF(crm.total_purchases, 0), 2) AS avg_transaction_usd,
  crm.country,
  crm.favorite_box_category,
  -- Purchases minus refunds over the last 30 days (rolling from now), the base for
  -- the 10%-of-30-day-spend voucher rule. Can go below 0 when a refund in the window
  -- is for an older purchase. Not user_fact_crm.d30_purchases_usd, which is the
  -- first 30 days after registration.
  CAST(COALESCE(p30.purchases_usd, 0) - COALESCE(r30.refunds_usd, 0) AS FLOAT64) AS spend_last_30_days
FROM crm_dedup crm
LEFT JOIN (
  SELECT user_id, SUM(amount_usd) AS purchases_usd
  FROM `goatbox-prod.processing_data.flat_purchase_events`
  WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  GROUP BY user_id
) p30 ON p30.user_id = crm.user_id
LEFT JOIN (
  -- Each refund is logged once per status (new, processing, succeeded), so count only
  -- succeeded or the refund is subtracted three times. amount is USD (currency checked
  -- 2026-10-14: every row is USD).
  SELECT user_id, SUM(amount) AS refunds_usd
  FROM `goatbox-prod.processing_data.flat_payment_refunded`
  WHERE status = 'succeeded'
    AND event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  GROUP BY user_id
) r30 ON r30.user_id = crm.user_id
LEFT JOIN vip_activity va ON va.user_id = crm.user_id
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

# Purchase and balance events carry the account email as an event param. Checked
# 2026-10-12: matched the CRM email for 30/30 new VIPs, one distinct email per user.
# Filtered to the blank-email VIPs; events is clustered on (event_name, user_id).
EMAIL_SQL = """
SELECT
  e.user_id,
  ARRAY_AGG(TRIM(p.string_value) ORDER BY e.event_timestamp DESC LIMIT 1)[OFFSET(0)] AS email
FROM `goatbox-prod.source_data.events` e, UNNEST(e.event_params) p
WHERE p.key = 'email'
  AND p.string_value IS NOT NULL
  AND TRIM(p.string_value) != ''
  AND e.event_name IN ('purchase_completed', 'purchase_initiated', 'balance_changed')
  AND e.user_id IN UNNEST(@vip_user_ids)
GROUP BY e.user_id
"""


# --- Scoring ---

def parse_contact_date(value):
    """Freya's last_contacted_at as a date. Returns (date or None, unparseable)."""
    text = str(value or "").strip()
    if not text:
        return None, False
    try:
        return datetime.strptime(text, CONTACT_DATE_FORMAT).date(), False
    except ValueError:
        return None, True


def calc_recency_score(contact_date, today):
    # 100 on the day of contact, straight line to 0 at RECENCY_WINDOW_DAYS. Never contacted = 0.
    if contact_date is None:
        return 0.0
    days = (today - contact_date).days
    raw = 100.0 - (days / RECENCY_WINDOW_DAYS) * 100.0
    return round(min(100.0, max(0.0, raw)), 1)


def calc_context_score(contact_date, last_purchase_date, last_active_date):
    """What the VIP did after Freya's last contact.

    Dates are compared by day (the contact has no time), so activity on the day of
    the contact counts as after it.
    """
    if contact_date is None:
        return CONTEXT_NEVER_CONTACTED
    if last_purchase_date and last_purchase_date >= contact_date:
        return CONTEXT_PURCHASED
    if last_active_date and last_active_date >= contact_date:
        return CONTEXT_ACTIVE
    return CONTEXT_NOTHING


def calc_voucher_usd(spend_last_30_days):
    # Blank spend stays blank; 0 or negative spend (refunds over purchases) gives 0.
    if spend_last_30_days in (None, ""):
        return ""
    return round(max(0.0, float(spend_last_30_days)) * VOUCHER_RATE, 2)


def calc_hot_cold_label(score, contacted):
    # Bucket boundaries from spec. Never contacted is always Inactive.
    if not contacted:
        return "Inactive"
    if score >= 70:
        return "Hot"
    if score >= 40:
        return "Warm"
    if score >= 15:
        return "Cold"
    return "Inactive"


def resolve_tier_change(new_tier, existing, is_new, today_str):
    """Compare the tier BQ reports now against the tier the Sheet recorded last run.

    Returns (fields_to_write, move) where move is "up" / "down" / "new" / "". move is
    only for today's Slack summary and is never written to the Sheet.
    """
    sheet_tier = str(existing.get("tier", "") or "").strip()

    if is_new:
        # First time on the roster: they arrived at this tier today.
        return {"previous_tier": "", "tier_changed_at": today_str}, "new"

    if sheet_tier and sheet_tier != new_tier:
        move = "up" if TIER_RANK.get(new_tier, 0) > TIER_RANK.get(sheet_tier, 0) else "down"
        return {"previous_tier": sheet_tier, "tier_changed_at": today_str}, move

    # No change today: carry forward whatever the last move was.
    return {
        "previous_tier": str(existing.get("previous_tier", "") or ""),
        "tier_changed_at": str(existing.get("tier_changed_at", "") or ""),
    }, ""


def compute_scores(row, today):
    contact = row["_contact_date"]
    rs = calc_recency_score(contact, today)
    cs = calc_context_score(contact, row["_last_purchase_event_date"], row["_last_active_date"])
    hcs = round((rs * 0.5) + (cs * 0.5), 1)
    return {
        "recency_score": rs,
        "context_score": cs,
        "hot_cold_score": hcs,
        "hot_cold_label": calc_hot_cold_label(hcs, contact is not None),
        "score_updated": today.isoformat(),
    }


# --- Helpers ---

def safe_str(val):
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def is_deleted(row):
    return str(row.get("first_name", "") or "").strip().upper() == "DELETED"


def days_since(d, today):
    return (today - d).days if d else None


def build_report_lists(merged_rows, today):
    """Rows for the Heat Report, DELETED accounts excluded. Lists are uncapped."""
    live = [r for r in merged_rows if not is_deleted(r)]
    by_ltv = lambda r: -float(r.get("ltv_usd") or 0)

    never = sorted([r for r in live if r["_contact_date"] is None], key=by_ltv)
    stale = sorted(
        [r for r in live
         if r["_contact_date"] is not None
         and days_since(r["_contact_date"], today) > NEEDS_ATTENTION_DAYS],
        key=by_ltv,
    )

    def back_on_site(r):
        session = r.get("days_since_last_session")
        purchase_days = days_since(r["_last_purchase_event_date"], today)
        return (
            session not in (None, "") and int(session) <= BACK_ON_SITE_DAYS
            and (purchase_days is None or purchase_days >= BACK_ON_SITE_NO_PURCHASE_DAYS)
        )

    return {
        "live": live,
        "needs_attention": never + stale,
        "n_never_contacted": len(never),
        "back_on_site": sorted([r for r in live if back_on_site(r)], key=by_ltv),
    }


def label_counts(rows):
    counts = {"Hot": 0, "Warm": 0, "Cold": 0, "Inactive": 0}
    for r in rows:
        label = str(r.get("hot_cold_label", "") or "")
        counts[label] = counts.get(label, 0) + 1
    return counts


def row_to_list(row_dict):
    return [str(row_dict.get(col, "") if row_dict.get(col, "") is not None else "") for col in COLUMNS]


def lookup_emails(bq, user_ids):
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("vip_user_ids", "STRING", list(user_ids)),
    ])
    return {
        row["user_id"]: row["email"]
        for row in bq.query(EMAIL_SQL, job_config=job_config).result()
    }


def check_email_source(bq, merged_rows):
    """Dry-run only: compare BigQuery emails with those already in the Sheet. Counts only."""
    filled = [r for r in merged_rows if r["email"].strip()]
    found = lookup_emails(bq, [r["user_id"] for r in filled])
    same = sum(
        1 for r in filled
        if found.get(r["user_id"], "").lower() == r["email"].strip().lower()
    )
    missing = sum(1 for r in filled if r["user_id"] not in found)
    print(
        f"  Email source check vs Sheet: {same} match, "
        f"{len(filled) - same - missing} differ, {missing} not in BQ"
    )


def fill_blank_emails(bq, merged_rows):
    """Fill email from BigQuery where the Sheet has none. Mutates merged_rows.

    Rows whose first_name is DELETED are skipped: BigQuery still holds emails for
    deleted accounts and they must not be written back. Logs counts and user_ids
    only, never emails.
    """
    blank = [
        r for r in merged_rows
        if not r["email"].strip() and r["first_name"].strip().upper() != "DELETED"
    ]
    if not blank:
        print("  Emails: no blank rows to fill")
        return

    found = lookup_emails(bq, [r["user_id"] for r in blank])

    still_blank = []
    for r in blank:
        if found.get(r["user_id"]):
            r["email"] = found[r["user_id"]]
        else:
            still_blank.append(r["user_id"])

    print(f"  Emails: filled {len(blank) - len(still_blank)}, still blank {len(still_blank)}")
    if still_blank:
        print(f"  Emails still blank for user_ids: {', '.join(still_blank)}")


def post_error(slack, msg):
    try:
        slack.chat_postMessage(
            channel=SLACK_CHANNEL,
            text=f":warning: VIP tracker refresh FAILED: {msg}",
        )
    except Exception as exc:
        print(f"Also failed to post error to Slack: {exc}", file=sys.stderr)


# --- Slack message ---

def build_slack_message(merged_rows, today):
    lists = build_report_lists(merged_rows, today)
    live = lists["live"]
    counts = label_counts(live)
    n_deleted = len(merged_rows) - len(live)

    def fmt_ltv(r):
        try:
            return f"${float(r.get('ltv_usd', 0)):,.0f}"
        except (ValueError, TypeError):
            return str(r.get("ltv_usd", ""))

    def capped(rows, fmt):
        out = [fmt(r) for r in rows[:REPORT_LIST_LIMIT]] or ["- None"]
        if len(rows) > REPORT_LIST_LIMIT:
            out.append(f"- ...and {len(rows) - REPORT_LIST_LIMIT} more in the Sheet")
        return out

    def fmt_attention(r):
        contact = r["_contact_date"]
        when = "never contacted" if contact is None else f"last contact {days_since(contact, today)}d ago"
        return f"- `{r['user_id']}` | {r.get('tier', '')} | {fmt_ltv(r)} | {when}"

    def fmt_back(r):
        purchase_days = days_since(r["_last_purchase_event_date"], today)
        bought = "no purchase on record" if purchase_days is None else f"last purchase {purchase_days}d ago"
        return (
            f"- `{r['user_id']}` | {r.get('tier', '')} | {fmt_ltv(r)} | "
            f"on site {r.get('days_since_last_session')}d ago | {bought}"
        )

    lines = [
        f":thermometer: *VIP Heat Report - {today.isoformat()}*",
        "",
        (
            f":fire: Hot: {counts['Hot']} | "
            f":yellow_circle: Warm: {counts['Warm']} | "
            f":snowflake: Cold: {counts['Cold']} | "
            f":black_square_button: Inactive: {counts['Inactive']}"
        ),
        f"Total VIPs: {len(live)}" + (f" (excludes {n_deleted} deleted)" if n_deleted else ""),
        "",
        (
            f"*Needs attention ({lists['n_never_contacted']} never contacted, then last contact "
            f"over {NEEDS_ATTENTION_DAYS}d ago):*"
        ),
        *capped(lists["needs_attention"], fmt_attention),
        "",
        (
            f"*Back on site, not buying (on site in the last {BACK_ON_SITE_DAYS}d, "
            f"no purchase in {BACK_ON_SITE_NO_PURCHASE_DAYS}d+):*"
        ),
        *capped(lists["back_on_site"], fmt_back),
    ]

    # Tier moves detected this run, biggest new tier first
    tier_moves = sorted(
        [r for r in live if r.get("_tier_move") in ("up", "down")],
        key=lambda r: (TIER_RANK.get(r.get("tier", ""), 0), float(r.get("ltv_usd") or 0)),
        reverse=True,
    )

    lines += ["", "*Tier moves today:*"]

    if tier_moves:
        for r in tier_moves:
            arrow = ":arrow_up:" if r["_tier_move"] == "up" else ":arrow_down:"
            lines.append(
                f"- {arrow} `{r['user_id']}` | {r.get('previous_tier', '')} → {r.get('tier', '')} | "
                f"{fmt_ltv(r)} LTV"
            )
    else:
        lines.append("- None")

    hot_vips = sorted(
        [r for r in live if r.get("hot_cold_label") == "Hot"],
        key=lambda r: float(r.get("hot_cold_score") or 0),
        reverse=True,
    )

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


def print_dry_run_summary(merged_rows, existing_by_uid, today):
    """Counts and user_ids only: no names or emails."""
    lists = build_report_lists(merged_rows, today)
    live = lists["live"]
    before = label_counts(
        existing_by_uid[r["user_id"]] for r in live if r["user_id"] in existing_by_uid
    )
    after = label_counts(live)
    print(f"  Labels before (Sheet, excl. deleted): {before}")
    print(f"  Labels after:                         {after}")

    contacted = sorted(r["_contact_date"] for r in live if r["_contact_date"] is not None)
    if contacted:
        median_days = days_since(contacted[len(contacted) // 2], today)
        print(
            f"  Contacted {len(contacted)} of {len(live)}: contact dates {contacted[0]} to "
            f"{contacted[-1]}, median {median_days}d ago. Recency hits 0 at "
            f"{RECENCY_WINDOW_DAYS}d, so old contacts drive most rows to Cold/Inactive: "
            "that is the real state, not a bug."
        )
    print(
        f"  Needs attention ({len(lists['needs_attention'])}, first "
        f"{lists['n_never_contacted']} never contacted, Slack shows {REPORT_LIST_LIMIT}): "
        + ", ".join(r["user_id"] for r in lists["needs_attention"])
    )
    print(
        f"  Back on site, not buying ({len(lists['back_on_site'])}): "
        + ", ".join(r["user_id"] for r in lists["back_on_site"])
    )
    deleted_in_lists = [
        r["user_id"] for r in lists["needs_attention"] + lists["back_on_site"] if is_deleted(r)
    ]
    print(f"  DELETED rows in lists: {len(deleted_in_lists)}")


# --- Main ---

def main():
    sa_json_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    today = date.today()
    today_str = today.isoformat()
    dry_run = os.environ.get("DRY_RUN") == "1"

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
    bad_contact_dates = []

    for bq_row in bq_rows:
        uid = str(bq_row.get("user_id", ""))
        existing = existing_by_uid.get(uid, {})
        is_new = uid not in existing_by_uid

        row = {}

        for col in AUTO_COLS:
            row[col] = safe_str(bq_row.get(col))

        row["voucher_usd"] = safe_str(calc_voucher_usd(bq_row.get("spend_last_30_days")))

        # Identity cols: preserved from Sheet; blank for VIPs not yet in Sheet.
        # Names are trimmed of stray spaces; nothing else in them is changed.
        for col in IDENTITY_COLS:
            row[col] = str(existing.get(col, "") or "")
            if col in ("first_name", "last_name"):
                row[col] = row[col].strip()

        # Freya cols: NEVER overwrite. New VIPs get relationship_stage="new", rest blank.
        for col in FREYA_COLS:
            if is_new and col == "relationship_stage":
                row[col] = "new"
            else:
                row[col] = str(existing.get(col, "") or "")

        tier_fields, move = resolve_tier_change(row["tier"], existing, is_new, today_str)
        row.update(tier_fields)
        row["_tier_move"] = move  # transient, not in COLUMNS so never written to the Sheet

        # Transient fields for scoring and the report; not in COLUMNS, never written.
        row["_contact_date"], bad_date = parse_contact_date(row["last_contacted_at"])
        if bad_date:
            bad_contact_dates.append(uid)
        row["_last_active_date"] = bq_row.get("last_active_date")
        row["_last_purchase_event_date"] = bq_row.get("last_purchase_event_date")

        row.update(compute_scores(row, today))
        merged_rows.append(row)

    if bad_contact_dates:
        print(
            f"  WARNING: last_contacted_at not MM/DD/YYYY for {len(bad_contact_dates)} rows, "
            f"scored as never contacted: {', '.join(bad_contact_dates)}",
            file=sys.stderr,
        )

    # A failed lookup leaves emails blank for a retry next run; it must not block the refresh.
    try:
        fill_blank_emails(bq, merged_rows)
    except Exception as exc:
        print(f"  WARNING: email fill skipped: {exc}", file=sys.stderr)

    vouchers = [
        float(r["voucher_usd"]) for r in merged_rows
        if r["voucher_usd"] != "" and not is_deleted(r)
    ]
    paying = [v for v in vouchers if v > 0]
    print(
        f"  Vouchers: {len(paying)} rows above 0, "
        f"total ${sum(paying):,.2f}, max ${max(paying, default=0):,.2f}"
    )

    n_up = sum(1 for r in merged_rows if r["_tier_move"] == "up")
    n_down = sum(1 for r in merged_rows if r["_tier_move"] == "down")
    print(f"  Tier moves since last run: {n_up} up, {n_down} down")

    # Sort ascending by hot_cold_score so coldest VIPs appear first in Sheet
    merged_rows.sort(key=lambda r: float(r.get("hot_cold_score") or 0))

    if dry_run:
        check_email_source(bq, merged_rows)
        print_dry_run_summary(merged_rows, existing_by_uid, today)
        print(f"DRY RUN: would write {len(merged_rows)} rows; Sheet and Slack left untouched.")
        return

    # --- 4. Write back to Sheet ---
    print(f"Writing {len(merged_rows)} rows to Sheet...")
    output = [COLUMNS] + [row_to_list(r) for r in merged_rows]
    try:
        # ws.update() fails if the grid is narrower than COLUMNS.
        if ws.col_count < len(COLUMNS):
            ws.add_cols(len(COLUMNS) - ws.col_count)
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
    msg = build_slack_message(merged_rows, today)

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
