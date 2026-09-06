import logging

import httpx

from app.config import settings
from app.models import Lead

logger = logging.getLogger("leadbridge.notify")


def send_followup_email(lead: Lead) -> None:
    """Best-effort follow-up email. Never blocks lead delivery on failure."""
    try:
        if settings.resend_api_key:
            httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.resend_from,
                    "to": [lead.email],
                    "subject": "Thanks for reaching out",
                    "text": f"Hi {lead.name}, thanks for your interest — we'll be in touch shortly.",
                },
                timeout=10,
            ).raise_for_status()
        else:
            logger.info("mock email: follow-up sent", extra={"lead_id": lead.id, "email": lead.email})
    except httpx.HTTPError as exc:
        logger.warning("follow-up email failed", extra={"lead_id": lead.id, "error": str(exc)})
