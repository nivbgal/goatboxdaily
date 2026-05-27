import os
import io
import sys
import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from google.cloud import bigquery
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

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
    "'01KR9AA6ZZ2J0BWNFRJR5AA494','01KPR58SZEJX7Y0AZJBRB8P737'"
)

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

def fig_to_bytes(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf

BG, GRID, TEXT, ACCENT, MUTED = "#FFFFFF", "#F0F0F0", "#1D1C1D", "#4A154B", "#8a8a8a"

def base_style(ax, title):
    ax.set_facecolor(BG); ax.figure.patch.set_facecolor(BG)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(GRID); ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT, labelsize=10)
    ax.set_title(title, fontsize=13, fontweight="bold", color=TEXT, pad=12, loc="left")
    ax.yaxis.grid(False); ax.xaxis.grid(True, color=GRID, linewidth=1); ax.set_axisbelow(True)

def upload_chart(buf, filename, thread_ts):
    slack.files_upload_v2(
        channel=CHANNEL_ID,
        file=buf,
        filename=filename,
        thread_ts=thread_ts,
    )

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
          COUNTIF(coupon_code IS NOT NULL)       AS transactions_with_coupon
        FROM `goatbox-prod.processing_data.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER'
          AND user_id NOT IN ({EXCLUDED})
    """)
    summary = summary_rows[0] if summary_rows else {
        "total_payers": 0, "total_transactions": 0,
        "total_revenue_usd": 0, "avg_transaction_usd": 0,
        "transactions_with_coupon": 0
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
          SUM(b.boxes_opened_count)                                          AS total_opens,
          COUNT(DISTINCT b.user_id)                                          AS unique_openers,
          ROUND(AVG(b.total_coins_spent / NULLIF(b.boxes_opened_count,0)), 2) AS box_price_coins
        FROM `goatbox-prod.processing_data.flat_box_events` b
        INNER JOIN payers p ON b.user_id = p.user_id
        WHERE DATE(b.event_timestamp) = 'DATE_FILTER'
        GROUP BY b.box_display_name, b.box_volatility
        ORDER BY total_opens DESC
        LIMIT 10
    """)

    top_openers = q(f"""
        WITH payers AS (
          SELECT DISTINCT user_id
          FROM `goatbox-prod.processing_data.flat_purchase_events`
          WHERE DATE(event_timestamp) = 'DATE_FILTER'
            AND user_id NOT IN ({EXCLUDED})
        )
        SELECT
          b.user_id,
          SUM(b.boxes_opened_count)          AS total_boxes_opened,
          COUNT(DISTINCT b.box_display_name) AS distinct_boxes,
          ROUND(SUM(b.total_coins_spent), 0) AS total_coins_spent
        FROM `goatbox-prod.processing_data.flat_box_events` b
        INNER JOIN payers p ON b.user_id = p.user_id
        WHERE DATE(b.event_timestamp) = 'DATE_FILTER'
        GROUP BY b.user_id
        ORDER BY total_boxes_opened DESC
        LIMIT 5
    """)

    top_spenders = q(f"""
        SELECT
          user_id,
          COUNT(*)                                                       AS transactions,
          ROUND(SUM(amount_usd), 2)                                      AS total_spend_usd,
          STRING_AGG(product_slug, ', ' ORDER BY event_timestamp)        AS products_bought
        FROM `goatbox-prod.processing_data.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER'
          AND user_id NOT IN ({EXCLUDED})
        GROUP BY user_id
        ORDER BY total_spend_usd DESC
        LIMIT 5
    """)

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
            COUNTIF(DATE(p.event_timestamp) < 'DATE_FILTER')                          AS prior_purchases,
            COUNT(*)                                                                    AS lifetime_purchases,
            ROUND(SUM(p.amount_usd), 2)                                                AS lifetime_value_usd,
            ROUND(SUM(IF(DATE(p.event_timestamp) = 'DATE_FILTER', p.amount_usd, 0)), 2) AS today_revenue_usd
          FROM `goatbox-prod.processing_data.flat_purchase_events` p
          INNER JOIN today_payers t ON p.user_id = t.user_id
          WHERE DATE(p.event_timestamp) <= 'DATE_FILTER'
          GROUP BY p.user_id
        )
        SELECT
          user_id,
          CASE WHEN prior_purchases = 0 THEN 'new' ELSE 'return' END AS payer_type,
          lifetime_purchases,
          lifetime_value_usd,
          DATE(last_prior_purchase_ts)                                AS last_prior_purchase_date,
          DATE_DIFF(DATE 'DATE_FILTER', DATE(last_prior_purchase_ts), DAY) AS days_since_last_purchase
        FROM all_history
        ORDER BY payer_type, lifetime_value_usd DESC
    """)

    top_box_name = box_opens[0]["box_display_name"] if box_opens else None

    top_box_users = []
    if top_box_name:
        top_box_users = q(f"""
            WITH payers AS (
              SELECT DISTINCT user_id
              FROM `goatbox-prod.processing_data.flat_purchase_events`
              WHERE DATE(event_timestamp) = 'DATE_FILTER'
                AND user_id NOT IN ({EXCLUDED})
            )
            SELECT
              b.user_id,
              SUM(b.boxes_opened_count)          AS opens,
              ROUND(SUM(b.total_coins_spent), 2) AS coins_spent
            FROM `goatbox-prod.processing_data.flat_box_events` b
            INNER JOIN payers p ON b.user_id = p.user_id
            WHERE DATE(b.event_timestamp) = 'DATE_FILTER'
              AND b.box_display_name = @box_name
            GROUP BY b.user_id
            ORDER BY opens DESC
        """, params=[bigquery.ScalarQueryParameter("box_name", "STRING", top_box_name)])

except Exception as e:
    send_error(f"BigQuery query failed:\n```{e}```")
    raise

# ── Derived values ────────────────────────────────────────────────────────────
dau            = dau_row["daily_active_users"]
total_payers   = summary["total_payers"]
total_txns     = summary["total_transactions"]
total_rev      = float(summary["total_revenue_usd"] or 0)
avg_spend      = float(summary["avg_transaction_usd"] or 0)
coupon_count   = summary["transactions_with_coupon"]

new_payers     = [r for r in cohort if r["payer_type"] == "new"]
return_payers  = [r for r in cohort if r["payer_type"] == "return"]

def clean_slug(s):
    return s.replace("-shop-item", "").replace("-", " ").title()

# ── Charts ────────────────────────────────────────────────────────────────────
try:
    charts = {}

    # Chart 1: Revenue by product
    if by_product:
        labels_rev = [clean_slug(r["product_slug"]) for r in by_product][::-1]
        values_rev = [float(r["revenue_usd"]) for r in by_product][::-1]
        max_rev = max(values_rev) if values_rev else 1
        fig, ax = plt.subplots(figsize=(7, max(3, len(labels_rev) * 0.55 + 1)))
        bars = ax.barh(labels_rev, values_rev, color=ACCENT, height=0.6)
        for bar, val in zip(bars, values_rev):
            ax.text(bar.get_width() + max_rev * 0.01, bar.get_y() + bar.get_height() / 2,
                    f"${val:,.2f}", va="center", ha="left", fontsize=10, color=TEXT)
        ax.set_xlabel("Revenue (USD)", color=MUTED, fontsize=10)
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        base_style(ax, f"Revenue by Store Product  -  {DATE}")
        plt.tight_layout()
        charts["revenue"] = fig_to_bytes(fig)

    # Chart 2: Box opens
    if box_opens:
        labels_box  = [b["box_display_name"] for b in box_opens][::-1]
        opens       = [b["total_opens"] for b in box_opens][::-1]
        unique      = [b["unique_openers"] for b in box_opens][::-1]
        volatility  = [b["box_volatility"] for b in box_opens][::-1]
        max_opens = max(opens) if opens else 1
        norm = plt.Normalize(0, 100); cmap = plt.cm.RdYlBu_r
        colors_box = [cmap(norm(v)) for v in volatility]
        fig, ax = plt.subplots(figsize=(8, max(4, len(labels_box) * 0.6 + 1.2)))
        bars = ax.barh(labels_box, opens, color=colors_box, height=0.6)
        for bar, o, u in zip(bars, opens, unique):
            ax.text(bar.get_width() + max_opens * 0.01, bar.get_y() + bar.get_height() / 2,
                    f"{o} opens, {u} user{'s' if u != 1 else ''}", va="center", ha="left", fontsize=9, color=TEXT)
        ax.set_xlabel("Box Opens", color=MUTED, fontsize=10)
        base_style(ax, f"Top Box Opens by Paying Users  -  {DATE}")
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm); sm.set_array([])
        cbar = plt.colorbar(sm, ax=ax, orientation="vertical", fraction=0.02, pad=0.12)
        cbar.set_label("Volatility", color=MUTED, fontsize=9); cbar.ax.tick_params(labelsize=8, colors=MUTED)
        plt.tight_layout()
        charts["boxes"] = fig_to_bytes(fig)

    # Chart 3: Top users
    if top_openers or top_spenders:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, max(3, max(len(top_openers), len(top_spenders), 1) * 0.65 + 1.5)))
        if top_openers:
            lo = [r["user_id"] for r in top_openers][::-1]
            vo = [int(r["total_boxes_opened"]) for r in top_openers][::-1]
            max_vo = max(vo) if vo else 1
            ax1.barh(lo, vo, color="#36C5F0", height=0.6)
            for i, v in enumerate(vo):
                ax1.text(v + max_vo * 0.01, i, str(v), va="center", fontsize=10, color=TEXT)
            ax1.set_xlabel("Boxes Opened", color=MUTED, fontsize=10)
            ax1.tick_params(axis="y", labelsize=7)
            base_style(ax1, "Top Openers")
        else:
            ax1.axis("off")
            ax1.text(0.5, 0.5, "No data", ha="center", va="center", color=MUTED, transform=ax1.transAxes)
        if top_spenders:
            ls = [r["user_id"] for r in top_spenders][::-1]
            vs = [float(r["total_spend_usd"]) for r in top_spenders][::-1]
            max_vs = max(vs) if vs else 1
            ax2.barh(ls, vs, color="#2EB67D", height=0.6)
            for i, v in enumerate(vs):
                ax2.text(v + max_vs * 0.01, i, f"${v:,.2f}", va="center", fontsize=10, color=TEXT)
            ax2.set_xlabel("Total Spend (USD)", color=MUTED, fontsize=10)
            ax2.tick_params(axis="y", labelsize=7)
            base_style(ax2, "Top Spenders")
        else:
            ax2.axis("off")
            ax2.text(0.5, 0.5, "No data", ha="center", va="center", color=MUTED, transform=ax2.transAxes)
        fig.suptitle(f"Top Users  -  {DATE}", fontsize=13, fontweight="bold", color=TEXT, x=0.02, ha="left")
        plt.tight_layout()
        charts["users"] = fig_to_bytes(fig)

    # Chart 4: Top box table
    if top_box_name and top_box_users:
        fig, ax = plt.subplots(figsize=(9, max(2.5, len(top_box_users) * 0.5 + 1.8)))
        ax.axis("off")
        col_labels = ["User ID", "Opens", "Coins Spent"]
        table_data = [[r["user_id"], r["opens"], f"{float(r['coins_spent']):,.0f}"] for r in top_box_users]
        if table_data:
            tbl = ax.table(cellText=table_data, colLabels=col_labels, cellLoc="left", loc="center", bbox=[0, 0, 1, 1])
            tbl.auto_set_font_size(False); tbl.set_fontsize(10)
            for j in range(len(col_labels)):
                tbl[0, j].set_facecolor(ACCENT)
                tbl[0, j].set_text_props(color="white", fontweight="bold")
                tbl[0, j].set_edgecolor(BG)
            for i in range(1, len(table_data) + 1):
                row_bg = "#F8F8F8" if i % 2 == 0 else BG
                for j in range(len(col_labels)):
                    tbl[i, j].set_facecolor(row_bg)
                    tbl[i, j].set_edgecolor("#E8E8E8")
                    tbl[i, j].set_text_props(color=TEXT)
            tbl.auto_set_column_width([0, 1, 2])
        fig.patch.set_facecolor(BG)
        ax.set_title(f"Who Opened '{top_box_name}'  -  {DATE}", fontsize=13, fontweight="bold",
                     color=TEXT, pad=14, loc="left", x=0.01)
        plt.tight_layout()
        charts["topbox"] = fig_to_bytes(fig)

except Exception as e:
    send_error(f"Chart generation failed:\n```{e}```")
    raise

# ── Build Slack message ───────────────────────────────────────────────────────
lines = [
    f"\U0001f4e6 *Goatbox Daily Report — {DATE}*",
    "",
    f"*{total_payers}* payers  ·  *{total_txns}* transactions  ·  *${total_rev:,.2f}* revenue  ·  "
    f"*${avg_spend:,.2f}* avg spend  ·  {coupon_count} coupon transactions  ·  *{dau}* daily active users",
]

# Payer cohort
lines += ["", f"*Payer Cohort* — *{len(new_payers)}* new  ·  *{len(return_payers)}* returning"]
if return_payers:
    lines.append("")
    for r in return_payers:
        days = r["days_since_last_purchase"]
        days_str = (f"{days} day" + ("s" if days != 1 else "") + " ago") if days is not None else "N/A"
        lines.append(
            f"• *{r['user_id']}* · ${float(r['lifetime_value_usd']):,.2f} LTV"
            f" · {r['lifetime_purchases']} purchases · last seen {days_str}"
        )

# Revenue by product
lines += ["", "*Revenue by Store Product*", ""]
if by_product:
    for r in by_product:
        lines.append(
            f"• *{clean_slug(r['product_slug'])}* · ${float(r['revenue_usd']):,.2f} · {r['transactions']} txns"
        )
else:
    lines.append("• No purchases recorded today")

# Top box opens
lines += ["", "*Top Box Opens*", ""]
if box_opens:
    for r in box_opens:
        lines.append(
            f"• *{r['box_display_name']}* · {r['total_opens']} opens"
            f" · {r['unique_openers']} users · vol {r['box_volatility']}"
        )
else:
    lines.append("• No box opens by paying users today")

# Who opened top box
if top_box_name and top_box_users:
    lines += ["", f"*Who Opened '{top_box_name}'*", ""]
    for r in top_box_users:
        lines.append(
            f"• *{r['user_id']}* · {r['opens']:,} opens · {float(r['coins_spent']):,.0f} coins"
        )

# Top openers
lines += ["", "*Top Openers*", ""]
if top_openers:
    for r in top_openers:
        lines.append(
            f"• *{r['user_id']}* · {r['total_boxes_opened']:,} opens · {r['distinct_boxes']} boxes"
        )
else:
    lines.append("• No data")

# Top spenders
lines += ["", "*Top Spenders*", ""]
if top_spenders:
    for r in top_spenders:
        lines.append(
            f"• *{r['user_id']}* · ${float(r['total_spend_usd']):,.2f} · {r['transactions']} txns"
        )
else:
    lines.append("• No data")

# ── Observations (rule-based, multi-signal) ───────────────────────────────────
new_count  = len(new_payers)
ret_count  = len(return_payers)
pct_new    = round(new_count / total_payers * 100) if total_payers else 0
new_rev    = sum(float(r["today_revenue_usd"]) for r in new_payers)
ret_rev    = sum(float(r["today_revenue_usd"]) for r in return_payers)
new_avg    = new_rev / new_count if new_count else 0
ret_avg    = ret_rev / ret_count if ret_count else 0

candidates = []  # (priority, label, detail)

if total_payers == 0:
    candidates.append((10, "No payers today",
        f"Zero purchase transactions were recorded for {DATE}."))
else:
    # 1. Cohort split
    if new_count == total_payers:
        candidates.append((10, "All new payers",
            f"Every payer today was a first-time buyer. {new_count} new users generated ${new_rev:,.2f} at ${new_avg:,.2f} avg."))
    elif ret_count == total_payers:
        candidates.append((10, "All return payers",
            f"No new buyers today. {ret_count} returning users generated ${ret_rev:,.2f} at ${ret_avg:,.2f} avg."))
    elif pct_new >= 70:
        candidates.append((9, "Heavy new payer skew",
            f"{new_count} of {total_payers} payers ({pct_new}%) were new, contributing ${new_rev:,.2f} vs ${ret_rev:,.2f} from {ret_count} returning users."))
    elif pct_new <= 30:
        candidates.append((9, "Return payer dominated",
            f"{ret_count} of {total_payers} payers ({100-pct_new}%) were returning, driving ${ret_rev:,.2f} ({round(ret_rev/total_rev*100) if total_rev else 0}% of revenue)."))
    else:
        candidates.append((6, "Balanced cohort",
            f"{new_count} new payers (${new_avg:,.2f} avg) vs {ret_count} returning (${ret_avg:,.2f} avg) — "
            f"{'returners spent more per head' if ret_avg > new_avg else 'new users spent more per head'}."))

    # 2. Return payer re-engagement gap
    if return_payers:
        gaps = [(r["days_since_last_purchase"], r) for r in return_payers if r.get("days_since_last_purchase")]
        if gaps:
            max_gap, max_gap_user = max(gaps, key=lambda x: x[0])
            if max_gap >= 14:
                candidates.append((9, "Long re-engagement",
                    f"User {max_gap_user['user_id']} returned after {max_gap} days away with ${float(max_gap_user['lifetime_value_usd']):,.2f} LTV across {max_gap_user['lifetime_purchases']} lifetime purchases."))
            elif max_gap >= 5:
                candidates.append((7, "Re-engagement gap",
                    f"Top returning user came back after {max_gap} days (${float(max_gap_user['lifetime_value_usd']):,.2f} LTV)."))

    # 3. Power user spend concentration
    if top_spenders and total_rev > 0:
        top_spend = float(top_spenders[0]["total_spend_usd"])
        top_pct   = round(top_spend / total_rev * 100)
        if top_pct >= 40:
            candidates.append((8, "Spend concentration",
                f"Top spender (user {top_spenders[0]['user_id']}) accounted for ${top_spend:,.2f} ({top_pct}% of total revenue) across {top_spenders[0]['transactions']} transactions."))
        elif top_pct >= 25:
            candidates.append((6, f"Top spender drove {top_pct}% of revenue",
                f"${top_spend:,.2f} from user {top_spenders[0]['user_id']}."))

    # 4. Box opens concentration
    if box_opens and top_box_users:
        total_opens_all = sum(b["total_opens"] for b in box_opens)
        top_box_opens   = box_opens[0]["total_opens"]
        top_box_pct     = round(top_box_opens / total_opens_all * 100) if total_opens_all else 0
        top_user_opens  = top_box_users[0]["opens"]
        top_user_pct    = round(top_user_opens / top_box_opens * 100) if top_box_opens else 0
        if top_user_pct >= 80:
            candidates.append((8, f"{top_box_name} monopolised",
                f"One user opened {top_user_opens:,} of {top_box_opens:,} {top_box_name} boxes ({top_user_pct}%)."))
        elif top_box_pct >= 50:
            candidates.append((7, f"{top_box_name} dominates opens",
                f"{top_box_opens:,} opens ({top_box_pct}% of all {total_opens_all:,}) across {box_opens[0]['unique_openers']} users."))

    # 5. Coupon usage rate
    if total_txns > 0:
        coupon_pct = round(coupon_count / total_txns * 100)
        if coupon_pct >= 40:
            candidates.append((7, "High coupon usage",
                f"{coupon_count} of {total_txns} transactions ({coupon_pct}%) used a coupon code, suggesting active discount-driven purchasing."))
        elif coupon_pct == 0 and total_txns >= 5:
            candidates.append((5, "No coupons today",
                f"All {total_txns} transactions were full price."))

    # 6. Box volatility preference
    if box_opens:
        total_box_opens = sum(b["total_opens"] for b in box_opens)
        if total_box_opens > 0:
            weighted_vol    = sum(b["box_volatility"] * b["total_opens"] for b in box_opens) / total_box_opens
            high_vol_opens  = sum(b["total_opens"] for b in box_opens if b["box_volatility"] >= 70)
            high_vol_pct    = round(high_vol_opens / total_box_opens * 100)
            if high_vol_pct >= 60:
                candidates.append((6, "Risk appetite high",
                    f"{high_vol_pct}% of box opens were on high-volatility boxes (vol >= 70), weighted avg volatility {round(weighted_vol)}."))
            elif high_vol_pct <= 20:
                candidates.append((6, "Conservative box preference",
                    f"Only {high_vol_pct}% of opens on high-volatility boxes, weighted avg volatility {round(weighted_vol)}."))

    # 7. Multi-transaction buyers
    multi_txn = [r for r in top_spenders if r["transactions"] >= 3]
    if multi_txn:
        candidates.append((7, "Repeat buyers",
            f"{len(multi_txn)} user{'s' if len(multi_txn) > 1 else ''} made 3+ transactions today, led by user {multi_txn[0]['user_id']} with {multi_txn[0]['transactions']} purchases."))

    # 8. Revenue product mix
    if by_product and total_rev > 0:
        top_product  = by_product[0]
        top_prod_pct = round(float(top_product["revenue_usd"]) / total_rev * 100)
        if top_prod_pct >= 60:
            candidates.append((6, f"{clean_slug(top_product['product_slug'])} drove {top_prod_pct}% of revenue",
                f"${float(top_product['revenue_usd']):,.2f} from {top_product['transactions']} transactions."))

    # 9. Payer-to-DAU conversion
    if dau > 0:
        conversion = round(total_payers / dau * 100, 1)
        if conversion >= 10:
            candidates.append((7, "Strong conversion",
                f"{total_payers} of {dau} active users paid today ({conversion}% payer conversion rate)."))
        elif conversion <= 2:
            candidates.append((5, "Low conversion",
                f"Only {conversion}% of {dau} active users made a purchase today."))

candidates.sort(key=lambda x: -x[0])
top_obs = candidates[:3]

lines += ["", "*Observations*", ""]
for _, label, detail in top_obs:
    lines.append(f"• *{label}:* {detail}")

message = "\n".join(lines)

# ── Post to Slack ─────────────────────────────────────────────────────────────
try:
    resp = slack.chat_postMessage(channel=CHANNEL_ID, text=message)
    thread_ts = resp["ts"]

    if "revenue" in charts:
        upload_chart(charts["revenue"], f"chart_revenue_{DATE}.png", thread_ts)
    if "boxes" in charts:
        upload_chart(charts["boxes"],   f"chart_boxes_{DATE}.png",   thread_ts)
    if "users" in charts:
        upload_chart(charts["users"],   f"chart_users_{DATE}.png",   thread_ts)
    if "topbox" in charts:
        upload_chart(charts["topbox"],  f"chart_topbox_{DATE}.png",  thread_ts)

    print(f"Report posted: https://secrethumans.slack.com/archives/{CHANNEL_ID}/p{thread_ts.replace('.','')}",
          flush=True)

except Exception as e:
    send_error(f"Slack post failed:\n```{e}```")
    raise
