"""
vip_report.py
Checks for VIP tier crossings in the last 24h and posts to #vip.

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

SLACK_VIP_CHANNEL = "C0BAJM1T8LE"   # #vip
ERROR_USER_ID     = "U0B0ZF5D6F9"   # niv — receives error DMs
MAX_BLOCKS        = 50              # Slack hard limit per message

Q_VIP_NEW = """
WITH crm_dedup AS (
  -- user_fact_crm contains exact duplicate rows for ~82 user_ids; without this
  -- a crossing gets reported once per duplicate.
  SELECT *
  FROM `goatbox-prod.processing_data.user_fact_crm`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY last_purchase_at DESC) = 1
),
vendor_costs AS (
  SELECT
    user_id,
    ROUND(SUM(vendor_price + vendor_fee), 2) AS total_vendor_cost
  FROM `goatbox-prod.processing_data.flat_order_vendor_pricing_events`
  GROUP BY user_id
)
SELECT
  crm.user_id,
  CAST(crm.lifetime_purchases_usd AS FLOAT64)                                   AS ltv_usd,
  ROUND(
    CAST(crm.lifetime_purchases_usd AS FLOAT64) - COALESCE(vc.total_vendor_cost, 0),
    2
  )                                                                               AS net_spend_usd,
  (vc.user_id IS NULL)                                                            AS no_vendor_pricing,
  CASE
    WHEN crm.lifetime_purchases_usd >= 1000
         AND (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) < 1000 THEN 'GOAT'
    WHEN crm.lifetime_purchases_usd >= 250
         AND (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) < 250  THEN 'Gold'
    WHEN crm.lifetime_purchases_usd >= 100
         AND (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) < 100  THEN 'VIP'
  END AS tier_crossed,
  -- Tier held immediately before the last purchase. NULL means they were not a
  -- VIP at all, i.e. this is a brand-new entrant rather than an upgrade.
  CASE
    WHEN (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) >= 1000 THEN 'GOAT'
    WHEN (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) >= 250  THEN 'Gold'
    WHEN (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) >= 100  THEN 'VIP'
  END AS prev_tier
FROM crm_dedup crm
LEFT JOIN vendor_costs vc ON vc.user_id = crm.user_id
WHERE crm.last_purchase_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND NOT EXISTS (
    SELECT 1 FROM `goatbox-prod.processing_data.internal_users` iu
    WHERE iu.user_id = crm.user_id
  )
  AND (
    (crm.lifetime_purchases_usd >= 1000
      AND (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) < 1000)
    OR (crm.lifetime_purchases_usd >= 250
      AND (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) < 250)
    OR (crm.lifetime_purchases_usd >= 100
      AND (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) < 100)
  )
ORDER BY crm.lifetime_purchases_usd DESC
"""

Q_VIP_PURCHASES = """
WITH new_vips AS (
  -- DISTINCT: duplicate crm rows would otherwise fan out the purchase join below
  SELECT DISTINCT user_id
  FROM `goatbox-prod.processing_data.user_fact_crm` ufc
  WHERE last_purchase_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
    AND NOT EXISTS (
      SELECT 1 FROM `goatbox-prod.processing_data.internal_users` iu
      WHERE iu.user_id = ufc.user_id
    )
    AND (
      (lifetime_purchases_usd >= 1000
        AND (lifetime_purchases_usd - last_purchase_amount_usd) < 1000)
      OR (lifetime_purchases_usd >= 250
        AND (lifetime_purchases_usd - last_purchase_amount_usd) < 250)
      OR (lifetime_purchases_usd >= 100
        AND (lifetime_purchases_usd - last_purchase_amount_usd) < 100)
    )
),
ranked AS (
  SELECT
    p.user_id,
    p.event_timestamp,
    CAST(p.amount_usd AS FLOAT64) AS amount_usd,
    p.product_slug,
    ROW_NUMBER() OVER (PARTITION BY p.user_id ORDER BY p.event_timestamp DESC) AS rn
  FROM `goatbox-prod.processing_data.flat_purchase_events` p
  INNER JOIN new_vips n ON n.user_id = p.user_id
)
SELECT user_id, event_timestamp, amount_usd, product_slug
FROM ranked
WHERE rn <= 2
ORDER BY user_id, rn
"""


def send_error_dm(client, msg):
    try:
        client.chat_postMessage(channel=ERROR_USER_ID, text=f":warning: vip_report error:\n{msg}")
    except Exception:
        pass


def run_query(bq, sql):
    return [dict(r) for r in bq.query(sql).result()]


def build_vip_blocks(vip_rows, purchase_rows):
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

    TIER_EMOJI = {"GOAT": "🐐", "Gold": "🥇", "VIP": "⭐"}

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

    for title, rows in (("🆕 New VIP Entrants", new_entrants), ("⬆️ Tier Upgrades", upgrades)):
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


def main():
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    if not slack_token:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    client = WebClient(token=slack_token)
    bq = bigquery.Client()

    print("Running VIP queries...")
    try:
        vip_rows      = run_query(bq, Q_VIP_NEW)
        purchase_rows = run_query(bq, Q_VIP_PURCHASES) if vip_rows else []
    except Exception as e:
        msg = f"BigQuery error: {e}"
        print(msg, file=sys.stderr)
        send_error_dm(client, msg)
        sys.exit(1)

    n_new      = sum(1 for r in vip_rows if not r.get("prev_tier"))
    n_upgrades = len(vip_rows) - n_new
    print(f"  Tier crossings: {len(vip_rows)} ({n_new} new entrant(s), {n_upgrades} upgrade(s))")

    blocks = build_vip_blocks(vip_rows, purchase_rows)
    if vip_rows:
        parts = []
        if n_new:
            parts.append(f"🆕 {n_new} new VIP entrant(s)")
        if n_upgrades:
            parts.append(f"⬆️ {n_upgrades} tier upgrade(s)")
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
        msg = f"Slack error: {e.response['error']}"
        print(msg, file=sys.stderr)
        send_error_dm(client, msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
