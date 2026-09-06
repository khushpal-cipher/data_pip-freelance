from app.deadletter import queue as dlq
from app.models import Lead, LeadStatus


def test_failed_delivery_dead_letters_then_replay_succeeds(session, monkeypatch):
    lead = Lead(name="Priya Nair", email="priya@fail.dev", source="website")
    session.add(lead)
    session.commit()
    session.refresh(lead)

    # CRM is down for this lead's domain -> should exhaust retries and dead-letter.
    result = dlq.deliver_lead(lead.id, session)
    assert result.status == LeadStatus.failed
    assert result.attempts == 3
    assert "simulated outage" in result.last_error
    assert result in dlq.list_failed_leads(session)

    # CRM comes back online -> replay should succeed and clear the dead letter.
    monkeypatch.setattr(dlq, "push_to_crm", lambda lead: None)
    replayed = dlq.deliver_lead(lead.id, session)
    assert replayed.status == LeadStatus.delivered
    assert replayed.last_error is None
    assert replayed not in dlq.list_failed_leads(session)


def test_demo_failure_address_passes_intake_validation():
    """The mock CRM's failure trigger must survive pydantic's email validation,
    otherwise POST /leads 422s and the dead-letter path is unreachable via the API.
    (Reserved TLDs like .test/.invalid are rejected by email-validator.)"""
    from app.intake.receive import LeadCreate

    lead = LeadCreate(name="Tomas Lindqvist", email="tomas@fail.dev")
    assert lead.email.endswith("@fail.dev")
