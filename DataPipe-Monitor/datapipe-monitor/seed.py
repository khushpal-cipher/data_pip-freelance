"""Fill the database with 14 days of plausible pipeline activity so the dashboard has
something to show: four healthy sources, one stale, one down, one silently under-writing.

    python seed.py
"""
import random
from datetime import timedelta

from sqlmodel import Session, delete, select

from app.checks import run_all
from app.db import engine, init_db
from app.models import Alert, Heartbeat, Source, utcnow

random.seed(7)
DAYS = 14

# name, interval_min, sla_min, rows/day, minutes since last record, recent-volume multiplier
SOURCES = [
    ("shopify_orders", 15, 45, 1400, 6, 1.0),
    ("stripe_payouts", 60, 180, 320, 22, 1.0),
    ("warehouse_inventory", 30, 90, 850, 140, 1.0),      # stale: past its 90m SLA
    ("marketing_events", 5, 20, 5200, 1580, 1.0),        # failing: silent for a day
    ("crm_contacts", 1440, 2880, 900, 300, 0.08),        # heartbeat fine, rows collapsed
    ("support_tickets", 60, 180, 210, 41, 1.0),
]


def seed() -> None:
    init_db()
    now = utcnow()

    with Session(engine) as session:
        session.exec(delete(Alert))
        session.exec(delete(Heartbeat))
        session.exec(delete(Source))
        session.commit()

        for name, interval, sla, rows_per_day, minutes_ago, recent_multiplier in SOURCES:
            source = Source(
                name=name,
                expected_interval_minutes=interval,
                max_staleness_minutes=sla,
                last_record_at=now - timedelta(minutes=minutes_ago),
                created_at=now - timedelta(days=DAYS + 1),
            )
            session.add(source)
            session.commit()
            session.refresh(source)

            # One heartbeat per expected interval, capped so seeding stays quick.
            per_day = max(1, min(24, 1440 // interval))
            for day in range(DAYS, 0, -1):
                multiplier = recent_multiplier if day == 1 else 1.0
                day_rows = int(rows_per_day * multiplier * random.uniform(0.85, 1.15))
                for i in range(per_day):
                    received = now - timedelta(days=day) + timedelta(
                        minutes=int(1440 * (i + 1) / (per_day + 1))
                    )
                    if received > now - timedelta(minutes=minutes_ago):
                        continue  # nothing after this source's last real delivery
                    session.add(
                        Heartbeat(
                            source_id=source.id,
                            row_count=max(0, day_rows // per_day),
                            received_at=received,
                        )
                    )
            session.commit()

        raised = run_all(session, now)
        rows = session.exec(select(Source).order_by(Source.id)).all()
        listing = [(s.id, s.name, s.status.value) for s in rows]

    print(f"seeded {len(listing)} sources, {DAYS} days of heartbeats, {len(raised)} alert(s) raised\n")
    for source_id, name, status in listing:
        print(f"  id={source_id:<4} {name:<22} {status}")


if __name__ == "__main__":
    seed()
