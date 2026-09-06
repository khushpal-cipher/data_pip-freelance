from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import select

from app.config import get_settings
from app.db import get_session, init_db
from app.logging import configure_logging, get_logger
from app.models import SyncRecord
from app.reconcile.nightly import run_reconciliation
from app.sync import ledger
from app.webhooks.shopify import router as shopify_webhook_router
from app.worker import process_pending

configure_logging()
logger = get_logger(component="main")
settings = get_settings()

scheduler = BackgroundScheduler()


def _worker_tick() -> None:
    session = get_session()
    try:
        result = process_pending(session, settings=settings)
        if result["processed"]:
            logger.info("worker_tick", **result)
    except Exception as exc:  # noqa: BLE001 - keep the scheduler alive across failures
        logger.error("worker_tick_failed", error=str(exc))
    finally:
        session.close()


def _reconcile_tick() -> None:
    session = get_session()
    try:
        run_reconciliation(session, settings=settings)
    except Exception as exc:  # noqa: BLE001
        logger.error("reconcile_tick_failed", error=str(exc))
    finally:
        session.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment, traces_sample_rate=0.1)

    scheduler.add_job(
        _worker_tick,
        IntervalTrigger(seconds=settings.worker_poll_seconds),
        id="worker_tick",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _reconcile_tick,
        CronTrigger(hour=settings.reconcile_hour_utc, minute=0, timezone="UTC"),
        id="nightly_reconcile",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("startup_complete", worker_poll_seconds=settings.worker_poll_seconds)

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="SyncGuard", version="1.0.0", lifespan=lifespan)
app.include_router(shopify_webhook_router)


@app.get("/")
def root():
    return RedirectResponse("/dashboard")


@app.get("/health")
def health():
    try:
        session = get_session()
        session.exec(select(SyncRecord).limit(1))
        session.close()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")


@app.get("/sync/status")
def sync_status():
    session = get_session()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        return ledger.get_status_counts(session, since=since)
    finally:
        session.close()


@app.post("/sync/replay/{source_id}")
def sync_replay(source_id: str):
    session = get_session()
    try:
        record = ledger.replay(session, source_id)
        if not record:
            raise HTTPException(status_code=404, detail=f"no ledger record for source_id={source_id}")
        return {
            "source_id": record.source_id,
            "status": record.status,
            "attempts": record.attempts,
        }
    finally:
        session.close()


DASHBOARD_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@500;600&family=Lora:wght@400;600&display=swap');
  :root { --dark:#141413; --light:#faf9f5; --orange:#d97757; --line:#e5e2d9; }
  * { box-sizing:border-box; }
  body { margin:0; padding:40px 24px; background:var(--light); color:var(--dark);
         font-family:Lora,Georgia,serif; }
  main { max-width:860px; margin:0 auto; }
  h1 { font-family:Poppins,system-ui,sans-serif; font-size:28px; font-weight:600; margin:0 0 4px; }
  .sub { color:#6b675e; font-size:14px; margin:0 0 32px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:16px; }
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
  .pill.synced { background:#e3efe4; } .pill.failed { background:#f6ded6; color:#a03c1c; }
  footer { margin-top:28px; font-size:13px; color:#6b675e; }
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Owner-facing status page: is the sync alive, and did anything break?"""
    session = get_session()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        counts = ledger.get_status_counts(session, since=since)
        recent = session.exec(select(SyncRecord).order_by(SyncRecord.id.desc()).limit(15)).all()
    finally:
        session.close()

    last = counts["last_sync"].strftime("%d %b %Y, %H:%M UTC") if counts["last_sync"] else "never"
    rows = "".join(
        f"<tr><td>{r.source_id}</td><td>{r.kind.value}</td>"
        f"<td><span class='pill {r.status.value}'>{r.status.value}</span></td>"
        f"<td>{r.destination_id or '—'}</td><td>{r.attempts}</td>"
        f"<td>{(r.last_error or '—')[:70]}</td></tr>"
        for r in recent
    ) or "<tr><td colspan='6'>No orders synced yet.</td></tr>"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SyncGuard status</title><meta http-equiv="refresh" content="30"><style>{DASHBOARD_CSS}</style></head>
<body><main>
  <h1>SyncGuard</h1>
  <p class="sub">Shopify → QuickBooks Online · last sync {last}</p>
  <div class="cards">
    <div class="card"><div class="label">Synced today</div><div class="value">{counts['synced_today']}</div></div>
    <div class="card"><div class="label">Waiting</div><div class="value">{counts['pending']}</div></div>
    <div class="card{' alert' if counts['failed'] else ''}"><div class="label">Needs attention</div>
      <div class="value">{counts['failed']}</div></div>
  </div>
  <h2>Recent activity</h2>
  <div class="scroll"><table>
    <tr><th>Shopify ID</th><th>Type</th><th>Status</th><th>QuickBooks ID</th><th>Attempts</th><th>Last error</th></tr>
    {rows}
  </table></div>
  <footer>Refreshes every 30s. Reconciliation runs nightly at {settings.reconcile_hour_utc:02d}:00 UTC and emails on any mismatch.</footer>
</main></body></html>"""
