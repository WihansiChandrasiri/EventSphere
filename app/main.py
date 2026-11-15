import asyncio
import contextlib
import hmac
import io
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pwdlib import PasswordHash
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import database as db
from app.agents import Bus, available
from app.schemas import (
    Action,
    BookInput,
    ClaimInput,
    Credentials,
    EventInput,
    HireDecision,
    HireInput,
    IDInput,
    JoinInput,
    Register,
    ScanInput,
    VenueInput,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.getenv("SECRET_KEY", "")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

if (
    len(SECRET_KEY) < 32
    or SECRET_KEY.startswith("replace-this")
):
    raise RuntimeError(
        "Set a random SECRET_KEY of at least 32 characters in .env."
    )

passwords = PasswordHash.recommended()
logger = logging.getLogger(__name__)


def sweep():
    with db.transaction() as session:
        Bus(session).call("Scheduler", "WaitlistAgent", "sweep")


async def timer():
    while True:
        try:
            await asyncio.to_thread(sweep)
        except Exception:
            logger.exception("Waitlist sweep failed")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app):
    db.initialize()

    task = asyncio.create_task(timer())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="EventSphere", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="eventsphere_session",
    max_age=8 * 3600,
    same_site="lax",
    https_only=COOKIE_SECURE,
)

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)

templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"

    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"

    return response


def check_csrf(request):
    expected = request.session.get("csrf", "")
    supplied = request.headers.get("X-CSRF-Token", "")

    if (
        not expected
        or not supplied
        or not hmac.compare_digest(expected, supplied)
    ):
        raise HTTPException(403, "Invalid CSRF token. Refresh the page.")


def current_user(request, session, roles=None):
    user_id = request.session.get("user_id")
    user = session.get(db.User, user_id) if user_id else None

    if user is None:
        raise HTTPException(401, "Please sign in.")

    if roles and user.role not in roles:
        raise HTTPException(403, "Your account does not have permission.")

    return user


@app.get("/health")
def health():
    with Session(db.engine) as session:
        session.execute(select(db.AppLock.id)).first()
    return {"status": "ok"}


@app.get("/")
@app.get("/admin")
@app.get("/staff")
def home(request: Request):
    if request.url.path in ("/admin", "/staff"):
        with Session(db.engine) as session:
            allowed = (
                {"admin"}
                if request.url.path == "/admin"
                else {"admin", "staff"}
            )
            current_user(request, session, allowed)

    if not request.session.get("csrf"):
        request.session["csrf"] = secrets.token_urlsafe(32)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"csrf": request.session["csrf"]},
    )


@app.post("/api/register")
def register(request: Request, data: Register):
    check_csrf(request)

    with db.transaction() as session:
        existing = session.scalar(
            select(db.User).where(db.User.email == data.email)
        )
        if existing:
            raise HTTPException(409, "An account already uses this email.")

        user = db.User(
            name=data.name,
            email=data.email,
            password_hash=passwords.hash(data.password),
            role="attendee",
        )
        session.add(user)
        session.flush()
        user_id = user.id

    request.session.clear()
    request.session["user_id"] = user_id
    request.session["csrf"] = secrets.token_urlsafe(32)
    return {"message": "Account created."}


@app.post("/api/login")
def login(request: Request, data: Credentials):
    check_csrf(request)

    with Session(db.engine) as session:
        user = session.scalar(
            select(db.User).where(db.User.email == data.email)
        )

        if user is None or not passwords.verify(
            data.password, user.password_hash
        ):
            raise HTTPException(401, "Incorrect email or password.")

        user_id = user.id

    request.session.clear()
    request.session["user_id"] = user_id
    request.session["csrf"] = secrets.token_urlsafe(32)
    return {"message": "Signed in."}


@app.post("/api/logout")
def logout(request: Request):
    check_csrf(request)
    request.session.clear()
    return {"message": "Signed out."}


# action: (validation model, allowed roles, receiver, internal action)
ACTIONS = {
    "venue": (
        VenueInput, {"admin"}, "VenueAgent", "save"
    ),
    "event": (
        EventInput, {"admin"}, "VenueAgent", "event"
    ),
    "publish": (
        IDInput, {"admin"}, "VenueAgent", "publish"
    ),
    "cancel_event": (
        IDInput, {"admin"}, "BookingAgent", "cancel_event"
    ),
    "book": (
        BookInput, {"attendee"}, "BookingAgent", "book"
    ),
    "cancel_booking": (
        IDInput, {"attendee"}, "BookingAgent", "cancel"
    ),
    "join": (
        JoinInput, {"attendee"}, "WaitlistAgent", "join"
    ),
    "claim": (
        ClaimInput, {"attendee"}, "WaitlistAgent", "claim"
    ),
    "hire": (
        HireInput, {"attendee"}, "VenueAgent", "hire"
    ),
    "hire_decision": (
        HireDecision, {"admin"}, "VenueAgent", "hire_decision"
    ),
    "scan": (
        ScanInput, {"admin", "staff"}, "TicketAgent", "scan"
    ),
}


@app.post("/api/action")
def action(request: Request, payload: Action):
    check_csrf(request)

    schema, roles, receiver, operation = ACTIONS[payload.action]

    try:
        validated = schema.model_validate(payload.data)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc

    with db.transaction() as session:
        user = current_user(request, session, roles)

        data = validated.model_dump()
        # Identity comes from the session, never from the browser.
        data["user_id"] = user.id

        result = Bus(session).call(
            "WebApp", receiver, operation, data
        )

    # A refusal is a valid agent decision, returned with its reason.
    return result


@app.get("/api/state")
def state(request: Request):
    with Session(db.engine) as session:
        user_id = request.session.get("user_id")
        user = session.get(db.User, user_id) if user_id else None

        is_admin = bool(user and user.role == "admin")
        is_staff = bool(user and user.role in ("admin", "staff"))

        events = [
            event for event in db.rows(session, db.Event)
            if is_admin or event.status == "published"
        ]
        event_ids = {event.id for event in events}

        tiers = [
            tier for tier in db.rows(session, db.Tier)
            if tier.event_id in event_ids
        ]

        my_bookings = [
            booking for booking in db.rows(session, db.Booking)
            if user and booking.user_id == user.id
        ]
        my_booking_ids = {booking.id for booking in my_bookings}

        # Include cancelled event/category details for booking history.
        history_tiers = {
            tier.id: tier for tier in db.rows(session, db.Tier)
            if tier.id in {b.tier_id for b in my_bookings}
        }
        history_event_ids = {
            tier.event_id for tier in history_tiers.values()
        }
        history_events = {
            event.id: event for event in db.rows(session, db.Event)
            if event.id in history_event_ids
        }

        result = {
            "user": (
                {"id": user.id, "name": user.name, "role": user.role}
                if user else None
            ),
            "venues": [
                db.serialize(venue)
                for venue in db.rows(session, db.Venue)
            ],
            "events": [db.serialize(event) for event in events],
            "tiers": [
                {
                    **db.serialize(tier),
                    "available": available(session, tier),
                }
                for tier in tiers
            ],
            "bookings": [db.serialize(b) for b in my_bookings],
            "history_tiers": [
                db.serialize(t) for t in history_tiers.values()
            ],
            "history_events": [
                db.serialize(e) for e in history_events.values()
            ],
            "tickets": [
                db.serialize(ticket)
                for ticket in db.rows(session, db.Ticket)
                if ticket.booking_id in my_booking_ids
            ],
            "waitlist": [
                db.serialize(entry)
                for entry in db.rows(session, db.WaitEntry)
                if user and entry.user_id == user.id
            ],
            "hires": [
                db.serialize(hire)
                for hire in db.rows(session, db.Hire)
                if user and (is_admin or hire.user_id == user.id)
            ],
            "notifications": [
                db.serialize(item)
                for item in db.rows(session, db.Notification)
                if user and item.user_id == user.id
            ][-100:],
            "dashboard": [],
            "logs": [],
            "agent_states": [],
            "can_scan": is_staff,
        }

        if is_admin:
            all_bookings = db.rows(session, db.Booking)
            all_tickets = db.rows(session, db.Ticket)

            for event in events:
                event_tiers = [
                    tier for tier in tiers
                    if tier.event_id == event.id
                ]
                tier_ids = {tier.id for tier in event_tiers}
                bookings = [
                    booking for booking in all_bookings
                    if booking.tier_id in tier_ids
                ]
                active = [
                    booking for booking in bookings
                    if booking.status == "confirmed"
                ]
                booking_ids = {booking.id for booking in active}

                result["dashboard"].append({
                    "event": event.title,
                    "status": event.status,
                    "capacity": session.get(
                        db.Venue, event.venue_id
                    ).capacity,
                    "allocated": sum(t.quantity for t in event_tiers),
                    "sold": sum(b.quantity for b in active),
                    "revenue": sum(b.total - b.refund for b in bookings),
                    "checkins": sum(
                        ticket.used_at is not None
                        for ticket in all_tickets
                        if ticket.booking_id in booking_ids
                    ),
                })

            result["logs"] = [
                db.serialize(log)
                for log in session.scalars(
                    select(db.MessageLog)
                    .order_by(db.MessageLog.id.desc())
                    .limit(200)
                )
            ]

            result["agent_states"] = [
                db.serialize(item)
                for item in db.rows(session, db.AgentState)
            ]

        return result


@app.get("/api/tickets/{ticket_id}/qr")
def ticket_qr(ticket_id: int, request: Request):
    with Session(db.engine) as session:
        user = current_user(request, session)
        ticket = session.get(db.Ticket, ticket_id)

        if ticket is None:
            raise HTTPException(404, "Ticket not found.")

        booking = session.get(db.Booking, ticket.booking_id)

        if booking.user_id != user.id:
            raise HTTPException(403, "This ticket does not belong to you.")

        image = qrcode.make(ticket.code)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        return Response(
            buffer.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )