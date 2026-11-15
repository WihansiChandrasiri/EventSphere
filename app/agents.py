import math
import secrets
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database import (
    AgentState,
    Booking,
    Event,
    Hire,
    MessageLog,
    Notification,
    Ticket,
    Tier,
    Venue,
    WaitEntry,
    rows,
)
from app.schemas import Message

BUFFER = 30 * 60
HOLD_SECONDS = 5 * 60
LOCAL_ZONE = ZoneInfo("Asia/Colombo")


def now():
    return time.time()


class Refusal(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def get(session, model, record_id):
    record = session.get(model, record_id)
    require(record is not None, f"{model.__name__} was not found.")
    return record


def available(session, tier):
    sold = sum(
        booking.quantity
        for booking in rows(session, Booking)
        if booking.tier_id == tier.id
        and booking.status == "confirmed"
    )

    # Expired offers remain reserved until the timer releases them.
    # That is conservative: it cannot cause overselling.
    held = sum(
        entry.quantity
        for entry in rows(session, WaitEntry)
        if entry.tier_id == tier.id
        and entry.status == "offered"
    )

    return tier.quantity - sold - held


class Bus:
    """
    Every inter-agent operation passes through call() or broadcast().

    Messages and responses are validated and persisted.
    Each handler runs in a savepoint so a refusal cannot leave
    a partially completed booking or cancellation.
    """

    def __init__(self, session):
        self.session = session

    def log(self, message):
        message = Message.model_validate(message)
        self.session.add(
            MessageLog(
                **message.model_dump(),
                created=now(),
            )
        )
        self.session.flush()
        return message

    def call(self, sender, receiver, action, data=None, conversation=None):
        conversation = conversation or uuid.uuid4().hex

        incoming = self.log({
            "sender": sender,
            "receiver": receiver,
            "performative": "REQUEST",
            "content": {"action": action, **(data or {})},
            "conversation_id": conversation,
        })

        state = self.session.get(AgentState, receiver)
        require(state is not None, "Unknown receiving agent.")

        state.processed += 1
        state.last_action = action
        state.updated = now()
        self.session.flush()

        try:
            with self.session.begin_nested():
                result = HANDLERS[receiver](self, incoming)
                result = result or {}
                performative = result.pop("_performative", "AGREE")

        except Refusal as exc:
            result = {"reason": str(exc)}
            performative = "REFUSE"

        self.log({
            "sender": receiver,
            "receiver": sender,
            "performative": performative,
            "content": result,
            "conversation_id": conversation,
        })

        return {
            "performative": performative,
            "conversation_id": conversation,
            **result,
        }

    def need(self, sender, receiver, action, data, conversation):
        result = self.call(
            sender, receiver, action, data, conversation
        )
        require(
            result["performative"] == "AGREE",
            result.get("reason", "Agent could not complete the request."),
        )
        return result

    def broadcast(self, sender, user_id, message, conversation):
        event = self.log({
            "sender": sender,
            "receiver": "*",
            "performative": "INFORM",
            "content": {
                "user_id": user_id,
                "message": message,
            },
            "conversation_id": conversation,
        })

        # NotificationAgent is subscribed to broadcast INFORM messages.
        state = self.session.get(AgentState, "NotificationAgent")
        state.processed += 1
        state.last_action = "broadcast"
        state.updated = now()

        notification_agent(self, event)

        self.log({
            "sender": "NotificationAgent",
            "receiver": sender,
            "performative": "INFORM",
            "content": {"notification_created": True},
            "conversation_id": conversation,
        })


def notification_agent(bus, message):
    content = message.content
    require(
        "user_id" in content and "message" in content,
        "Notification requires a recipient and message.",
    )

    bus.session.add(Notification(
        user_id=content["user_id"],
        message=content["message"],
        created=now(),
    ))

    return {"message": "Notification recorded."}


def slot_free(session, venue_id, start, end, exclude_hire=None):
    for event in rows(session, Event):
        if (
            event.venue_id == venue_id
            and event.status in ("draft", "published")
            and start < event.end + BUFFER
            and end + BUFFER > event.start
        ):
            return False

    for hire in rows(session, Hire):
        if (
            hire.id != exclude_hire
            and hire.venue_id == venue_id
            and hire.status in ("pending", "confirmed")
            and start < hire.end + BUFFER
            and end + BUFFER > hire.start
        ):
            return False

    return True


def hire_price(venue, start, end):
    hours = (end - start) / 3600
    weekend = (
        datetime.fromtimestamp(start, LOCAL_ZONE).weekday() >= 5
    )
    multiplier = 1.2 if weekend else 1.0
    return math.ceil(hours * venue.hourly_rate * multiplier)


def alternatives(session, requested_venue, start, end, guests):
    choices = []
    duration = end - start

    for venue in rows(session, Venue):
        if (
            venue.id != requested_venue
            and venue.private_hire
            and venue.capacity >= guests
            and slot_free(session, venue.id, start, end)
        ):
            choices.append({
                "venue_id": venue.id,
                "venue_name": venue.name,
                "start": start,
                "end": end,
                "price": hire_price(venue, start, end),
            })

    venue = get(session, Venue, requested_venue)

    if venue.private_hire and venue.capacity >= guests:
        for day in range(1, 8):
            proposed_start = start + day * 86400
            proposed_end = proposed_start + duration
            if slot_free(
                session, venue.id, proposed_start, proposed_end
            ):
                choices.append({
                    "venue_id": venue.id,
                    "venue_name": venue.name,
                    "start": proposed_start,
                    "end": proposed_end,
                    "price": hire_price(
                        venue, proposed_start, proposed_end
                    ),
                })
            if len(choices) >= 3:
                break

    return choices[:3]


def venue_agent(bus, message):
    session = bus.session
    data = message.content
    action = data["action"]
    conversation = message.conversation_id

    if action == "save":
        values = {
            key: value for key, value in data.items()
            if key not in ("action", "user_id", "id")
        }

        if data.get("id"):
            venue = get(session, Venue, data["id"])

            for event in rows(session, Event):
                if (
                    event.venue_id == venue.id
                    and event.status in ("draft", "published")
                    and event.end > now()
                ):
                    quantity = sum(
                        tier.quantity for tier in rows(session, Tier)
                        if tier.event_id == event.id
                    )
                    require(
                        values["capacity"] >= quantity,
                        "Capacity is below an existing event's ticket allocation.",
                    )

            for hire in rows(session, Hire):
                if (
                    hire.venue_id == venue.id
                    and hire.status in ("pending", "confirmed")
                    and hire.end > now()
                ):
                    require(
                        values["capacity"] >= hire.guests,
                        "Capacity is below an existing hire's guest count.",
                    )

            for key, value in values.items():
                setattr(venue, key, value)

        else:
            venue = Venue(**values)
            session.add(venue)

        session.flush()
        return {"message": "Venue saved.", "id": venue.id}

    if action == "event":
        venue = get(session, Venue, data["venue_id"])

        require(data["start"] > now(), "Event must start in the future.")
        require(
            data["end"] > data["start"],
            "End time must be after start time.",
        )
        require(
            sum(t["quantity"] for t in data["tiers"]) <= venue.capacity,
            "Ticket allocation exceeds venue capacity.",
        )
        require(
            slot_free(
                session, venue.id, data["start"], data["end"]
            ),
            "Venue is occupied, including its 30-minute turnaround buffer.",
        )

        event = Event(
            title=data["title"],
            category=data["category"],
            venue_id=venue.id,
            start=data["start"],
            end=data["end"],
            description=data["description"],
            status=data["status"],
        )
        session.add(event)
        session.flush()

        for tier in data["tiers"]:
            session.add(Tier(event_id=event.id, **tier))

        bus.broadcast(
            "VenueAgent",
            data["user_id"],
            f"Event '{event.title}' saved as {event.status}.",
            conversation,
        )
        return {"message": "Event created.", "id": event.id}

    if action == "publish":
        event = get(session, Event, data["id"])
        require(event.status == "draft", "Only draft events can be published.")
        require(event.start > now(), "This event has already started.")
        event.status = "published"
        return {"message": "Event published."}

    if action == "hire":
        venue = get(session, Venue, data["venue_id"])
        require(data["start"] > now(), "Hire must start in the future.")
        require(data["end"] > data["start"], "Invalid hire time interval.")

        valid = (
            venue.private_hire
            and data["guests"] <= venue.capacity
            and slot_free(
                session, venue.id, data["start"], data["end"]
            )
        )

        if not valid:
            choices = alternatives(
                session,
                venue.id,
                data["start"],
                data["end"],
                data["guests"],
            )

            if choices:
                return {
                    "_performative": "PROPOSE",
                    "reason": (
                        "The requested venue is unavailable or unsuitable. "
                        "These alternatives are currently available."
                    ),
                    "alternatives": choices,
                }

            raise Refusal(
                "No suitable venue or time was found within the next seven days. "
                "Try a different date or reduce the guest count."
            )

        routine = (
            data["guests"] <= 100
            and data["guests"] <= venue.capacity * 0.8
        )

        hire = Hire(
            user_id=data["user_id"],
            venue_id=venue.id,
            start=data["start"],
            end=data["end"],
            purpose=data["purpose"],
            guests=data["guests"],
            price=hire_price(venue, data["start"], data["end"]),
            status="confirmed" if routine else "pending",
        )
        session.add(hire)
        session.flush()

        bus.broadcast(
            "VenueAgent",
            hire.user_id,
            f"Hire #{hire.id}: {hire.status}. "
            f"Quoted price LKR {hire.price / 100:,.2f}.",
            conversation,
        )

        return {
            "message": (
                "Hire automatically confirmed."
                if routine
                else "Hire escalated for admin approval; the slot is reserved."
            ),
            "id": hire.id,
            "price": hire.price,
            "status": hire.status,
        }

    if action == "hire_decision":
        hire = get(session, Hire, data["id"])
        require(hire.status == "pending", "This hire is not pending.")

        if data["approve"]:
            venue = get(session, Venue, hire.venue_id)
            require(hire.start > now(), "The requested start time has passed.")
            require(hire.guests <= venue.capacity, "Venue capacity is insufficient.")
            require(
                slot_free(
                    session, hire.venue_id, hire.start, hire.end,
                    exclude_hire=hire.id,
                ),
                "The venue is no longer available.",
            )
            hire.status = "confirmed"
        else:
            hire.status = "rejected"

        bus.broadcast(
            "VenueAgent",
            hire.user_id,
            f"Hire #{hire.id} was {hire.status} by the organiser.",
            conversation,
        )
        return {"message": f"Hire {hire.status}."}

    raise Refusal("Unsupported VenueAgent action.")


def booking_agent(bus, message):
    session = bus.session
    data = message.content
    action = data["action"]
    conversation = message.conversation_id

    if action == "book":
        tier = get(session, Tier, data["tier_id"])
        event = get(session, Event, tier.event_id)
        quantity = data["quantity"]

        require(1 <= quantity <= 6, "Book between one and six tickets.")
        require(event.status == "published", "Event is not published.")
        require(now() < event.start, "Bookings close when the event starts.")
        require(data.get("simulate_payment") is True, "Confirm simulated payment.")

        offer = None
        seats = available(session, tier)

        if data.get("offer_id"):
            offer = get(session, WaitEntry, data["offer_id"])
            require(
                offer.user_id == data["user_id"]
                and offer.tier_id == tier.id,
                "This offer does not belong to you.",
            )
            require(
                offer.status == "offered" and offer.expires > now(),
                "This seat offer has expired or is no longer available.",
            )
            require(
                offer.quantity == quantity,
                "Claim the complete offered quantity.",
            )
            seats += offer.quantity

        require(seats >= quantity, "Not enough tickets remain.")

        if offer:
            offer.status = "claimed"

        booking = Booking(
            user_id=data["user_id"],
            tier_id=tier.id,
            quantity=quantity,
            total=quantity * tier.price,
            refund=0,
            payment_reference=f"SIM-{uuid.uuid4().hex}",
            status="confirmed",
            created=now(),
        )
        session.add(booking)
        session.flush()

        bus.need(
            "BookingAgent", "TicketAgent", "issue",
            {"booking_id": booking.id}, conversation,
        )

        bus.broadcast(
            "BookingAgent",
            booking.user_id,
            f"Booking #{booking.id} confirmed for {event.title}. "
            f"Simulated payment: {booking.payment_reference}.",
            conversation,
        )

        return {
            "message": "Booking confirmed. Your QR tickets are in My account.",
            "id": booking.id,
            "payment_reference": booking.payment_reference,
        }

    if action == "cancel":
        booking = get(session, Booking, data["id"])
        forced = (
            message.sender == "BookingAgent"
            and data.get("event_cancelled") is True
        )

        require(
            forced or booking.user_id == data["user_id"],
            "This booking does not belong to you.",
        )
        require(booking.status == "confirmed", "Booking is already cancelled.")

        tier = get(session, Tier, booking.tier_id)
        event = get(session, Event, tier.event_id)

        if forced:
            refund = booking.total
        else:
            require(now() < event.start, "Cancellation closes at event start.")
            used = any(
                ticket.used_at is not None
                for ticket in rows(session, Ticket)
                if ticket.booking_id == booking.id
            )
            require(not used, "A checked-in booking cannot be cancelled.")
            remaining = event.start - now()
            refund = (
                booking.total
                if remaining >= 24 * 3600
                else booking.total // 2
            )

        booking.status = "cancelled"
        booking.refund = refund

        bus.need(
            "BookingAgent", "TicketAgent", "invalidate",
            {"booking_id": booking.id}, conversation,
        )

        bus.need(
            "BookingAgent", "WaitlistAgent", "sweep",
            {}, conversation,
        )

        bus.broadcast(
            "BookingAgent",
            booking.user_id,
            f"Booking #{booking.id} cancelled. "
            f"Simulated refund: LKR {refund / 100:,.2f}.",
            conversation,
        )

        return {
            "message": "Booking cancelled.",
            "refund": refund,
        }

    if action == "cancel_event":
        event = get(session, Event, data["id"])
        require(event.status == "published", "Only published events can be cancelled.")
        require(now() < event.start, "Event cancellation closes at event start.")

        event.status = "cancelled"
        session.flush()

        tier_ids = {
            tier.id for tier in rows(session, Tier)
            if tier.event_id == event.id
        }

        for booking in rows(session, Booking):
            if (
                booking.tier_id in tier_ids
                and booking.status == "confirmed"
            ):
                bus.need(
                    "BookingAgent", "BookingAgent", "cancel",
                    {
                        "id": booking.id,
                        "user_id": booking.user_id,
                        "event_cancelled": True,
                    },
                    conversation,
                )

        bus.need(
            "BookingAgent", "WaitlistAgent", "sweep",
            {}, conversation,
        )

        return {
            "message": "Event cancelled. Active bookings received full refunds."
        }

    if action == "ticket_status":
        require(message.sender == "TicketAgent", "Internal ticket query only.")
        booking = get(session, Booking, data["booking_id"])
        tier = get(session, Tier, booking.tier_id)
        event = get(session, Event, tier.event_id)

        return {
            "booking_status": booking.status,
            "event_status": event.status,
            "event_id": event.id,
            "start": event.start,
            "end": event.end,
        }

    raise Refusal("Unsupported BookingAgent action.")


def ticket_agent(bus, message):
    session = bus.session
    data = message.content
    action = data["action"]
    conversation = message.conversation_id

    if action == "issue":
        require(message.sender == "BookingAgent", "Internal issuance only.")
        booking = get(session, Booking, data["booking_id"])

        existing = [
            ticket for ticket in rows(session, Ticket)
            if ticket.booking_id == booking.id
        ]

        if existing:
            return {"message": "Tickets already issued."}

        for _ in range(booking.quantity):
            session.add(Ticket(
                booking_id=booking.id,
                code=secrets.token_urlsafe(32),
                cancelled=False,
                scan_attempts=0,
            ))

        return {"message": f"{booking.quantity} tickets issued."}

    if action == "invalidate":
        require(message.sender == "BookingAgent", "Internal invalidation only.")

        for ticket in rows(session, Ticket):
            if ticket.booking_id == data["booking_id"]:
                ticket.cancelled = True

        return {"message": "Tickets invalidated."}

    if action == "scan":
        ticket = session.scalar(
            select(Ticket).where(Ticket.code == data["code"])
        )

        require(ticket is not None, "Invalid ticket code.")

        # Return REFUSE normally rather than raising so the attempt
        # counter persists even when this scan is refused.
        ticket.scan_attempts += 1

        status = bus.need(
            "TicketAgent", "BookingAgent", "ticket_status",
            {"booking_id": ticket.booking_id}, conversation,
        )

        reason = None

        if status["event_id"] != data["event_id"]:
            reason = "Wrong event."
        elif (
            ticket.cancelled
            or status["booking_status"] != "confirmed"
            or status["event_status"] != "published"
        ):
            reason = "Ticket or event has been cancelled."
        elif ticket.used_at is not None:
            reason = "Already used. This ticket has previously checked in."
        elif not (
            status["start"] - 3600 <= now() <= status["end"]
        ):
            reason = "Outside check-in time: one hour before start until event end."

        if reason:
            return {
                "_performative": "REFUSE",
                "reason": reason,
                "scan_attempts": ticket.scan_attempts,
            }

        ticket.used_at = now()

        return {
            "message": "ADMIT: ticket accepted.",
            "ticket_id": ticket.id,
        }

    raise Refusal("Unsupported TicketAgent action.")


def waitlist_agent(bus, message):
    session = bus.session
    data = message.content
    action = data["action"]
    conversation = message.conversation_id

    if action == "join":
        tier = get(session, Tier, data["tier_id"])
        event = get(session, Event, tier.event_id)

        require(1 <= data["quantity"] <= 6, "Request between one and six seats.")
        require(
            event.status == "published" and now() < event.start,
            "Waitlist is closed.",
        )
        require(
            available(session, tier) < data["quantity"],
            "These seats are available now. Book directly.",
        )

        active = any(
            entry.user_id == data["user_id"]
            and entry.tier_id == tier.id
            and entry.status in ("waiting", "offered")
            for entry in rows(session, WaitEntry)
        )
        require(not active, "You already have an active entry for this category.")

        entry = WaitEntry(
            user_id=data["user_id"],
            tier_id=tier.id,
            quantity=data["quantity"],
            status="waiting",
            created=now(),
        )
        session.add(entry)
        session.flush()

        bus.broadcast(
            "WaitlistAgent", entry.user_id,
            f"Joined the waitlist for {event.title}.",
            conversation,
        )

        return {"message": "Added to the waitlist.", "id": entry.id}

    if action == "claim":
        entry = get(session, WaitEntry, data["id"])
        require(entry.user_id == data["user_id"], "This offer does not belong to you.")
        require(
            entry.status == "offered" and entry.expires > now(),
            "This offer has expired or is unavailable.",
        )

        return bus.need(
            "WaitlistAgent", "BookingAgent", "book",
            {
                "user_id": entry.user_id,
                "tier_id": entry.tier_id,
                "quantity": entry.quantity,
                "offer_id": entry.id,
                "simulate_payment": data["simulate_payment"],
            },
            conversation,
        )

    if action == "sweep":
        current = now()

        for tier in rows(session, Tier):
            event = get(session, Event, tier.event_id)

            entries = sorted(
                [
                    entry for entry in rows(session, WaitEntry)
                    if entry.tier_id == tier.id
                    and entry.status in ("waiting", "offered")
                ],
                key=lambda entry: (entry.created, entry.id),
            )

            for entry in entries:
                if event.status != "published" or current >= event.start:
                    entry.status = "closed"
                    bus.broadcast(
                        "WaitlistAgent", entry.user_id,
                        f"Waitlist for {event.title} is closed.",
                        conversation,
                    )
                elif (
                    entry.status == "offered"
                    and entry.expires <= current
                ):
                    entry.status = "expired"
                    bus.broadcast(
                        "WaitlistAgent", entry.user_id,
                        f"Your seat offer for {event.title} expired.",
                        conversation,
                    )

            session.flush()

            if event.status != "published" or current >= event.start:
                continue

            for entry in entries:
                if entry.status != "waiting":
                    continue

                # Strict FIFO: do not bypass a larger request at the front.
                if available(session, tier) < entry.quantity:
                    break

                entry.status = "offered"
                entry.expires = min(
                    current + HOLD_SECONDS,
                    event.start,
                )
                session.flush()

                bus.broadcast(
                    "WaitlistAgent", entry.user_id,
                    f"{entry.quantity} seat(s) held for {event.title}. "
                    "Claim and simulate payment within five minutes, "
                    "or before the event starts if sooner.",
                    conversation,
                )

        return {"message": "Waitlist sweep completed."}

    raise Refusal("Unsupported WaitlistAgent action.")


HANDLERS = {
    "VenueAgent": venue_agent,
    "BookingAgent": booking_agent,
    "TicketAgent": ticket_agent,
    "WaitlistAgent": waitlist_agent,
    "NotificationAgent": notification_agent,
}