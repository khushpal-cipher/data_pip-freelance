"""DataPipe-Monitor API + dashboard."""
import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.checks import run_all
from app.config import CHECK_INTERVAL_SECONDS, SENTRY_DSN
from app.checks import volume
from app.db import engine, get_session, init_db
from app.models import Alert, Heartbeat, Source, Status, utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("datapipe")

if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.1)

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
SPARKLINE_DAYS = 14

scheduler = BackgroundScheduler()


def scheduled_checks() -> None:
    with Session(engine) as session:
        raised = run_all(session)
    if raised:
        log.warning("scheduler raised %d alert(s)", len(raised))


def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(
        scheduled_checks,
        "interval",
        seconds=CHECK_INTERVAL_SECONDS,
        id="checks",
        replace_existing=True,
    )
    scheduler.start()
    log.info("scheduler running every %ss", CHECK_INTERVAL_SECONDS)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="DataPipe-Monitor", lifespan=lifespan)


class SourceIn(BaseModel):
    name: str
    expected_interval_minutes: int = 60
    max_staleness_minutes: int = 180


class HeartbeatIn(BaseModel):
    row_count: int = 0


@app.get("/")
def dashboard():
    return FileResponse(FRONTEND)


@app.post("/sources")
def create_source(body: SourceIn, session: Session = Depends(get_session)):
    if session.exec(select(Source).where(Source.name == body.name)).first():
        raise HTTPException(409, f"source {body.name!r} already exists")

    source = Source(**body.model_dump())
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@app.get("/sources")
def list_sources(session: Session = Depends(get_session)):
    return session.exec(select(Source).order_by(Source.name)).all()


@app.post("/heartbeat/{source_id}")
def heartbeat(source_id: int, body: HeartbeatIn, session: Session = Depends(get_session)):
    """A pipeline reporting that it ran and how many rows it wrote."""
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "unknown source")

    now = utcnow()
    session.add(Heartbeat(source_id=source_id, row_count=body.row_count, received_at=now))
    source.last_record_at = now
    source.status = Status.healthy
    session.add(source)
    session.commit()
    return {"source": source.name, "last_record_at": now, "row_count": body.row_count}


def _sparkline(session: Session, source_id: int, now) -> list:
    """Daily row counts for the last SPARKLINE_DAYS days."""
    start = (now - timedelta(days=SPARKLINE_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    beats = session.exec(
        select(Heartbeat).where(
            Heartbeat.source_id == source_id, Heartbeat.received_at >= start
        )
    ).all()

    buckets = {(start + timedelta(days=i)).date(): 0 for i in range(SPARKLINE_DAYS + 1)}
    for beat in beats:
        day = beat.received_at.date()
        if day in buckets:
            buckets[day] += beat.row_count
    return [{"day": str(d), "rows": n} for d, n in sorted(buckets.items())]


@app.get("/health")
def health(session: Session = Depends(get_session)):
    """Everything the dashboard needs: per-source status, freshness, volume, sparkline."""
    now = utcnow()
    sources = session.exec(select(Source).order_by(Source.name)).all()
    open_alerts = session.exec(select(Alert).where(Alert.resolved == False)).all()  # noqa: E712
    by_source = {}
    for alert in open_alerts:
        by_source.setdefault(alert.source_id, []).append(alert.type.value)

    payload = []
    for source in sources:
        today, baseline, drop, _ = volume.evaluate(session, source, now)
        age = source.age_minutes(now)
        payload.append(
            {
                "id": source.id,
                "name": source.name,
                "status": source.status.value,
                "expected_interval_minutes": source.expected_interval_minutes,
                "max_staleness_minutes": source.max_staleness_minutes,
                "last_record_at": source.last_record_at,
                "age_minutes": round(age, 1) if age is not None else None,
                "rows_24h": today,
                "baseline_per_day": round(baseline, 1),
                "volume_drop_pct": round(drop * 100, 1),
                "open_alerts": by_source.get(source.id, []),
                "sparkline": _sparkline(session, source.id, now),
            }
        )

    counts = {s.value: 0 for s in Status}
    for source in sources:
        counts[source.status.value] += 1

    return {
        "checked_at": now,
        "sources": payload,
        "summary": {**counts, "open_alerts": len(open_alerts)},
    }


@app.get("/alerts")
def alerts(resolved: Optional[bool] = None, session: Session = Depends(get_session)):
    query = select(Alert).order_by(Alert.created_at.desc())
    if resolved is not None:
        query = query.where(Alert.resolved == resolved)
    rows = session.exec(query.limit(100)).all()

    names = {s.id: s.name for s in session.exec(select(Source)).all()}
    return [{**a.model_dump(), "source_name": names.get(a.source_id)} for a in rows]


@app.post("/checks/run")
def run_checks_now(session: Session = Depends(get_session)):
    """Force a check pass instead of waiting for the scheduler."""
    raised = run_all(session)
    return {"raised": [a.model_dump() for a in raised]}
