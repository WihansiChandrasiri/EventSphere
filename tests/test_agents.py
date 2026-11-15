from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import agents
from app import database as db
from app.agents import Bus

CLOCK = 1900000000.0


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = db.make_engine(
        f"sqlite:///{tmp_path / 'test.db'}"
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(agents, "now", lambda: CLOCK)
    db.initialize()

    with db.transaction() as session:
        session.add_all([
            db.User(
                id=1,
                name="Attendee One",
                email="one@example.test",
                password_hash="unused-in-agent-tests",
                role="attendee",
            ),
            db.User(
                id=2,
                name="Attendee Two",
                email="two@example.test",
                password_hash="unused-in-agent-tests",
                role="attendee",
            ),
            db.User(
                id=3,
                name="Attendee Three",
                email="three@example.test",
                password_hash="unused-in-agent-tests",
                role="attendee",
            ),
        ])
        session.flush()

        session.add(db.Venue(
            id=1,
            name="Main Hall",
            location="Campus",
            capacity=100,
            description="Test hall",
            image="",
            hourly_rate=10000,
            private_hire=True,
        ))
        session.flush()

        session.add(db.Event(
            id=1,
            title="Test Event",
            category="Workshop",
            venue_id=1,
            start=CLOCK + 1800,
            end=CLOCK + 7200,
            description="Test event",
            status="published",
        ))
        session.flush()

        session.add(db.Tier(
            id=1,
            event_id=1,
            name="General",
            price=10000,
            quantity=1,
        ))

    yield engine
    engine.dispose()


def call(receiver, action, data=None, sender="WebApp"):
    with db.transaction() as session:
        return Bus(session).call(
            sender, receiver, action, data or {}
        )


def book(user_id=1, quantity=1):
    return call("BookingAgent", "book", {
        "user_id": user_id,
        "tier_id": 1,
        "quantity": quantity,
        "simulate_payment": True,
    })


def ticket_code():
    with Session(db.engine) as session:
        return session.scalar(select(db.Ticket)).code


def test_successful_booking_creates_payment_and_ticket(setup):
    result = book()
    assert result["performative"] == "AGREE"

    with Session(db.engine) as session:
        booking = session.scalar(select(db.Booking))
        ticket = session.scalar(select(db.Ticket))
        assert booking.payment_reference.startswith("SIM-")
        assert ticket.booking_id == booking.id
        assert len(ticket.code) >= 32


def test_booking_refuses_overselling(setup):
    assert book()["performative"] == "AGREE"
    assert book(2)["performative"] == "REFUSE"

    with Session(db.engine) as session:
        assert len(db.rows(session, db.Booking)) == 1


def test_simultaneous_last_seat_booking(setup):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(book, [1, 2]))

    assert sorted(r["performative"] for r in results) == [
        "AGREE", "REFUSE"
    ]

    with Session(db.engine) as session:
        assert sum(
            b.quantity for b in db.rows(session, db.Booking)
            if b.status == "confirmed"
        ) == 1


def test_quantity_limit(setup):
    result = book(quantity=7)
    assert result["performative"] == "REFUSE"
    assert "six" in result["reason"]


def test_double_checkin_rejected_and_attempts_persist(setup):
    book()
    code = ticket_code()

    first = call("TicketAgent", "scan", {
        "event_id": 1, "code": code,
    })
    second = call("TicketAgent", "scan", {
        "event_id": 1, "code": code,
    })

    assert first["performative"] == "AGREE"
    assert second["performative"] == "REFUSE"
    assert "Already used" in second["reason"]

    with Session(db.engine) as session:
        assert session.scalar(select(db.Ticket)).scan_attempts == 2


def test_wrong_event_rejected(setup):
    book()
    result = call("TicketAgent", "scan", {
        "event_id": 99, "code": ticket_code(),
    })

    assert result["performative"] == "REFUSE"
    assert result["reason"] == "Wrong event."


def test_cancelled_ticket_rejected(setup):
    booking = book()
    call("BookingAgent", "cancel", {
        "id": booking["id"], "user_id": 1,
    })

    result = call("TicketAgent", "scan", {
        "event_id": 1, "code": ticket_code(),
    })
    assert result["performative"] == "REFUSE"
    assert "cancelled" in result["reason"]


def test_outside_checkin_window(setup, monkeypatch):
    book()
    monkeypatch.setattr(agents, "now", lambda: CLOCK - 7200)

    result = call("TicketAgent", "scan", {
        "event_id": 1, "code": ticket_code(),
    })
    assert result["performative"] == "REFUSE"
    assert "Outside check-in" in result["reason"]


def test_cancellation_refunds_half_within_24_hours(setup):
    booking = book()
    result = call("BookingAgent", "cancel", {
        "id": booking["id"], "user_id": 1,
    })

    assert result["performative"] == "AGREE"
    assert result["refund"] == 5000


def test_event_cancellation_refunds_full(setup):
    book()

    result = call("BookingAgent", "cancel_event", {
        "id": 1, "user_id": 1,
    })
    assert result["performative"] == "AGREE"

    with Session(db.engine) as session:
        booking = session.scalar(select(db.Booking))
        assert booking.refund == booking.total
        assert booking.status == "cancelled"


def test_venue_overlap_refused(setup):
    result = call("VenueAgent", "event", {
        "user_id": 1,
        "title": "Overlapping",
        "category": "Seminar",
        "venue_id": 1,
        "start": CLOCK + 3600,
        "end": CLOCK + 8000,
        "description": "",
        "status": "published",
        "tiers": [{"name": "General", "price": 100, "quantity": 10}],
    })

    assert result["performative"] == "REFUSE"
    assert "occupied" in result["reason"]


def test_hire_conflict_proposes_usable_alternative(setup):
    result = call("VenueAgent", "hire", {
        "user_id": 1,
        "venue_id": 1,
        "start": CLOCK + 1800,
        "end": CLOCK + 3600,
        "purpose": "Club meeting",
        "guests": 10,
    })

    assert result["performative"] == "PROPOSE"
    assert result["alternatives"]
    assert result["alternatives"][0]["start"] > CLOCK


def test_routine_hire_auto_confirms(setup):
    result = call("VenueAgent", "hire", {
        "user_id": 1,
        "venue_id": 1,
        "start": CLOCK + 86400,
        "end": CLOCK + 90000,
        "purpose": "Club meeting",
        "guests": 10,
    })

    assert result["performative"] == "AGREE"
    assert result["status"] == "confirmed"
    assert result["price"] > 0


def test_large_hire_escalates(setup):
    result = call("VenueAgent", "hire", {
        "user_id": 1,
        "venue_id": 1,
        "start": CLOCK + 86400,
        "end": CLOCK + 90000,
        "purpose": "Large meeting",
        "guests": 90,
    })

    assert result["performative"] == "AGREE"
    assert result["status"] == "pending"


def test_waitlist_expiry_reoffers_to_next_person(setup, monkeypatch):
    booking = book()

    for user_id in (2, 3):
        result = call("WaitlistAgent", "join", {
            "user_id": user_id,
            "tier_id": 1,
            "quantity": 1,
        })
        assert result["performative"] == "AGREE"

    call("BookingAgent", "cancel", {
        "id": booking["id"], "user_id": 1,
    })

    with Session(db.engine) as session:
        entries = db.rows(session, db.WaitEntry)
        assert entries[0].status == "offered"
        assert entries[1].status == "waiting"

    monkeypatch.setattr(agents, "now", lambda: CLOCK + 301)
    call("WaitlistAgent", "sweep", sender="Scheduler")

    with Session(db.engine) as session:
        entries = db.rows(session, db.WaitEntry)
        assert entries[0].status == "expired"
        assert entries[1].status == "offered"


def test_waitlist_claim_is_single_use(setup):
    booking = book()

    entry = call("WaitlistAgent", "join", {
        "user_id": 2, "tier_id": 1, "quantity": 1,
    })

    call("BookingAgent", "cancel", {
        "id": booking["id"], "user_id": 1,
    })

    data = {
        "id": entry["id"],
        "user_id": 2,
        "simulate_payment": True,
    }

    first = call("WaitlistAgent", "claim", data)
    second = call("WaitlistAgent", "claim", data)

    assert first["performative"] == "AGREE"
    assert second["performative"] == "REFUSE"


def test_notification_agent_reacts_to_broadcast(setup):
    book()

    with Session(db.engine) as session:
        notifications = db.rows(session, db.Notification)
        assert len(notifications) >= 1
        assert notifications[0].user_id == 1
        assert "confirmed" in notifications[0].message

        state = session.get(db.AgentState, "NotificationAgent")
        assert state.processed >= 1


def test_conversation_logs_and_state_survive_new_session(setup):
    result = book()

    with Session(db.engine) as session:
        logs = [
            item for item in db.rows(session, db.MessageLog)
            if item.conversation_id == result["conversation_id"]
        ]

        assert any(item.receiver == "TicketAgent" for item in logs)
        assert any(item.receiver == "*" for item in logs)
        assert session.get(
            db.AgentState, "BookingAgent"
        ).processed >= 1