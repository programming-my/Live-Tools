const liveEventUrlInput = document.getElementById("liveEventUrl");
const liveStartBtn = document.getElementById("liveStartBtn");
const liveEventsListEl = document.getElementById("liveEventsList");
const tunnelStatusBadge = document.getElementById("tunnelStatusBadge");

const LIVE_STATUS_BADGE_CLASSES = {
  starting: "badge-waiting",
  connecting: "badge-waiting",
  live: "badge-live",
  reconnecting: "badge-active",
  error: "badge-error",
  stopped: "badge-idle",
};

const TUNNEL_STATUS_BADGE_CLASSES = {
  idle: "badge-idle",
  starting: "badge-waiting",
  connected: "badge-live",
  error: "badge-error",
};
const TUNNEL_STATUS_LABELS = {
  idle: "Idle",
  starting: "Starting tunnel",
  connected: "Tunnel connected",
  error: "Tunnel unavailable",
};

// The tunnel is a single app-wide resource (started at launch - see
// app.py's _start_tunnel_at_launch), not tied to any one watched event, so
// it gets its own small indicator and polling loop rather than living
// inside renderLiveEvents/refreshLiveEvents.
async function refreshTunnelStatus() {
  const res = await fetch("/api/tunnel-status");
  const data = await res.json();
  tunnelStatusBadge.className = "badge " + (TUNNEL_STATUS_BADGE_CLASSES[data.status] || "badge-idle");
  tunnelStatusBadge.textContent = TUNNEL_STATUS_LABELS[data.status] || data.status;
  // Starting to watch an event already waits on the tunnel internally
  // before showing "live" (see live/session.py's gate) - greying the
  // button out until then makes that wait visible up front instead of
  // looking like nothing happened after clicking "Start watching".
  liveStartBtn.disabled = data.status !== "connected";
}

async function startLiveEvent() {
  const url = liveEventUrlInput.value.trim();
  if (!url) {
    alert("Please enter an eBay Live event URL.");
    return;
  }
  const res = await fetch("/api/live/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to start watching that event.");
    return;
  }
  liveEventUrlInput.value = "";
  refreshLiveEvents();
}

async function stopLiveEvent(eventId) {
  await fetch(`/api/live/events/${encodeURIComponent(eventId)}/stop`, { method: "POST" });
  refreshLiveEvents();
}

// Cached across calls (and across every button click) so the tunnel only
// ever gets started once per app run - see /api/tunnel-url (tunnel.py).
let tunnelUrlPromise = null;

function getTunnelUrl() {
  if (!tunnelUrlPromise) {
    tunnelUrlPromise = fetch("/api/tunnel-url")
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || "Could not start the sharing tunnel.");
        }
        return (await res.json()).url;
      })
      .catch((err) => {
        tunnelUrlPromise = null; // let a later click retry instead of caching the failure
        throw err;
      });
  }
  return tunnelUrlPromise;
}

async function withTunnelButtonState(button, action) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Starting tunnel...";
  try {
    await action();
  } catch (err) {
    alert(err.message || "Could not start the sharing tunnel.");
  } finally {
    // If the live-events list re-rendered meanwhile, this button is already
    // detached from the page - harmless, the fresh one already shows the
    // normal text.
    button.disabled = false;
    button.textContent = originalText;
  }
}

function openTalentView(eventId, button) {
  return withTunnelButtonState(button, async () => {
    const base = await getTunnelUrl();
    window.open(`${base}/overlay/${encodeURIComponent(eventId)}`, "_blank");
  });
}

function copyObsUrl(eventId, button) {
  return withTunnelButtonState(button, async () => {
    const base = await getTunnelUrl();
    const url = `${base}/overlay/${encodeURIComponent(eventId)}/mobile`;
    navigator.clipboard.writeText(url).then(
      () => alert("OBS browser-source URL copied to clipboard."),
      () => prompt("Copy this URL:", url),
    );
  });
}

function renderLiveEvents(events) {
  liveEventsListEl.innerHTML = "";
  if (!events || events.length === 0) {
    const empty = document.createElement("div");
    empty.className = "queue-list-empty";
    empty.textContent = "No events being watched yet.";
    liveEventsListEl.appendChild(empty);
    return;
  }

  for (const event of events) {
    const item = document.createElement("div");
    item.className = "live-event-item";

    const header = document.createElement("div");
    header.className = "live-event-item-header";

    const idSpan = document.createElement("span");
    idSpan.className = "live-event-item-id";
    idSpan.textContent = event.event_id;

    const badge = document.createElement("span");
    badge.className = "badge " + (LIVE_STATUS_BADGE_CLASSES[event.status] || "badge-idle");
    badge.textContent = event.status;

    header.appendChild(idSpan);
    header.appendChild(badge);

    const actions = document.createElement("div");
    actions.className = "live-event-item-actions";

    const previewBtn = document.createElement("button");
    previewBtn.className = "secondary";
    previewBtn.textContent = "Open Talent View";
    previewBtn.addEventListener("click", () => openTalentView(event.event_id, previewBtn));

    const copyBtn = document.createElement("button");
    copyBtn.className = "secondary";
    copyBtn.textContent = "Copy OBS URL";
    copyBtn.addEventListener("click", () => copyObsUrl(event.event_id, copyBtn));

    const stopBtn = document.createElement("button");
    stopBtn.textContent = "Stop";
    stopBtn.addEventListener("click", () => stopLiveEvent(event.event_id));

    actions.appendChild(previewBtn);
    actions.appendChild(copyBtn);
    actions.appendChild(stopBtn);

    item.appendChild(header);
    item.appendChild(actions);
    liveEventsListEl.appendChild(item);
  }
}

async function refreshLiveEvents() {
  const res = await fetch("/api/live/events");
  const data = await res.json();
  renderLiveEvents(data.events);
}

liveStartBtn.addEventListener("click", startLiveEvent);
setInterval(refreshLiveEvents, 2000);
refreshLiveEvents();

setInterval(refreshTunnelStatus, 2000);
refreshTunnelStatus();
