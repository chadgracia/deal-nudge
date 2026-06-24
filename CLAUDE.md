# deal-nudge — project memory

Single AWS Lambda (Python) that sends a deal's primary contact a warm "we've seen
activity, please confirm/update" nudge email. Invoked from a per-deal "bell" link on the
trades book and the daily brief. Fronted by a Lambda Function URL (Auth NONE). Deployed by
a GitHub Action on push to `main`.

## Build & deploy
- No build step. The whole function is `lambda_function.py` (stdlib + `boto3`, which the
  Lambda runtime provides).
- Deploy: commit to `main`; `.github/workflows/deploy.yml` zips `lambda_function.py` and
  runs `aws lambda update-function-code --function-name deal-nudge`. No manual deploy step.
- The `--function-name` must stay exactly `deal-nudge` — it targets the live function.
- The deploy only updates **code**, never configuration. Env vars, IAM, and the Function
  URL are managed in AWS, not in this repo.
- Sanity-check before committing: `python3 -c "import ast; ast.parse(open('lambda_function.py').read())"`.

## Request model (the load-bearing safety property)
The Function URL is unauthenticated, so treat every request as untrusted and keep GET safe:
- **GET** `?deal_id=<id>&key=<NUDGE_KEY>` → renders an HTML confirmation page and sends
  **nothing**. Link scanners, browser prefetch, and chat/email unfurlers all fetch with
  GET and never submit forms, so they can no longer trigger a send.
- **POST** (from the confirmation page's "Send the request" button) → runs the send logic.
- Never reintroduce a side effect on GET. A send-on-GET endpoint here once mailed every
  deal on the book in one scanner walk; that is the bug this design exists to prevent.
- Every call logs `method / sourceIp / userAgent / x-forwarded-for / deal_id` for forensics.

## Configuration (env vars, set on the Lambda — not in this repo)
- `HMAC_SECRET` — signs the per-deal token in the update-form link.
- `FORM_LAMBDA_URL` — base URL of the downstream update-form Lambda.
- `NUDGE_KEY` — shared secret required on every request (`key=` query param).
- `TEST_EMAIL_OVERRIDE` (optional) — when set, **every** nudge goes to this address instead
  of the real contact, the 7-day resend guard is bypassed, and no S3 lock is written. Clear
  it to go live. The burst-safety incident happened while this was set, which is the only
  reason no client was emailed.

## Behavior notes
- Resend guard: one nudge per deal per `RESEND_GUARD_DAYS` (7), enforced by a per-deal lock
  object in S3 (`gracia-deal-qa` bucket, `nudge-locks/` prefix). The guard is **per-deal**,
  so a single top-to-bottom walk of the whole book hits each deal once and the guard never
  trips — it is not a defense against bulk fan-out.
- CRM data comes from Pipeline CRM (`PIPELINE_BASE`), authenticated with a JWT read from the
  `pipeline-token` S3 bucket (`pipeline-jwt.json`).
- Email is sent via SES (`us-east-1`) from `SES_SENDER`; contact questions route to `CHAD_EMAIL`.

## Conventions
- Keep everything in the single `lambda_function.py`; no external deps beyond `boto3`.
- All HTML returned to the browser must `html.escape()` any CRM-derived value (contact name,
  company) — it is untrusted input.
- Edit-only-what's-asked: the email wording lives in `NUDGE_SUBJECT` / `NUDGE_BODY` and the
  confirmation copy in `_confirm_page`; change those rather than restructuring the handler.

## Known open items (not yet done)
- Per-deal token is HMAC over `deal_id` only — no expiry, infinitely reusable; the shared
  static `NUDGE_KEY` is still sprayed across every bell URL in the page HTML.
- No single-use enforcement, so a refresh/double-submit can re-send.
- The downstream form Lambda's GET-safety has not been independently confirmed from here.
