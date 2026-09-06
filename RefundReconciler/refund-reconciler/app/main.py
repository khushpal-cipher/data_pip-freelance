from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import select

from app import ledger
from app.celery_app import celery_app
from app.config import get_settings
from app.db import get_session, init_db
from app.logging import configure_logging, get_logger
from app.models import RefundRecord
from app.tasks.reconcile import reconcile_refund_task
from app.webhooks.shopify import router as shopify_webhook_router
from app.webhooks.stripe import router as stripe_webhook_router

configure_logging()
logger = get_logger(component="main")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment, traces_sample_rate=0.1)

    logger.info("startup_complete", environment=settings.environment)
    yield


app = FastAPI(title="RefundReconciler", version="1.0.0", lifespan=lifespan)
app.include_router(stripe_webhook_router)
app.include_router(shopify_webhook_router)


@app.get("/")
def root():
    return RedirectResponse("/dashboard")


@app.get("/health")
def health():
    try:
        session = get_session()
        session.exec(select(RefundRecord).limit(1))
        session.close()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")

    try:
        celery_app.broker_connection().ensure_connection(max_retries=1)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"broker unavailable: {exc}")

    return {"status": "ok"}


@app.get("/reconcile/status")
def reconcile_status():
    session = get_session()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        return ledger.get_status_counts(session, since=since)
    finally:
        session.close()


@app.post("/reconcile/replay/{refund_id}")
def reconcile_replay(refund_id: str):
    session = get_session()
    try:
        record = ledger.replay(session, refund_id)
        if not record:
            raise HTTPException(status_code=404, detail=f"no ledger record for refund_id={refund_id}")
        reconcile_refund_task.delay(record.id)
        return {"refund_id": record.refund_id, "status": record.status, "attempts": record.attempts}
    finally:
        session.close()


DASHBOARD_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@500;600&family=Lora:wght@400;600&display=swap');
  :root { --dark:#141413; --light:#faf9f5; --orange:#d97757; --line:#e5e2d9; }
  * { box-sizing:border-box; }
  body { margin:0; padding:40px 24px; background:var(--light); color:var(--dark);
         font-family:Lora,Georgia,serif; }
  main { max-width:900px; margin:0 auto; }
  h1 { font-family:Poppins,system-ui,sans-serif; font-size:28px; font-weight:600; margin:0 0 4px; }
  .sub { color:#6b675e; font-size:14px; margin:0 0 32px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:16px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:20px; }
  .card .label { font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:#6b675e; }
  .card .value { font-family:Poppins,system-ui,sans-serif; font-size:32px; font-weight:600; margin-top:6px; }
  .card.alert { border-color:var(--orange); }
  .card.alert .value { color:var(--orange); }
  h2 { font-family:Poppins,system-ui,sans-serif; font-size:16px; font-weight:600; margin:36px 0 12px; }
  .scroll { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line);
          border-radius:10px; font-size:14px; }
  th,td { text-align:left; padding:10px 14px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { font-family:Poppins,system-ui,sans-serif; font-size:11px; letter-spacing:.06em;
       text-transform:uppercase; color:#6b675e; font-weight:500; }
  tr:last-child td { border-bottom:none; }
  .pill { font-family:Poppins,system-ui,sans-serif; font-size:11px; padding:2px 9px; border-radius:99px;
          background:#eeece4; }
  .pill.synced { background:#e3efe4; }
  .pill.failed, .pill.over_refund { background:#f6ded6; color:#a03c1c; }
  .pill.unmatched { background:#f6ecd6; color:#8a5a10; }
  footer { margin-top:28px; font-size:13px; color:#6b675e; }
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Owner-facing status page: which refunds synced, which need a human."""
    session = get_session()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        counts = ledger.get_status_counts(session, since=since)
        recent = session.exec(select(RefundRecord).order_by(RefundRecord.id.desc()).limit(15)).all()
    finally:
        session.close()

    last = counts["last_sync"].strftime("%d %b %Y, %H:%M UTC") if counts["last_sync"] else "never"
    needs_attention = counts["failed"] + counts["unmatched"] + counts["over_refund"]
    rows = "".join(
        f"<tr><td>{r.refund_id}</td><td>{r.source_system}</td>"
        f"<td>{r.amount} {r.currency}</td>"
        f"<td>{f'{r.refund_portion:.1%}' if r.refund_portion is not None else '—'}</td>"
        f"<td><span class='pill {r.status.value}'>{r.status.value}</span></td>"
        f"<td>{r.dest_id or '—'}</td>"
        f"<td>{(r.last_error or '—')[:60]}</td></tr>"
        for r in recent
    ) or "<tr><td colspan='7'>No refunds processed yet.</td></tr>"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RefundReconciler status</title><meta http-equiv="refresh" content="30"><style>{DASHBOARD_CSS}</style></head>
<body><main>
  <h1>RefundReconciler</h1>
  <p class="sub">Stripe + Shopify → QuickBooks Online · last sync {last}</p>
  <div class="cards">
    <div class="card"><div class="label">Synced today</div><div class="value">{counts['synced_today']}</div></div>
    <div class="card"><div class="label">Waiting</div><div class="value">{counts['pending']}</div></div>
    <div class="card{' alert' if needs_attention else ''}"><div class="label">Needs attention</div>
      <div class="value">{needs_attention}</div></div>
  </div>
  <h2>Recent refunds</h2>
  <div class="scroll"><table>
    <tr><th>Refund ID</th><th>Source</th><th>Amount</th><th>Portion</th><th>Status</th><th>QBO ID</th><th>Last error</th></tr>
    {rows}
  </table></div>
  <footer>Refreshes every 30s. Unmatched or over-refund rows need a human; failed rows are dead-lettered and can be replayed via POST /reconcile/replay/{{refund_id}}.</footer>
</main></body></html>"""
