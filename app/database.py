import os
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    create_engine,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session

load_dotenv()


class Base(DeclarativeBase):
    pass


class AppLock(Base):
    __tablename__ = "app_lock"

    id = Column(Integer, primary_key=True)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    email = Column(String(254), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(20), nullable=False, default="attendee")


class Venue(Base):
    __tablename__ = "venues"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    location = Column(String(200), nullable=False)
    capacity = Column(Integer, nullable=False)
    description = Column(Text, nullable=False, default="")
    image = Column(Text, nullable=False, default="")
    hourly_rate = Column(Integer, nullable=False)
    private_hire = Column(Boolean, nullable=False, default=True)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    title = Column(String(150), nullable=False)
    category = Column(String(80), nullable=False)
    venue_id = Column(Integer, ForeignKey("venues.id"), nullable=False)
    start = Column(Float, nullable=False)
    end = Column(Float, nullable=False)
    description = Column(Text, nullable=False, default="")
    status = Column(String(20), nullable=False, default="draft")


class Tier(Base):
    __tablename__ = "ticket_categories"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    name = Column(String(80), nullable=False)
    price = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False)


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tier_id = Column(Integer, ForeignKey("ticket_categories.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    total = Column(Integer, nullable=False)
    refund = Column(Integer, nullable=False, default=0)
    payment_reference = Column(String(100), unique=True, nullable=False)
    status = Column(String(20), nullable=False, default="confirmed")
    created = Column(Float, nullable=False)


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    code = Column(String(100), unique=True, nullable=False)
    used_at = Column(Float, nullable=True)
    cancelled = Column(Boolean, nullable=False, default=False)
    scan_attempts = Column(Integer, nullable=False, default=0)


class WaitEntry(Base):
    __tablename__ = "waitlist"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tier_id = Column(Integer, ForeignKey("ticket_categories.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="waiting")
    expires = Column(Float, nullable=True)
    created = Column(Float, nullable=False)


class Hire(Base):
    __tablename__ = "hires"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    venue_id = Column(Integer, ForeignKey("venues.id"), nullable=False)
    start = Column(Float, nullable=False)
    end = Column(Float, nullable=False)
    purpose = Column(Text, nullable=False)
    guests = Column(Integer, nullable=False)
    price = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    message = Column(Text, nullable=False)
    created = Column(Float, nullable=False)


class AgentState(Base):
    __tablename__ = "agent_states"

    name = Column(String(50), primary_key=True)
    processed = Column(Integer, nullable=False, default=0)
    last_action = Column(String(80), nullable=False, default="")
    updated = Column(Float, nullable=False, default=0)


class MessageLog(Base):
    __tablename__ = "message_logs"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(String(64), nullable=False, index=True)
    sender = Column(String(50), nullable=False)
    receiver = Column(String(50), nullable=False)
    performative = Column(String(20), nullable=False)
    content = Column(JSON, nullable=False)
    created = Column(Float, nullable=False)


def make_engine(url):
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    options = {"pool_pre_ping": True}

    if url.startswith("sqlite"):
        options["connect_args"] = {
            "check_same_thread": False,
            "timeout": 30,
        }

    return create_engine(url, **options)


#engine = make_engine(
#    os.getenv("DATABASE_URL", "sqlite:///./eventsphere.db")
#)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./eventsphere.db",
)

if os.getenv("RENDER", "").lower() == "true":
    if not DATABASE_URL.startswith(
        ("postgres://", "postgresql://", "postgresql+psycopg://")
    ):
        raise RuntimeError(
            "Render deployment requires a PostgreSQL DATABASE_URL."
        )

engine = make_engine(DATABASE_URL)

AGENT_NAMES = [
    "VenueAgent",
    "BookingAgent",
    "TicketAgent",
    "WaitlistAgent",
    "NotificationAgent",
]


def initialize():
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        if session.get(AppLock, 1) is None:
            session.add(AppLock(id=1))

        for name in AGENT_NAMES:
            if session.get(AgentState, name) is None:
                session.add(AgentState(name=name))

        session.commit()


@contextmanager
def transaction():
    """
    Serialize all application writes.

    PostgreSQL: lock a shared row until commit/rollback.
    SQLite: BEGIN IMMEDIATE acquires the database write lock.

    This is deliberately coarse-grained for a small assignment app.
    It protects bookings, offers, venue calendars, and ticket scans
    across separate requests and application processes.
    """
    with Session(engine, expire_on_commit=False) as session:
        try:
            if engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            else:
                session.execute(
                    select(AppLock)
                    .where(AppLock.id == 1)
                    .with_for_update()
                ).scalar_one()

            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def rows(session, model):
    return list(session.scalars(select(model)))


def serialize(record):
    return {
        column.name: getattr(record, column.name)
        for column in record.__table__.columns
    }