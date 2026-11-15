# EventSphere

EventSphere is a FastAPI venue-booking and digital-ticketing application
built around five cooperating rule-based agents.

## Stack

- Python 3.11+
- FastAPI and Pydantic
- SQLAlchemy
- SQLite for local development
- PostgreSQL for production
- Jinja2, HTML, CSS and JavaScript
- qrcode and html5-qrcode
- Pytest

## Local startup

1. Create and activate a virtual environment.
2. Install requirements:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`.
4. Generate a secret:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

5. Put the generated value in `SECRET_KEY`.
6. Set the three demo passwords in `.env`.
7. Create the demo accounts:

   ```bash
   python -m app.seed
   ```

8. Start the application:

   ```bash
   python -m uvicorn app.main:app --reload
   ```

9. Visit http://127.0.0.1:8000.

## Environment variables

| Variable | Purpose |
| --- | --- |
| DATABASE_URL | SQLite locally; PostgreSQL in production |
| SECRET_KEY | Random secret for signed session cookies |
| COOKIE_SECURE | false locally; true on HTTPS |
| DEMO_ADMIN_PASSWORD | Initial demo admin password |
| DEMO_STAFF_PASSWORD | Initial demo staff password |
| DEMO_ATTENDEE_PASSWORD | Initial demo attendee password |

Never commit `.env`.

## Demo accounts

- Admin: admin@eventsphere.test
- Staff: staff@eventsphere.test
- Attendee: attendee@eventsphere.test

Passwords are chosen locally through environment variables.
List the chosen demo credentials in the assignment submission.
Do not reuse personal passwords.

## Tests

```bash
python -m pytest -q
```

The included agent tests use a temporary SQLite database.
Repeat concurrency and integration checks against PostgreSQL before
claiming production database verification.

## Agent architecture

All agent-to-agent communication goes through the message bus.
Messages include sender, receiver, performative, content and
conversation_id.

Messages and responses are stored in the database.

NotificationAgent listens to broadcast INFORM messages.
WaitlistAgent runs every 15 seconds while the application is running.

## Decision rules

### VenueAgent

- Activities require a 30-minute turnaround buffer.
- Draft and published events reserve venue time.
- Pending and confirmed hires reserve venue time.
- Event ticket allocations cannot exceed venue capacity.
- Venue capacity reductions cannot invalidate future allocations.
- Hires must fit venue capacity and use a private-hire-enabled venue.
- Routine hires have at most 100 guests and at most 80% occupancy.
- Larger valid requests are escalated to the organiser.
- Hire price is hourly rate multiplied by duration.
- Hires starting on a weekend in Asia/Colombo cost 20% more.
- Conflicts produce suitable alternatives when any are found:
  other venues at the requested time or the same venue over seven days.

### BookingAgent

- One booking may contain one to six tickets.
- Only published events accept bookings.
- Booking closes when the event starts.
- Offered waitlist seats are reserved.
- Payment is simulated and receives a unique SIM reference.
- Cancellation at least 24 hours before start refunds 100%.
- Cancellation within 24 hours refunds 50%.
- Cancellation closes at event start.
- A booking with any checked-in ticket cannot be cancelled by its owner.
- Organiser cancellation before event start fully refunds active bookings.

### TicketAgent

- One unguessable code is issued per ticket.
- Repeated issue requests do not issue additional tickets.
- Cancelled tickets and wrong-event tickets are rejected.
- Check-in opens one hour before start and closes at event end.
- A ticket may be admitted only once.
- Scan attempt counts persist, including refused repeated scans.

### WaitlistAgent

- Waitlist requests contain one to six seats.
- A user may have one active entry per ticket category.
- Allocation uses strict FIFO order.
- A request is offered only when its whole quantity is available.
- Offers last five minutes or until event start, whichever is earlier.
- Expired offers leave the queue and release their seats.
- Released seats are offered to the next waiting customer.
- The timer runs without a user request.
- Cancelled or started events close their waitlists.

### NotificationAgent

- Reacts to broadcast INFORM messages.
- Saves an in-app notification for the affected user.

## Persistent state

Agent counters and last actions are stored in agent_states.
Business state is also persistent:

- Venue calendars and hire decisions
- Bookings, payment references and refunds
- Tickets, scan attempts and admission times
- Waitlist positions, offer statuses and expiry timestamps
- Notifications and message logs

## Concurrency

All writes acquire a database-level application lock.

- PostgreSQL uses SELECT FOR UPDATE on a singleton lock row.
- SQLite uses BEGIN IMMEDIATE.

The lock covers availability checks and subsequent writes within the
same transaction. It protects seat allocation, waitlist claims, venue
calendar decisions and repeated check-in.

This coarse lock trades throughput for simplicity and correctness in
a small demonstration application.

## Security

- Argon2 password hashing
- Signed HTTP-only session cookies
- CSRF header required for every mutation
- Server-side role and ownership checks
- Attendee registration cannot select privileged roles
- User-generated text is escaped before HTML rendering
- QR images require ownership of the corresponding booking
- No real payment information is collected

## Operational limitations

- The waitlist timer requires a running application process.
- On restart, the next sweep releases overdue offers.
- The frontend QR-scanning library loads from a public CDN.
- Camera access requires HTTPS or localhost.
- The schema is initialized with create_all; existing schema changes
  require an explicit migration before modifying a deployed database.
- The dashboard displays the most recent 200 messages.
- This is a small educational application, not a payment platform.

## Manual acceptance checks

1. Register and sign in as an attendee.
2. Confirm attendee access to /admin and /staff is refused.
3. Create a venue and publish an event as admin.
4. Attempt an overlapping event and inspect the refusal.
5. Book tickets and inspect the payment reference and QR codes.
6. Scan a ticket as staff, then scan it again.
7. Scan for the wrong event and inspect the refusal.
8. Cancel an unused booking and check its refund.
9. Fill an event, join its waitlist and release a seat.
10. Leave an offer unclaimed and check automatic re-offer.
11. Request a routine hire and an escalated hire.
12. Request a conflicting hire and inspect alternatives.
13. Cancel a published event and inspect full refunds.
14. Inspect agent messages sharing one conversation ID.
15. Restart the app and confirm persistent data remains.
16. Check every screen at 375 px width.
17. Test camera scanning on a real phone over HTTPS.