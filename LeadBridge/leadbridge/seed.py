"""Seed demo leads so the dashboard isn't empty on first run."""

from sqlmodel import Session

from app.db import engine, init_db
from app.deadletter.queue import deliver_lead
from app.models import Lead

DEMO_LEADS = [
    ("Ava Chen", "ava@acme.com", "555-0101", "website"),
    ("Marcus Webb", "marcus@brightside.io", "555-0102", "landing_page"),
    ("Priya Nair", "priya@fail.dev", "555-0103", "website"),  # will dead-letter
    ("Diego Ruiz", "diego@fail.dev", "555-0104", "referral"),  # will dead-letter
    ("Sofia Almeida", "sofia@northwind.co", "555-0105", "website"),
]


def seed() -> None:
    init_db()
    with Session(engine) as session:
        for name, email, phone, source in DEMO_LEADS:
            lead = Lead(name=name, email=email, phone=phone, source=source)
            session.add(lead)
            session.commit()
            session.refresh(lead)
            deliver_lead(lead.id, session)
    print(f"Seeded {len(DEMO_LEADS)} demo leads.")


if __name__ == "__main__":
    seed()
