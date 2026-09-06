import logging

import httpx

from app.config import settings
from app.models import Lead

logger = logging.getLogger("leadbridge.crm")

# Demo affordance: with no HUBSPOT_TOKEN the mock CRM fails @fail.dev addresses
# forever, so a replay could never be seen succeeding. This flag simulates
# "the CRM came back / we fixed the record" so the recovery path is clickable.
# Ignored entirely when a real HUBSPOT_TOKEN is configured.
_mock_crm_recovered = False


def set_mock_crm_recovered(value: bool) -> None:
    global _mock_crm_recovered
    _mock_crm_recovered = value


def is_mock_crm_recovered() -> bool:
    return _mock_crm_recovered


class CRMDeliveryError(Exception):
    pass


def push_to_crm(lead: Lead) -> None:
    """Push a lead to the configured CRM. Raises CRMDeliveryError on failure."""
    if settings.hubspot_token:
        _push_to_hubspot(lead)
    else:
        _push_to_mock_crm(lead)


def _push_to_hubspot(lead: Lead) -> None:
    try:
        resp = httpx.post(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            headers={"Authorization": f"Bearer {settings.hubspot_token}"},
            json={
                "properties": {
                    "email": lead.email,
                    "firstname": lead.name,
                    "phone": lead.phone or "",
                }
            },
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise CRMDeliveryError(str(exc)) from exc


def _push_to_mock_crm(lead: Lead) -> None:
    """No HubSpot token configured: simulate a CRM.

    Deterministic failure hook for demo/tests: any lead with an
    @fail.dev email address simulates a CRM outage.
    """
    if _mock_crm_recovered:
        logger.info("mock CRM (recovered): stored contact", extra={"lead_id": lead.id})
        return
    if lead.email.endswith("@fail.dev"):
        raise CRMDeliveryError("mock CRM: simulated outage for @fail.dev addresses")
    logger.info("mock CRM: stored contact", extra={"lead_id": lead.id, "email": lead.email})
