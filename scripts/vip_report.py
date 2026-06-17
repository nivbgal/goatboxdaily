"""
vip_report.py
Checks for new VIP tier crossings in the last 24h and posts to #vip.

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

Q_VIP_NEW = """
WITH vendor_costs AS (
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
  END AS tier_crossed
FROM `goatbox-prod.processing_data.user_fact_crm` crm
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
  SELECT user_id
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
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "No new VIPs today."}}]

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "🆕 New VIP Entrants"}}]

    for row in vip_rows:
        uid               = row["user_id"]
        tier              = row["tier_crossed"]
        ltv               = row["ltv_usd"]
        net_spend         = row["net_spend_usd"]
        no_vendor_pricing = row["no_vendor_pricing"]
        emoji = TIER_EMOJI.get(tier, "⭐")
        purchase_lines = "\n".join(fmt_purchase(p) for p in purchases_by_user.get(uid, []))
        if not purchase_lines:
            purchase_lines = "• No purchase records found"

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{tier}* — `{uid}`\n"
                    f"LTV: *${ltv:,.2f}*  |  Net Spend: *{fmt_net_spend(net_spend, no_vendor_pricing)}*\n"
                    f"Last 2 purchases:\n{purchase_lines}"
                )
            }
        })
        blocks.append({"type": "divider"})

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

    print(f"  New VIP rows: {len(vip_rows)}")

    blocks = build_vip_blocks(vip_rows, purchase_rows)
    fallback = f"🆕 {len(vip_rows)} new VIP entrant(s) today" if vip_rows else "No new VIPs today."

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
