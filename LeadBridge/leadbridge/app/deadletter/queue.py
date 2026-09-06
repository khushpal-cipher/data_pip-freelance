import logging
import time

from sqlmodel import Session, select

from app.config import settings
from app.models import Lead, LeadStatus
from app.routing.notify import send_followup_email
from app.routing.to_crm import CRMDeliveryError, push_to_crm

logger = logging.getLogger("leadbridge.deadletter")


def deliver_lead(lead_id: int, session: Session) -> Lead:
    """Attempt CRM delivery with retry + backoff. Success -> delivered + follow-up
    email. Exhausted retries -> dead-lettered as failed with last_error set.

    This is the single delivery path used by both intake and the replay endpoint,
    so replaying a lead can never behave differently than the original attempt.
    """
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise ValueError(f"lead {lead_id} not found")

    last_error = ""
    for attempt in range(1, settings.max_delivery_attempts + 1):
        lead.attempts += 1
        try:
            push_to_crm(lead)
            lead.status = LeadStatus.delivered
            lead.last_error = None
            session.add(lead)
            session.commit()
            session.refresh(lead)
            send_followup_email(lead)
            return lead
        except CRMDeliveryError as exc:
            last_error = str(exc)
            logger.warning(
                "CRM delivery attempt failed",
                extra={"lead_id": lead_id, "attempt": attempt, "error": last_error},
            )
            if attempt < settings.max_delivery_attempts:
                time.sleep(0.3 * (2 ** (attempt - 1)))

    lead.status = LeadStatus.failed
    lead.last_error = last_error
    session.add(lead)
    session.commit()
    session.refresh(lead)
    return lead


def list_failed_leads(session: Session) -> list[Lead]:
    return list(session.exec(select(Lead).where(Lead.status == LeadStatus.failed)).all())
