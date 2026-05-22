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
def make_table(headers, rows):
    str_rows = [[str(c) for c in row] for row in rows]
    all_rows = [headers] + str_rows
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(headers))]
    sep = "  ".join("-" * w for w in widths)
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)), sep]
    for row in str_rows:
        out.append("  ".join(c.ljust(w) for c, w in zip(row, widths)))
    return "```\n" + "\n".join(out) + "\n```"

lines = [
    f"\U0001f4e6 *Goatbox Daily Report — {DATE}*",
    "",
    f"*{total_payers}* payers  ·  *{total_txns}* transactions  ·  *${total_rev:,.2f}* revenue  ·  "
    f"*${avg_spend:,.2f}* avg spend  ·  {coupon_count} coupon transactions  ·  *{dau}* daily active users",
    "",
    "*Payer Cohort*",
    f"*{len(new_payers)}* new payers  ·  *{len(return_payers)}* return payers",
]

if return_payers:
    lines += [
        "",
        make_table(
            ["User ID", "Lifetime Purchases", "LTV", "Days Since Last", "Last Prior Purchase"],
            [(r["user_id"], r["lifetime_purchases"], f"${float(r['lifetime_value_usd']):,.2f}",
              r["days_since_last_purchase"] if r["days_since_last_purchase"] is not None else "N/A",
              str(r["last_prior_purchase_date"]) if r["last_prior_purchase_date"] else "N/A")
             for r in return_payers],
        ),
    ]

lines += [
    "",
    "*Revenue by Store Product*",
    "",
    make_table(
        ["Product", "Payers", "Transactions", "Revenue", "Avg Spend"],
        [(clean_slug(r["product_slug"]), r["payers"], r["transactions"],
          f"${float(r['revenue_usd']):,.2f}", f"${float(r['avg_usd']):,.2f}")
         for r in by_product],
    ),
]

lines += [
    "",
    "*Top Box Opens*",
    "",
    make_table(
        ["Box", "Volatility", "Opens", "Unique Openers", "Box Price (coins)"],
        [(r["box_display_name"], r["box_volatility"], r["total_opens"],
          r["unique_openers"], r["box_price_coins"])
         for r in box_opens],
    ),
]

if top_box_name and top_box_users:
    lines += [
        "",
        f"*Who Opened '{top_box_name}'*",
        "",
        make_table(
            ["User ID", "Opens", "Coins Spent"],
            [(r["user_id"], r["opens"], f"{float(r['coins_spent']):,.0f}")
             for r in top_box_users],
        ),
    ]

lines += [
    "",
    "*Top Openers*",
    "",
    make_table(
        ["User ID", "Boxes Opened", "Distinct Boxes", "Coins Spent"],
        [(r["user_id"], r["total_boxes_opened"], r["distinct_boxes"],
          f"{int(r['total_coins_spent']):,}")
         for r in top_openers],
    ),
    "",
    "*Top Spenders*",
    "",
    make_table(
        ["User ID", "Transactions", "Total Spend", "Products"],
        [(r["user_id"], r["transactions"], f"${float(r['total_spend_usd']):,.2f}",
          ", ".join(clean_slug(p.strip()) for p in r["products_bought"].split(",")))
         for r in top_spenders],
    ),
]

# ── Observations ──────────────────────────────────────────────────────────────
new_rev   = sum(float(r["lifetime_value_usd"]) for r in new_payers)
ret_rev   = sum(float(r["lifetime_value_usd"]) for r in return_payers)
new_count = len(new_payers)
ret_count = len(return_payers)
pct_new   = round(new_count / total_payers * 100) if total_payers else 0

obs = []

# Cohort observation
if new_count > 0 and ret_count > 0:
    if pct_new >= 60:
        obs.append(
            f"*New payer majority:* {new_count} of {total_payers} payers ({pct_new}%) were new, "
            f"generating ${new_rev:,.2f} in first-time revenue."
        )
    elif ret_count > new_count:
        obs.append(
            f"*Return payer majority:* {ret_count} of {total_payers} payers ({100-pct_new}%) were returning, "
            f"contributing ${ret_rev:,.2f} in revenue."
        )
    else:
        obs.append(
            f"*Even cohort split:* {new_count} new vs {ret_count} return payers today."
        )
elif new_count == total_payers:
    obs.append(f"*All new payers today:* All {total_payers} payers were first-time buyers, generating ${new_rev:,.2f}.")
elif ret_count == total_payers:
    obs.append(f"*All return payers today:* Every payer today was a repeat buyer.")

# Re-engagement gap observation
if return_payers:
    max_gap_user = max(return_payers, key=lambda r: r["days_since_last_purchase"] or 0)
    gap = max_gap_user.get("days_since_last_purchase")
    if gap and gap >= 5:
        obs.append(
            f"*Long re-engagement gap:* User {max_gap_user['user_id']} returned after {gap} days away "
            f"with ${float(max_gap_user['lifetime_value_usd']):,.2f} in lifetime value."
        )

# Top box concentration observation
if box_opens and top_box_users:
    top_user_opens = max((r["opens"] for r in top_box_users), default=0)
    top_box_total  = box_opens[0]["total_opens"]
    pct_concentrated = round(top_user_opens / top_box_total * 100) if top_box_total else 0
    if pct_concentrated >= 70:
        obs.append(
            f"*{top_box_name} dominated by one user:* Single user accounted for "
            f"{top_user_opens:,} of {top_box_total:,} opens ({pct_concentrated}%)."
        )
    else:
        obs.append(
            f"*{top_box_name} was the top box* with {top_box_total:,} opens across "
            f"{box_opens[0]['unique_openers']} users."
        )

# Fallback if not enough observations
while len(obs) < 2:
    obs.append(f"*Revenue concentration:* Top product ({clean_slug(by_product[0]['product_slug'])}) "
               f"drove ${float(by_product[0]['revenue_usd']):,.2f} ({round(float(by_product[0]['revenue_usd'])/total_rev*100)}% of total).")

lines += ["", "*Observations*"]
for o in obs[:3]:
    lines.append(f"• {o}")

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
