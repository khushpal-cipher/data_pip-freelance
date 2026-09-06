"""The core feature: an SLA breach raises exactly one alert, and recovery closes it."""
from datetime import timedelta

from sqlmodel import select

from app.checks import freshness, run_all, volume
from app.models import Alert, AlertType, Heartbeat, Source, Status, utcnow


def make_source(session, **kw):
    source = Source(
        name=kw.pop("name", "orders_etl"),
        expected_interval_minutes=kw.pop("expected_interval_minutes", 60),
        max_staleness_minutes=kw.pop("max_staleness_minutes", 180),
        **kw,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def open_alerts(session, type_=None):
    query = select(Alert).where(Alert.resolved == False)  # noqa: E712
    if type_:
        query = query.where(Alert.type == type_)
    return session.exec(query).all()


def test_fresh_source_stays_healthy(session):
    now = utcnow()
    make_source(session, last_record_at=now - timedelta(minutes=30))

    freshness.run(session, now)

    assert session.exec(select(Source)).first().status == Status.healthy
    assert open_alerts(session) == []


def test_sla_breach_raises_one_alert_and_recovery_resolves_it(session):
    now = utcnow()
    source = make_source(session, last_record_at=now - timedelta(minutes=200))

    freshness.run(session, now)
    session.refresh(source)
    assert source.status == Status.stale
    assert len(open_alerts(session, AlertType.stale_data)) == 1

    # Still breaching on the next pass — must not raise a second alert.
    freshness.run(session, now + timedelta(minutes=5))
    assert len(open_alerts(session, AlertType.stale_data)) == 1

    # Deeply late is failing, not merely stale.
    freshness.run(session, now + timedelta(minutes=200))
    session.refresh(source)
    assert source.status == Status.failing

    # Data lands again -> healthy, and the alert closes.
    source.last_record_at = now + timedelta(minutes=200)
    session.add(source)
    session.commit()
    freshness.run(session, now + timedelta(minutes=201))

    session.refresh(source)
    assert source.status == Status.healthy
    assert open_alerts(session, AlertType.stale_data) == []
    assert session.exec(select(Alert)).first().resolved is True


def test_never_seen_source_fails_once_past_its_sla(session):
    now = utcnow()
    source = make_source(session, max_staleness_minutes=60)
    source.created_at = now - timedelta(minutes=120)
    session.add(source)
    session.commit()

    freshness.run(session, now)

    session.refresh(source)
    assert source.status == Status.failing
    assert "no data ever received" in open_alerts(session)[0].message


def test_volume_drop_against_rolling_average(session):
    now = utcnow()
    source = make_source(session, last_record_at=now - timedelta(minutes=5))

    # 7 full days of 1000 rows/day before the trailing 24h, then 100 rows in it: 90% down.
    for day in range(1, 8):
        session.add(
            Heartbeat(source_id=source.id, row_count=1000, received_at=now - timedelta(days=day, hours=1))
        )
    session.add(
        Heartbeat(source_id=source.id, row_count=100, received_at=now - timedelta(hours=2))
    )
    session.commit()

    today, baseline, drop, message = volume.evaluate(session, source, now)
    assert (today, baseline) == (100, 1000)
    assert round(drop, 2) == 0.9
    assert "volume down 90%" in message

    run_all(session, now)
    assert len(open_alerts(session, AlertType.volume_drop)) == 1
    # Heartbeat is recent, so freshness is fine — the volume check caught what it can't see.
    session.refresh(source)
    assert source.status == Status.healthy


def test_normal_volume_raises_nothing(session):
    now = utcnow()
    source = make_source(session, last_record_at=now - timedelta(minutes=5))
    for day in range(1, 8):
        session.add(
            Heartbeat(source_id=source.id, row_count=1000, received_at=now - timedelta(days=day, hours=1))
        )
    session.add(
        Heartbeat(source_id=source.id, row_count=950, received_at=now - timedelta(hours=2))
    )
    session.commit()

    run_all(session, now)
    assert open_alerts(session) == []
