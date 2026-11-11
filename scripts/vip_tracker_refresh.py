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

Gifts: gifts_total_usd and gifts_last_30_days_usd are summed per user_id from the
"Gifts log" tab. Freya owns that tab: the job reads it and never writes to it.

Extra columns: a main-tab column that is neither a job column nor a Freya column is
carried forward by user_id under the same header, after all known columns. Formula
cells keep their formula.

Roster archive: a row that leaves the roster (LTV drops under $100, or the user turns
internal) is copied to the "Roster archive" tab before the main tab is rewritten, so
Freya's notes on it are kept. The job owns that tab and only appends to it.

Creators: CREATOR_USER_IDS are left out of the Heat Report and get no voucher.

Brands: user_fact_crm holds GOATbox and Hero Rips rows since 2026-11-10. Each brand has
its own tab and its own tier config in BRANDS. GOATbox stays on the first tab; Hero Rips
has a "Hero Rips" tab with the same columns plus goatbox_tier at the far right. A Hero
Rips row found on the GOATbox tab moves to the Hero Rips tab with every Freya value. The
Slack heat summary covers GOATbox only.

Env vars required:
  GCP_SERVICE_ACCOUNT_JSON   full JSON of the GCP service account key
  SLACK_BOT_TOKEN            xoxb-... token with chat:write scope
Optional:
  DRY_RUN=1                  query and merge, print counts, but don't write the Sheet or post

Sheet: 1OE2X6Cbg3a0CGO2oRy_uZ28GFt1yFZQmHdjSRbeX-nk (first tab = GOATbox, "Hero Rips" tab)
Slack: C0BAJM1T8LE (#vip)
"""

import json
import os
import sys
from datetime import date, datetime, timedelta

import gspread
from google.cloud import bigquery
from google.oauth2 import service_account
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SHEET_ID = "1OE2X6Cbg3a0CGO2oRy_uZ28GFt1yFZQmHdjSRbeX-nk"
SLACK_CHANNEL = "C0BAJM1T8LE"  # #vip
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"

# Top Hero Rips tier. The name is not final (Niv, 2026-11-11): change it here only.
HERORIPS_TOP_TIER = "Legend"

# One config block per brand. Tiers run highest first as (name, min lifetime purchases
# USD, recent purchase days). Tier is set by lifetime_purchases_usd (money paid in),
# never by pack spend. When recent purchase days is set, the tier also needs a purchase
# in that many days. A user who fits no tier is not on the roster.
# voucher_rate: share of spend_last_30_days, never below 0, no caps yet (add them in
# calc_voucher_usd). None leaves voucher_usd blank: no voucher rule is set for Hero Rips.
BRANDS = {
    "goatbox": {
        "label": "GOATbox",
        "tab": None,  # the first tab, read by index as before
        "tiers": [
            ("GOAT", 1000, None),
            ("Gold", 250, None),
            ("VIP", 100, None),
        ],
        "voucher_rate": 0.10,
    },
    "herorips": {
        "label": "Hero Rips",
        "tab": "Hero Rips",
        "tiers": [
            (HERORIPS_TOP_TIER, 5000, None),
            ("Gold", 1000, None),
            ("VIP", 250, None),
            ("Insider", 100, 30),
        ],
        "voucher_rate": None,
    },
}

# Creator accounts (Niv, 2026-10-15). They get creator grants, so they are left out of
# the Heat Report lists and counts, get a blank voucher_usd, and are excluded from any
# Gifts log vs ledger check. Their rows stay on the main tab.
CREATOR_USER_IDS = {
    "01M1RWNG9HVNF6HR7XT6JBAPTA",
}

ARCHIVE_TAB = "Roster archive"

# Gifts log tab, typed by hand by Freya. Read by header name, never written.
GIFTS_TAB = "Gifts log"
GIFTS_WINDOW_DAYS = 30  # same window as spend_last_30_days
GIFT_DATE_FORMATS = ("%d %b %Y", "%m/%d/%Y", "%Y-%m-%d")  # "07 Sep 2026" is what she types

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
GIFT_COLS = ["gifts_total_usd", "gifts_last_30_days_usd"]  # summed from the Gifts log tab
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
    # Appended last, where Freya first put them, so none of her columns move.
    "gifts_total_usd", "gifts_last_30_days_usd",
]

# Hero Rips tab only: the tier of the same person's GOATbox account, matched by email.
GOATBOX_TIER_COL = "goatbox_tier"


def brand_columns(brand):
    return COLUMNS + [GOATBOX_TIER_COL] if brand == "herorips" else COLUMNS


def tier_rank(brand):
    tiers = BRANDS[brand]["tiers"]
    return {name: len(tiers) - i for i, (name, _, _) in enumerate(tiers)}


def tier_case_sql(brand):
    """SQL CASE for the brand's tiers, from BRANDS. Names and numbers are our own constants."""
    lines = []
    for name, floor, recent_days in BRANDS[brand]["tiers"]:
        cond = f"crm.lifetime_purchases_usd >= {floor}"
        if recent_days:
            cond += (
                f" AND DATE(crm.last_purchase_at) >= "
                f"DATE_SUB(CURRENT_DATE(), INTERVAL {recent_days} DAY)"
            )
        lines.append(f"    WHEN {cond} THEN '{name}'")
    return "CASE\n" + "\n".join(lines) + "\n  END"


def roster_sql(brand):
    floor = min(f for _, f, _ in BRANDS[brand]["tiers"])
    return BQ_SQL.replace("__TIER_CASE__", tier_case_sql(brand)).replace("__FLOOR__", str(floor))


# @brand is 'goatbox' or 'herorips'. __TIER_CASE__ and __FLOOR__ come from BRANDS.
BQ_SQL = """
WITH crm_dedup AS (
  -- user_fact_crm holds both brands, and has duplicate rows for 107 user_ids (same
  -- brand each time, checked 2026-11-11). Without the dedupe those VIPs get two rows
  -- in the Sheet and are double-counted everywhere.
  SELECT *
  FROM `goatbox-prod.processing_data.user_fact_crm`
  WHERE brand = @brand
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
    AND user_id IN (SELECT user_id FROM crm_dedup WHERE lifetime_purchases_usd >= __FLOOR__)
  GROUP BY user_id
)
SELECT * FROM (
SELECT
  crm.user_id,
  __TIER_CASE__ AS tier,
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
  -- user_fact_crm.country is the GA4 location of the registration session, so a US
  -- user who signed up while traveling shows the wrong country (Italy for
  -- 01M4E9AHQC4KC0V00CCV98CQC1, 2026-10-28). registration_country is the account's
  -- own code; the old field is only a fallback when it is null.
  COALESCE(
    CASE crm.registration_country
      WHEN 'US' THEN 'United States'
      WHEN 'GB' THEN 'United Kingdom'
      WHEN 'CA' THEN 'Canada'
      ELSE crm.registration_country
    END,
    crm.country
  ) AS country,
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
WHERE crm.lifetime_purchases_usd >= __FLOOR__
  AND NOT EXISTS (
    SELECT 1 FROM `goatbox-prod.processing_data.internal_users` iu
    WHERE iu.user_id = crm.user_id
  )
)
-- A tier can need more than the floor (Hero Rips Insider needs a recent purchase).
WHERE tier IS NOT NULL
ORDER BY ltv_usd DESC
"""

# Hero Rips goatbox_tier match: the email on each user's latest purchase_completed.
PURCHASE_EMAIL_SQL = """
SELECT
  e.user_id,
  ARRAY_AGG(LOWER(TRIM(p.string_value)) ORDER BY e.event_timestamp DESC LIMIT 1)[OFFSET(0)] AS email
FROM `goatbox-prod.source_data.events` e, UNNEST(e.event_params) p
WHERE p.key = 'email'
  AND p.string_value IS NOT NULL
  AND TRIM(p.string_value) != ''
  AND e.event_name = 'purchase_completed'
  AND e.user_id IN UNNEST(@user_ids)
GROUP BY e.user_id
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


def calc_voucher_usd(spend_last_30_days, rate):
    # Blank spend or no rate stays blank; 0 or negative spend (refunds over purchases) gives 0.
    if rate is None or spend_last_30_days in (None, ""):
        return ""
    return round(max(0.0, float(spend_last_30_days)) * rate, 2)


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


def resolve_tier_change(new_tier, existing, is_new, today_str, rank, moved=False):
    """Compare the tier BQ reports now against the tier the Sheet recorded last run.

    Returns (fields_to_write, move) where move is "up" / "down" / "new" / "moved" / "".
    move is only for today's summary and is never written to the Sheet. A row moved in
    from the other brand's tab starts its tier history fresh: its old tier was on the
    other brand's scale, so comparing them would log a false move.
    """
    sheet_tier = str(existing.get("tier", "") or "").strip()

    if is_new or moved:
        # First time on this brand's roster: they arrived at this tier today.
        return {"previous_tier": "", "tier_changed_at": today_str}, ("moved" if moved else "new")

    if sheet_tier and sheet_tier != new_tier:
        move = "up" if rank.get(new_tier, 0) > rank.get(sheet_tier, 0) else "down"
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


def is_creator(row):
    return str(row.get("user_id", "")).strip() in CREATOR_USER_IDS


def excluded_from_report(row):
    return is_deleted(row) or is_creator(row)


def col_letter(n):
    """1-based column number to its letter, e.g. 34 -> AH."""
    return gspread.utils.rowcol_to_a1(1, n)[:-1]


def days_since(d, today):
    return (today - d).days if d else None


def build_report_lists(merged_rows, today):
    """Rows for the Heat Report, DELETED accounts excluded. Lists are uncapped."""
    live = [r for r in merged_rows if not excluded_from_report(r)]
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


def row_to_list(row_dict, columns=COLUMNS):
    return [str(row_dict.get(col, "") if row_dict.get(col, "") is not None else "") for col in columns]


def parse_gift_date(value):
    text = str(value or "").strip()
    for fmt in GIFT_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def read_gifts_log(spreadsheet, today):
    """Sum the Gifts log per user_id. Returns (totals, rows_by_uid), or (None, {}) if
    the tab or a needed header is missing. Read only: Freya owns this tab.

    totals maps user_id -> (gifts_total_usd, gifts_last_30_days_usd). Problem rows are
    logged by Sheet row number, never by name.
    """
    try:
        gws = spreadsheet.worksheet(GIFTS_TAB)
    except gspread.exceptions.WorksheetNotFound:
        print(f"  WARNING: no '{GIFTS_TAB}' tab, gift columns left blank", file=sys.stderr)
        return None, {}

    values = gws.get_all_values()
    if not values:
        return {}, {}
    header = [h.strip().lower() for h in values[0]]
    needed = ("date", "user_id", "amount_usd")
    missing = [h for h in needed if h not in header]
    if missing:
        print(
            f"  WARNING: '{GIFTS_TAB}' has no {', '.join(missing)} header, gift columns left blank",
            file=sys.stderr,
        )
        return None, {}
    i_date, i_uid, i_amt = (header.index(h) for h in needed)
    i_type = header.index("type") if "type" in header else None

    cutoff = today - timedelta(days=GIFTS_WINDOW_DAYS)
    totals, rows_by_uid = {}, {}
    bad_amount, bad_date, no_uid = [], [], []

    for n, row in enumerate(values[1:], start=2):
        cell = lambda i: row[i].strip() if i is not None and i < len(row) else ""
        uid, amt_text, date_text = cell(i_uid), cell(i_amt), cell(i_date)
        if not (uid or amt_text or date_text):
            continue
        if not uid:
            no_uid.append(n)
            continue
        try:
            amount = float(amt_text.replace("$", "").replace(",", ""))
        except ValueError:
            amount = 0.0
            bad_amount.append(n)
        gift_date = parse_gift_date(date_text)
        if gift_date is None:
            bad_date.append(n)  # still counts in the total, not in the 30-day sum

        total, last30 = totals.get(uid, (0.0, 0.0))
        in_window = gift_date is not None and gift_date >= cutoff
        totals[uid] = (total + amount, last30 + (amount if in_window else 0.0))
        rows_by_uid.setdefault(uid, []).append((n, date_text, amount, cell(i_type)))

    print(f"  Gifts log: {sum(len(r) for r in rows_by_uid.values())} rows, {len(totals)} user_ids")
    for label, rows in (
        ("blank or non-numeric amount_usd, counted as 0", bad_amount),
        ("unreadable date, left out of the 30-day sum", bad_date),
        ("no user_id, skipped", no_uid),
    ):
        if rows:
            print(f"  WARNING: Gifts log rows with {label}: {', '.join(map(str, rows))}", file=sys.stderr)

    return totals, rows_by_uid


def read_extra_columns(ws, shown, known=COLUMNS):
    """Columns on a roster tab the job does not own, by header, keyed on user_id.

    Returns (extra_headers, values_by_uid). A formula cell keeps its formula; any
    other cell keeps its displayed value. Order follows the Sheet.
    """
    if not shown:
        return [], {}
    formulas = ws.get_all_values(value_render_option="FORMULA")
    header = shown[0]
    if "user_id" not in header:
        return [], {}
    i_uid = header.index("user_id")

    extra_idx = [i for i, h in enumerate(header) if h.strip() and h.strip() not in known]
    unnamed = [
        gspread.utils.rowcol_to_a1(1, i + 1).rstrip("1")
        for i, h in enumerate(header)
        if not h.strip() and any(i < len(r) and r[i].strip() for r in shown[1:])
    ]
    if unnamed:
        print(
            f"  WARNING: main-tab columns with data but no header are not kept: {', '.join(unnamed)}",
            file=sys.stderr,
        )

    def cell(grid, r, i):
        return grid[r][i] if r < len(grid) and i < len(grid[r]) else ""

    values_by_uid = {}
    for r in range(1, len(shown)):
        uid = str(cell(shown, r, i_uid)).strip()
        if not uid:
            continue
        kept = []
        for i in extra_idx:
            f = cell(formulas, r, i)
            kept.append(f if isinstance(f, str) and f.startswith("=") else cell(shown, r, i))
        values_by_uid[uid] = kept

    return [header[i].strip() for i in extra_idx], values_by_uid


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


def norm_email(value):
    return str(value or "").strip().lower()


def fill_goatbox_tier(bq, hr_rows, gb_rows):
    """Set goatbox_tier on each Hero Rips row. Mutates hr_rows. Returns the match count.

    The Hero Rips side is the email on the user's latest purchase_completed event. The
    GOATbox side is the email column of the GOATbox tab, so only GOATbox VIPs can match.
    No match leaves the cell blank. Logs counts and user_ids only, never emails.
    """
    rank = tier_rank("goatbox")
    gb_tier_by_email = {}
    for r in gb_rows:
        email = norm_email(r.get("email"))
        if not email or is_deleted(r):
            continue
        best = gb_tier_by_email.get(email)
        if best is None or rank.get(r["tier"], 0) > rank.get(best, 0):
            gb_tier_by_email[email] = r["tier"]

    for r in hr_rows:
        r[GOATBOX_TIER_COL] = ""
    if not hr_rows:
        return 0

    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("user_ids", "STRING", [r["user_id"] for r in hr_rows]),
    ])
    hr_email = {
        row["user_id"]: norm_email(row["email"])
        for row in bq.query(PURCHASE_EMAIL_SQL, job_config=job_config).result()
    }

    matched = []
    for r in hr_rows:
        tier = gb_tier_by_email.get(hr_email.get(r["user_id"], ""))
        if tier:
            r[GOATBOX_TIER_COL] = tier
            matched.append(r["user_id"])
    no_email = sum(1 for r in hr_rows if r["user_id"] not in hr_email)
    print(
        f"  goatbox_tier: {len(matched)} of {len(hr_rows)} Hero Rips rows matched a GOATbox VIP"
        f" ({no_email} with no purchase_completed email)"
        + (f": {', '.join(matched)}" if matched else "")
    )
    return len(matched)


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
    n_excluded = len(merged_rows) - len(live)

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
        f"Total VIPs: {len(live)}" + (f" (excludes {n_excluded} deleted or creator)" if n_excluded else ""),
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

    # Tier moves detected this run, biggest new tier first. The summary is GOATbox only.
    rank = tier_rank("goatbox")
    tier_moves = sorted(
        [r for r in live if r.get("_tier_move") in ("up", "down")],
        key=lambda r: (rank.get(r.get("tier", ""), 0), float(r.get("ltv_usd") or 0)),
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


def archive_rows(spreadsheet, rows, today_str):
    """Append rows that left the roster to the archive tab, matched by header.

    rows are {header: shown value} dicts read from the main tab. New headers are
    added to the end of the archive header; old archive rows are never changed.
    """
    try:
        aws = spreadsheet.worksheet(ARCHIVE_TAB)
    except gspread.exceptions.WorksheetNotFound:
        aws = spreadsheet.add_worksheet(ARCHIVE_TAB, rows=100, cols=len(COLUMNS) + 2)

    values = aws.get_all_values()
    header = [h for h in (values[0] if values else []) if h.strip()]
    wanted = list(rows[0].keys()) + ["archived_at"]
    new_header = header + [h for h in wanted if h not in header]
    if new_header != header:
        if aws.col_count < len(new_header):
            aws.add_cols(len(new_header) - aws.col_count)
        aws.update([new_header], "A1", value_input_option="RAW")

    # A run that failed after archiving would archive the same rows again; skip a
    # user_id already archived today.
    i_uid, i_at = new_header.index("user_id"), new_header.index("archived_at")
    done_today = {
        r[i_uid] for r in values[1:]
        if len(r) > max(i_uid, i_at) and r[i_at] == today_str
    }
    out = [
        [today_str if h == "archived_at" else row.get(h, "") for h in new_header]
        for row in rows if row.get("user_id", "").strip() not in done_today
    ]
    if out:
        aws.append_rows(out, value_input_option="RAW")
    return len(out)


def print_gift_sample(merged_rows, gift_rows, n=5):
    """Dry-run only: gift totals for a few user_ids with the log rows behind them."""
    sample = [r for r in merged_rows if r["user_id"] in gift_rows][:n]
    for r in sample:
        print(
            f"  Gifts {r['user_id']}: total {r['gifts_total_usd']}, "
            f"last {GIFTS_WINDOW_DAYS}d {r['gifts_last_30_days_usd']}"
        )
        for row_num, date_text, amount, gift_type in gift_rows[r["user_id"]]:
            print(f"      log row {row_num}: {date_text} | {amount:.2f} | {gift_type}")


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
        r["user_id"] for r in lists["needs_attention"] + lists["back_on_site"]
        if excluded_from_report(r)
    ]
    print(f"  DELETED or creator rows in lists: {len(deleted_in_lists)}")


# --- Main ---

def read_tab(spreadsheet, brand):
    """Read one brand's roster tab. Returns a dict; a missing Hero Rips tab reads as empty.

    Nothing is created here, so a failed run leaves the Sheet as it was.
    """
    cfg = BRANDS[brand]
    if cfg["tab"] is None:
        ws = spreadsheet.get_worksheet(0)
    else:
        try:
            ws = spreadsheet.worksheet(cfg["tab"])
        except gspread.exceptions.WorksheetNotFound:
            ws = None
    if ws is None:
        return {"ws": None, "existing": {}, "shown": [], "extra_headers": [], "extra_by_uid": {}}

    existing_data = ws.get_all_records(default_blank="")
    shown = ws.get_all_values()
    extra_headers, extra_by_uid = read_extra_columns(ws, shown, brand_columns(brand))
    existing = {str(r.get("user_id", "")): r for r in existing_data if r.get("user_id")}
    return {
        "ws": ws, "existing": existing, "shown": shown,
        "extra_headers": extra_headers, "extra_by_uid": extra_by_uid,
    }


def merge_rows(brand, bq_rows, existing_by_uid, moved_in, gift_totals, today):
    """Merge one brand's BigQuery rows with its Sheet rows. Returns (rows, bad_contact_uids).

    moved_in is the set of user_ids whose Sheet values came from the other brand's tab.
    """
    cfg = BRANDS[brand]
    rank = tier_rank(brand)
    today_str = today.isoformat()
    merged_rows, bad_contact_dates = [], []

    for bq_row in bq_rows:
        uid = str(bq_row.get("user_id", ""))
        existing = existing_by_uid.get(uid, {})
        is_new = uid not in existing_by_uid

        row = {}

        for col in AUTO_COLS:
            row[col] = safe_str(bq_row.get(col))

        row["voucher_usd"] = (
            "" if uid in CREATOR_USER_IDS
            else safe_str(calc_voucher_usd(bq_row.get("spend_last_30_days"), cfg["voucher_rate"]))
        )

        if gift_totals is None:
            row["gifts_total_usd"] = row["gifts_last_30_days_usd"] = ""
        else:
            total, last30 = gift_totals.get(uid, (0.0, 0.0))
            row["gifts_total_usd"] = safe_str(round(total, 2))
            row["gifts_last_30_days_usd"] = safe_str(round(last30, 2))

        # Identity cols: preserved from Sheet; blank for VIPs not yet in Sheet.
        # Names are trimmed of stray spaces; nothing else in them is changed.
        for col in IDENTITY_COLS:
            row[col] = str(existing.get(col, "") or "")
            if col in ("first_name", "last_name"):
                row[col] = row[col].strip()

        # Freya cols: NEVER overwrite. New VIPs get relationship_stage="new", rest blank.
        # A row moved from the other tab is not new: it keeps every Freya value.
        for col in FREYA_COLS:
            if is_new and col == "relationship_stage":
                row[col] = "new"
            else:
                row[col] = str(existing.get(col, "") or "")

        tier_fields, move = resolve_tier_change(
            row["tier"], existing, is_new, today_str, rank, moved=uid in moved_in,
        )
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

    # Sort ascending by hot_cold_score so coldest VIPs appear first in Sheet
    merged_rows.sort(key=lambda r: float(r.get("hot_cold_score") or 0))
    return merged_rows, bad_contact_dates


def rows_leaving(tab, keep_uids):
    """Rows on a tab whose user_id is not in keep_uids, with every column as shown."""
    shown = tab["shown"]
    header = shown[0] if shown else []
    out = []
    for vals in shown[1:]:
        rec = {h: (vals[i] if i < len(vals) else "") for i, h in enumerate(header) if h.strip()}
        if str(rec.get("user_id", "")).strip() and rec["user_id"].strip() not in keep_uids:
            out.append(rec)
    return out


def write_tab(ws, output):
    """Write output over a tab, then clear only what is left beyond it.

    Raises on a failed write; a failed write leaves the old data in place instead of an
    empty tab. Returns the ranges it could not clear, as an error string, or "".
    """
    height, width = len(output), len(output[0])
    # Grow the grid first: ws.update() fails on a range past the sheet edge.
    if ws.col_count < width:
        ws.add_cols(width - ws.col_count)
    if ws.row_count < height:
        ws.add_rows(height - ws.row_count)
    ws.update(output, "A1", value_input_option="USER_ENTERED")

    leftovers = []
    if ws.row_count > height:
        leftovers.append(f"A{height + 1}:{col_letter(ws.col_count)}{ws.row_count}")
    if ws.col_count > width:
        leftovers.append(f"{col_letter(width + 1)}1:{col_letter(ws.col_count)}{height}")
    try:
        if leftovers:
            ws.batch_clear(leftovers)
    except Exception as exc:
        return f"clearing old rows below it failed ({', '.join(leftovers)}): {exc}"
    return ""


def tab_output(brand, rows, tab):
    # Extra columns go after every known column; a new VIP gets blanks in them.
    blank_extras = [""] * len(tab["extra_headers"])
    cols = brand_columns(brand)
    return [cols + tab["extra_headers"]] + [
        row_to_list(r, cols) + tab["extra_by_uid"].get(r["user_id"], blank_extras) for r in rows
    ]


def tier_counts(rows, brand):
    counts = {name: 0 for name, _, _ in BRANDS[brand]["tiers"]}
    for r in rows:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    return counts


def print_brand_dry_run(brand, rows, tab):
    label = BRANDS[brand]["label"]
    out = tab_output(brand, rows, tab)
    print(f"  [{label}] {len(rows)} rows, tiers {tier_counts(rows, brand)}")
    print(f"  [{label}] output {len(out)} x {len(out[0])}, header ends ...{', '.join(out[0][-3:])}")


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
    bq_creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    bq = bigquery.Client(credentials=bq_creds, project=sa_info["project_id"])

    bq_rows = {}
    for brand in BRANDS:
        print(f"Querying BigQuery for the {BRANDS[brand]['label']} VIP roster...")
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("brand", "STRING", brand),
        ])
        try:
            bq_rows[brand] = [dict(r) for r in bq.query(roster_sql(brand), job_config=job_config).result()]
        except Exception as exc:
            msg = str(exc)
            print(f"BigQuery error: {msg}", file=sys.stderr)
            post_error(slack, msg)
            sys.exit(1)
        print(f"  BQ returned {len(bq_rows[brand])} rows")

    # Guard: a 0-row result almost certainly means a query/data problem, not a real empty VIP list.
    # Clearing the sheet on 0 rows would wipe all of Freya's notes.
    for brand, rows in bq_rows.items():
        if not rows:
            msg = f"BQ returned 0 {BRANDS[brand]['label']} VIP rows - refusing to overwrite Sheet data"
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
    spreadsheet = gc.open_by_key(SHEET_ID)

    # Nothing is written yet, so a failed read stops the run with the Sheet untouched.
    try:
        tabs = {brand: read_tab(spreadsheet, brand) for brand in BRANDS}
        gift_totals, gift_rows = read_gifts_log(spreadsheet, today)
    except Exception as exc:
        msg = f"Sheet read failed, nothing written: {exc}"
        print(msg, file=sys.stderr)
        post_error(slack, msg)
        sys.exit(1)

    gb_tab, hr_tab = tabs["goatbox"], tabs["herorips"]
    for brand, tab in tabs.items():
        where = "no tab yet" if tab["ws"] is None else f"{len(tab['existing'])} existing rows"
        print(f"  [{BRANDS[brand]['label']}] {where}")
        if tab["extra_headers"]:
            print(f"  [{BRANDS[brand]['label']}] keeping {len(tab['extra_headers'])} extra columns: "
                  f"{', '.join(tab['extra_headers'])}")

    # --- 3. Move Hero Rips rows off the GOATbox tab ---
    # A Hero Rips user on the GOATbox tab takes its Sheet values (Freya's columns, names,
    # email) to the Hero Rips tab. The Hero Rips tab wins if the user is on both.
    hr_roster = {str(r["user_id"]) for r in bq_rows["herorips"]}
    moved_in = {
        uid for uid in gb_tab["existing"]
        if uid in hr_roster and uid not in hr_tab["existing"]
    }
    # Extra columns on the GOATbox tab have no place on the Hero Rips tab yet. Stop
    # rather than drop a note.
    stuck = [
        uid for uid in moved_in
        if any(str(v).strip() for v in gb_tab["extra_by_uid"].get(uid, []))
    ]
    if stuck:
        msg = (
            "Hero Rips rows to move have values in extra GOATbox columns "
            f"({', '.join(gb_tab['extra_headers'])}), nothing written: {', '.join(sorted(stuck))}"
        )
        print(msg, file=sys.stderr)
        post_error(slack, msg)
        sys.exit(1)
    hr_existing = dict(hr_tab["existing"])
    for uid in moved_in:
        hr_existing[uid] = gb_tab["existing"][uid]
    if moved_in:
        print(f"  Moving {len(moved_in)} Hero Rips rows to '{BRANDS['herorips']['tab']}': "
              f"{', '.join(sorted(moved_in))}")

    # --- 4. Merge BQ data with existing Sheet rows ---
    merged = {}
    merged["goatbox"], bad_gb = merge_rows(
        "goatbox", bq_rows["goatbox"], gb_tab["existing"], set(), gift_totals, today,
    )
    merged["herorips"], bad_hr = merge_rows(
        "herorips", bq_rows["herorips"], hr_existing, moved_in, gift_totals, today,
    )
    bad_contact_dates = bad_gb + bad_hr
    if bad_contact_dates:
        print(
            f"  WARNING: last_contacted_at not MM/DD/YYYY for {len(bad_contact_dates)} rows, "
            f"scored as never contacted: {', '.join(bad_contact_dates)}",
            file=sys.stderr,
        )

    all_rows = merged["goatbox"] + merged["herorips"]
    if gift_totals:
        roster = {r["user_id"] for r in all_rows}
        off_roster = sorted(u for u in gift_totals if u not in roster)
        if off_roster:
            print(
                f"  WARNING: Gifts log user_ids on neither roster tab, skipped: {', '.join(off_roster)}",
                file=sys.stderr,
            )

    # A failed lookup leaves emails blank for a retry next run; it must not block the refresh.
    try:
        fill_blank_emails(bq, all_rows)
    except Exception as exc:
        print(f"  WARNING: email fill skipped: {exc}", file=sys.stderr)

    # A failed match leaves goatbox_tier blank; it must not block the refresh.
    try:
        fill_goatbox_tier(bq, merged["herorips"], merged["goatbox"])
    except Exception as exc:
        for r in merged["herorips"]:
            r[GOATBOX_TIER_COL] = ""
        print(f"  WARNING: goatbox_tier match skipped: {exc}", file=sys.stderr)

    vouchers = [
        float(r["voucher_usd"]) for r in merged["goatbox"]
        if r["voucher_usd"] != "" and not excluded_from_report(r)
    ]
    paying = [v for v in vouchers if v > 0]
    print(
        f"  Vouchers (GOATbox): {len(paying)} rows above 0, "
        f"total ${sum(paying):,.2f}, max ${max(paying, default=0):,.2f}"
    )

    for brand, rows in merged.items():
        n_up = sum(1 for r in rows if r["_tier_move"] == "up")
        n_down = sum(1 for r in rows if r["_tier_move"] == "down")
        print(f"  [{BRANDS[brand]['label']}] tier moves since last run: {n_up} up, {n_down} down")

    # Rows that left a roster go to the archive. A Hero Rips row leaving the GOATbox tab
    # is moving tabs, not leaving, so it is not archived.
    gb_keep = {r["user_id"] for r in merged["goatbox"]} | hr_roster
    hr_keep = {r["user_id"] for r in merged["herorips"]}
    dropped = rows_leaving(gb_tab, gb_keep) + rows_leaving(hr_tab, hr_keep)
    if dropped:
        print(f"  Left the roster, to archive: {', '.join(r['user_id'] for r in dropped)}")

    if dry_run:
        for brand in BRANDS:
            print_brand_dry_run(brand, merged[brand], tabs[brand])
        gb_before = set(gb_tab["existing"])
        gb_after = {r["user_id"] for r in merged["goatbox"]}
        print(f"  [GOATbox] leaving the tab: {', '.join(sorted(gb_before - gb_after)) or 'none'}")
        print(f"  [GOATbox] new on the tab: {', '.join(sorted(gb_after - gb_before)) or 'none'}")
        for r in sorted(merged["herorips"], key=lambda r: -float(r["ltv_usd"] or 0)):
            src = "moved from GOATbox tab" if r["user_id"] in moved_in else (
                "already on tab" if r["user_id"] in hr_tab["existing"] else "new")
            print(
                f"  [Hero Rips] {r['user_id']} | {r['tier']} | ${float(r['ltv_usd']):,.2f} | "
                f"goatbox_tier '{r[GOATBOX_TIER_COL]}' | {src}"
            )
        # Freya and identity values on moved rows, checked against the GOATbox tab. Counts only.
        check_cols = FREYA_COLS + IDENTITY_COLS
        def sheet_value(uid, col):
            # merge_rows trims names, so compare names trimmed too.
            v = str(gb_tab["existing"][uid].get(col, "") or "")
            return v.strip() if col in ("first_name", "last_name") else v

        same = [
            r["user_id"] for r in merged["herorips"]
            if r["user_id"] in moved_in
            and all(str(r[c]) == sheet_value(r["user_id"], c) for c in check_cols)
        ]
        print(f"  Moved rows with all {len(check_cols)} Freya and identity values unchanged: "
              f"{len(same)} of {len(moved_in)}")
        check_email_source(bq, merged["goatbox"])
        print_dry_run_summary(merged["goatbox"], gb_tab["existing"], today)
        print_gift_sample(merged["goatbox"], gift_rows)
        print("DRY RUN: Sheet and Slack left untouched.")
        return

    # --- 5. Write back to Sheet ---
    # Archive first: if this fails, the roster tabs are untouched and the rows are still there.
    if dropped:
        try:
            n = archive_rows(spreadsheet, dropped, today_str)
            print(f"  Archived {n} rows to '{ARCHIVE_TAB}' ({len(dropped) - n} already archived today)")
        except Exception as exc:
            msg = f"Archive write failed, roster tabs not touched: {exc}"
            print(msg, file=sys.stderr)
            post_error(slack, msg)
            sys.exit(1)

    # Hero Rips tab before GOATbox tab: if it fails, the moved rows are still on the
    # GOATbox tab with Freya's values.
    for brand in ("herorips", "goatbox"):
        label = BRANDS[brand]["label"]
        output = tab_output(brand, merged[brand], tabs[brand])
        print(f"Writing {len(merged[brand])} rows to the {label} tab...")
        try:
            ws = tabs[brand]["ws"]
            if ws is None:
                ws = spreadsheet.add_worksheet(
                    BRANDS[brand]["tab"], rows=len(output) + 10, cols=len(output[0]),
                )
            clear_err = write_tab(ws, output)
        except Exception as exc:
            msg = f"{label} tab write failed, old data left in place: {exc}"
            print(msg, file=sys.stderr)
            post_error(slack, msg)
            sys.exit(1)
        if clear_err:
            msg = f"{label} tab written, but {clear_err}"
            print(msg, file=sys.stderr)
            post_error(slack, msg)
            sys.exit(1)
        print(f"  {label} tab updated.")

    # --- 6. Post heat summary to Slack #vip (GOATbox only) ---
    print("Posting heat summary to #vip...")
    msg = build_slack_message(merged["goatbox"], today)

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
