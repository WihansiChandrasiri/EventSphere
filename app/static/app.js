let state = null;
let selectedView = "events";
let scanner = null;
let scanBusy = false;

const $ = (id) => document.getElementById(id);

const escapeHTML = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[character]));

const money = (cents) =>
  new Intl.NumberFormat("en-LK", {
    style: "currency",
    currency: "LKR",
  }).format(Number(cents) / 100);

const dateText = (seconds) =>
  new Date(seconds * 1000).toLocaleString();

const csrf = () =>
  document.querySelector('meta[name="csrf-token"]').content;

function notify(message, error = false) {
  const box = $("notice");
  box.hidden = false;
  box.className = error ? "error" : "";
  box.textContent = message;
}

async function request(path, data = undefined) {
  const options = {
    credentials: "same-origin",
    headers: {},
  };

  if (data !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = csrf();
    options.body = JSON.stringify(data);
  }

  const response = await fetch(path, options);
  const result = await response.json();

  if (!response.ok) {
    const detail = typeof result.detail === "string"
      ? result.detail
      : JSON.stringify(result.detail);
    throw new Error(detail || "The request failed.");
  }

  return result;
}

async function perform(action, data, refresh = true) {
  const result = await request("/api/action", { action, data });

  if (result.performative === "PROPOSE") {
    showAlternatives(result);
  } else if (result.performative === "REFUSE") {
    notify(result.reason, true);
  } else {
    let message = result.message || "Completed.";
    if (result.refund !== undefined) {
      message += ` Refund: ${money(result.refund)}.`;
    }
    if (result.price !== undefined) {
      message += ` Price: ${money(result.price)}.`;
    }
    notify(message);
  }

  if (refresh) await load();
  return result;
}

function venueName(id) {
  return state.venues.find((venue) => venue.id === id)?.name || "Venue";
}

function eventNameForTier(tierId) {
  const tier = [...state.tiers, ...state.history_tiers]
    .find((item) => item.id === tierId);

  const event = [...state.events, ...state.history_events]
    .find((item) => item.id === tier?.event_id);

  return event?.title || `Ticket category #${tierId}`;
}

function options(items, label = "name") {
  return items.map((item) =>
    `<option value="${item.id}">${escapeHTML(item[label])}</option>`
  ).join("");
}

async function stopScanner() {
  if (scanner) {
    try {
      await scanner.stop();
    } catch (_) {
      // The scanner may already be stopped.
    }
    scanner = null;
  }
}

async function view(name) {
  if (selectedView === "scanner" && name !== "scanner") {
    await stopScanner();
  }

  selectedView = name;

  for (const item of [
    "events", "account", "hire", "admin", "scanner", "auth",
  ]) {
    $(`${item}-view`).hidden = item !== name;
  }
}

function navigation() {
  const user = state.user;

  $("identity").innerHTML = user
    ? `${escapeHTML(user.name)} · ${escapeHTML(user.role)}
       <button class="secondary" data-do="logout">Sign out</button>`
    : `<button data-view="auth">Sign in</button>`;

  const tabs = [
    ["events", "Events"],
  ];

  if (user?.role === "attendee") {
    tabs.push(["account", "My account"], ["hire", "Hire a venue"]);
  }

  if (user?.role === "admin") {
    tabs.push(["admin", "Organiser"]);
  }

  if (state.can_scan) {
    tabs.push(["scanner", "Scanner"]);
  }

  $("navigation").innerHTML = tabs.map(([id, label]) =>
    `<button class="secondary" data-view="${id}">${label}</button>`
  ).join("");
}

function renderEvents() {
  const query = $("search").value.toLowerCase().trim();
  const category = $("category").value;

  const events = state.events.filter((event) =>
    event.status === "published"
    && `${event.title} ${event.description} ${venueName(event.venue_id)}`
      .toLowerCase().includes(query)
    && (!category || event.category === category)
  );

  $("events").innerHTML = events.map((event) => {
    const venue = state.venues.find((item) => item.id === event.venue_id);
    const tiers = state.tiers.filter((tier) => tier.event_id === event.id);
    const open = Date.now() / 1000 < event.start;

    return `
      <article class="card">
        ${venue?.image
          ? `<img class="card-image" src="${escapeHTML(venue.image)}"
                  alt="${escapeHTML(venue.name)}" loading="lazy">`
          : ""}
        <p><span class="badge">${escapeHTML(event.category)}</span></p>
        <h2>${escapeHTML(event.title)}</h2>
        <p>${escapeHTML(event.description)}</p>
        <p class="muted">
          ${escapeHTML(venueName(event.venue_id))}<br>
          ${escapeHTML(dateText(event.start))}<br>
          Ends: ${escapeHTML(dateText(event.end))}
        </p>

        <details>
          <summary>Ticket categories and booking</summary>
          ${tiers.map((tier) => `
            <div class="ticket">
              <strong>${escapeHTML(tier.name)}</strong>
              <p>${money(tier.price)} · ${tier.available} remaining</p>
              ${state.user?.role === "attendee" && open ? `
                <form data-form="booking">
                  <input type="hidden" name="tier_id" value="${tier.id}">
                  <label>Quantity
                    <input type="number" name="quantity"
                           value="1" min="1" max="6" required>
                  </label>
                  <label>
                    <input type="checkbox" name="simulate_payment">
                    I confirm simulated payment only.
                  </label>
                  <div class="row">
                    <button type="submit" value="book">Book tickets</button>
                    <button type="submit" value="join" class="secondary">
                      Join waitlist
                    </button>
                  </div>
                </form>
              ` : `<p class="muted">${
                open
                  ? "Sign in with an attendee account to book."
                  : "Bookings are closed."
              }</p>`}
            </div>
          `).join("")}
        </details>
      </article>
    `;
  }).join("") || `<div class="panel">No matching published events.</div>`;
}

function renderAuth() {
  $("auth-view").innerHTML = `
    <div class="grid">
      <section class="panel">
        <h2>Sign in</h2>
        <form data-form="login">
          <label>Email
            <input name="email" type="email" autocomplete="username" required>
          </label>
          <label>Password
            <input name="password" type="password"
                   autocomplete="current-password" minlength="8" required>
          </label>
          <button>Sign in</button>
        </form>
      </section>

      <section class="panel">
        <h2>Create attendee account</h2>
        <form data-form="register">
          <label>Name
            <input name="name" autocomplete="name" minlength="2" required>
          </label>
          <label>Email
            <input name="email" type="email" autocomplete="email" required>
          </label>
          <label>Password
            <input name="password" type="password"
                   autocomplete="new-password" minlength="8" required>
          </label>
          <button>Create account</button>
        </form>
      </section>
    </div>
  `;
}

function renderAccount() {
  if (state.user?.role !== "attendee") return;

  $("account-view").innerHTML = `
    <section class="panel">
      <h2>My bookings</h2>
      <p class="muted">
        Refund policy: 100% at least 24 hours before the event;
        50% within 24 hours; cancellation closes at event start.
        Checked-in bookings cannot be cancelled.
        Organiser cancellation gives active bookings a full refund.
      </p>

      ${state.bookings.map((booking) => `
        <article class="ticket">
          <h3>#${booking.id} · ${escapeHTML(eventNameForTier(booking.tier_id))}</h3>
          <p>
            ${booking.quantity} ticket(s) · ${money(booking.total)}
            · <span class="badge">${escapeHTML(booking.status)}</span>
          </p>
          <p>Payment reference: <code>${escapeHTML(booking.payment_reference)}</code></p>
          <p>Refund: ${money(booking.refund)}</p>

          ${booking.status === "confirmed" ? `
            <button class="danger" data-do="cancel-booking"
                    data-id="${booking.id}">
              Cancel booking
            </button>
          ` : ""}

          <details>
            <summary>View QR tickets</summary>
            ${state.tickets
              .filter((ticket) => ticket.booking_id === booking.id)
              .map((ticket) => `
                <div class="ticket">
                  <strong>Ticket #${ticket.id}</strong>
                  <p>${
                    ticket.cancelled
                      ? "Cancelled"
                      : ticket.used_at
                        ? `Checked in: ${escapeHTML(dateText(ticket.used_at))}`
                        : "Not yet used"
                  }</p>
                  <img src="/api/tickets/${ticket.id}/qr"
                       alt="QR code for ticket ${ticket.id}" loading="lazy">
                  <small>Manual code:</small>
                  <p><code>${escapeHTML(ticket.code)}</code></p>
                </div>
              `).join("")}
          </details>
        </article>
      `).join("") || "<p>You have no bookings yet.</p>"}
    </section>

    <section class="panel">
      <h2>Waitlist</h2>
      <button class="secondary" data-do="refresh">Refresh offers</button>
      <p class="muted">
        Offers are reserved for up to five minutes. Expired offers are
        released automatically. Unclaimed expired entries leave the queue.
      </p>
      ${state.waitlist.map((entry) => `
        <div class="ticket">
          <strong>${escapeHTML(eventNameForTier(entry.tier_id))}</strong>
          <p>${entry.quantity} seat(s) · ${escapeHTML(entry.status)}</p>
          ${entry.status === "offered" ? `
            <p>Time remaining:
              <strong data-countdown="${entry.expires}"></strong>
            </p>
            <button data-do="claim" data-id="${entry.id}">
              Claim and simulate payment
            </button>
          ` : ""}
        </div>
      `).join("") || "<p>No waitlist entries.</p>"}
    </section>

    <section class="panel">
      <h2>My venue hire requests</h2>
      ${state.hires.map((hire) => `
        <div class="ticket">
          <strong>#${hire.id} · ${escapeHTML(venueName(hire.venue_id))}</strong>
          <p>${escapeHTML(dateText(hire.start))} to ${escapeHTML(dateText(hire.end))}</p>
          <p>${escapeHTML(hire.purpose)}</p>
          <p>${hire.guests} guests · ${money(hire.price)} · ${escapeHTML(hire.status)}</p>
        </div>
      `).join("") || "<p>No hire requests.</p>"}
    </section>

    <section class="panel">
      <h2>Notifications</h2>
      ${[...state.notifications].reverse().map((item) => `
        <div class="ticket">
          <small>${escapeHTML(dateText(item.created))}</small>
          <p>${escapeHTML(item.message)}</p>
        </div>
      `).join("") || "<p>No notifications yet.</p>"}
    </section>
  `;
}

function renderHire() {
  if (state.user?.role !== "attendee") return;

  $("hire-view").innerHTML = `
    <section class="panel">
      <h2>Request a venue</h2>
      <p class="muted">
        Times use your browser's local timezone. Weekend hires have a 20%
        surcharge. Requests above 100 guests or 80% of venue capacity
        require organiser approval.
      </p>

      <form data-form="hire" class="form-grid">
        <label>Venue
          <select name="venue_id" required>
            ${options(state.venues.filter((venue) => venue.private_hire))}
          </select>
        </label>
        <label>Guests
          <input name="guests" type="number" min="1" required>
        </label>
        <label>Start
          <input name="start" type="datetime-local" required>
        </label>
        <label>End
          <input name="end" type="datetime-local" required>
        </label>
        <label class="full">Purpose
          <textarea name="purpose" minlength="3" required></textarea>
        </label>
        <div class="full"><button>Request hire and price</button></div>
      </form>
    </section>
  `;
}

function showAlternatives(result) {
  const box = $("notice");
  box.hidden = false;
  box.className = "";
  box.textContent = result.reason;

  const list = document.createElement("ul");

  for (const item of result.alternatives) {
    const li = document.createElement("li");
    li.textContent =
      `${item.venue_name} (venue ID ${item.venue_id}): ` +
      `${dateText(item.start)} to ${dateText(item.end)}; ` +
      `${money(item.price)}.`;
    list.appendChild(li);
  }

  box.appendChild(list);

  const instruction = document.createElement("p");
  instruction.textContent =
    "Select one of these venues and times in the hire form, then submit again. " +
    "Alternatives are proposals, not reservations.";
  box.appendChild(instruction);
}

function renderAdmin() {
  if (state.user?.role !== "admin") return;

  $("admin-view").innerHTML = `
    <section class="panel">
      <h2>Organiser dashboard</h2>
      <button class="secondary" data-do="refresh">Refresh dashboard</button>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Event</th><th>Status</th><th>Venue capacity</th>
              <th>Allocated</th><th>Sold</th><th>Net revenue</th><th>Check-ins</th>
            </tr>
          </thead>
          <tbody>
            ${state.dashboard.map((item) => `
              <tr>
                <td>${escapeHTML(item.event)}</td>
                <td>${escapeHTML(item.status)}</td>
                <td>${item.capacity}</td>
                <td>${item.allocated}</td>
                <td>${item.sold}</td>
                <td>${money(item.revenue)}</td>
                <td>${item.checkins}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>Venues</h2>
      ${state.venues.map((venue) => `
        <div class="ticket">
          <strong>${escapeHTML(venue.name)}</strong>
          <p>
            ${escapeHTML(venue.location)} · capacity ${venue.capacity}
            · ${money(venue.hourly_rate)}/hour
          </p>
          <button class="secondary" data-do="edit-venue" data-id="${venue.id}">
            Edit
          </button>
        </div>
      `).join("")}

      <h3 id="venue-form-title">Create venue</h3>
      <form data-form="venue" id="venue-form" class="form-grid">
        <input type="hidden" name="id">
        <label>Name
          <input name="name" maxlength="120" required>
        </label>
        <label>Location
          <input name="location" required>
        </label>
        <label>Capacity
          <input name="capacity" type="number" min="1" required>
        </label>
        <label>Hourly rate (LKR)
          <input name="hourly_rate" type="number" min="0" step=".01" required>
        </label>
        <label class="full">Image URL (HTTPS, optional)
          <input name="image" type="url" placeholder="https://...">
        </label>
        <label class="full">Description
          <textarea name="description"></textarea>
        </label>
        <label class="full">
          <input name="private_hire" type="checkbox" checked>
          Available for private hire
        </label>
        <div class="full row">
          <button>Save venue</button>
          <button type="button" class="secondary" data-do="reset-venue">
            Clear form
          </button>
        </div>
      </form>
    </section>

    <section class="panel">
      <h2>Create event</h2>
      <p class="muted">
        Drafts also reserve their venue slot. A 30-minute turnaround
        buffer is required between activities.
      </p>
      <form data-form="event" class="form-grid">
        <label>Title
          <input name="title" required>
        </label>
        <label>Category
          <input name="category" placeholder="Workshop, Concert, Seminar..." required>
        </label>
        <label>Venue
          <select name="venue_id" required>${options(state.venues)}</select>
        </label>
        <label>Status
          <select name="status">
            <option value="draft">Draft</option>
            <option value="published">Published</option>
          </select>
        </label>
        <label>Start
          <input name="start" type="datetime-local" required>
        </label>
        <label>End
          <input name="end" type="datetime-local" required>
        </label>
        <label class="full">Description
          <textarea name="description"></textarea>
        </label>
        <label class="full">Ticket categories: name, price in LKR, quantity
          <textarea name="tiers" required
            placeholder="General, 1500, 80&#10;VIP, 3000, 20"></textarea>
        </label>
        <div class="full"><button>Create event</button></div>
      </form>
    </section>

    <section class="panel">
      <h2>Manage events</h2>
      ${state.events.map((event) => `
        <div class="ticket">
          <strong>${escapeHTML(event.title)}</strong>
          <p>${escapeHTML(dateText(event.start))} · ${escapeHTML(event.status)}</p>
          ${event.status === "draft" ? `
            <button data-do="publish" data-id="${event.id}">Publish</button>
          ` : ""}
          ${event.status === "published" ? `
            <button class="danger" data-do="cancel-event" data-id="${event.id}">
              Cancel event and refund
            </button>
          ` : ""}
        </div>
      `).join("")}
    </section>

    <section class="panel">
      <h2>Private hire decisions</h2>
      ${state.hires.map((hire) => `
        <div class="ticket">
          <strong>#${hire.id} · ${escapeHTML(venueName(hire.venue_id))}</strong>
          <p>Attendee ID: ${hire.user_id}</p>
          <p>${escapeHTML(dateText(hire.start))} to ${escapeHTML(dateText(hire.end))}</p>
          <p>${escapeHTML(hire.purpose)}</p>
          <p>${hire.guests} guests · ${money(hire.price)} · ${escapeHTML(hire.status)}</p>
          ${hire.status === "pending" ? `
            <button data-do="approve-hire" data-id="${hire.id}">Approve</button>
            <button class="danger" data-do="reject-hire" data-id="${hire.id}">Reject</button>
          ` : ""}
        </div>
      `).join("") || "<p>No requests.</p>"}
    </section>

    <section class="panel">
      <h2>Persistent agent state</h2>
      ${state.agent_states.map((item) => `
        <p><strong>${escapeHTML(item.name)}</strong>:
          ${item.processed} messages processed;
          last action ${escapeHTML(item.last_action || "none")}.
        </p>
      `).join("")}
    </section>

    <section class="panel">
      <h2>Agent message log</h2>
      <p class="muted">
        Latest 200 messages. Match conversation IDs to trace a complete workflow.
      </p>
      ${state.logs.map((log) => `
        <details>
          <summary>
            #${log.id} ${escapeHTML(log.sender)} → ${escapeHTML(log.receiver)}
            · ${escapeHTML(log.performative)}
          </summary>
          <p>Conversation: <code>${escapeHTML(log.conversation_id)}</code></p>
          <small>${escapeHTML(dateText(log.created))}</small>
          <pre>${escapeHTML(JSON.stringify(log.content, null, 2))}</pre>
        </details>
      `).join("")}
    </section>
  `;
}

function renderScanner() {
  if (!state.can_scan || scanner) return;

  $("scanner-view").innerHTML = `
    <section class="panel">
      <h2>Ticket scanner</h2>
      <p class="muted">
        Select the event before scanning. Check-in opens one hour before
        the event and closes at its end.
      </p>

      <label>Event
        <select id="scan-event">
          ${options(
            state.events.filter((event) => event.status === "published"),
            "title"
          )}
        </select>
      </label>

      <form data-form="scan">
        <label>Manual ticket code
          <input name="code" autocomplete="off" required>
        </label>
        <button>Check ticket</button>
      </form>

      <hr>
      <div class="row">
        <button data-do="start-camera">Start camera</button>
        <button class="secondary" data-do="stop-camera">Stop camera</button>
      </div>

      <p class="muted">
        Camera access requires HTTPS or localhost. Manual entry is always available.
      </p>
      <div id="reader"></div>
    </section>
  `;
}

async function load() {
  state = await request("/api/state");

  navigation();

  const previousCategory = $("category").value;
  const categories = [...new Set(
    state.events
      .filter((event) => event.status === "published")
      .map((event) => event.category)
  )];

  $("category").innerHTML =
    '<option value="">All categories</option>' +
    categories.map((category) =>
      `<option value="${escapeHTML(category)}">${escapeHTML(category)}</option>`
    ).join("");

  $("category").value = categories.includes(previousCategory)
    ? previousCategory
    : "";

  renderEvents();
  renderAuth();
  renderAccount();
  renderHire();
  renderAdmin();
  renderScanner();

  await view(selectedView);
}

function timestamp(value) {
  const result = new Date(value).getTime() / 1000;
  if (!Number.isFinite(result)) throw new Error("Enter valid dates and times.");
  return result;
}

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.dataset.form) return;
  event.preventDefault();

  const values = Object.fromEntries(new FormData(form));
  const submitter = event.submitter;
  if (submitter) submitter.disabled = true;

  try {
    switch (form.dataset.form) {
      case "login":
      case "register":
        await request(`/api/${form.dataset.form}`, values);
        location.href = "/";
        break;

      case "booking": {
        const operation = submitter?.value || "book";
        const data = {
          tier_id: Number(values.tier_id),
          quantity: Number(values.quantity),
        };

        if (operation === "book") {
          if (!values.simulate_payment) {
            throw new Error("Tick the simulated payment confirmation.");
          }
          data.simulate_payment = true;
        }

        await perform(operation, data);
        break;
      }

      case "venue":
        await perform("venue", {
          id: values.id ? Number(values.id) : null,
          name: values.name,
          location: values.location,
          capacity: Number(values.capacity),
          hourly_rate: Math.round(Number(values.hourly_rate) * 100),
          description: values.description,
          image: values.image,
          private_hire: Boolean(values.private_hire),
        });
        break;

      case "event": {
        const tiers = values.tiers.trim().split("\n").map((line) => {
          const parts = line.split(",").map((part) => part.trim());

          if (
            parts.length !== 3
            || !parts[0]
            || parts[1] === ""
            || parts[2] === ""
            || !Number.isFinite(Number(parts[1]))
            || !Number.isInteger(Number(parts[2]))
          ) {
            throw new Error(
              "Use one category per line: General, 1500, 80"
            );
          }

          return {
            name: parts[0],
            price: Math.round(Number(parts[1]) * 100),
            quantity: Number(parts[2]),
          };
        });

        await perform("event", {
          title: values.title,
          category: values.category,
          venue_id: Number(values.venue_id),
          start: timestamp(values.start),
          end: timestamp(values.end),
          description: values.description,
          status: values.status,
          tiers,
        });
        break;
      }

      case "hire":
        // Keep this form intact when alternatives are proposed.
        await perform("hire", {
          venue_id: Number(values.venue_id),
          guests: Number(values.guests),
          start: timestamp(values.start),
          end: timestamp(values.end),
          purpose: values.purpose,
        }, false);
        break;

      case "scan":
        if (!$("scan-event").value) {
          throw new Error("Select a published event.");
        }
        await perform("scan", {
          event_id: Number($("scan-event").value),
          code: values.code.trim(),
        }, false);
        break;
    }
  } catch (error) {
    notify(error.message, true);
  } finally {
    if (submitter) submitter.disabled = false;
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;

  try {
    if (button.dataset.view) {
      if (button.dataset.view === "account") await load();
      await view(button.dataset.view);
      return;
    }

    const id = Number(button.dataset.id);

    switch (button.dataset.do) {
      case "logout":
        await stopScanner();
        await request("/api/logout", {});
        location.href = "/";
        break;

      case "refresh":
        await load();
        break;

      case "publish":
        await perform("publish", { id });
        break;

      case "cancel-event":
        if (confirm("Cancel this event and fully refund active bookings?")) {
          await perform("cancel_event", { id });
        }
        break;

      case "cancel-booking":
        if (confirm("Cancel this booking under the displayed refund policy?")) {
          await perform("cancel_booking", { id });
        }
        break;

      case "claim":
        await perform("claim", { id, simulate_payment: true });
        break;

      case "approve-hire":
        await perform("hire_decision", { id, approve: true });
        break;

      case "reject-hire":
        await perform("hire_decision", { id, approve: false });
        break;

      case "edit-venue": {
        const venue = state.venues.find((item) => item.id === id);
        const form = $("venue-form");

        for (const key of [
          "id", "name", "location", "capacity", "description", "image",
        ]) {
          form.elements[key].value = venue[key];
        }

        form.elements.hourly_rate.value = venue.hourly_rate / 100;
        form.elements.private_hire.checked = venue.private_hire;
        $("venue-form-title").textContent = `Edit ${venue.name}`;
        form.scrollIntoView({ behavior: "smooth" });
        break;
      }

      case "reset-venue":
        $("venue-form").reset();
        $("venue-form").elements.id.value = "";
        $("venue-form-title").textContent = "Create venue";
        break;

      case "start-camera":
        if (!$("scan-event").value) {
          throw new Error("Select a published event first.");
        }
        if (!window.Html5Qrcode) {
          throw new Error("Scanner library could not load. Use manual entry.");
        }
        if (scanner) break;

        scanner = new Html5Qrcode("reader");

        try {
          await scanner.start(
            { facingMode: "environment" },
            { fps: 10, qrbox: { width: 220, height: 220 } },
            async (code) => {
              if (scanBusy) return;
              scanBusy = true;

              try {
                await perform("scan", {
                  event_id: Number($("scan-event").value),
                  code,
                }, false);
              } catch (error) {
                notify(error.message, true);
              } finally {
                // Stop after one read so the same frame does not
                // immediately trigger a duplicate scan.
                await stopScanner();
                scanBusy = false;
              }
            },
            () => {}
          );
        } catch (error) {
          scanner = null;
          throw new Error(`Camera unavailable: ${error.message || error}`);
        }
        break;

      case "stop-camera":
        await stopScanner();
        break;
    }
  } catch (error) {
    notify(error.message, true);
  }
});

$("search").addEventListener("input", renderEvents);
$("category").addEventListener("change", renderEvents);

setInterval(() => {
  document.querySelectorAll("[data-countdown]").forEach((element) => {
    const remaining = Math.max(
      0,
      Math.ceil(Number(element.dataset.countdown) - Date.now() / 1000)
    );

    element.textContent = remaining > 0
      ? `${Math.floor(remaining / 60)}m ${remaining % 60}s`
      : "Expired";
  });
}, 1000);

load().catch((error) => notify(error.message, true));