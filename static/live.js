const liveEventUrlInput = document.getElementById("liveEventUrl");
const liveStartBtn = document.getElementById("liveStartBtn");
const liveEventsListEl = document.getElementById("liveEventsList");

const LIVE_STATUS_BADGE_CLASSES = {
  starting: "badge-waiting",
  connecting: "badge-waiting",
  live: "badge-live",
  reconnecting: "badge-active",
  error: "badge-error",
  stopped: "badge-idle",
};

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

function openTalentView(eventId) {
  window.open(`/overlay/${encodeURIComponent(eventId)}`, "_blank");
}

function copyObsUrl(eventId) {
  const url = `${location.origin}/overlay/${encodeURIComponent(eventId)}/mobile`;
  navigator.clipboard.writeText(url).then(
    () => alert("OBS browser-source URL copied to clipboard."),
    () => prompt("Copy this URL:", url),
  );
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
    previewBtn.addEventListener("click", () => openTalentView(event.event_id));

    const copyBtn = document.createElement("button");
    copyBtn.className = "secondary";
    copyBtn.textContent = "Copy OBS URL";
    copyBtn.addEventListener("click", () => copyObsUrl(event.event_id));

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
