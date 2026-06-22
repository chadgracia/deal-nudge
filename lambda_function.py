"""
deal-nudge — manual "we've seen activity, please confirm/update" nudge.

Triggered from the daily brief (section 4) via the Function URL:
    GET ...?deal_id=<id>&key=<NUDGE_KEY>

Sends the deal's primary contact a warm email with the signed update-form link.
Does NOT obsolete or modify the deal. Re-send guard: one nudge per deal per 7 days.

Testing: set env var TEST_EMAIL_OVERRIDE to your own address. While set, every
nudge goes to you instead of the real contact, the 7-day guard is bypassed, and
no lock is written — so you can test freely. Clear it to go live.
"""

import json
import os
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
import logging
from datetime import datetime, timezone
from html import escape

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Config (secrets come from env) ─────────────────────────────────────────
HMAC_SECRET         = os.environ["HMAC_SECRET"]
FORM_LAMBDA_URL     = os.environ["FORM_LAMBDA_URL"]
NUDGE_KEY           = os.environ["NUDGE_KEY"]
TEST_EMAIL_OVERRIDE = os.environ.get("TEST_EMAIL_OVERRIDE", "").strip()

SES_SENDER        = "agent@agent.graciagroup.com"
CHAD_EMAIL        = "cgracia@rainmakersecurities.com"
TOKEN_BUCKET      = "pipeline-token"
TOKEN_KEY         = "pipeline-jwt.json"
LOCK_BUCKET       = "gracia-deal-qa"
LOCK_PREFIX       = "nudge-locks/"
RESEND_GUARD_DAYS = 7
PIPELINE_BASE     = "https://api.pipelinecrm.com/api/v3"

# ── Email wording (edit these two lines freely) ────────────────────────────
NUDGE_SUBJECT = "Activity on your {company} order"
NUDGE_BODY    = ("We've seen some activity on your indication. Could you take a "
                 "moment to confirm it's still current — or update the price or "
                 "size if anything's changed?")


# ── Helpers ────────────────────────────────────────────────────────────────

def _page(status, message):
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Deal Nudge</title></head>'
        '<body style="font-family:Arial,sans-serif;max-width:480px;margin:80px auto;'
        'text-align:center;color:#222;">'
        '<p style="font-size:18px;">' + message + '</p>'
        '</body></html>'
    )
    return {"statusCode": status,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": html}


def _confirm_page(deal_id, key, contact_name, company, test_mode, contact_email):
    who = escape(contact_name or "the contact")
    co = (" re: " + escape(company)) if company else ""
    note = ('<p style="background:#fffbe6;padding:8px;font-size:12px;">'
            'Test mode: this will go to ' + escape(TEST_EMAIL_OVERRIDE) +
            ' (real recipient would be ' + escape(contact_email) + ').</p>') if test_mode else ""
    action = "?deal_id=" + escape(deal_id) + "&key=" + escape(key)
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Send nudge?</title></head>'
        '<body style="font-family:Arial,sans-serif;max-width:480px;margin:80px auto;'
        'text-align:center;color:#222;">'
        + note +
        '<p style="font-size:18px;">Send an update request to <b>' + who + '</b>' + co + '?</p>'
        '<form method="POST" action="' + action + '">'
        '<button type="submit" style="background:#1a1a1a;color:#fff;padding:12px 28px;'
        'border:none;border-radius:6px;font-size:15px;cursor:pointer;">Send the request</button>'
        '</form>'
        '<p style="margin-top:24px;font-size:12px;color:#aaa;">'
        'Nothing is sent until you press the button.</p>'
        '</body></html>'
    )
    return {"statusCode": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": html}


def make_token(deal_id):
    sig = hmac.new(HMAC_SECRET.encode(), f"{deal_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def form_url(deal_id):
    return f"{FORM_LAMBDA_URL}?deal_id={deal_id}&token={make_token(deal_id)}"


def get_jwt():
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=TOKEN_BUCKET, Key=TOKEN_KEY)
    return json.loads(obj["Body"].read())["jwt"]


def pipeline_get(endpoint, jwt):
    req = urllib.request.Request(
        f"{PIPELINE_BASE}{endpoint}",
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
        method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 500, str(e)


def recently_nudged(deal_id):
    """Return the prior sent_at string if nudged within the guard window, else None."""
    s3 = boto3.client("s3")
    try:
        obj = s3.get_object(Bucket=LOCK_BUCKET, Key=f"{LOCK_PREFIX}{deal_id}.json")
        data = json.loads(obj["Body"].read())
        last = datetime.fromisoformat(data["sent_at"])
        if (datetime.now(timezone.utc) - last).days < RESEND_GUARD_DAYS:
            return data.get("sent_at", "")
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        logger.warning(f"lock read failed for {deal_id}: {e}")
    return None


def write_lock(deal_id, email):
    s3 = boto3.client("s3")
    body = json.dumps({"deal_id": deal_id,
                       "email": email,
                       "sent_at": datetime.now(timezone.utc).isoformat()})
    s3.put_object(Bucket=LOCK_BUCKET, Key=f"{LOCK_PREFIX}{deal_id}.json",
                  Body=body.encode(), ContentType="application/json")


def send_email(to_addr, subject, link, lead=""):
    plain = f"{lead}{NUDGE_BODY}\n\nUpdate your order: {link}\n\nQuestions? {CHAD_EMAIL}"
    lead_html = ('<p style="background:#fffbe6;padding:8px;font-size:12px;">' + lead + '</p>') if lead else ''
    html = (
        '<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222;'
        'max-width:600px;margin:40px auto;line-height:1.6;">'
        + lead_html +
        '<p>' + NUDGE_BODY + '</p>'
        '<p style="margin-top:28px;">'
        '<a href="' + link + '" style="background:#1a1a1a;color:#fff;padding:10px 20px;'
        'text-decoration:none;border-radius:4px;font-size:13px;">Update your order</a>'
        '</p>'
        '<p style="margin-top:40px;font-size:11px;color:#aaa;">Questions? Contact Chad '
        'Gracia at <a href="mailto:' + CHAD_EMAIL + '" style="color:#aaa;">' + CHAD_EMAIL + '</a></p>'
        '</body></html>'
    )
    ses = boto3.client("ses", region_name="us-east-1")
    ses.send_email(
        Source=SES_SENDER,
        Destination={"ToAddresses": [to_addr]},
        Message={"Subject": {"Data": subject},
                 "Body": {"Text": {"Data": plain}, "Html": {"Data": html}}})


# ── Handler ────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    _rc = (event.get("requestContext") or {}).get("http") or {}
    _hdrs = event.get("headers") or {}
    logger.info("NUDGE_CALL method=%s ip=%s ua=%s xff=%s deal_id=%s",
                _rc.get("method"),
                _rc.get("sourceIp"),
                _rc.get("userAgent"),
                _hdrs.get("x-forwarded-for") or _hdrs.get("X-Forwarded-For"),
                (event.get("queryStringParameters") or {}).get("deal_id"))

    method = (_rc.get("method") or "GET").upper()
    params = event.get("queryStringParameters") or {}
    test_mode = bool(TEST_EMAIL_OVERRIDE)

    if not hmac.compare_digest(params.get("key") or "", NUDGE_KEY):
        return _page(403, "Not authorized.")

    deal_id = (params.get("deal_id") or "").strip()
    if not deal_id.isdigit():
        return _page(400, "Missing or invalid deal_id.")

    if not test_mode:
        prior = recently_nudged(deal_id)
        if prior:
            return _page(200, f"Already nudged within the last {RESEND_GUARD_DAYS} days "
                              f"(sent {prior[:10]}). No email sent.")

    try:
        jwt = get_jwt()
    except Exception as e:
        logger.error(f"jwt error: {e}")
        return _page(500, "Could not authenticate to the CRM.")

    status, deal = pipeline_get(f"/deals/{deal_id}.json", jwt)
    if status != 200 or not isinstance(deal, dict):
        return _page(502, f"Could not load deal {deal_id} (HTTP {status}).")
    if "deal" in deal:
        deal = deal["deal"]

    company = ((deal.get("company") or {}).get("name")
               or deal.get("company_name")
               or deal.get("name") or "")

    contact = deal.get("primary_contact") or {}
    contact_id = contact.get("id") or deal.get("primary_contact_id")
    contact_name = (contact.get("full_name")
                    or f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}".strip()
                    or "there")
    contact_email = (contact.get("email") or "").strip()

    if not contact_email and contact_id:
        pstatus, person = pipeline_get(f"/people/{contact_id}.json", jwt)
        if pstatus == 200 and isinstance(person, dict):
            if "person" in person:
                person = person["person"]
            contact_email = (person.get("email") or "").strip()
            contact_name = person.get("full_name") or person.get("name") or contact_name

    if not contact_email:
        return _page(200, f"No email on file for the contact on deal {deal_id}. Nothing sent.")

    # Safe GET: a GET only RENDERS a confirmation page — nothing is sent. The email is
    # sent only when the Send button POSTs back, so scanners/prefetch/unfurlers (which
    # fetch with GET and never submit forms) can no longer trigger a send.
    if method != "POST":
        return _confirm_page(deal_id, params.get("key") or "", contact_name, company,
                             test_mode, contact_email)

    subject = NUDGE_SUBJECT.format(company=company) if company else "Activity on your order"
    link = form_url(deal_id)

    recipient = TEST_EMAIL_OVERRIDE if test_mode else contact_email
    lead = f"[TEST — real recipient would be: {contact_email}] " if test_mode else ""

    try:
        send_email(recipient, subject, link, lead)
    except Exception as e:
        logger.error(f"SES error: {e}")
        return _page(502, "The email failed to send. Try again shortly.")

    if not test_mode:
        try:
            write_lock(deal_id, contact_email)
        except Exception as e:
            logger.warning(f"lock write failed for {deal_id}: {e}")

    tag = f" [TEST → {recipient}]" if test_mode else ""
    return _page(200, f"Sent to {contact_name} ({recipient}).{tag} ✓")
