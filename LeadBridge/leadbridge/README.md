# LeadBridge

A lead lost between form and CRM is lost revenue no one notices.

A prospect fills out a form on your site. The request times out, the CRM API
hiccups, a field validation error slips through — and the lead just vanishes.
No error page, no alert, no record. Sales never calls back. Nobody finds out
until the prospect complains, or more likely, never.

LeadBridge sits between the form and the CRM and makes that failure mode
impossible: every submission is durably stored the instant it arrives,
*before* anything is attempted with the CRM. If delivery fails, the lead is
dead-lettered with the exact error, and a one-click **Replay** in the
dashboard retries the identical delivery path once the CRM (or your fix) is
back. Nothing is ever silently dropped.

## How it works

```
                     ┌──────────────┐
  Website form  ───► │  POST /leads │  validate (pydantic) + honeypot + rate limit
                     └──────┬───────┘
                            │ store immediately
                            ▼
                     ┌──────────────┐
                     │ Lead(received)│  Postgres — the lead is now safe no matter
                     └──────┬───────┘  what happens next
                            │ background delivery
                            ▼
                     ┌──────────────┐   success    ┌───────────────┐
                     │ push to CRM  ├─────────────► │ delivered     │
                     │ (3x + backoff)│              │ + follow-up   │
                     └──────┬───────┘              │   email        │
                            │ exhausted retries      └───────────────┘
                            ▼
                     ┌──────────────┐   Replay button   ┌────────────┐
                     │ failed        │ ─────────────────►│ deliver_lead│ (same
                     │ (dead-letter) │◄───────────────────│ code path  │  path)
                     │ + last_error  │      still failing └────────────┘
                     └──────────────┘
```

The delivery function (`app/deadletter/queue.py::deliver_lead`) is the single
code path used by both the original intake attempt and the dashboard's
"Replay" button — a replay can never behave differently than the original
delivery, because it's literally the same function.

## Stack

- **Backend:** FastAPI + SQLModel (Postgres)
- **CRM:** HubSpot Contacts API (falls back to a deterministic mock transport
  when no API key is configured, so the demo runs with zero external accounts)
- **Email:** Resend (same mock fallback pattern)
- **Frontend:** React + Vite replay dashboard
- **Errors:** Sentry (optional, only initializes if `SENTRY_DSN` is set)

## Project layout

```
leadbridge/
  app/
    main.py              FastAPI app, routes, CORS, logging, Sentry init
    intake/receive.py    pydantic schema + honeypot check
    routing/to_crm.py    CRM push (HubSpot or mock), raises CRMDeliveryError
    routing/notify.py    follow-up email (Resend or mock)
    deadletter/queue.py  deliver_lead() — retry+backoff, dead-letter, replay
    models.py            Lead model
    db.py, config.py, ratelimit.py
  frontend/              Vite + React replay dashboard
  tests/                 dead-letter + replay path test
  seed.py                demo data (2 of 5 leads intentionally dead-letter)
```

## API

| Endpoint | Description |
|---|---|
| `POST /leads` | Public intake. Validates input, checks honeypot field (`website`), rate-limited per IP. Stores the lead immediately, delivers to CRM in the background. |
| `GET /leads?status=failed` | List leads, optionally filtered by `received` / `delivered` / `failed`. |
| `POST /leads/{id}/replay` | Re-run delivery for one lead through the exact same code path as the original attempt. |
| `GET /mock-crm` / `POST /mock-crm/toggle` | Demo-only. Flips the mock CRM between "outage" and "healthy" so the dead-letter → replay → delivered path can be demonstrated without a real CRM. Inert once `HUBSPOT_TOKEN` is set. |

## Production concerns already handled

- Input validation (pydantic, `EmailStr`)
- Spam: hidden honeypot field, silently accepted so bots get no signal
- Per-IP rate limiting (`RATE_LIMIT_PER_MINUTE`, default 10/min)
- Retry with exponential backoff (3 attempts) before dead-lettering
- Dead-letter queue with exact error message, replayable from the dashboard
- CORS locked to `ALLOWED_ORIGINS` (no wildcard)
- Structured (JSON) logging
- Optional Sentry error tracking

## Setup

Requires Python 3.12 and a local Postgres (both already used in this repo's
environment; see below for exact commands).

```bash
cd leadbridge

# backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

createdb leadbridge          # skip if it already exists
cp .env.example .env         # fill in DATABASE_URL if yours differs

PYTHONPATH=. python seed.py  # seeds 5 demo leads, 2 intentionally dead-letter
PYTHONPATH=. uvicorn app.main:app --port 8010

# frontend (separate terminal)
cd frontend
npm install
npm run dev                  # http://localhost:5175
```

`npm run dev` also works from the `leadbridge/` root — it forwards to
`frontend/`.

Open **http://localhost:5175** — the dashboard defaults to the "Failed" filter
so the dead-letter queue is the first thing you see.

### Demoing the whole loop in 30 seconds

1. The red banner shows the mock CRM is **simulating an outage**.
2. Submit a lead with any `@fail.dev` address — it dead-letters after 3 retries
   and appears in the Failed queue with its error.
3. Click **Replay** — it retries and honestly reports that it's still failing.
4. Click **Simulate CRM recovery**, then **Replay** again — the lead delivers
   and drops out of the dead-letter queue. Nothing was lost in between.

### Environment variables (`.env`)

All CRM/email keys are optional — omit them and the app uses deterministic
mock transports (any lead with an `@fail.dev` email address simulates a CRM
outage, which is what the seed data uses to populate the dead-letter queue).

| Variable | Required? | Where to get it |
|---|---|---|
| `DATABASE_URL` | yes | local Postgres connection string |
| `ALLOWED_ORIGINS` | yes | comma-separated list of frontend origins |
| `HUBSPOT_TOKEN` | no | HubSpot → Settings → Integrations → Private Apps |
| `RESEND_API_KEY` | no | resend.com → API Keys |
| `SENTRY_DSN` | no | sentry.io → Project → Client Keys |

## Tests

```bash
PYTHONPATH=. python -m pytest tests/ -v
```

Two unit tests cover the core trust guarantee: a lead that fails CRM delivery
is dead-lettered with its error recorded, then a replay (once the CRM is
healthy again) delivers it and clears it from the dead-letter queue; plus a
regression guard that the demo failure address survives pydantic's email
validation (reserved TLDs like `.test` are rejected by `email-validator`,
which would make the dead-letter path unreachable through the API).

There is also a full browser click-through, driven with Playwright, that
exercises the app the way a user does — form submissions, every filter tab,
the replay button, the CRM toggle, and the honeypot:

```bash
python -m playwright install chromium   # one time
# with both servers running:
python tests/e2e_clickthrough.py
```

It asserts 26 checks and writes screenshots of each step to
`tests/screenshots/`. The script reseeds the demo data and resets the mock CRM
before and after itself, so it is safe to re-run and always leaves the demo in
its intended starting state.
