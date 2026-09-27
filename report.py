import os
import sys
import datetime
from collections import defaultdict
from google.cloud import bigquery
from slack_sdk import WebClient

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
CHANNEL_ID    = "C0B3KS5KNTC"
ERROR_USER_ID = "U0B0ZF5D6F9"

EXCLUDED = (
    "'01KMNX0P8P8YS059XD11X9W1C8','01KP3FQG9FSJ2D0WDRR2CH8E28',"
    "'01KPT19E79379R80PPAFDSZX67','01KQJ170MB0KJMG3XPQHBG9TNQ',"
    "'01KNM91GV3JKY3YH3H48680G22','01KR2BK0ZASKGHHH45345F793Q',"
    "'01KP63M6W60TM2RCTJNCW6EG3P','01KNWY3PDZJN57SZ7XPEJR0JAF',"
    "'01KMG7YDVC7AW60C4DQDD2SZF5','01KH6GBXAH8A5R7HGHX7710Q00',"
    "'01KR9AA6ZZ2J0BWNFRJR5AA494','01KPR58SZEJX7Y0AZJBRB8P737',"
    "'01KSQ8QQJJQSAME8BBC6CWWMH7'"
)

# How many returning payers to break down transaction-by-transaction
MAX_RETURNING_DETAIL = 15
# How many products to list inline per user before collapsing
MAX_ITEMS_PER_USER   = 6

slack  = WebClient(token=SLACK_TOKEN)
bq     = bigquery.Client()
DATE   = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

# ── Helpers ───────────────────────────────────────────────────────────────────
def q(sql, params=None):
    job_config = bigquery.QueryJobConfig(query_parameters=params) if params else None
    return [dict(row) for row in bq.query(sql.replace("DATE_FILTER", DATE), job_config=job_config).result()]

def send_error(msg):
    try:
        slack.chat_postMessage(channel=ERROR_USER_ID, text=f":warning: *Daily report failed ({DATE})*\n{msg}")
    except Exception:
        print(f"[send_error] Slack notification failed. Original error: {msg}", file=sys.stderr)

def usd(v):
    return f"${float(v or 0):,.2f}"

def pct(part, whole):
    return round(part / whole * 100) if whole else 0

def clean_slug(s):
    return (s or "unknown").replace("-shop-item", "").replace("-", " ").title()

def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")

# ── Queries ───────────────────────────────────────────────────────────────────
try:
    dau_rows = q(f"""
        SELECT COUNT(DISTINCT user_id) AS daily_active_users
        FROM `goatbox-prod.processing_data.flat_login_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER'
          AND user_id NOT IN ({EXCLUDED})
    """)
    dau_row = dau_rows[0] if dau_rows else {"daily_active_users": 0}

    summary_rows = q(f"""
        SELECT
          COUNT(DISTINCT user_id)               AS total_payers,
          COUNT(*)                               AS total_transactions,
          ROUND(SUM(amount_usd), 2)              AS total_revenue_usd,
          ROUND(AVG(amount_usd), 2)              AS avg_transaction_usd,
          ROUND(MAX(amount_usd), 2)              AS max_transaction_usd,
          COUNTIF(coupon_code IS NOT NULL)       AS transactions_with_coupon,
          ROUND(SUM(IF(coupon_code IS NOT NULL, amount_usd, 0)), 2) AS coupon_revenue_usd
        FROM `goatbox-prod.processing_data.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER'
          AND user_id NOT IN ({EXCLUDED})
    """)
    summary = summary_rows[0] if summary_rows else {
        "total_payers": 0, "total_transactions": 0,
        "total_revenue_usd": 0, "avg_transaction_usd": 0,
        "max_transaction_usd": 0, "transactions_with_coupon": 0,
        "coupon_revenue_usd": 0
    }

    by_product = q(f"""
        SELECT
          product_slug,
          COUNT(DISTINCT user_id)   AS payers,
          COUNT(*)                   AS transactions,
          ROUND(SUM(amount_usd), 2)  AS revenue_usd,
          ROUND(AVG(amount_usd), 2)  AS avg_usd
        FROM `goatbox-prod.processing_data.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER'
          AND user_id NOT IN ({EXCLUDED})
        GROUP BY product_slug
        ORDER BY revenue_usd DESC
    """)

    # Per-user purchase activity for the day
    today_by_user = q(f"""
        SELECT
          user_id,
          COUNT(*)                                             AS txns,
          ROUND(SUM(amount_usd), 2)                            AS revenue_usd,
          ROUND(AVG(amount_usd), 2)                            AS avg_txn_usd,
          ROUND(MAX(amount_usd), 2)                            AS max_txn_usd,
          COUNTIF(coupon_code IS NOT NULL)                     AS coupon_txns,
          COUNT(DISTINCT product_slug)                         AS distinct_products,
          FORMAT_TIMESTAMP('%H:%M', MIN(event_timestamp))      AS first_purchase_utc,
          FORMAT_TIMESTAMP('%H:%M', MAX(event_timestamp))      AS last_purchase_utc,
          TIMESTAMP_DIFF(MAX(event_timestamp), MIN(event_timestamp), MINUTE) AS span_minutes
        FROM `goatbox-prod.processing_data.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER'
          AND user_id NOT IN ({EXCLUDED})
        GROUP BY user_id
        ORDER BY revenue_usd DESC
    """)

    # Per-user / per-product breakdown for the day
    today_items = q(f"""
        SELECT
          user_id,
          product_slug,
          COUNT(*)                  AS txns,
          ROUND(SUM(amount_usd), 2) AS revenue_usd
        FROM `goatbox-prod.processing_data.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER'
          AND user_id NOT IN ({EXCLUDED})
        GROUP BY user_id, product_slug
        ORDER BY revenue_usd DESC
    """)

    # New vs returning classification + purchase history for today's payers
    cohort = q(f"""
        WITH today_payers AS (
          SELECT DISTINCT user_id
          FROM `goatbox-prod.processing_data.flat_purchase_events`
          WHERE DATE(event_timestamp) = 'DATE_FILTER'
            AND user_id NOT IN ({EXCLUDED})
        ),
        all_history AS (
          SELECT
            p.user_id,
            MAX(IF(DATE(p.event_timestamp) < 'DATE_FILTER', p.event_timestamp, NULL)) AS last_prior_purchase_ts,
            COUNTIF(DATE(p.event_timestamp) < 'DATE_FILTER')                            AS prior_purchases,
            ROUND(SUM(IF(DATE(p.event_timestamp) < 'DATE_FILTER', p.amount_usd, 0)), 2) AS prior_revenue_usd,
            COUNT(*)                                                                    AS lifetime_purchases,
            ROUND(SUM(p.amount_usd), 2)                                                 AS lifetime_value_usd,
            ROUND(SUM(IF(DATE(p.event_timestamp) = 'DATE_FILTER', p.amount_usd, 0)), 2) AS today_revenue_usd
          FROM `goatbox-prod.processing_data.flat_purchase_events` p
          INNER JOIN today_payers t ON p.user_id = t.user_id
          WHERE DATE(p.event_timestamp) <= 'DATE_FILTER'
          GROUP BY p.user_id
        )
        SELECT
          user_id,
          CASE WHEN prior_purchases = 0 THEN 'new' ELSE 'return' END AS payer_type,
          prior_purchases,
          prior_revenue_usd,
          lifetime_purchases,
          lifetime_value_usd,
          today_revenue_usd,
          DATE(last_prior_purchase_ts)                                     AS last_prior_purchase_date,
          DATE_DIFF(DATE 'DATE_FILTER', DATE(last_prior_purchase_ts), DAY) AS days_since_last_purchase
        FROM all_history
        ORDER BY today_revenue_usd DESC
    """)

    box_opens = q(f"""
        WITH payers AS (
          SELECT DISTINCT user_id
          FROM `goatbox-prod.processing_data.flat_purchase_events`
          WHERE DATE(event_timestamp) = 'DATE_FILTER'
            AND user_id NOT IN ({EXCLUDED})
        )
        SELECT
          b.box_display_name,
          b.box_volatility,
          SUM(b.boxes_opened_count)  AS total_opens,
          COUNT(DISTINCT b.user_id)  AS unique_openers
        FROM `goatbox-prod.processing_data.flat_box_events` b
        INNER JOIN payers p ON b.user_id = p.user_id
        WHERE DATE(b.event_timestamp) = 'DATE_FILTER'
        GROUP BY b.box_display_name, b.box_volatility
        ORDER BY total_opens DESC
        LIMIT 10
    """)

except Exception as e:
    send_error(f"BigQuery query failed:\n```{e}```")
    raise

# ── Derived values ────────────────────────────────────────────────────────────
dau            = dau_row["daily_active_users"]
total_payers   = summary["total_payers"]
total_txns     = summary["total_transactions"]
total_rev      = float(summary["total_revenue_usd"] or 0)
avg_spend      = float(summary["avg_transaction_usd"] or 0)
max_txn        = float(summary["max_transaction_usd"] or 0)
coupon_count   = summary["transactions_with_coupon"]
coupon_rev     = float(summary["coupon_revenue_usd"] or 0)

arppu          = total_rev / total_payers if total_payers else 0
txn_per_payer  = total_txns / total_payers if total_payers else 0
conversion     = round(total_payers / dau * 100, 1) if dau else 0

# Index today's activity by user
activity = {r["user_id"]: r for r in today_by_user}

items_by_user = defaultdict(list)
for r in today_items:
    items_by_user[r["user_id"]].append(r)

new_payers    = [r for r in cohort if r["payer_type"] == "new"]
return_payers = [r for r in cohort if r["payer_type"] == "return"]

def cohort_stats(rows):
    users = len(rows)
    rev   = sum(float(r.get("today_revenue_usd") or 0) for r in rows)
    txns  = sum(int(activity.get(r["user_id"], {}).get("txns") or 0) for r in rows)
    return {
        "users": users,
        "rev": rev,
        "txns": txns,
        "arppu": rev / users if users else 0,
        "avg_txn": rev / txns if txns else 0,
        "user_pct": pct(users, total_payers),
        "rev_pct": pct(rev, total_rev),
    }

new_stats = cohort_stats(new_payers)
ret_stats = cohort_stats(return_payers)

# Buyers who transacted more than once today
repeat_buyers  = [r for r in today_by_user if int(r["txns"] or 0) >= 2]
repeat_rev     = sum(float(r["revenue_usd"] or 0) for r in repeat_buyers)

def items_line(user_id):
    """One-line product breakdown of a user's same-day purchases."""
    items = sorted(items_by_user.get(user_id, []), key=lambda i: -float(i["revenue_usd"] or 0))
    shown = items[:MAX_ITEMS_PER_USER]
    parts = []
    for i in shown:
        count = int(i["txns"] or 0)
        label = clean_slug(i["product_slug"])
        if count > 1:
            label += f" ×{count}"
        parts.append(f"{label} {usd(i['revenue_usd'])}")
    extra = len(items) - len(shown)
    if extra > 0:
        parts.append(f"+{plural(extra, 'more product')}")
    return " | ".join(parts) if parts else "—"

def timing_line(user_id):
    a = activity.get(user_id)
    if not a:
        return ""
    first, last = a.get("first_purchase_utc"), a.get("last_purchase_utc")
    if not first:
        return ""
    if int(a.get("txns") or 0) <= 1 or first == last:
        return f"single purchase at {first} UTC"
    span = int(a.get("span_minutes") or 0)
    if span >= 60:
        window = f"{span // 60}h {span % 60}m"
    else:
        window = f"{span}m"
    return f"{first}–{last} UTC ({window})"

# ── Build Slack message ───────────────────────────────────────────────────────
lines = [
    f"\U0001f4b0 *Goatbox Daily Report — {DATE}*",
    "",
    f"*{usd(total_rev)}* revenue  ·  *{total_txns}* transactions  ·  *{total_payers}* payers",
    f"*{usd(arppu)}* ARPPU  ·  *{usd(avg_spend)}* avg/txn  ·  *{txn_per_payer:.1f}* txns/payer  ·  "
    f"*{usd(max_txn)}* largest txn",
    f"*{dau:,}* DAU  ·  *{conversion}%* payer conversion  ·  "
    f"*{coupon_count}* coupon txns (*{pct(coupon_count, total_txns)}%* of txns, {usd(coupon_rev)})",
]

# ── Revenue split: new vs returning ───────────────────────────────────────────
lines += ["", "*Revenue Split — New vs Returning*", ""]
if total_payers:
    for label, s in (("Returning", ret_stats), ("New", new_stats)):
        lines.append(
            f"• *{label}* · {usd(s['rev'])} (*{s['rev_pct']}%* of revenue) · "
            f"{plural(s['users'], 'payer')} ({s['user_pct']}%) · {plural(s['txns'], 'txn')} · "
            f"{usd(s['arppu'])} ARPPU · {usd(s['avg_txn'])} avg/txn"
        )
    if repeat_buyers:
        lines.append(
            f"• *Repeat buyers today* · {plural(len(repeat_buyers), 'payer')} bought 2+ times · "
            f"{usd(repeat_rev)} (*{pct(repeat_rev, total_rev)}%* of revenue)"
        )
else:
    lines.append("• No purchases recorded today")

# ── Returning payers, transaction by transaction ──────────────────────────────
lines += ["", f"*Returning Payers — Same-Day Purchases* ({len(return_payers)} total)", ""]
if return_payers:
    ranked = sorted(return_payers, key=lambda r: -float(r.get("today_revenue_usd") or 0))
    for idx, r in enumerate(ranked[:MAX_RETURNING_DETAIL], start=1):
        uid    = r["user_id"]
        a      = activity.get(uid, {})
        txns   = int(a.get("txns") or 0)
        today  = float(r.get("today_revenue_usd") or 0)
        days   = r.get("days_since_last_purchase")
        last   = r.get("last_prior_purchase_date")
        if days is None:
            gap = "no prior purchase date on record"
        else:
            gap = f"back after {plural(days, 'day')} (last: {last})"
        share  = pct(today, total_rev)
        timing = timing_line(uid)

        lines.append(f"*{idx}. `{uid}`* — *{usd(today)}* today · {plural(txns, 'txn')} · {share}% of daily revenue")
        lines.append(f"     ↳ bought: {items_line(uid)}")
        if timing:
            lines.append(f"     ↳ {timing}")
        lines.append(
            f"     ↳ {gap} · {usd(r.get('prior_revenue_usd'))} prior spend over "
            f"{plural(int(r.get('prior_purchases') or 0), 'purchase')} · {usd(r.get('lifetime_value_usd'))} LTV"
        )
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    hidden = len(ranked) - MAX_RETURNING_DETAIL
    if hidden > 0:
        hidden_rev = sum(float(x.get("today_revenue_usd") or 0) for x in ranked[MAX_RETURNING_DETAIL:])
        lines.append(f"_+{plural(hidden, 'more returning payer')} · {usd(hidden_rev)} combined_")
else:
    lines.append("• No returning payers today")

# ── New payers, one line each ─────────────────────────────────────────────────
lines += ["", f"*New Payers — First Purchase* ({len(new_payers)} total)", ""]
if new_payers:
    ranked_new = sorted(new_payers, key=lambda r: -float(r.get("today_revenue_usd") or 0))
    for r in ranked_new[:10]:
        uid  = r["user_id"]
        a    = activity.get(uid, {})
        lines.append(
            f"• *`{uid}`* · {usd(r.get('today_revenue_usd'))} · {plural(int(a.get('txns') or 0), 'txn')}"
            f" · {items_line(uid)}"
        )
    hidden_new = len(ranked_new) - 10
    if hidden_new > 0:
        hidden_new_rev = sum(float(x.get("today_revenue_usd") or 0) for x in ranked_new[10:])
        lines.append(f"_+{plural(hidden_new, 'more new payer')} · {usd(hidden_new_rev)} combined_")
else:
    lines.append("• No new payers today")

# ── Revenue by product ────────────────────────────────────────────────────────
lines += ["", "*Revenue by Store Product*", ""]
if by_product:
    for r in by_product:
        rev = float(r["revenue_usd"] or 0)
        lines.append(
            f"• *{clean_slug(r['product_slug'])}* · {usd(rev)} (*{pct(rev, total_rev)}%*) · "
            f"{plural(int(r['transactions']), 'txn')} · {plural(int(r['payers']), 'payer')} · "
            f"{usd(r['avg_usd'])} avg"
        )
else:
    lines.append("• No purchases recorded today")

# ── Top spenders ──────────────────────────────────────────────────────────────
lines += ["", "*Top Spenders*", ""]
if today_by_user:
    payer_type = {r["user_id"]: r["payer_type"] for r in cohort}
    for r in today_by_user[:10]:
        uid = r["user_id"]
        rev = float(r["revenue_usd"] or 0)
        tag = "new" if payer_type.get(uid) == "new" else "returning"
        lines.append(
            f"• *`{uid}`* · {usd(rev)} (*{pct(rev, total_rev)}%*) · "
            f"{plural(int(r['txns'] or 0), 'txn')} · {usd(r['avg_txn_usd'])} avg/txn · _{tag}_"
        )
else:
    lines.append("• No data")

# ── Top box opens (paying users) ──────────────────────────────────────────────
lines += ["", "*Top Box Opens*", ""]
if box_opens:
    for r in box_opens:
        lines.append(
            f"• *{r['box_display_name']}* · {int(r['total_opens']):,} opens"
            f" · {plural(int(r['unique_openers']), 'user')} · vol {r['box_volatility']}"
        )
else:
    lines.append("• No box opens by paying users today")

message = "\n".join(lines)

# ── Post to Slack ─────────────────────────────────────────────────────────────
try:
    resp = slack.chat_postMessage(channel=CHANNEL_ID, text=message)
    thread_ts = resp["ts"]
    print(f"Report posted: https://secrethumans.slack.com/archives/{CHANNEL_ID}/p{thread_ts.replace('.','')}",
          flush=True)
except Exception as e:
    send_error(f"Slack post failed:\n```{e}```")
    raise
