import os
import io
import sys
import datetime
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from google.cloud import bigquery
from slack_sdk import WebClient

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
CHANNEL_ID    = "C0B3KS5KNTC"
ERROR_USER_ID = "U0B0ZF5D6F9"

PROJECT  = "goatbox-prod.processing_data"
INTERNAL = f"`{PROJECT}.internal_users`"

TREND_DAYS           = 7   # trailing window, inclusive of the report date
MAX_RETURNING_DETAIL = 15  # returning payers broken out transaction-by-transaction
MAX_ITEMS_PER_USER   = 6   # products listed inline per user before collapsing
MAX_BLOCKED_DETAIL   = 12  # users listed in the AML-wall recovery list

slack = WebClient(token=SLACK_TOKEN)
bq    = bigquery.Client()
DATE  = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

def not_internal(alias=""):
    """Exclude staff/test accounts using the warehouse's canonical list."""
    col = f"{alias}user_id" if alias else "user_id"
    return f"{col} NOT IN (SELECT user_id FROM {INTERNAL} WHERE user_id IS NOT NULL)"

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

def usd0(v):
    return f"${float(v or 0):,.0f}"

def pct(part, whole):
    return round(part / whole * 100) if whole else 0

def clean_slug(s):
    return (s or "unknown").replace("-shop-item", "").replace("-", " ").title()

def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")

# ── Queries ───────────────────────────────────────────────────────────────────
try:
    # Trailing 7-day funnel spine — drives both the averages and the charts.
    trend = q(f"""
        WITH spine AS (
          SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(
            DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY), DATE 'DATE_FILTER')) d
        ),
        reg AS (
          SELECT DATE(event_timestamp) d, COUNT(DISTINCT user_id) n
          FROM `{PROJECT}.flat_registration_events`
          WHERE DATE(event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'
            AND {not_internal()}
          GROUP BY 1
        ),
        shop AS (
          SELECT DATE(event_timestamp) d, COUNT(DISTINCT user_id) n
          FROM `{PROJECT}.flat_shop_opened_events`
          WHERE DATE(event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'
            AND {not_internal()}
          GROUP BY 1
        ),
        ini AS (
          SELECT DATE(event_timestamp) d, COUNT(DISTINCT user_id) u, ROUND(SUM(amount_usd), 2) usd
          FROM `{PROJECT}.flat_purchase_initiated_events`
          WHERE DATE(event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'
            AND {not_internal()}
          GROUP BY 1
        ),
        pur AS (
          SELECT DATE(event_timestamp) d, COUNT(DISTINCT user_id) u, COUNT(*) n, ROUND(SUM(amount_usd), 2) usd
          FROM `{PROJECT}.flat_purchase_events`
          WHERE DATE(event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'
            AND {not_internal()}
          GROUP BY 1
        ),
        blk AS (
          SELECT DATE(event_timestamp) d, COUNT(DISTINCT user_id) u, COUNT(*) n, ROUND(SUM(amount_usd), 2) usd
          FROM `{PROJECT}.flat_purchase_blocked_events`
          WHERE DATE(event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'
            AND {not_internal()}
          GROUP BY 1
        ),
        -- Refunds land three times (new / processing / succeeded); only settled ones count.
        ref AS (
          SELECT DATE(event_timestamp) d, COUNT(*) n, ROUND(SUM(amount), 2) usd
          FROM `{PROJECT}.flat_payment_refunded`
          WHERE DATE(event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'
            AND status = 'succeeded' AND currency = 'USD'
            AND {not_internal()}
          GROUP BY 1
        ),
        dis AS (
          SELECT DATE(event_timestamp) d, COUNT(DISTINCT user_id) u
          FROM `{PROJECT}.flat_payment_disputed`
          WHERE DATE(event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'
            AND {not_internal()}
          GROUP BY 1
        ),
        aml AS (
          SELECT DATE(event_timestamp) d,
            COUNT(DISTINCT IF(event_name = 'aml_submitted', user_id, NULL)) submitted,
            COUNT(DISTINCT IF(aml_status = 'APPROVED',      user_id, NULL)) approved,
            COUNT(DISTINCT IF(aml_status = 'REJECTED',      user_id, NULL)) rejected
          FROM `{PROJECT}.flat_aml_events`
          WHERE DATE(event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'
            AND {not_internal()}
          GROUP BY 1
        )
        SELECT
          spine.d                          AS day,
          IFNULL(reg.n, 0)                 AS registrations,
          IFNULL(shop.n, 0)                AS shop_users,
          IFNULL(ini.u, 0)                 AS intent_users,
          IFNULL(ini.usd, 0)               AS intent_usd,
          IFNULL(pur.u, 0)                 AS paid_users,
          IFNULL(pur.n, 0)                 AS paid_txns,
          IFNULL(pur.usd, 0)               AS gross_usd,
          IFNULL(blk.u, 0)                 AS blocked_users,
          IFNULL(blk.n, 0)                 AS blocked_events,
          IFNULL(blk.usd, 0)               AS blocked_usd,
          IFNULL(ref.n, 0)                 AS refunds,
          IFNULL(ref.usd, 0)               AS refund_usd,
          IFNULL(pur.usd, 0) - IFNULL(ref.usd, 0) AS net_usd,
          IFNULL(dis.u, 0)                 AS disputes,
          IFNULL(aml.submitted, 0)         AS aml_submitted,
          IFNULL(aml.approved, 0)          AS aml_approved,
          IFNULL(aml.rejected, 0)          AS aml_rejected
        FROM spine
        LEFT JOIN reg  ON reg.d  = spine.d
        LEFT JOIN shop ON shop.d = spine.d
        LEFT JOIN ini  ON ini.d  = spine.d
        LEFT JOIN pur  ON pur.d  = spine.d
        LEFT JOIN blk  ON blk.d  = spine.d
        LEFT JOIN ref  ON ref.d  = spine.d
        LEFT JOIN dis  ON dis.d  = spine.d
        LEFT JOIN aml  ON aml.d  = spine.d
        ORDER BY day
    """)

    dau_rows = q(f"""
        SELECT COUNT(DISTINCT user_id) AS daily_active_users
        FROM `{PROJECT}.flat_login_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
    """)
    dau_row = dau_rows[0] if dau_rows else {"daily_active_users": 0}

    summary_rows = q(f"""
        SELECT
          COUNT(DISTINCT user_id)          AS total_payers,
          COUNT(*)                          AS total_transactions,
          ROUND(SUM(amount_usd), 2)         AS total_revenue_usd,
          ROUND(AVG(amount_usd), 2)         AS avg_transaction_usd,
          ROUND(MAX(amount_usd), 2)         AS max_transaction_usd,
          COUNTIF(coupon_code IS NOT NULL)  AS transactions_with_coupon,
          ROUND(SUM(IF(coupon_code IS NOT NULL, amount_usd, 0)), 2) AS coupon_revenue_usd
        FROM `{PROJECT}.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
    """)
    summary = summary_rows[0] if summary_rows else {
        "total_payers": 0, "total_transactions": 0, "total_revenue_usd": 0,
        "avg_transaction_usd": 0, "max_transaction_usd": 0,
        "transactions_with_coupon": 0, "coupon_revenue_usd": 0
    }

    by_product = q(f"""
        SELECT
          product_slug,
          COUNT(DISTINCT user_id)   AS payers,
          COUNT(*)                   AS transactions,
          ROUND(SUM(amount_usd), 2)  AS revenue_usd,
          ROUND(AVG(amount_usd), 2)  AS avg_usd
        FROM `{PROJECT}.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
        GROUP BY product_slug
        ORDER BY revenue_usd DESC
    """)

    today_by_user = q(f"""
        SELECT
          user_id,
          COUNT(*)                                        AS txns,
          ROUND(SUM(amount_usd), 2)                       AS revenue_usd,
          ROUND(AVG(amount_usd), 2)                       AS avg_txn_usd,
          COUNTIF(coupon_code IS NOT NULL)                AS coupon_txns,
          FORMAT_TIMESTAMP('%H:%M', MIN(event_timestamp)) AS first_purchase_utc,
          FORMAT_TIMESTAMP('%H:%M', MAX(event_timestamp)) AS last_purchase_utc,
          TIMESTAMP_DIFF(MAX(event_timestamp), MIN(event_timestamp), MINUTE) AS span_minutes
        FROM `{PROJECT}.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
        GROUP BY user_id
        ORDER BY revenue_usd DESC
    """)

    today_items = q(f"""
        SELECT user_id, product_slug, COUNT(*) AS txns, ROUND(SUM(amount_usd), 2) AS revenue_usd
        FROM `{PROJECT}.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
        GROUP BY user_id, product_slug
        ORDER BY revenue_usd DESC
    """)

    cohort = q(f"""
        WITH today_payers AS (
          SELECT DISTINCT user_id
          FROM `{PROJECT}.flat_purchase_events`
          WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
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
          FROM `{PROJECT}.flat_purchase_events` p
          INNER JOIN today_payers t ON p.user_id = t.user_id
          WHERE DATE(p.event_timestamp) <= 'DATE_FILTER'
          GROUP BY p.user_id
        )
        SELECT
          user_id,
          CASE WHEN prior_purchases = 0 THEN 'new' ELSE 'return' END AS payer_type,
          prior_purchases, prior_revenue_usd, lifetime_purchases, lifetime_value_usd, today_revenue_usd,
          DATE(last_prior_purchase_ts)                                     AS last_prior_purchase_date,
          DATE_DIFF(DATE 'DATE_FILTER', DATE(last_prior_purchase_ts), DAY) AS days_since_last_purchase
        FROM all_history
        ORDER BY today_revenue_usd DESC
    """)

    # Who hit the AML wall, what they meant to spend, and whether they have recovered since.
    blocked_detail = q(f"""
        WITH blocked AS (
          SELECT
            user_id,
            COUNT(*)                                          AS attempts,
            ROUND(SUM(amount_usd), 2)                         AS intent_usd,
            STRING_AGG(DISTINCT aml_status)                   AS aml_status,
            FORMAT_TIMESTAMP('%H:%M', MAX(event_timestamp))   AS last_attempt_utc
          FROM `{PROJECT}.flat_purchase_blocked_events`
          WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
          GROUP BY user_id
        ),
        cleared AS (
          SELECT DISTINCT user_id FROM `{PROJECT}.flat_aml_events`
          WHERE DATE(event_timestamp) >= 'DATE_FILTER' AND aml_status = 'APPROVED'
        ),
        paid AS (
          SELECT DISTINCT user_id FROM `{PROJECT}.flat_purchase_events`
          WHERE DATE(event_timestamp) >= 'DATE_FILTER'
        )
        SELECT
          b.*,
          b.user_id IN (SELECT user_id FROM cleared) AS cleared_aml,
          b.user_id IN (SELECT user_id FROM paid)    AS purchased_since
        FROM blocked b
        ORDER BY intent_usd DESC
    """)

    blocked_reasons = q(f"""
        SELECT event_name, aml_status, COUNT(DISTINCT user_id) AS users,
               COUNT(*) AS events, ROUND(SUM(amount_usd), 2) AS intent_usd
        FROM `{PROJECT}.flat_purchase_blocked_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
        GROUP BY event_name, aml_status
        ORDER BY intent_usd DESC
    """)

    box_opens = q(f"""
        WITH payers AS (
          SELECT DISTINCT user_id FROM `{PROJECT}.flat_purchase_events`
          WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
        )
        SELECT b.box_display_name, b.box_volatility,
               SUM(b.boxes_opened_count) AS total_opens,
               COUNT(DISTINCT b.user_id) AS unique_openers
        FROM `{PROJECT}.flat_box_events` b
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
today_trend = trend[-1] if trend else {}

def t(key, default=0):
    return float(today_trend.get(key) or default)

def avg7(key):
    return (sum(float(r.get(key) or 0) for r in trend) / len(trend)) if trend else 0

def vs_avg(value, key, money=False):
    """'(7d avg $X, +12%)' — the trailing-average comparison Quentin asked for."""
    a = avg7(key)
    if not a:
        return f"(7d avg {usd(0) if money else 0})"
    delta = round((value - a) / a * 100)
    sign = "+" if delta >= 0 else ""
    return f"(7d avg {usd0(a) if money else round(a)}, {sign}{delta}%)"

dau           = dau_row["daily_active_users"]
total_payers  = summary["total_payers"]
total_txns    = summary["total_transactions"]
gross_rev     = float(summary["total_revenue_usd"] or 0)
avg_spend     = float(summary["avg_transaction_usd"] or 0)
max_txn       = float(summary["max_transaction_usd"] or 0)
coupon_count  = summary["transactions_with_coupon"]
coupon_rev    = float(summary["coupon_revenue_usd"] or 0)

refund_usd    = t("refund_usd")
refund_count  = int(t("refunds"))
dispute_count = int(t("disputes"))
net_rev       = gross_rev - refund_usd

registrations = int(t("registrations"))
shop_users    = int(t("shop_users"))
intent_users  = int(t("intent_users"))
intent_usd    = t("intent_usd")
blocked_users = int(t("blocked_users"))
blocked_evts  = int(t("blocked_events"))
blocked_usd   = t("blocked_usd")
aml_submitted = int(t("aml_submitted"))
aml_approved  = int(t("aml_approved"))
aml_rejected  = int(t("aml_rejected"))

intent_capture_pct = pct(gross_rev, intent_usd)
arppu         = gross_rev / total_payers if total_payers else 0
txn_per_payer = total_txns / total_payers if total_payers else 0
conversion    = round(total_payers / dau * 100, 1) if dau else 0

blocked_7d = sum(float(r.get("blocked_usd") or 0) for r in trend)
gross_7d   = sum(float(r.get("gross_usd") or 0) for r in trend)

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
    return {"users": users, "rev": rev, "txns": txns,
            "arppu": rev / users if users else 0,
            "avg_txn": rev / txns if txns else 0,
            "user_pct": pct(users, total_payers), "rev_pct": pct(rev, gross_rev)}

new_stats = cohort_stats(new_payers)
ret_stats = cohort_stats(return_payers)

repeat_buyers = [r for r in today_by_user if int(r["txns"] or 0) >= 2]
repeat_rev    = sum(float(r["revenue_usd"] or 0) for r in repeat_buyers)

def items_line(user_id):
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
    window = f"{span // 60}h {span % 60}m" if span >= 60 else f"{span}m"
    return f"{first}–{last} UTC ({window})"

# ── Charts: 7-day trends only ─────────────────────────────────────────────────
SURFACE, GRID, INK, INK2, MUTED = "#fcfcfb", "#e1e0d9", "#0b0b0b", "#52514e", "#898781"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"   # validated categorical slots 1-3

def fig_to_bytes(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=SURFACE)
    buf.seek(0)
    plt.close(fig)
    return buf

def style_axes(ax, title=None):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=1)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, fontsize=11, fontweight="bold", color=INK, pad=8, loc="left")

def upload_chart(buf, filename, thread_ts, title):
    slack.files_upload_v2(channel=CHANNEL_ID, file=buf, filename=filename,
                          title=title, thread_ts=thread_ts)

charts = {}
try:
    if trend:
        day_labels = [datetime.date.fromisoformat(str(r["day"])).strftime("%a %d") for r in trend]
        xs = range(len(trend))

        # Chart 1 — the money chart: captured vs blocked revenue, same unit, one axis.
        captured = [float(r["gross_usd"] or 0) for r in trend]
        blocked  = [float(r["blocked_usd"] or 0) for r in trend]
        fig, ax = plt.subplots(figsize=(10, 4.6))
        fig.patch.set_facecolor(SURFACE)
        w, gap = 0.38, 0.02
        cap_avg, blk_avg = sum(captured) / len(captured), sum(blocked) / len(blocked)
        ax.bar([x - w / 2 - gap for x in xs], captured, width=w, color=S1,
               label=f"Captured revenue  (7d avg {usd0(cap_avg)})")
        ax.bar([x + w / 2 + gap for x in xs], blocked, width=w, color=S2,
               label=f"Blocked at AML wall  (7d avg {usd0(blk_avg)})")
        # Dashed reference lines carry no inline label — the legend states both averages.
        ax.axhline(cap_avg, color=S1, linestyle="--", linewidth=1.5, alpha=0.9)
        ax.axhline(blk_avg, color=S2, linestyle="--", linewidth=1.5, alpha=0.9)
        # Direct-label the most recent day only, not every bar.
        ax.text(len(trend) - 1 - w / 2 - gap, captured[-1], usd0(captured[-1]), ha="center",
                va="bottom", fontsize=9, color=INK, fontweight="bold")
        ax.text(len(trend) - 1 + w / 2 + gap, blocked[-1], usd0(blocked[-1]), ha="center",
                va="bottom", fontsize=9, color=INK, fontweight="bold")
        ax.set_ylim(0, (max(max(captured), max(blocked)) or 1) * 1.22)
        ax.set_xticks(list(xs)); ax.set_xticklabels(day_labels)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
        style_axes(ax, f"Revenue captured vs blocked at the AML wall — {TREND_DAYS} days to {DATE}")
        leg = ax.legend(frameon=False, loc="upper left", fontsize=9, ncol=2)
        for txt in leg.get_texts():
            txt.set_color(INK2)
        plt.tight_layout()
        charts["revenue_trend"] = fig_to_bytes(fig)

        # Chart 2 — small multiples: every panel its own scale, each with its 7d mean.
        panels = [
            ("Registrations",      [float(r["registrations"] or 0) for r in trend], "{:,.0f}"),
            ("Shop opened (users)",[float(r["shop_users"] or 0) for r in trend],    "{:,.0f}"),
            ("Purchase intent (users)", [float(r["intent_users"] or 0) for r in trend], "{:,.0f}"),
            ("Paying users",       [float(r["paid_users"] or 0) for r in trend],    "{:,.0f}"),
            ("Blocked users",      [float(r["blocked_users"] or 0) for r in trend], "{:,.0f}"),
            ("Intent captured %",  [pct(float(r["gross_usd"] or 0), float(r["intent_usd"] or 0)) for r in trend], "{:,.0f}%"),
            ("Net revenue",        [float(r["net_usd"] or 0) for r in trend],       "${:,.0f}"),
            ("AML approved",       [float(r["aml_approved"] or 0) for r in trend],  "{:,.0f}"),
        ]
        fig, axes = plt.subplots(2, 4, figsize=(15, 6.4))
        fig.patch.set_facecolor(SURFACE)
        for ax, (label, series, fmt) in zip(axes.flat, panels):
            mean = sum(series) / len(series) if series else 0
            ax.bar(list(xs), series, width=0.62, color=S1)
            ax.axhline(mean, color=MUTED, linestyle="--", linewidth=1.4)
            ax.text(len(trend) - 1, series[-1], fmt.format(series[-1]), ha="center", va="bottom",
                    fontsize=8.5, color=INK, fontweight="bold")
            ax.set_xticks(list(xs))
            ax.set_xticklabels([d.split()[1] for d in day_labels], fontsize=8)
            ax.set_ylim(0, max(max(series), mean) * 1.28 if max(series) else 1)
            style_axes(ax, f"{label}  ·  7d avg {fmt.format(mean)}")
        fig.suptitle(f"Daily funnel vs {TREND_DAYS}-day average (dashed line) — to {DATE}",
                     fontsize=13, fontweight="bold", color=INK, x=0.005, ha="left")
        plt.tight_layout(rect=[0, 0, 1, 0.96])
        charts["funnel_trend"] = fig_to_bytes(fig)

except Exception as e:
    send_error(f"Chart generation failed:\n```{e}```")
    raise

# ── Main message: macro only ──────────────────────────────────────────────────
# Per-user breakdowns go to the thread so the channel message stays a constant
# size regardless of how many payers, blocked users or new signups there were.
lines = [
    f"\U0001f4b0 *Goatbox Daily Report — {DATE}*",
    "",
    f"*{usd(net_rev)}* net revenue {vs_avg(net_rev, 'net_usd', money=True)}  ·  "
    f"*{total_txns}* transactions  ·  *{total_payers}* payers {vs_avg(total_payers, 'paid_users')}",
    f"*{usd(arppu)}* ARPPU  ·  *{usd(avg_spend)}* avg/txn  ·  *{txn_per_payer:.1f}* txns/payer  ·  "
    f"*{usd(max_txn)}* largest txn",
    f"*{dau:,}* DAU  ·  *{conversion}%* payer conversion  ·  "
    f"*{coupon_count}* coupon txns ({pct(coupon_count, total_txns)}%, {usd(coupon_rev)})",
]
if refund_usd or dispute_count:
    lines.append(
        f"_Gross {usd(gross_rev)} less {plural(refund_count, 'refund')} {usd(refund_usd)}"
        + (f" · {plural(dispute_count, 'dispute')} opened" if dispute_count else "") + "_"
    )

# Funnel
lines += ["", f"*Purchase Funnel* — yesterday vs {TREND_DAYS}-day average", ""]
lines += [
    f"• *Registrations* · {registrations:,} {vs_avg(registrations, 'registrations')}",
    f"• *Opened shop* · {shop_users:,} users {vs_avg(shop_users, 'shop_users')}",
    f"• *Started a purchase* · {intent_users:,} users · {usd(intent_usd)} of intent {vs_avg(intent_usd, 'intent_usd', money=True)}",
    f"• *Completed* · {total_payers:,} users · {usd(gross_rev)} — *{intent_capture_pct}%* of intent captured",
    f"• *Blocked* · {blocked_users:,} users · {usd(blocked_usd)} {vs_avg(blocked_usd, 'blocked_usd', money=True)}",
]

# AML wall — headline numbers only; the per-user recovery list lives in the thread.
if blocked_detail:
    recovered = [b for b in blocked_detail if b.get("purchased_since")]
    cleared   = [b for b in blocked_detail if b.get("cleared_aml") and not b.get("purchased_since")]
    ratio     = (blocked_7d / gross_7d) if gross_7d else 0
    top       = blocked_reasons[0] if blocked_reasons else None
    lines += ["", "*Blocked at the AML Wall*", ""]
    lines.append(
        f"• *{usd(blocked_usd)}* blocked across *{plural(blocked_users, 'user')}* "
        f"({blocked_evts} attempts) — {TREND_DAYS}d: {usd(blocked_7d)} vs {usd(gross_7d)} captured "
        f"(*{ratio:.1f}×*)"
    )
    if top:
        others = len(blocked_reasons) - 1
        lines.append(
            f"• Reason · `{top['event_name']}` / `{top['aml_status']}` "
            f"({pct(int(top['users']), blocked_users)}% of blocked users)"
            + (f" · +{plural(others, 'other reason')}" if others > 0 else "")
        )
    lines.append(
        f"• Since blocked · *{len(recovered)}* purchased · *{len(cleared)}* cleared AML but no purchase · "
        f"*{len(blocked_detail) - len(recovered) - len(cleared)}* still blocked"
    )

# KYC — one line
lines += ["", "*KYC / AML*", ""]
lines.append(
    f"• {aml_submitted} submitted · {aml_approved} approved"
    + (f" (*{pct(aml_approved, aml_submitted)}%* pass)" if aml_submitted else "")
    + f" · {aml_rejected} rejected  _({TREND_DAYS}d avg {round(avg7('aml_submitted'))} / "
      f"{round(avg7('aml_approved'))} / {round(avg7('aml_rejected'))})_"
)

# New vs returning
lines += ["", "*Revenue Split — New vs Returning*", ""]
if total_payers:
    for label, s in (("Returning", ret_stats), ("New", new_stats)):
        lines.append(
            f"• *{label}* · {usd(s['rev'])} (*{s['rev_pct']}%*) · {plural(s['users'], 'payer')} "
            f"({s['user_pct']}%) · {plural(s['txns'], 'txn')} · {usd(s['arppu'])} ARPPU"
        )
    if repeat_buyers:
        lines.append(
            f"• *Repeat buyers* · {plural(len(repeat_buyers), 'payer')} bought 2+ times · "
            f"{usd(repeat_rev)} (*{pct(repeat_rev, gross_rev)}%* of revenue)"
        )
else:
    lines.append("• No purchases recorded today")

# Product mix — top 5, remainder rolled up
TOP_PRODUCTS = 5
lines += ["", "*Revenue by Store Product*", ""]
if by_product:
    for r in by_product[:TOP_PRODUCTS]:
        rev = float(r["revenue_usd"] or 0)
        lines.append(
            f"• *{clean_slug(r['product_slug'])}* · {usd(rev)} (*{pct(rev, gross_rev)}%*) · "
            f"{plural(int(r['transactions']), 'txn')} · {plural(int(r['payers']), 'payer')}"
        )
    rest = by_product[TOP_PRODUCTS:]
    if rest:
        rest_rev = sum(float(x["revenue_usd"] or 0) for x in rest)
        lines.append(f"• _+{plural(len(rest), 'other product')} · {usd(rest_rev)} "
                     f"({pct(rest_rev, gross_rev)}%)_")
else:
    lines.append("• No purchases recorded today")

# Boxes — top 5
lines += ["", "*Top Box Opens*", ""]
if box_opens:
    for r in box_opens[:5]:
        lines.append(
            f"• *{r['box_display_name']}* · {int(r['total_opens']):,} opens · "
            f"{plural(int(r['unique_openers']), 'user')} · vol {r['box_volatility']}"
        )
else:
    lines.append("• No box opens by paying users today")

lines += ["", "\U0001f9f5 _In thread: returning-payer detail · new payers & top spenders · "
              "AML recovery list · 7-day trend charts_"]

message = "\n".join(lines)

# ── Thread detail ─────────────────────────────────────────────────────────────
def returning_detail():
    out = [f"*Returning Payers — Same-Day Purchases* ({len(return_payers)} total)", ""]
    if not return_payers:
        return out + ["• No returning payers today"]
    ranked = sorted(return_payers, key=lambda r: -float(r.get("today_revenue_usd") or 0))
    for idx, r in enumerate(ranked[:MAX_RETURNING_DETAIL], start=1):
        uid   = r["user_id"]
        a     = activity.get(uid, {})
        txns  = int(a.get("txns") or 0)
        today = float(r.get("today_revenue_usd") or 0)
        days  = r.get("days_since_last_purchase")
        gap = "no prior purchase date on record" if days is None else \
              f"back after {plural(days, 'day')} (last: {r.get('last_prior_purchase_date')})"
        timing = timing_line(uid)
        out.append(f"*{idx}. `{uid}`* — *{usd(today)}* today · {plural(txns, 'txn')} · "
                   f"{pct(today, gross_rev)}% of daily revenue")
        out.append(f"     ↳ bought: {items_line(uid)}")
        if timing:
            out.append(f"     ↳ {timing}")
        out.append(
            f"     ↳ {gap} · {usd(r.get('prior_revenue_usd'))} prior spend over "
            f"{plural(int(r.get('prior_purchases') or 0), 'purchase')} · {usd(r.get('lifetime_value_usd'))} LTV"
        )
        out.append("")
    if out and out[-1] == "":
        out.pop()
    hidden = len(ranked) - MAX_RETURNING_DETAIL
    if hidden > 0:
        hidden_rev = sum(float(x.get("today_revenue_usd") or 0) for x in ranked[MAX_RETURNING_DETAIL:])
        out.append(f"_+{plural(hidden, 'more returning payer')} · {usd(hidden_rev)} combined_")
    return out

def payers_detail():
    out = [f"*New Payers — First Purchase* ({len(new_payers)} total)", ""]
    if new_payers:
        ranked_new = sorted(new_payers, key=lambda r: -float(r.get("today_revenue_usd") or 0))
        for r in ranked_new[:10]:
            uid = r["user_id"]
            a   = activity.get(uid, {})
            out.append(f"• *`{uid}`* · {usd(r.get('today_revenue_usd'))} · "
                       f"{plural(int(a.get('txns') or 0), 'txn')} · {items_line(uid)}")
        hidden_new = len(ranked_new) - 10
        if hidden_new > 0:
            hidden_rev = sum(float(x.get("today_revenue_usd") or 0) for x in ranked_new[10:])
            out.append(f"_+{plural(hidden_new, 'more new payer')} · {usd(hidden_rev)} combined_")
    else:
        out.append("• No new payers today")

    out += ["", "*Top Spenders*", ""]
    if today_by_user:
        payer_type = {r["user_id"]: r["payer_type"] for r in cohort}
        for r in today_by_user[:10]:
            uid = r["user_id"]
            rev = float(r["revenue_usd"] or 0)
            tag = "new" if payer_type.get(uid) == "new" else "returning"
            out.append(f"• *`{uid}`* · {usd(rev)} (*{pct(rev, gross_rev)}%*) · "
                       f"{plural(int(r['txns'] or 0), 'txn')} · {usd(r['avg_txn_usd'])} avg/txn · _{tag}_")
    else:
        out.append("• No data")
    return out

def recovery_detail():
    if not blocked_detail:
        return []
    recovered = [b for b in blocked_detail if b.get("purchased_since")]
    out = [f"*AML Wall — Recovery List* ({len(recovered)}/{len(blocked_detail)} have purchased since)", ""]
    for b in blocked_detail[:MAX_BLOCKED_DETAIL]:
        if b.get("purchased_since"):
            state = "✅ purchased since"
        elif b.get("cleared_aml"):
            state = "⚠️ cleared AML, no purchase"
        else:
            state = "❌ still blocked"
        out.append(f"• *`{b['user_id']}`* · {usd(b['intent_usd'])} intended · "
                   f"{plural(int(b['attempts']), 'attempt')} · last {b['last_attempt_utc']} UTC · {state}")
    hidden = len(blocked_detail) - MAX_BLOCKED_DETAIL
    if hidden > 0:
        hidden_usd = sum(float(x["intent_usd"] or 0) for x in blocked_detail[MAX_BLOCKED_DETAIL:])
        out.append(f"_+{plural(hidden, 'more blocked user')} · {usd(hidden_usd)} combined_")
    return out

# ── Post to Slack ─────────────────────────────────────────────────────────────
CHUNK_CHARS = 3500

def post_thread(section_lines, thread_ts):
    """Post a detail section into the thread, split on line boundaries if long."""
    if not section_lines:
        return
    chunk = []
    size = 0
    for line in section_lines:
        if size + len(line) + 1 > CHUNK_CHARS and chunk:
            slack.chat_postMessage(channel=CHANNEL_ID, thread_ts=thread_ts, text="\n".join(chunk))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        slack.chat_postMessage(channel=CHANNEL_ID, thread_ts=thread_ts, text="\n".join(chunk))

try:
    resp = slack.chat_postMessage(channel=CHANNEL_ID, text=message)
    thread_ts = resp["ts"]

    post_thread(returning_detail(), thread_ts)
    post_thread(payers_detail(), thread_ts)
    post_thread(recovery_detail(), thread_ts)

    if "revenue_trend" in charts:
        upload_chart(charts["revenue_trend"], f"trend_revenue_{DATE}.png", thread_ts,
                     "Captured vs blocked revenue, 7 days")
    if "funnel_trend" in charts:
        upload_chart(charts["funnel_trend"], f"trend_funnel_{DATE}.png", thread_ts,
                     "Daily funnel vs 7-day average")

    print(f"Report posted: https://secrethumans.slack.com/archives/{CHANNEL_ID}/p{thread_ts.replace('.','')}",
          flush=True)
except Exception as e:
    send_error(f"Slack post failed:\n```{e}```")
    raise
