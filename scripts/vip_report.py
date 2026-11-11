"""
vip_report.py
Checks for VIP tier crossings in the last 24h and posts to #vip, one post per brand.
GOATbox posts every day, "no new VIPs" included. Hero Rips posts only when someone
crossed a tier. Tiers come from BRANDS in vip_tracker_refresh.py, the same config the
tracker uses. A crossing is a user whose tier after the last purchase ranks above the
tier before it, with the Hero Rips Insider 30-day rule applied to both (see
crossing_sql).

Splits crossings into two groups:
  - new entrants  — had no tier before this purchase (crossed $100 for the first time)
  - tier upgrades — already a VIP/Gold and moved up (e.g. Gold -> GOAT)

Secrets required:
  GCP_SERVICE_ACCOUNT_JSON   full JSON content of the GCP service account key
  SLACK_BOT_TOKEN            xoxb-... bot token with chat:write scope
"""

import os
import sys
from datetime import datetime, timezone, timedelta

from google.cloud import bigquery
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from vip_tracker_refresh import BRANDS, HERORIPS_TOP_TIER

SLACK_VIP_CHANNEL = "C0BAJM1T8LE"   # #vip
ERROR_USER_ID     = "U0B0ZF5D6F9"   # niv — receives error DMs
MAX_BLOCKS        = 50              # Slack hard limit per message

# Per brand: header titles and tier emoji. GOATbox keeps the titles it always had.
POST = {
    "goatbox": {
        "new_title": "🆕 New VIP Entrants",
        "up_title": "⬆️ Tier Upgrades",
        "emoji": {"GOAT": "🐐", "Gold": "🥇", "VIP": "⭐"},
        "post_when_empty": True,
    },
    "herorips": {
        "new_title": "🆕 New Hero Rips VIP Entrants",
        "up_title": "⬆️ Hero Rips Tier Upgrades",
        "emoji": {HERORIPS_TOP_TIER: "👑", "Gold": "🥇", "VIP": "⭐", "Insider": "🔹"},
        "post_when_empty": False,
    },
}


def tier_sql(brand, ltv, purchase_at, as_of):
    """SQL CASE giving a brand's tier from lifetime purchases ltv, using the tracker
    config. A tier with a recent-purchase rule also needs purchase_at to be no more than
    that many days before as_of. Names and numbers are our own constants."""
    lines = []
    for name, floor, recent_days in BRANDS[brand]["tiers"]:
        cond = f"{ltv} >= {floor}"
        if recent_days:
            cond += (
                f" AND {purchase_at} IS NOT NULL"
                f" AND {purchase_at} >= TIMESTAMP_SUB({as_of}, INTERVAL {recent_days} DAY)"
            )
        lines.append(f"    WHEN {cond} THEN '{name}'")
    return "CASE\n" + "\n".join(lines) + "\n  END"


def rank_sql(brand, col):
    """SQL CASE turning a tier name into its rank (higher is better), NULL for no tier."""
    tiers = BRANDS[brand]["tiers"]
    lines = [f"    WHEN '{name}' THEN {len(tiers) - i}" for i, (name, _, _) in enumerate(tiers)]
    return f"CASE {col}\n" + "\n".join(lines) + "\n  END"


def crossing_sql(brand):
    """The tier-crossing query for one brand.

    tier now: the tier rules applied to lifetime purchases now. The last purchase is
    within 24h, so a recent-purchase rule always holds.
    tier before: the same rules applied to lifetime purchases minus the last purchase,
    where a recent-purchase rule needs the purchase before the last one to fall within
    that many days before the last one. So a lapsed Hero Rips player at $150 who buys
    again counts as a new entrant, not as an Insider moving up.
    A crossing is any row whose tier now ranks above its tier before.
    """
    tier_now = tier_sql(
        brand, "crm.lifetime_purchases_usd", "crm.last_purchase_at", "CURRENT_TIMESTAMP()",
    )
    tier_before = tier_sql(
        brand, "(crm.lifetime_purchases_usd - crm.last_purchase_amount_usd)",
        "pp.prev_purchase_at", "crm.last_purchase_at",
    )
    return (
        Q_VIP_NEW.replace("__BRAND__", brand)
        .replace("__TIER_NOW__", tier_now)
        .replace("__TIER_BEFORE__", tier_before)
        .replace("__RANK_NOW__", rank_sql(brand, "tier_crossed"))
        .replace("__RANK_BEFORE__", rank_sql(brand, "prev_tier"))
    )


Q_VIP_NEW = """
WITH crm_dedup AS (
  -- user_fact_crm contains duplicate rows for 107 user_ids; without this a crossing
  -- gets reported once per duplicate. It holds both brands, so each brand's post
  -- reads its own rows only (Hero Rips 01M73R9Y was posted as GOATbox, 2026-11-11).
  SELECT *
  FROM `goatbox-prod.processing_data.user_fact_crm`
  WHERE brand = '__BRAND__'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY last_purchase_at DESC) = 1
),
recent AS (
  SELECT *
  FROM crm_dedup crm
  WHERE crm.last_purchase_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
    AND NOT EXISTS (
      SELECT 1 FROM `goatbox-prod.processing_data.internal_users` iu
      WHERE iu.user_id = crm.user_id
    )
),
prev_purchase AS (
  -- The purchase before the last one, for tiers that need a recent purchase.
  SELECT user_id, event_timestamp AS prev_purchase_at
  FROM `goatbox-prod.processing_data.flat_purchase_events`
  WHERE user_id IN (SELECT user_id FROM recent)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY event_timestamp DESC) = 2
),
vendor_costs AS (
  SELECT
    user_id,
    ROUND(SUM(vendor_price + vendor_fee), 2) AS total_vendor_cost
  FROM `goatbox-prod.processing_data.flat_order_vendor_pricing_events`
  GROUP BY user_id
),
tiered AS (
  SELECT
    crm.user_id,
    CAST(crm.lifetime_purchases_usd AS FLOAT64)                                   AS ltv_usd,
    ROUND(
      CAST(crm.lifetime_purchases_usd AS FLOAT64) - COALESCE(vc.total_vendor_cost, 0),
      2
    )                                                                             AS net_spend_usd,
    (vc.user_id IS NULL)                                                          AS no_vendor_pricing,
    __TIER_NOW__ AS tier_crossed,
    -- Tier held immediately before the last purchase. NULL means they were not a
    -- VIP at all, i.e. this is a brand-new entrant rather than an upgrade.
    __TIER_BEFORE__ AS prev_tier
  FROM recent crm
  LEFT JOIN prev_purchase pp ON pp.user_id = crm.user_id
  LEFT JOIN vendor_costs vc ON vc.user_id = crm.user_id
)
SELECT user_id, ltv_usd, net_spend_usd, no_vendor_pricing, tier_crossed, prev_tier
FROM tiered
WHERE tier_crossed IS NOT NULL
  AND (prev_tier IS NULL OR __RANK_NOW__ > __RANK_BEFORE__)
ORDER BY ltv_usd DESC
"""

# Last 2 purchases for the users in the post. @user_ids comes from Q_VIP_NEW.
Q_VIP_PURCHASES = """
SELECT user_id, event_timestamp, CAST(amount_usd AS FLOAT64) AS amount_usd, product_slug
FROM `goatbox-prod.processing_data.flat_purchase_events`
WHERE user_id IN UNNEST(@user_ids)
QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY event_timestamp DESC) <= 2
ORDER BY user_id, event_timestamp DESC
"""


def send_error_dm(client, msg):
    try:
        client.chat_postMessage(channel=ERROR_USER_ID, text=f":warning: vip_report error:\n{msg}")
    except Exception:
        pass


def run_query(bq, sql, user_ids=None):
    if user_ids is None:
        return [dict(r) for r in bq.query(sql).result()]
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("user_ids", "STRING", list(user_ids)),
    ])
    return [dict(r) for r in bq.query(sql, job_config=job_config).result()]


def build_vip_blocks(vip_rows, purchase_rows, brand="goatbox"):
    IDT = timezone(timedelta(hours=3))

    purchases_by_user = {}
    for p in purchase_rows:
        purchases_by_user.setdefault(p["user_id"], []).append(p)

    def fmt_purchase(p):
        ts = p["event_timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(IDT)
        return f"• {local.strftime('%a %b %-d, %-I:%M %p IDT')} — ${p['amount_usd']:.2f} ({p['product_slug'] or '—'})"

    def fmt_net_spend(v, no_vendor_pricing):
        sign = "+" if v >= 0 else ""
        flag = " ⚠️" if no_vendor_pricing else ""
        return f"{sign}${v:,.2f}{flag}"

    TIER_EMOJI = POST[brand]["emoji"]

    if not vip_rows:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "No new VIPs or tier upgrades today."}}]

    # prev_tier NULL => they held no tier before this purchase => brand-new entrant
    new_entrants = [r for r in vip_rows if not r.get("prev_tier")]
    upgrades     = [r for r in vip_rows if r.get("prev_tier")]

    def vip_section(row):
        uid               = row["user_id"]
        tier              = row["tier_crossed"]
        prev_tier         = row.get("prev_tier")
        ltv               = row["ltv_usd"]
        net_spend         = row["net_spend_usd"]
        no_vendor_pricing = row["no_vendor_pricing"]
        emoji = TIER_EMOJI.get(tier, "⭐")
        purchase_lines = "\n".join(fmt_purchase(p) for p in purchases_by_user.get(uid, []))
        if not purchase_lines:
            purchase_lines = "• No purchase records found"

        if prev_tier:
            headline = f"{emoji} *{prev_tier} → {tier}* — `{uid}`"
        else:
            headline = f"{emoji} *{tier}* — `{uid}`"

        return {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{headline}\n"
                    f"LTV: *${ltv:,.2f}*  |  Net Spend: *{fmt_net_spend(net_spend, no_vendor_pricing)}*\n"
                    f"Last 2 purchases:\n{purchase_lines}"
                )
            }
        }

    blocks = []
    truncated = 0

    titles = (POST[brand]["new_title"], POST[brand]["up_title"])
    for title, rows in zip(titles, (new_entrants, upgrades)):
        if not rows:
            continue
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": title}})
        for row in rows:
            # Slack rejects a message with >50 blocks outright; leave room for the
            # remaining header and the truncation notice rather than failing the run.
            if len(blocks) >= MAX_BLOCKS - 3:
                truncated += 1
                continue
            blocks.append(vip_section(row))
            blocks.append({"type": "divider"})

    if truncated:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_…and {truncated} more not shown (Slack block limit). Full list in the tracker Sheet._",
            }],
        })

    return blocks


def post_brand(client, bq, brand):
    """Query one brand's tier crossings and post them to #vip. Exits on any error."""
    label = BRANDS[brand]["label"]
    print(f"Running {label} VIP queries...")
    try:
        vip_rows = run_query(bq, crossing_sql(brand))
        purchase_rows = (
            run_query(bq, Q_VIP_PURCHASES, [r["user_id"] for r in vip_rows]) if vip_rows else []
        )
    except Exception as e:
        msg = f"BigQuery error ({label}): {e}"
        print(msg, file=sys.stderr)
        send_error_dm(client, msg)
        sys.exit(1)

    n_new      = sum(1 for r in vip_rows if not r.get("prev_tier"))
    n_upgrades = len(vip_rows) - n_new
    print(f"  Tier crossings: {len(vip_rows)} ({n_new} new entrant(s), {n_upgrades} upgrade(s))")

    if not vip_rows and not POST[brand]["post_when_empty"]:
        print(f"  Nothing to post for {label}.")
        return

    blocks = build_vip_blocks(vip_rows, purchase_rows, brand)
    name = "" if brand == "goatbox" else f"{label} "
    if vip_rows:
        parts = []
        if n_new:
            parts.append(f"🆕 {n_new} new {name}VIP entrant(s)")
        if n_upgrades:
            parts.append(f"⬆️ {n_upgrades} {name}tier upgrade(s)")
        fallback = ", ".join(parts) + " today"
    else:
        fallback = "No new VIPs or tier upgrades today."

    print("Posting to #vip...")
    try:
        resp = client.chat_postMessage(
            channel=SLACK_VIP_CHANNEL,
            blocks=blocks,
            text=fallback,
            unfurl_links=False,
            unfurl_media=False,
        )
        print(f"Posted. ts={resp['ts']}")
    except SlackApiError as e:
        msg = f"Slack error ({label}): {e.response['error']}"
        print(msg, file=sys.stderr)
        send_error_dm(client, msg)
        sys.exit(1)


def main():
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    if not slack_token:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    client = WebClient(token=slack_token)
    bq = bigquery.Client()

    for brand in ("goatbox", "herorips"):
        post_brand(client, bq, brand)


if __name__ == "__main__":
    main()
