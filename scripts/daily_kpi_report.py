"""
daily_kpi_report.py
Queries BigQuery for the 4 GOATBox 90-day KPIs and posts a formatted
summary to #daily-report-claude on Slack.
Also checks for new VIP tier crossings in the last 24h and posts to #vip.

Secrets required (set in GitHub → Settings → Secrets and variables → Actions):
  GCP_SERVICE_ACCOUNT_JSON   full JSON content of the GCP service account key
  SLACK_BOT_TOKEN            xoxb-... bot token with chat:write scope
"""

import os
import sys
from datetime import datetime, timezone, timedelta

from google.cloud import bigquery
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_CHANNEL     = "C0B3KS5KNTC"   # #daily-report-claude
SLACK_VIP_CHANNEL = "C0BAJM1T8LE"   # #vip
CAMPAIGN_START = datetime(2026, 5, 10, tzinfo=timezone.utc)
CAMPAIGN_END   = datetime(2026, 8,  7, tzinfo=timezone.utc)

BASELINES = {"ftd": 5.0,  "arpu": 0.90, "ret": 1.4,  "pu": 6.5}
TARGETS   = {"ftd": 7.0,  "arpu": 1.50, "ret": 4.25, "pu": 15.0}

# ── BigQuery queries ──────────────────────────────────────────────────────────
Q_FTD = """
SELECT
  FORMAT_DATE('%Y-%m-%d', DATE_TRUNC(registration_date, WEEK)) AS week,
  SUM(registrations)  AS registrations,
  SUM(conversions)    AS ftds,
  ROUND(SUM(conversions) / NULLIF(SUM(registrations), 0) * 100, 2) AS ftd_rate
FROM `michael_experiments.registration_cohort_dashboard`
WHERE registration_date >= '2026-04-27'
GROUP BY week
ORDER BY week DESC
LIMIT 5
"""

Q_ARPU = """
WITH regs AS (
  SELECT user_id, MIN(event_timestamp) AS reg_time
  FROM `processing_data.flat_registration_events`
  WHERE event_timestamp >= TIMESTAMP('2026-04-01')
  GROUP BY user_id
),
d7_logins AS (
  SELECT DISTINCT r.user_id,
    FORMAT_TIMESTAMP('%Y-%m-%d', DATE_TRUNC(r.reg_time, WEEK)) AS cohort_week
  FROM regs r
  JOIN `processing_data.flat_login_events` l
    ON  l.user_id = r.user_id
    AND l.event_timestamp >= r.reg_time
    AND l.event_timestamp <  TIMESTAMP_ADD(r.reg_time, INTERVAL 7 DAY)
),
d7_rev AS (
  SELECT
    r.user_id,
    FORMAT_TIMESTAMP('%Y-%m-%d', DATE_TRUNC(r.reg_time, WEEK)) AS cohort_week,
    COALESCE(SUM(CAST(p.amount_usd AS FLOAT64)), 0) AS rev
  FROM regs r
  LEFT JOIN `processing_data.flat_purchase_events` p
    ON  p.user_id = r.user_id
    AND p.event_timestamp >= r.reg_time
    AND p.event_timestamp <  TIMESTAMP_ADD(r.reg_time, INTERVAL 7 DAY)
  GROUP BY r.user_id, cohort_week
)
SELECT
  dr.cohort_week                                                   AS week,
  COUNT(DISTINCT dr.user_id)                                       AS registrations,
  COUNT(DISTINCT l.user_id)                                        AS d7_logged_in,
  ROUND(SUM(dr.rev), 2)                                            AS d7_revenue,
  ROUND(SUM(dr.rev) / NULLIF(COUNT(DISTINCT l.user_id), 0), 2)    AS d7_arpu
FROM d7_rev dr
LEFT JOIN d7_logins l ON l.user_id = dr.user_id AND l.cohort_week = dr.cohort_week
WHERE dr.cohort_week >= '2026-04-27'
GROUP BY dr.cohort_week
ORDER BY dr.cohort_week DESC
LIMIT 5
"""

Q_RET = """
SELECT
  cohort_week,
  MAX(playing_cohort_size) AS sz,
  MAX(CASE WHEN weeks_since_signup = 1
        THEN ROUND(playing_retention_pct * 100, 2) END) AS d7p,
  MAX(CASE WHEN weeks_since_signup = 1
        THEN ROUND(retention_pct * 100, 2) END)         AS d7pu
FROM `michael_experiments.weekly_cohort_retention`
WHERE playing_cohort_size > 100
  AND cohort_week >= '2026-W17'
GROUP BY cohort_week
HAVING MAX(CASE WHEN weeks_since_signup = 1
               THEN playing_retention_pct END) IS NOT NULL
ORDER BY cohort_week DESC
LIMIT 5
"""

# ── VIP queries ──────────────────────────────────────────────────────────────
Q_VIP_NEW = """
SELECT
  crm.user_id,
  CAST(crm.lifetime_purchases_usd AS FLOAT64)  AS ltv_usd,
  CAST(crm.net_loss_usd_lifetime   AS FLOAT64)  AS ggr_usd,
  CASE
    WHEN crm.lifetime_purchases_usd >= 1000
         AND (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) < 1000 THEN 'GOAT'
    WHEN crm.lifetime_purchases_usd >= 250
         AND (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) < 250  THEN 'Gold'
    WHEN crm.lifetime_purchases_usd >= 100
         AND (crm.lifetime_purchases_usd - crm.last_purchase_amount_usd) < 100  THEN 'VIP'
  END AS tier_crossed
FROM `goatbox-prod.processing_data.user_fact_crm` crm
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

# Fetches last 2 purchases for all new VIPs in one query (no N+1)
Q_VIP_PURCHASES = """
WITH new_vips AS (
  SELECT user_id
  FROM `goatbox-prod.processing_data.user_fact_crm`
  WHERE last_purchase_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
    AND NOT EXISTS (
      SELECT 1 FROM `goatbox-prod.processing_data.internal_users` iu
      WHERE iu.user_id = user_fact_crm.user_id
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

# ── Helpers ───────────────────────────────────────────────────────────────────
def run_query(bq: bigquery.Client, sql: str) -> list[dict]:
    rows = list(bq.query(sql).result())
    return [dict(r) for r in rows]


def fmt_pct(v, decimals=1):
    return f"{v:.{decimals}f}%" if v is not None else "—"

def fmt_usd(v):
    return f"${v:.2f}" if v is not None else "—"

def delta_pct(val, bl):
    if val is None:
        return "n/a"
    d = val - bl
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.1f}pp"

def delta_usd(val, bl):
    if val is None:
        return "n/a"
    d = val - bl
    if d >= 0:
        return f"+${d:.2f}"
    return f"-${abs(d):.2f}"

def progress_bar(val, bl, tg, width=10):
    if val is None:
        return "░" * width
    pct = max(0.0, min(1.0, (val - bl) / (tg - bl))) if tg != bl else 0.0
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled)

def status_emoji(val, bl, tg):
    if val is None:
        return "⬜"
    if val >= tg:
        return "✅"
    if val >= bl:
        return "🟡"
    return "🔴"

def campaign_progress():
    now = datetime.now(tz=timezone.utc)
    total = (CAMPAIGN_END - CAMPAIGN_START).days
    elapsed = max(0, (now - CAMPAIGN_START).days)
    remaining = max(0, (CAMPAIGN_END - now).days)
    pct = min(100, round(elapsed / total * 100))
    return elapsed, remaining, pct


# ── Slack message builder ─────────────────────────────────────────────────────
def build_blocks(ftd_rows, arpu_rows, ret_rows):
    now = datetime.now(tz=timezone.utc)
    today_str = now.strftime("%a %b %-d, %Y")
    elapsed, remaining, camp_pct = campaign_progress()

    # Latest values (rows are DESC, so index 0 is most recent)
    lF   = ftd_rows[0]["ftd_rate"]  if ftd_rows  else None
    lA   = arpu_rows[0]["d7_arpu"]  if arpu_rows else None
    lR   = ret_rows[0]["d7p"]       if ret_rows  else None
    lPU  = ret_rows[0]["d7pu"]      if ret_rows  else None

    # FTD week label
    ftd_week  = ftd_rows[0]["week"][:10]  if ftd_rows  else "—"
    arpu_week = arpu_rows[0]["week"][:10] if arpu_rows else "—"
    ret_week  = ret_rows[0]["cohort_week"] if ret_rows  else "—"

    def kpi_line(label, val, bl, tg, fmt_val, fmt_delta):
        bar   = progress_bar(val, bl, tg)
        emoji = status_emoji(val, bl, tg)
        pct_to_target = round((val - bl) / (tg - bl) * 100) if val is not None and tg != bl else 0
        pct_to_target = max(0, min(200, pct_to_target))
        return (
            f"{emoji} *{label}*: {fmt_val}  "
            f"`{bar}` {pct_to_target}% to target  "
            f"({fmt_delta} vs baseline)"
        )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 GOATBox Daily KPIs — {today_str}"
            }
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f"Day *{elapsed}* of 90 · "
                    f"*{remaining}* days remaining · "
                    f"Campaign {camp_pct}% elapsed"
                )
            }]
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join([
                    kpi_line(
                        "FTD Rate",
                        lF, BASELINES["ftd"], TARGETS["ftd"],
                        fmt_pct(lF), delta_pct(lF, BASELINES["ftd"])
                    ),
                    kpi_line(
                        "D7 ARPU",
                        lA, BASELINES["arpu"], TARGETS["arpu"],
                        fmt_usd(lA), delta_usd(lA, BASELINES["arpu"])
                    ),
                    kpi_line(
                        "D7 Playing Retention",
                        lR, BASELINES["ret"], TARGETS["ret"],
                        fmt_pct(lR), delta_pct(lR, BASELINES["ret"])
                    ),
                    kpi_line(
                        "D7 Paying Retention",
                        lPU, BASELINES["pu"], TARGETS["pu"],
                        fmt_pct(lPU), delta_pct(lPU, BASELINES["pu"])
                    ),
                ])
            }
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f"FTD data: w/o {ftd_week} · "
                    f"ARPU data: w/o {arpu_week} · "
                    f"Retention data: {ret_week} · "
                    f"_Note: current week numbers may be incomplete_"
                )
            }]
        }
    ]
    return blocks


# ── VIP Slack message builder ─────────────────────────────────────────────────
def build_vip_blocks(vip_rows, purchase_rows):
    IDT = timezone(timedelta(hours=3))

    # Group purchases by user_id
    purchases_by_user: dict[str, list] = {}
    for p in purchase_rows:
        purchases_by_user.setdefault(p["user_id"], []).append(p)

    def fmt_purchase(p):
        ts = p["event_timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(IDT)
        date_str = local.strftime("%a %b %-d, %-I:%M %p IDT")
        return f"• {date_str} — ${p['amount_usd']:.2f} ({p['product_slug'] or '—'})"

    def fmt_ggr(ggr):
        # ggr > 0: user net lost (platform profit). ggr < 0: user net won (platform loss).
        sign = "+" if ggr >= 0 else ""
        return f"{sign}${ggr:,.2f}"

    TIER_EMOJI = {"GOAT": "🐐", "Gold": "🥇", "VIP": "⭐"}

    if not vip_rows:
        return [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": "No new VIPs today."}
        }]

    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": "🆕 New VIP Entrants"}
    }]

    for row in vip_rows:
        uid   = row["user_id"]
        tier  = row["tier_crossed"]
        ltv   = row["ltv_usd"]
        ggr   = row["ggr_usd"]
        emoji = TIER_EMOJI.get(tier, "⭐")
        user_purchases = purchases_by_user.get(uid, [])
        purchase_lines = "\n".join(fmt_purchase(p) for p in user_purchases)
        if not purchase_lines:
            purchase_lines = "• No purchase records found"

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{tier}* — `{uid}`\n"
                    f"LTV: *${ltv:,.2f}*  |  Platform GGR: *{fmt_ggr(ggr)}*\n"
                    f"Last 2 purchases:\n{purchase_lines}"
                )
            }
        })
        blocks.append({"type": "divider"})

    return blocks


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    if not slack_token:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    print("Connecting to BigQuery...")
    bq = bigquery.Client()

    print("Running queries...")
    try:
        ftd_rows      = run_query(bq, Q_FTD)
        arpu_rows     = run_query(bq, Q_ARPU)
        ret_rows      = run_query(bq, Q_RET)
        vip_rows      = run_query(bq, Q_VIP_NEW)
        purchase_rows = run_query(bq, Q_VIP_PURCHASES) if vip_rows else []
    except Exception as e:
        print(f"BigQuery error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  FTD rows:      {len(ftd_rows)}")
    print(f"  ARPU rows:     {len(arpu_rows)}")
    print(f"  Ret rows:      {len(ret_rows)}")
    print(f"  New VIP rows:  {len(vip_rows)}")

    client = WebClient(token=slack_token)

    # ── Post main KPI report to #daily-report-claude ──────────────────────────
    blocks = build_blocks(ftd_rows, arpu_rows, ret_rows)
    print("Posting KPI report to #daily-report-claude...")
    try:
        resp = client.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=blocks,
            text="📊 GOATBox Daily KPIs",
            unfurl_links=False,
            unfurl_media=False,
        )
        print(f"KPI report posted. ts={resp['ts']}")
    except SlackApiError as e:
        print(f"Slack error (KPI): {e.response['error']}", file=sys.stderr)
        sys.exit(1)

    # ── Post VIP new entrants to #vip ─────────────────────────────────────────
    vip_blocks = build_vip_blocks(vip_rows, purchase_rows)
    vip_fallback = (
        f"🆕 {len(vip_rows)} new VIP entrant(s) today"
        if vip_rows else "No new VIPs today."
    )
    print("Posting VIP report to #vip...")
    try:
        resp = client.chat_postMessage(
            channel=SLACK_VIP_CHANNEL,
            blocks=vip_blocks,
            text=vip_fallback,
            unfurl_links=False,
            unfurl_media=False,
        )
        print(f"VIP report posted. ts={resp['ts']}")
    except SlackApiError as e:
        print(f"Slack error (VIP): {e.response['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
