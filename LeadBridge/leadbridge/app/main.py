import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session, init_db
from app.deadletter.queue import deliver_lead, list_failed_leads
from app.intake.receive import LeadCreate
from app.models import Lead, LeadStatus
from app.ratelimit import enforce_rate_limit
from app.routing.to_crm import is_mock_crm_recovered, set_mock_crm_recovered

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
logger = logging.getLogger("leadbridge")

if settings.sentry_dsn:
    import sentry_sdk

    sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="LeadBridge", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.post("/leads", status_code=201)
def create_lead(
    lead_in: LeadCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    session: Session = Depends(get_session),
):
    enforce_rate_limit(request)

    if lead_in.is_spam:
        # Silently accept so bots get no signal their submission was rejected.
        logger.info("honeypot triggered, dropping submission")
        return {"status": "received"}

    lead = Lead(
        name=lead_in.name,
        email=lead_in.email,
        phone=lead_in.phone,
        source=lead_in.source,
        payload=lead_in.to_payload(),
        status=LeadStatus.received,
    )
    session.add(lead)
    session.commit()
    session.refresh(lead)

    background_tasks.add_task(_deliver_in_background, lead.id)
    return {"id": lead.id, "status": lead.status}


def _deliver_in_background(lead_id: int) -> None:
    from app.db import engine

    with Session(engine) as session:
        deliver_lead(lead_id, session)


@app.get("/leads")
def get_leads(status: LeadStatus | None = None, session: Session = Depends(get_session)):
    query = select(Lead).order_by(Lead.created_at.desc())
    if status is not None:
        query = query.where(Lead.status == status)
    return session.exec(query).all()


@app.post("/leads/{lead_id}/replay")
def replay_lead(lead_id: int, session: Session = Depends(get_session)):
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return deliver_lead(lead_id, session)


@app.get("/mock-crm")
def mock_crm_state():
    """Demo-only: is the mock CRM currently simulating an outage?"""
    return {"mock_mode": settings.hubspot_token is None, "recovered": is_mock_crm_recovered()}


@app.post("/mock-crm/toggle")
def mock_crm_toggle():
    """Demo-only: flip the mock CRM between 'outage' and 'recovered' so the
    dead-letter -> replay -> delivered path can be demonstrated end-to-end."""
    set_mock_crm_recovered(not is_mock_crm_recovered())
    return {"mock_mode": settings.hubspot_token is None, "recovered": is_mock_crm_recovered()}
