import os
import io
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
def q(sql):
    return [dict(row) for row in bq.query(sql.replace("DATE_FILTER", DATE)).result()]

def send_error(msg):
    slack.chat_postMessage(channel=ERROR_USER_ID, text=f":warning: *Daily report failed ({DATE})*\n{msg}")

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
    dau_row = q(f"""
        SELECT COUNT(DISTINCT user_id) AS daily_active_users
        FROM `goatbox-prod.processing_data.flat_login_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER'
          AND user_id NOT IN ({EXCLUDED})
    """)[0]

    summary = q(f"""
        SELECT
          COUNT(DISTINCT user_id)               AS total_payers,
          COUNT(*)                               AS total_transactions,
          ROUND(SUM(amount_usd), 2)              AS total_revenue_usd,
          ROUND(AVG(amount_usd), 2)              AS avg_transaction_usd,
          COUNTIF(coupon_code IS NOT NULL)       AS transactions_with_coupon
        FROM `goatbox-prod.processing_data.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER'
          AND user_id NOT IN ({EXCLUDED})
    """)[0]

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
          COUNT(*)                                                          AS total_opens,
          COUNT(DISTINCT b.user_id)                                         AS unique_openers,
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
            ROUND(SUM(p.amount_usd), 2)                                                AS lifetime_value_usd
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
              AND b.box_display_name = '{top_box_name}'
            GROUP BY b.user_id
            ORDER BY opens DESC
        """)

except Exception as e:
    send_error(f"BigQuery query failed:\n```{e}```")
    raise

# ── Derived values ────────────────────────────────────────────────────────────
dau            = dau_row["daily_active_users"]
total_payers   = summary["total_payers"]
total_txns     = summary["total_transactions"]
total_rev      = float(summary["total_revenue_usd"])
avg_spend      = float(summary["avg_transaction_usd"])
coupon_count   = summary["transactions_with_coupon"]

new_payers     = [r for r in cohort if r["payer_type"] == "new"]
return_payers  = [r for r in cohort if r["payer_type"] == "return"]

def clean_slug(s):
    return s.replace("-shop-item", "").replace("-", " ").title()

# ── Charts ────────────────────────────────────────────────────────────────────
try:
    charts = {}

    # Chart 1: Revenue by product
    labels_rev = [clean_slug(r["product_slug"]) for r in by_product][::-1]
    values_rev = [float(r["revenue_usd"]) for r in by_product][::-1]
    fig, ax = plt.subplots(figsize=(7, max(3, len(labels_rev) * 0.55 + 1)))
    bars = ax.barh(labels_rev, values_rev, color=ACCENT, height=0.6)
    for bar, val in zip(bars, values_rev):
        ax.text(bar.get_width() + max(values_rev) * 0.01, bar.get_y() + bar.get_height() / 2,
                f"${val:,.2f}", va="center", ha="left", fontsize=10, color=TEXT)
    ax.set_xlabel("Revenue (USD)", color=MUTED, fontsize=10)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    base_style(ax, f"Revenue by Store Product  -  {DATE}")
    plt.tight_layout()
    charts["revenue"] = fig_to_bytes(fig)

    # Chart 2: Box opens
    labels_box  = [b["box_display_name"] for b in box_opens][::-1]
    opens       = [b["total_opens"] for b in box_opens][::-1]
    unique      = [b["unique_openers"] for b in box_opens][::-1]
    volatility  = [b["box_volatility"] for b in box_opens][::-1]
    norm = plt.Normalize(0, 100); cmap = plt.cm.RdYlBu_r
    colors_box = [cmap(norm(v)) for v in volatility]
    fig, ax = plt.subplots(figsize=(8, max(4, len(labels_box) * 0.6 + 1.2)))
    bars = ax.barh(labels_box, opens, color=colors_box, height=0.6)
    for bar, o, u in zip(bars, opens, unique):
        ax.text(bar.get_width() + max(opens) * 0.01, bar.get_y() + bar.get_height() / 2,
                f"{o} opens, {u} user{'s' if u != 1 else ''}", va="center", ha="left", fontsize=9, color=TEXT)
    ax.set_xlabel("Box Opens", color=MUTED, fontsize=10)
    base_style(ax, f"Top Box Opens by Paying Users  -  {DATE}")
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm); sm.set_array([])
    cbar = plt.colorbar(sm, ax=ax, orientation="vertical", fraction=0.02, pad=0.12)
    cbar.set_label("Volatility", color=MUTED, fontsize=9); cbar.ax.tick_params(labelsize=8, colors=MUTED)
    plt.tight_layout()
    charts["boxes"] = fig_to_bytes(fig)

    # Chart 3: Top users
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, max(3, len(top_openers) * 0.65 + 1.5)))
    if top_openers:
        lo = [r["user_id"] for r in top_openers][::-1]
        vo = [int(r["total_boxes_opened"]) for r in top_openers][::-1]
        ax1.barh(lo, vo, color="#36C5F0", height=0.6)
        for i, v in enumerate(vo):
            ax1.text(v + max(vo) * 0.01, i, str(v), va="center", fontsize=10, color=TEXT)
        ax1.set_xlabel("Boxes Opened", color=MUTED, fontsize=10)
        ax1.tick_params(axis="y", labelsize=7)
        base_style(ax1, "Top Openers")
    if top_spenders:
        ls = [r["user_id"] for r in top_spenders][::-1]
        vs = [float(r["total_spend_usd"]) for r in top_spenders][::-1]
        ax2.barh(ls, vs, color="#2EB67D", height=0.6)
        for i, v in enumerate(vs):
            ax2.text(v + max(vs) * 0.01, i, f"${v:,.2f}", va="center", fontsize=10, color=TEXT)
        ax2.set_xlabel("Total Spend (USD)", color=MUTED, fontsize=10)
        ax2.tick_params(axis="y", labelsize=7)
        base_style(ax2, "Top Spenders")
    fig.suptitle(f"Top Users  -  {DATE}", fontsize=13, fontweight="bold", color=TEXT, x=0.02, ha="left")
    plt.tight_layout()
    charts["users"] = fig_to_bytes(fig)

    # Chart 4: Top box table
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
    "",
    f"*Payer Cohort* — *{len(new_payers)}* new  ·  *{len(return_payers)}* returning",
]

if return_payers:
    lines.append("")
    for r in return_payers:
        days = r["days_since_last_purchase"]
        last = str(r["last_prior_purchase_date"]) if r["last_prior_purchase_date"] else "N/A"
        lines.append(
            f"• `{r['user_id']}` — {r['lifetime_purchases']} purchases · "
            f"${float(r['lifetime_value_usd']):,.2f} LTV · "
            f"last seen {days if days is not None else 'N/A'} days ago ({last})"
        )

lines += ["", "*Revenue by Store Product*", ""]
for r in by_product:
    lines.append(
        f"• *{clean_slug(r['product_slug'])}* — "
        f"{r['payers']} payers · {r['transactions']} txns · "
        f"${float(r['revenue_usd']):,.2f} · ${float(r['avg_usd']):,.2f} avg"
    )

lines += ["", "*Top Box Opens*", ""]
for r in box_opens:
    lines.append(
        f"• *{r['box_display_name']}* (vol {r['box_volatility']}) — "
        f"{r['total_opens']} opens · {r['unique_openers']} users · {r['box_price_coins']} coins/box"
    )

if top_box_name and top_box_users:
    lines += ["", f"*Who Opened '{top_box_name}'*", ""]
    for r in top_box_users:
        lines.append(
            f"• `{r['user_id']}` — {r['opens']} opens · {float(r['coins_spent']):,.0f} coins"
        )

lines += ["", "*Top Openers*", ""]
for i, r in enumerate(top_openers, 1):
    lines.append(
        f"{i}. `{r['user_id']}` — {r['total_boxes_opened']} boxes · "
        f"{r['distinct_boxes']} types · {int(r['total_coins_spent']):,} coins"
    )

lines += ["", "*Top Spenders*", ""]
for i, r in enumerate(top_spenders, 1):
    products = ", ".join(clean_slug(p.strip()) for p in r["products_bought"].split(","))
    lines.append(
        f"{i}. `{r['user_id']}` — {r['transactions']} txns · "
        f"${float(r['total_spend_usd']):,.2f} · {products}"
    )

# ── Observations ──────────────────────────────────────────────────────────────
new_count  = len(new_payers)
ret_count  = len(return_payers)
pct_new    = round(new_count / total_payers * 100) if total_payers else 0
new_rev    = sum(float(r["lifetime_value_usd"]) for r in new_payers)
ret_rev    = sum(float(r["lifetime_value_usd"]) for r in return_payers)
new_avg    = new_rev / new_count if new_count else 0
ret_avg    = ret_rev / ret_count if ret_count else 0

# ── Observations (rule-based, multi-signal) ───────────────────────────────────
candidates = []  # list of (priority, text) — higher priority = more noteworthy

# 1. Cohort split
if new_count == total_payers:
    candidates.append((10, f"*\1* Every payer today was a first-time buyer. "
                           f"{new_count} new users generated ${new_rev:,.2f} at ${new_avg:,.2f} avg."))
elif ret_count == total_payers:
    candidates.append((10, f"*\1* No new buyers today. "
                           f"{ret_count} returning users generated ${ret_rev:,.2f} at ${ret_avg:,.2f} avg."))
elif pct_new >= 70:
    candidates.append((9, f"*\1* {new_count} of {total_payers} payers ({pct_new}%) were new, "
                          f"contributing ${new_rev:,.2f} vs ${ret_rev:,.2f} from {ret_count} returning users."))
elif pct_new <= 30:
    candidates.append((9, f"*\1* {ret_count} of {total_payers} payers ({100-pct_new}%) were returning, "
                          f"driving ${ret_rev:,.2f} ({round(ret_rev/total_rev*100)}% of revenue)."))
else:
    candidates.append((6, f"*\1* {new_count} new payers (${new_avg:,.2f} avg) vs "
                          f"{ret_count} returning (${ret_avg:,.2f} avg) — "
                          f"{'returners spent more per head' if ret_avg > new_avg else 'new users spent more per head'}."))

# 2. Return payer re-engagement gap
if return_payers:
    gaps = [(r["days_since_last_purchase"], r) for r in return_payers if r.get("days_since_last_purchase")]
    if gaps:
        max_gap, max_gap_user = max(gaps, key=lambda x: x[0])
        if max_gap >= 14:
            candidates.append((9, f"*\1* User {max_gap_user['user_id']} returned after "
                                   f"{max_gap} days away with ${float(max_gap_user['lifetime_value_usd']):,.2f} LTV "
                                   f"across {max_gap_user['lifetime_purchases']} lifetime purchases."))
        elif max_gap >= 5:
            candidates.append((7, f"*\1* Top returning user came back after {max_gap} days "
                                   f"(${float(max_gap_user['lifetime_value_usd']):,.2f} LTV)."))

# 3. Power user spend concentration
if top_spenders:
    top_spend = float(top_spenders[0]["total_spend_usd"])
    top_pct   = round(top_spend / total_rev * 100)
    if top_pct >= 40:
        candidates.append((8, f"*\1* Top spender (user {top_spenders[0]['user_id']}) "
                               f"accounted for ${top_spend:,.2f} ({top_pct}% of total revenue) "
                               f"across {top_spenders[0]['transactions']} transactions."))
    elif top_pct >= 25:
        candidates.append((6, f"*\1* "
                               f"${top_spend:,.2f} from user {top_spenders[0]['user_id']}."))

# 4. Box opens concentration
if box_opens and top_box_users:
    total_opens_all  = sum(b["total_opens"] for b in box_opens)
    top_box_opens    = box_opens[0]["total_opens"]
    top_box_pct      = round(top_box_opens / total_opens_all * 100)
    top_user_opens   = top_box_users[0]["opens"]
    top_user_pct     = round(top_user_opens / top_box_opens * 100)
    if top_user_pct >= 80:
        candidates.append((8, f"*\1* One user opened {top_user_opens:,} of "
                               f"{top_box_opens:,} {top_box_name} boxes ({top_user_pct}%)."))
    elif top_box_pct >= 50:
        candidates.append((7, f"*\1* {top_box_opens:,} opens "
                               f"({top_box_pct}% of all {total_opens_all:,}) across "
                               f"{box_opens[0]['unique_openers']} users."))

# 5. Coupon usage rate
if total_txns > 0:
    coupon_pct = round(coupon_count / total_txns * 100)
    if coupon_pct >= 40:
        candidates.append((7, f"*\1* {coupon_count} of {total_txns} transactions ({coupon_pct}%) "
                               f"used a coupon code, suggesting active discount-driven purchasing."))
    elif coupon_pct == 0 and total_txns >= 5:
        candidates.append((5, f"*\1* All {total_txns} transactions were full price."))

# 6. Box volatility preference
if box_opens:
    total_box_opens = sum(b["total_opens"] for b in box_opens)
    weighted_vol    = sum(b["box_volatility"] * b["total_opens"] for b in box_opens) / total_box_opens
    high_vol_opens  = sum(b["total_opens"] for b in box_opens if b["box_volatility"] >= 70)
    high_vol_pct    = round(high_vol_opens / total_box_opens * 100)
    if high_vol_pct >= 60:
        candidates.append((6, f"*\1* {high_vol_pct}% of box opens were on high-volatility boxes "
                               f"(vol >= 70), weighted avg volatility {round(weighted_vol)}."))
    elif high_vol_pct <= 20:
        candidates.append((6, f"*\1* Only {high_vol_pct}% of opens on high-volatility boxes, "
                               f"weighted avg volatility {round(weighted_vol)}."))

# 7. Multi-transaction buyers
multi_txn = [r for r in top_spenders if r["transactions"] >= 3]
if multi_txn:
    candidates.append((7, f"*\1* {len(multi_txn)} user{'s' if len(multi_txn)>1 else ''} made 3+ "
                           f"transactions today, led by user {multi_txn[0]['user_id']} "
                           f"with {multi_txn[0]['transactions']} purchases."))

# 8. Revenue product mix (high-ticket share)
if by_product:
    top_product     = by_product[0]
    top_prod_pct    = round(float(top_product["revenue_usd"]) / total_rev * 100)
    if top_prod_pct >= 60:
        candidates.append((6, f"*\1* "
                               f"${float(top_product['revenue_usd']):,.2f} from {top_product['transactions']} transactions."))

# 9. Payer-to-DAU conversion
if dau > 0:
    conversion = round(total_payers / dau * 100, 1)
    if conversion >= 10:
        candidates.append((7, f"*\1* {total_payers} of {dau} active users paid today "
                               f"({conversion}% payer conversion rate)."))
    elif conversion <= 2:
        candidates.append((5, f"*\1* Only {conversion}% of {dau} active users made a purchase today."))

# Pick top 3 by priority, deduplicate themes
candidates.sort(key=lambda x: -x[0])
obs = [text for _, text in candidates[:3]]

lines += ["", "---", "", "*\1*"]
for o in obs:
    lines.append(f"- {o}")

message = "\n".join(lines)

# ── Post to Slack ─────────────────────────────────────────────────────────────
try:
    resp = slack.chat_postMessage(channel=CHANNEL_ID, text=message)
    thread_ts = resp["ts"]

    upload_chart(charts["revenue"], f"chart_revenue_{DATE}.png", thread_ts)
    upload_chart(charts["boxes"],   f"chart_boxes_{DATE}.png",   thread_ts)
    upload_chart(charts["users"],   f"chart_users_{DATE}.png",   thread_ts)
    upload_chart(charts["topbox"],  f"chart_topbox_{DATE}.png",  thread_ts)

    print(f"Report posted: https://secrethumans.slack.com/archives/{CHANNEL_ID}/p{thread_ts.replace('.','')}")

except SlackApiError as e:
    send_error(f"Slack post failed:\n```{e}```")
    raise
