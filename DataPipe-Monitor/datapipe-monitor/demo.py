"""DataPipe-Monitor end to end, in one process, against a real database.

    python demo.py

No server, no network, no credentials. Six scenes: a healthy pipeline, an SLA breach that
raises exactly one alert however many times it's checked, escalation to failing, recovery
that closes the alert, and the silent failure freshness can't see — heartbeats still
arriving while the row count collapses.
"""
import logging
import os
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/demo.db")
logging.disable(logging.WARNING)  # the scenes narrate; the alert logger would double up

from datetime import timedelta  # noqa: E402

from sqlmodel import Session, select  # noqa: E402

from app.checks import freshness, run_all, volume  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import Alert, Heartbeat, Source, utcnow  # noqa: E402

WIDTH = 78


def scene(n, title, subtitle=""):
    print("\n" + "─" * WIDTH)
    print(f"SCENE {n} — {title}")
    if subtitle:
        print(subtitle)
    print()


def show(session, now, label=""):
    for source in session.exec(select(Source)).all():
        age = source.age_minutes(now)
        age_txt = f"{age:>5.0f}m ago" if age is not None else "     never"
        opens = session.exec(
            select(Alert).where(Alert.source_id == source.id, Alert.resolved == False)  # noqa: E712
        ).all()
        flags = ", ".join(a.type.value for a in opens) or "-"
        print(f"  {source.name:<22} {source.status.value:<8} last record {age_txt}   open: {flags}")
    if label:
        print(f"  {label}")


def beat(session, source, at, rows):
    session.add(Heartbeat(source_id=source.id, row_count=rows, received_at=at))
    source.last_record_at = at
    session.add(source)
    session.commit()


def main():
    init_db()
    now = utcnow()

    with Session(engine) as session:
        for table in (Alert, Heartbeat, Source):
            for row in session.exec(select(table)).all():
                session.delete(row)
        session.commit()

        print("DataPipe-Monitor — SLA freshness + volume anomaly detection")
        print("Live run against a real database. No network calls.")

        scene(1, "Two sources register their SLA",
              "Each declares how often it should deliver and how late is too late.")
        orders = Source(name="shopify_orders", expected_interval_minutes=15,
                        max_staleness_minutes=45, created_at=now - timedelta(days=9))
        crm = Source(name="crm_contacts", expected_interval_minutes=60,
                     max_staleness_minutes=180, created_at=now - timedelta(days=9))
        session.add(orders)
        session.add(crm)
        session.commit()
        session.refresh(orders)
        session.refresh(crm)
        for source in (orders, crm):
            print(f"  {source.name:<22} every {source.expected_interval_minutes:>4}m   "
                  f"SLA {source.max_staleness_minutes}m")

        scene(2, "Eight days of normal deliveries",
              "Both pipelines run on schedule. This is the baseline every check compares to.")
        for day in range(8, -1, -1):
            for hour in (2, 8, 14, 20):
                at = now - timedelta(days=day, hours=hour)
                if at > now:
                    continue
                beat(session, orders, at, 300)
                beat(session, crm, at, 60)
        beat(session, orders, now - timedelta(minutes=10), 300)
        beat(session, crm, now - timedelta(minutes=30), 60)
        run_all(session, now)
        show(session, now)

        scene(3, "shopify_orders stops delivering",
              "60 minutes of silence against a 45-minute SLA. One alert, not a storm:")
        later = now + timedelta(minutes=60)
        run_all(session, later)
        show(session, later)
        for i in range(1, 4):
            run_all(session, later + timedelta(minutes=i))
        open_stale = session.exec(
            select(Alert).where(Alert.source_id == orders.id, Alert.resolved == False)  # noqa: E712
        ).all()
        print(f"\n  4 check passes while breaching -> {len(open_stale)} open alert")
        print(f"  \"{open_stale[0].message}\"")
        assert len(open_stale) == 1

        scene(4, "Still nothing, two hours later",
              "Past 2x the SLA a source isn't late, it's down. Status escalates on its own.")
        much_later = now + timedelta(minutes=140)
        run_all(session, much_later)
        session.refresh(orders)
        show(session, much_later)
        assert orders.status.value == "failing"

        scene(5, "The backfill lands", "Recovery closes the alert without anyone clicking anything.")
        recovered = much_later + timedelta(minutes=1)
        beat(session, orders, recovered, 1200)
        run_all(session, recovered)
        session.refresh(orders)
        show(session, recovered)
        closed = session.exec(select(Alert).where(Alert.resolved == True)).all()  # noqa: E712
        print(f"\n  alerts auto-resolved: {len(closed)}")
        assert orders.status.value == "healthy" and len(closed) == 1

        scene(6, "The silent failure — crm_contacts keeps its heartbeat",
              "An upstream filter breaks. The job still runs on time, it just writes almost\n"
              "nothing. Freshness sees a healthy source; the volume check sees the truth.")
        quiet = recovered + timedelta(hours=24)
        for hour in range(1, 25):
            at = recovered + timedelta(hours=hour)
            beat(session, crm, at, 2)          # the job still runs, hourly, on time
            if hour % 6 == 0:
                beat(session, orders, at, 300)  # orders keeps writing its usual volume
        status, _ = freshness.evaluate(crm, quiet)
        today, baseline, drop, message = volume.evaluate(session, crm, quiet)
        run_all(session, quiet)
        session.refresh(crm)
        print(f"  freshness says:  {status.value}  (delivered on the hour, every hour — nothing to see)")
        print(f"  volume says:     {today} rows in 24h vs {baseline:.0f}/day baseline "
              f"-> {drop * 100:.0f}% down")
        print(f"\n  alert raised:    \"{message}\"")
        assert status.value == "healthy" and message

        print("\n" + "─" * WIDTH)
        print("FINAL STATE")
        print()
        show(session, quiet)
        alerts = session.exec(select(Alert).order_by(Alert.created_at)).all()
        print(f"\n  {len(alerts)} alert(s) total, "
              f"{sum(1 for a in alerts if a.resolved)} resolved, "
              f"{sum(1 for a in alerts if not a.resolved)} open")
        print("\nEvery assertion above passed.\n")


if __name__ == "__main__":
    main()
