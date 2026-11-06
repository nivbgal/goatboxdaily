"""HeroRips (HRP) daily report.

Posts a standalone HRP message to the daily report channel, separate from the GOATbox
report. Metrics follow Quentin's HRP asks (2026-11-05): onboarding funnel, ARPPU, CVR,
CAC, W0/W1/W2, APD and packs per payer.

Definitions
- Net revenue: completed purchases minus settled USD refunds, same as the GBX report.
- CVR: payers / DAU for the day; registration to payer conversion lives in the funnel.
- APD (average purchase days): distinct UTC days with a purchase, averaged over every
  HRP payer up to the report date.
- Packs per payer: packs opened (boxes_opened_count) by paying users, divided by them.
- Retention: Quentin's definition, as in the GBX report. Registered in a Mon-Sun UTC
  week; week 0 = active on days 1-6 after registering, week 1 = 7-13, week 2 = 14-20.
- CAC: not available until HRP ad spend reaches BigQuery. Only the GOATbox Meta account
  is synced and processing_data.ad_account_brand is empty.
"""
import os
import re
import sys
import datetime
from google.cloud import bigquery
from slack_sdk import WebClient

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
CHANNEL_ID    = "C0B3KS5KNTC"
ERROR_USER_ID = "U0B0ZF5D6F9"

BRAND    = "herorips"
LAUNCH   = "2026-10-22"  # first HRP registration
PROJECT  = "goatbox-prod.processing_data"
INTERNAL = f"`{PROJECT}.internal_users`"
EVENTS   = "`goatbox-prod.source_data.events`"  # no brand column; always joined to HRP users
TREND_DAYS = 7

slack = WebClient(token=SLACK_TOKEN)
bq    = bigquery.Client()
DATE  = os.environ.get("REPORT_DATE") or (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

# Every processing_data.flat_* reference becomes a brand-filtered subquery.
FLAT_TABLE = re.compile(r"`goatbox-prod\.processing_data\.flat_\w+`")

def q(sql):
    sql = FLAT_TABLE.sub(lambda m: f"(SELECT * FROM {m.group(0)} WHERE brand = '{BRAND}')", sql)
    sql = sql.replace("DATE_FILTER", DATE)
    return [dict(r) for r in bq.query(sql).result()]

def not_internal(alias=""):
    return f"{alias}user_id NOT IN (SELECT user_id FROM {INTERNAL} WHERE user_id IS NOT NULL)"

def send_error(msg):
    try:
        slack.chat_postMessage(channel=ERROR_USER_ID, text=f":warning: *HRP daily report failed ({DATE})*\n{msg}")
    except Exception:
        print(f"[send_error] Slack notification failed. Original error: {msg}", file=sys.stderr)

def usd(v):
    v = float(v or 0)
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"

def pct(part, whole):
    return f"{part / whole * 100:.0f}%" if whole else "n/a"

def ratio(a, b, fmt="{:.1f}"):
    return fmt.format(a / b) if b else "n/a"

def delta(today, avg):
    if avg <= 0:
        return ""
    if avg < 1:  # % change off a near-zero base is noise while HRP is this small
        return f" (7d avg {avg:,.1f})"
    shown = f"{avg:,.1f}" if avg < 100 else f"{avg:,.0f}"
    return f" (7d avg {shown}, {(today - avg) / avg * 100:+.0f}%)"

def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")

def clean_slug(s):
    return (s or "unknown").replace("-shop-item", "").replace("-", " ").title()

WINDOW = f"DATE(event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'"

# ── Queries ───────────────────────────────────────────────────────────────────
try:
    # Daily spine for the trailing 7 days: revenue, payers, DAU, registrations, packs.
    trend = q(f"""
        WITH spine AS (
          SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(
            DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY), DATE 'DATE_FILTER')) d
        ),
        pur AS (
          SELECT DATE(event_timestamp) d, COUNT(DISTINCT user_id) payers, COUNT(*) txns,
                 SUM(amount_usd) gross
          FROM `{PROJECT}.flat_purchase_events`
          WHERE {WINDOW} AND {not_internal()} GROUP BY 1
        ),
        ref AS (
          SELECT DATE(event_timestamp) d, SUM(amount) refunds
          FROM `{PROJECT}.flat_payment_refunded`
          WHERE {WINDOW} AND status = 'succeeded' AND currency = 'USD' AND {not_internal()} GROUP BY 1
        ),
        dau AS (
          SELECT DATE(event_timestamp) d, COUNT(DISTINCT user_id) dau
          FROM `{PROJECT}.flat_login_events`
          WHERE {WINDOW} AND {not_internal()} GROUP BY 1
        ),
        reg AS (
          SELECT DATE(event_timestamp) d, COUNT(DISTINCT user_id) regs
          FROM `{PROJECT}.flat_registration_events`
          WHERE {WINDOW} AND {not_internal()} GROUP BY 1
        ),
        packs AS (
          SELECT DATE(event_timestamp) d, SUM(boxes_opened_count) packs, COUNT(DISTINCT user_id) openers
          FROM `{PROJECT}.flat_box_events`
          WHERE {WINDOW} AND {not_internal()} GROUP BY 1
        ),
        payer_packs AS (
          SELECT DATE(b.event_timestamp) d, SUM(b.boxes_opened_count) packs
          FROM `{PROJECT}.flat_box_events` b
          WHERE DATE(b.event_timestamp) BETWEEN DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS - 1} DAY) AND DATE 'DATE_FILTER'
            AND {not_internal('b.')}
            AND EXISTS (SELECT 1 FROM `{PROJECT}.flat_purchase_events` p
                        WHERE p.user_id = b.user_id AND DATE(p.event_timestamp) = DATE(b.event_timestamp))
          GROUP BY 1
        )
        SELECT spine.d AS day,
               IFNULL(pur.payers, 0) payers, IFNULL(pur.txns, 0) txns,
               ROUND(IFNULL(pur.gross, 0), 2) gross, ROUND(IFNULL(ref.refunds, 0), 2) refunds,
               ROUND(IFNULL(pur.gross, 0) - IFNULL(ref.refunds, 0), 2) net,
               IFNULL(dau.dau, 0) dau, IFNULL(reg.regs, 0) regs,
               IFNULL(packs.packs, 0) packs, IFNULL(packs.openers, 0) openers,
               IFNULL(payer_packs.packs, 0) payer_packs
        FROM spine
        LEFT JOIN pur USING (d) LEFT JOIN ref USING (d) LEFT JOIN dau USING (d)
        LEFT JOIN reg USING (d) LEFT JOIN packs USING (d) LEFT JOIN payer_packs USING (d)
        ORDER BY day
    """)

    # Onboarding funnel by registration cohort. A step counts if reached by end of DATE.
    funnel = q(f"""
        WITH reg AS (
          SELECT user_id, DATE(MIN(event_timestamp)) rd
          FROM `{PROJECT}.flat_registration_events`
          WHERE {not_internal()} GROUP BY 1
          HAVING rd <= DATE 'DATE_FILTER'
        ),
        upto AS (SELECT TIMESTAMP_ADD(TIMESTAMP(DATE 'DATE_FILTER'), INTERVAL 1 DAY) AS t),
        verified AS (
          SELECT DISTINCT user_id FROM {EVENTS}
          WHERE event_name = 'email_verified' AND event_timestamp < (SELECT t FROM upto)
            AND DATE(event_timestamp) >= DATE '{LAUNCH}'
        ),
        opened AS (SELECT DISTINCT user_id FROM `{PROJECT}.flat_box_events` WHERE event_timestamp < (SELECT t FROM upto)),
        shop AS (SELECT DISTINCT user_id FROM `{PROJECT}.flat_shop_opened_events` WHERE event_timestamp < (SELECT t FROM upto)),
        started AS (SELECT DISTINCT user_id FROM `{PROJECT}.flat_purchase_initiated_events` WHERE event_timestamp < (SELECT t FROM upto)),
        walled AS (SELECT DISTINCT user_id FROM `{PROJECT}.flat_purchase_blocked_events` WHERE event_timestamp < (SELECT t FROM upto)),
        aml_ok AS (
          SELECT DISTINCT user_id FROM `{PROJECT}.flat_aml_events`
          WHERE event_name = 'aml_completed' AND aml_status = 'APPROVED' AND event_timestamp < (SELECT t FROM upto)
        ),
        paid AS (
          SELECT user_id, COUNT(DISTINCT DATE(event_timestamp)) pdays
          FROM `{PROJECT}.flat_purchase_events` WHERE event_timestamp < (SELECT t FROM upto) GROUP BY 1
        ),
        cohorts AS (
          SELECT 'yday' c, user_id FROM reg WHERE rd = DATE 'DATE_FILTER'
          UNION ALL SELECT '7d', user_id FROM reg WHERE rd > DATE_SUB(DATE 'DATE_FILTER', INTERVAL {TREND_DAYS} DAY)
          UNION ALL SELECT 'all', user_id FROM reg
        )
        SELECT c,
               COUNT(*) registered,
               COUNTIF(user_id IN (SELECT user_id FROM verified)) verified,
               COUNTIF(user_id IN (SELECT user_id FROM opened))   opened,
               COUNTIF(user_id IN (SELECT user_id FROM shop))     shop,
               COUNTIF(user_id IN (SELECT user_id FROM started))  started,
               COUNTIF(user_id IN (SELECT user_id FROM walled))   walled,
               COUNTIF(user_id IN (SELECT user_id FROM aml_ok))   aml_ok,
               COUNTIF(user_id IN (SELECT user_id FROM paid))     paid,
               COUNTIF(user_id IN (SELECT user_id FROM paid WHERE pdays >= 2)) repeat_days
        FROM cohorts GROUP BY c
    """)
    funnel = {r["c"]: r for r in funnel}

    # Lifetime payer economics up to DATE: ARPPU, APD, packs per payer, purchases per payer.
    lifetime = q(f"""
        WITH p AS (
          SELECT user_id, COUNT(*) txns, SUM(amount_usd) gross, COUNT(DISTINCT DATE(event_timestamp)) pdays
          FROM `{PROJECT}.flat_purchase_events`
          WHERE DATE(event_timestamp) <= DATE 'DATE_FILTER' AND {not_internal()} GROUP BY 1
        ),
        ref AS (
          SELECT SUM(amount) refunds FROM `{PROJECT}.flat_payment_refunded`
          WHERE DATE(event_timestamp) <= DATE 'DATE_FILTER' AND status = 'succeeded' AND currency = 'USD'
            AND {not_internal()}
        ),
        packs AS (
          SELECT user_id, SUM(boxes_opened_count) packs FROM `{PROJECT}.flat_box_events`
          WHERE DATE(event_timestamp) <= DATE 'DATE_FILTER' GROUP BY 1
        )
        SELECT COUNT(*) payers, SUM(txns) txns, ROUND(SUM(gross), 2) gross,
               ROUND(IFNULL((SELECT refunds FROM ref), 0), 2) refunds,
               SUM(pdays) pdays, COUNTIF(pdays >= 2) multi_day,
               IFNULL(SUM(packs.packs), 0) packs
        FROM p LEFT JOIN packs USING (user_id)
    """)[0]

    by_product = q(f"""
        SELECT product_slug, COUNT(*) txns, COUNT(DISTINCT user_id) payers, ROUND(SUM(amount_usd), 2) rev
        FROM `{PROJECT}.flat_purchase_events`
        WHERE DATE(event_timestamp) = 'DATE_FILTER' AND {not_internal()}
        GROUP BY 1 ORDER BY rev DESC
    """)

    # Retention by registration week, for all registrants and for payers.
    retention = q(f"""
        WITH reg AS (
          SELECT user_id, DATE(MIN(event_timestamp)) rd
          FROM `{PROJECT}.flat_registration_events`
          WHERE {not_internal()} GROUP BY 1
          HAVING rd <= DATE 'DATE_FILTER'
        ),
        payers AS (
          SELECT DISTINCT user_id FROM `{PROJECT}.flat_purchase_events`
          WHERE DATE(event_timestamp) <= DATE 'DATE_FILTER'
        ),
        act AS (
          SELECT DISTINCT e.user_id, DATE(e.event_timestamp) d
          FROM {EVENTS} e JOIN reg USING (user_id)
          WHERE e.event_name IN ('login_completed', 'boxes_opened', 'shop_opened', 'assets_sold',
                                 'purchase_initiated', 'purchase_completed', 'payment_attempt_failed',
                                 'purchase_blocked_aml_required', 'purchase_blocked_aml_rejected',
                                 'aml_submitted', 'id_verification_completed', 'proof_of_address_completed',
                                 'email_verified', 'order_created', 'basket_created', 'claim_shipping_paid',
                                 'offer_accepted', 'offer_dismissed', 'promo_code_claimed',
                                 'withdrawal_requested', 'challenge_started')
            AND DATE(e.event_timestamp) BETWEEN DATE '{LAUNCH}' AND DATE 'DATE_FILTER'
        ),
        u AS (
          SELECT r.user_id, DATE_TRUNC(r.rd, WEEK(MONDAY)) wk,
                 r.user_id IN (SELECT user_id FROM payers) is_payer,
                 LOGICAL_OR(DATE_DIFF(a.d, r.rd, DAY) BETWEEN 1 AND 6)   q0,
                 LOGICAL_OR(DATE_DIFF(a.d, r.rd, DAY) BETWEEN 7 AND 13)  q1,
                 LOGICAL_OR(DATE_DIFF(a.d, r.rd, DAY) BETWEEN 14 AND 20) q2
          FROM reg r LEFT JOIN act a USING (user_id)
          GROUP BY 1, 2, 3
        )
        SELECT wk, COUNT(*) regs, COUNTIF(q0) w0, COUNTIF(q1) w1, COUNTIF(q2) w2,
               COUNTIF(is_payer) payers, COUNTIF(is_payer AND q0) pw0,
               COUNTIF(is_payer AND q1) pw1, COUNTIF(is_payer AND q2) pw2,
               DATE_ADD(wk, INTERVAL 12 DAY) <= DATE 'DATE_FILTER' w0_done,
               DATE_ADD(wk, INTERVAL 19 DAY) <= DATE 'DATE_FILTER' w1_done,
               DATE_ADD(wk, INTERVAL 26 DAY) <= DATE 'DATE_FILTER' w2_done
        FROM u GROUP BY wk ORDER BY wk DESC
    """)
except Exception as e:
    send_error(f"BigQuery failed:\n```{e}```")
    raise

# ── Build message ─────────────────────────────────────────────────────────────
today = trend[-1]
prior = trend[:-1]
def avg(key):
    return sum(float(r[key]) for r in prior) / len(prior) if prior else 0.0

net, payers, txns, dau = float(today["net"]), int(today["payers"]), int(today["txns"]), int(today["dau"])
arppu_7d = sum(float(r["net"]) for r in trend) / max(1, sum(int(r["payers"]) for r in trend))

lines = [
    f"\U0001f9b8 *HeroRips Daily Report — {DATE}*",
    "",
    f"*{usd(net)}* net revenue{delta(net, avg('net'))}  ·  *{txns}* txns  ·  *{payers}* payers{delta(payers, avg('payers'))}",
    f"*{usd(net / payers) if payers else 'n/a'}* ARPPU (7d {usd(arppu_7d)})  ·  "
    f"*{pct(payers, dau)}* CVR (payers / DAU)  ·  *{dau}* DAU{delta(dau, avg('dau'))}",
    f"*CAC* · n/a, HRP ad spend isn't connected to BigQuery yet",
]

# Onboarding funnel
steps = [
    ("registered",  "Registered"),
    ("verified",    "Verified email"),
    ("opened",      "Opened a pack"),
    ("shop",        "Opened shop"),
    ("started",     "Started a purchase"),
    ("walled",      "Blocked at AML wall at checkout"),
    ("aml_ok",      "Passed AML (any time)"),
    ("paid",        "Purchased (reg → payer CVR)"),
    ("repeat_days", "Purchased on 2+ days"),
]
y, w, a = funnel.get("yday", {}), funnel.get("7d", {}), funnel.get("all", {})
lines += ["", f"*Onboarding Funnel* · registered yesterday / last 7d / since launch ({LAUNCH})", ""]
for key, label in steps:
    def cell(c):
        n, base = int(c.get(key, 0) or 0), int(c.get("registered", 0) or 0)
        return f"{n}" if key == "registered" else f"{n} ({pct(n, base)})"
    lines.append(f"• *{label}* · {cell(y)} / {cell(w)} / {cell(a)}")
lines.append("_A step counts if the user reached it by the end of the report day._")

# Payer economics
lt_payers = int(lifetime["payers"] or 0)
lt_net = float(lifetime["gross"] or 0) - float(lifetime["refunds"] or 0)
packs_7d = sum(int(r["payer_packs"]) for r in trend)
payer_days_7d = sum(int(r["payers"]) for r in trend)
lines += ["", "*Payer Economics*", ""]
lines.append(f"• *Packs per payer* · {ratio(int(today['payer_packs']), payers, '{:.0f}')} yesterday · "
             f"{ratio(packs_7d, payer_days_7d, '{:.0f}')} 7d · "
             f"{ratio(int(lifetime['packs'] or 0), lt_payers, '{:.0f}')} lifetime")
lines.append(f"• *APD* (avg purchase days per payer) · {ratio(int(lifetime['pdays'] or 0), lt_payers, '{:.2f}')} · "
             f"{int(lifetime['multi_day'] or 0)} of {lt_payers} payers bought on 2+ days")
lines.append(f"• *Purchases per payer* · {ratio(txns, payers)} yesterday · "
             f"{ratio(int(lifetime['txns'] or 0), lt_payers)} lifetime")
lines.append(f"• *Lifetime* · {usd(lt_net)} net from {plural(lt_payers, 'payer')} · "
             f"{usd(lt_net / lt_payers) if lt_payers else 'n/a'} LTV per payer")
lines.append(f"• *Packs opened* · {int(today['packs']):,} by {plural(int(today['openers']), 'user')} yesterday"
             f"{delta(int(today['packs']), avg('packs'))}")

# Retention
def ret(row, n, payers_only):
    base = int(row["payers"] if payers_only else row["regs"])
    kept = int(row[f"pw{n}" if payers_only else f"w{n}"])
    return f"*{kept / base * 100:.0f}%* ({kept} of {base})" if base else "n/a"

lines += ["", "*Retention* · W0 days 1-6, W1 days 7-13, W2 days 14-20 after registering", ""]
for n in (0, 1, 2):
    done = [r for r in retention if r[f"w{n}_done"]]
    if not done:
        lines.append(f"• *W{n}* · no finished registration week yet")
        continue
    r = done[0]
    lines.append(f"• *W{n}* · week of {r['wk']:%b} {r['wk'].day} · all registrants {ret(r, n, False)} · "
                 f"payers {ret(r, n, True)}")

# Product mix
lines += ["", "*Revenue by Product* (yesterday)", ""]
if by_product:
    for r in by_product:
        lines.append(f"• *{clean_slug(r['product_slug'])}* · {usd(r['rev'])} · "
                     f"{plural(int(r['txns']), 'txn')} · {plural(int(r['payers']), 'payer')}")
else:
    lines.append("• No purchases")

message = "\n".join(lines)

# ── Post ──────────────────────────────────────────────────────────────────────
try:
    resp = slack.chat_postMessage(channel=CHANNEL_ID, text=message)
    print(f"HRP report posted: https://secrethumans.slack.com/archives/{CHANNEL_ID}/p{resp['ts'].replace('.', '')}",
          flush=True)
except Exception as e:
    send_error(f"Slack post failed:\n```{e}```")
    raise
