const downloadsDirInput = document.getElementById("downloadsDirInput");
const saveSettingsBtn = document.getElementById("saveSettingsBtn");
const settingsGearBtn = document.getElementById("settingsGearBtn");
const settingsDropdown = document.getElementById("settingsDropdown");
const appVersionLabel = document.getElementById("appVersionLabel");
const checkUpdatesBtn = document.getElementById("checkUpdatesBtn");
const updateResultEl = document.getElementById("updateResultText");

const urlInput = document.getElementById("url");
const intervalInput = document.getElementById("interval");
const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const statusBadge = document.getElementById("statusBadge");
const m3u8Row = document.getElementById("m3u8Row");
const m3u8UrlInput = document.getElementById("m3u8Url");
const progressRow = document.getElementById("progressRow");
const progressFill = document.getElementById("progressFill");
const progressLabel = document.getElementById("progressLabel");
const downloadRow = document.getElementById("downloadRow");
const downloadLink = document.getElementById("downloadLink");
const logEl = document.getElementById("log");

const queueUrlInput = document.getElementById("queueUrl");
const queueAddBtn = document.getElementById("queueAddBtn");
const queueListEl = document.getElementById("queueList");
const queuePausedBadge = document.getElementById("queuePausedBadge");
const resumeQueueBtn = document.getElementById("resumeQueueBtn");
const clearQueueBtn = document.getElementById("clearQueueBtn");

const overlayExportEventUrlInput = document.getElementById("overlayExportEventUrl");
const overlayExportListingSelect = document.getElementById("overlayExportListingSelect");
const overlayExportTimeOffsetInput = document.getElementById("overlayExportTimeOffset");
const overlayExportTimeScaleInput = document.getElementById("overlayExportTimeScale");
const overlayExportBtn = document.getElementById("overlayExportBtn");
const overlayExportStatusBadge = document.getElementById("overlayExportStatusBadge");
const overlayExportStopBtn = document.getElementById("overlayExportStopBtn");
const overlayExportClearBtn = document.getElementById("overlayExportClearBtn");
const overlayExportProgressRow = document.getElementById("overlayExportProgressRow");
const overlayExportProgressFill = document.getElementById("overlayExportProgressFill");
const overlayExportProgressLabel = document.getElementById("overlayExportProgressLabel");
const overlayExportDownloadRow = document.getElementById("overlayExportDownloadRow");
const overlayExportDownloadLink = document.getElementById("overlayExportDownloadLink");
const overlayExportLogEl = document.getElementById("overlayExportLog");

const overlayItemsLoadBtn = document.getElementById("overlayItemsLoadBtn");
const overlayItemsStatusBadge = document.getElementById("overlayItemsStatusBadge");
const overlayItemsStopBtn = document.getElementById("overlayItemsStopBtn");
const overlayItemsProgressRow = document.getElementById("overlayItemsProgressRow");
const overlayItemsProgressFill = document.getElementById("overlayItemsProgressFill");
const overlayItemsProgressLabel = document.getElementById("overlayItemsProgressLabel");
const overlayItemsLogEl = document.getElementById("overlayItemsLog");

const clipExportEventUrlInput = document.getElementById("clipExportEventUrl");
const clipRowsEl = document.getElementById("clipRows");
const clipAddRowBtn = document.getElementById("clipAddRowBtn");
const clipExportBtn = document.getElementById("clipExportBtn");
const clipExportStatusBadge = document.getElementById("clipExportStatusBadge");
const clipExportClearBtn = document.getElementById("clipExportClearBtn");
const clipExportProgressRow = document.getElementById("clipExportProgressRow");
const clipExportProgressFill = document.getElementById("clipExportProgressFill");
const clipExportProgressLabel = document.getElementById("clipExportProgressLabel");
const clipResultsListEl = document.getElementById("clipResultsList");
const clipExportLogEl = document.getElementById("clipExportLog");

const BADGE_CLASSES = {
  IDLE: "badge-idle",
  WAITING: "badge-waiting",
  LIVE: "badge-live",
  ENDED: "badge-ended",
  UNKNOWN: "badge-waiting",
  ERROR: "badge-error",
  DOWNLOADING: "badge-downloading",
  DONE: "badge-done",
  QUEUED: "badge-queued",
  ACTIVE: "badge-active",
  SKIPPED: "badge-skipped",
};

// A downloads-relative path can now include a subfolder (see
// _move_into_dated_folder in app.py) - encodeURIComponent on the whole
// string would also escape the "/" itself (into "%2F"), which the server
// won't treat as a real path separator. Encode each segment on its own.
function encodeDownloadPath(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}

let pollTimer = null;

async function loadSettings() {
  const res = await fetch("/api/settings");
  const settings = await res.json();
  downloadsDirInput.value = settings.downloads_dir;
}

async function loadAppVersion() {
  const res = await fetch("/api/version");
  const data = await res.json();
  appVersionLabel.textContent = `v${data.version}`;
}

async function checkForUpdates() {
  checkUpdatesBtn.disabled = true;
  checkUpdatesBtn.textContent = "Checking...";
  updateResultEl.innerHTML = "";

  try {
    const res = await fetch("/api/check-updates");
    const data = await res.json();

    if (!res.ok) {
      updateResultEl.textContent = data.detail || "Failed to check for updates.";
      return;
    }

    if (data.update_available) {
      const text = document.createElement("span");
      text.textContent = `Version ${data.latest_version} is available. `;
      const link = document.createElement("a");
      link.href = data.release_url;
      link.target = "_blank";
      link.textContent = "Open release page";
      updateResultEl.appendChild(text);
      updateResultEl.appendChild(link);
    } else {
      updateResultEl.textContent = `You're up to date (v${data.current_version}).`;
    }
  } finally {
    checkUpdatesBtn.disabled = false;
    checkUpdatesBtn.textContent = "Check for Updates";
  }
}

function toggleSettingsDropdown() {
  settingsDropdown.classList.toggle("hidden");
}

document.addEventListener("click", (e) => {
  if (!settingsDropdown.classList.contains("hidden") && !e.target.closest(".settings-menu")) {
    settingsDropdown.classList.add("hidden");
  }
});

async function saveSettings() {
  const downloadsDir = downloadsDirInput.value.trim();
  if (!downloadsDir) {
    alert("Please enter a downloads folder path.");
    return;
  }

  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ downloads_dir: downloadsDir }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to save settings.");
    return;
  }

  const settings = await res.json();
  downloadsDirInput.value = settings.downloads_dir;
  alert("Saved. New downloads/exports will be saved there from now on.");
  settingsDropdown.classList.add("hidden");
}

async function startJob() {
  const url = urlInput.value.trim();
  const pollInterval = parseInt(intervalInput.value, 10) || 15;
  if (!url) {
    alert("Please enter an eBay Live event URL.");
    return;
  }

  const res = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, poll_interval: pollInterval }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to start monitoring.");
    return;
  }

  renderMainLog.reset();
  refreshStatus();
}

async function stopJob() {
  await fetch("/api/stop", { method: "POST" });
  refreshStatus();
}

async function addToQueue() {
  const url = queueUrlInput.value.trim();
  if (!url) {
    alert("Please enter an eBay Live event URL.");
    return;
  }

  const res = await fetch("/api/queue/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to add to the queue.");
    return;
  }

  queueUrlInput.value = "";
  refreshQueueStatus();
}

async function resumeQueue() {
  await fetch("/api/queue/resume", { method: "POST" });
  refreshQueueStatus();
}

async function clearQueue() {
  if (!confirm("Clear the queue? This removes all queued/finished entries (the item currently downloading, if any, keeps running).")) {
    return;
  }
  await fetch("/api/queue/clear", { method: "POST" });
  refreshQueueStatus();
}

async function removeFromQueue(itemId) {
  await fetch(`/api/queue/${encodeURIComponent(itemId)}`, { method: "DELETE" });
  refreshQueueStatus();
}

async function retryQueueItem(itemId) {
  await fetch(`/api/queue/${encodeURIComponent(itemId)}/retry`, { method: "POST" });
  refreshQueueStatus();
}

async function startOverlayExport() {
  const eventUrl = overlayExportEventUrlInput.value.trim();
  if (!eventUrl) {
    alert("Please enter an eBay Live event URL.");
    return;
  }
  const listingId = overlayExportListingSelect.value;
  if (!listingId) {
    alert("Please load auctions and select which one to export.");
    return;
  }

  const timeOffsetRaw = overlayExportTimeOffsetInput.value.trim();
  const timeScaleRaw = overlayExportTimeScaleInput.value.trim();
  const body = { event_url: eventUrl, listing_id: listingId };
  if (timeOffsetRaw !== "") body.time_offset = parseFloat(timeOffsetRaw);
  if (timeScaleRaw !== "") body.time_scale = parseFloat(timeScaleRaw);

  const res = await fetch("/api/overlay-export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    alert(errBody.detail || "Failed to start overlay export.");
    return;
  }

  renderOverlayExportLog.reset();
  refreshOverlayExportStatus();
}

async function stopOverlayExport() {
  const res = await fetch("/api/overlay-export/stop", { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to stop the export.");
    return;
  }
  refreshOverlayExportStatus();
}

async function clearOverlayExport() {
  const res = await fetch("/api/overlay-export/clear", { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to clear export status.");
    return;
  }
  renderOverlayExportLog.reset();
  refreshOverlayExportStatus();
}

function addClipRow(prefillStart = "", prefillEnd = "") {
  const row = document.createElement("div");
  row.className = "clip-row";

  const startInput = document.createElement("input");
  startInput.type = "text";
  startInput.className = "clip-start-input";
  startInput.placeholder = "Start (hh:mm:ss)";
  startInput.value = prefillStart;

  const endInput = document.createElement("input");
  endInput.type = "text";
  endInput.className = "clip-end-input";
  endInput.placeholder = "End (hh:mm:ss)";
  endInput.value = prefillEnd;

  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "secondary";
  removeBtn.textContent = "Remove";
  removeBtn.addEventListener("click", () => {
    row.remove();
    // Always keep at least one row so there's somewhere to type.
    if (!clipRowsEl.children.length) addClipRow();
  });

  row.appendChild(startInput);
  row.appendChild(endInput);
  row.appendChild(removeBtn);
  clipRowsEl.appendChild(row);
}

function collectClipRequests() {
  const requests = [];
  for (const row of clipRowsEl.children) {
    const start = row.querySelector(".clip-start-input").value.trim();
    const end = row.querySelector(".clip-end-input").value.trim();
    if (!start && !end) continue;
    if (!start || !end) {
      throw new Error("Each clip needs both a start and an end timestamp.");
    }
    requests.push({ start, end });
  }
  return requests;
}

async function startClipExport() {
  const eventUrl = clipExportEventUrlInput.value.trim();
  if (!eventUrl) {
    alert("Please enter an eBay Live event URL.");
    return;
  }

  let clipRequests;
  try {
    clipRequests = collectClipRequests();
  } catch (e) {
    alert(e.message);
    return;
  }
  if (clipRequests.length === 0) {
    alert("Please enter at least one clip (start and end timestamps).");
    return;
  }

  const res = await fetch("/api/clip-export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_url: eventUrl, clips: clipRequests }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to start clip export.");
    return;
  }

  renderClipExportLog.reset();
  refreshClipExportStatus();
}

async function clearClipExport() {
  const res = await fetch("/api/clip-export/clear", { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to clear clip export status.");
    return;
  }
  renderClipExportLog.reset();
  refreshClipExportStatus();
}

function setBadge(status) {
  statusBadge.textContent = status;
  statusBadge.className = "badge " + (BADGE_CLASSES[status] || "badge-idle");
}

function makeLogRenderer(el) {
  // The backend log is a bounded ring buffer (deque(maxlen=...)) - once full,
  // older entries drop off the front while length stays constant. A fast
  // download can fill it within a second or two, so appending only
  // logs[renderedCount:] (a plain growing-index assumption) would silently
  // stop rendering forever the moment the buffer saturates. Full re-render
  // each poll instead - the buffer is small, so the cost is negligible.
  const render = function (logs) {
    el.innerHTML = "";
    for (const entry of logs) {
      const div = document.createElement("div");
      div.className = "log-entry";
      div.innerHTML = `<span class="ts">${entry.ts}</span>${escapeHtml(entry.msg)}`;
      el.appendChild(div);
    }
    el.scrollTop = el.scrollHeight;
  };
  render.reset = function () {
    el.innerHTML = "";
  };
  return render;
}

const renderMainLog = makeLogRenderer(logEl);
const renderOverlayExportLog = makeLogRenderer(overlayExportLogEl);
const renderOverlayItemsLog = makeLogRenderer(overlayItemsLogEl);
const renderClipExportLog = makeLogRenderer(clipExportLogEl);

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function render(state) {
  setBadge(state.status);

  if (state.m3u8_url) {
    m3u8Row.classList.remove("hidden");
    m3u8UrlInput.value = state.m3u8_url;
  } else {
    m3u8Row.classList.add("hidden");
  }

  if (state.status === "DOWNLOADING") {
    progressRow.classList.remove("hidden");
    const pct = state.download_progress ?? 0;
    progressFill.style.width = pct + "%";
    progressLabel.textContent = pct.toFixed(1) + "%";
  } else {
    progressRow.classList.add("hidden");
  }

  if (state.status === "DONE" && state.download_filename) {
    downloadRow.classList.remove("hidden");
    downloadLink.href = `/downloads/${encodeDownloadPath(state.download_filename)}`;
    downloadLink.textContent = `Download ${state.download_filename}`;
  } else {
    downloadRow.classList.add("hidden");
  }

  renderMainLog(state.logs);

  startBtn.disabled = state.running;
  stopBtn.disabled = !state.running;

  if (state.url && !urlInput.value) {
    urlInput.value = state.url;
  }
}

function renderQueueStatus(queueState) {
  renderQueue(queueState.queue);

  queuePausedBadge.classList.toggle("hidden", !queueState.queue_paused);
  resumeQueueBtn.disabled = !queueState.queue_paused;
}

function renderQueue(queue) {
  queueListEl.innerHTML = "";
  if (!queue || queue.length === 0) {
    const empty = document.createElement("div");
    empty.className = "queue-list-empty";
    empty.textContent = "No events queued yet.";
    queueListEl.appendChild(empty);
    return;
  }

  for (const item of queue) {
    const row = document.createElement("div");
    row.className = "queue-item";

    const urlSpan = document.createElement("span");
    urlSpan.className = "queue-item-url";
    urlSpan.textContent = item.url;

    const badge = document.createElement("span");
    badge.className = "badge " + (BADGE_CLASSES[item.status] || "badge-idle");
    badge.textContent = item.status;

    row.appendChild(urlSpan);
    row.appendChild(badge);

    if (item.status === "QUEUED") {
      const removeBtn = document.createElement("button");
      removeBtn.className = "secondary";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => removeFromQueue(item.id));
      row.appendChild(removeBtn);
    }

    if (item.status === "ERROR") {
      const retryBtn = document.createElement("button");
      retryBtn.className = "secondary";
      retryBtn.textContent = "Retry";
      retryBtn.addEventListener("click", () => retryQueueItem(item.id));
      row.appendChild(retryBtn);
    }

    queueListEl.appendChild(row);
  }
}

async function refreshStatus() {
  const res = await fetch("/api/status");
  const state = await res.json();
  render(state);
}

async function refreshQueueStatus() {
  const res = await fetch("/api/queue/status");
  const queueState = await res.json();
  renderQueueStatus(queueState);
}

function renderOverlayExport(exportState) {
  if (exportState.status && exportState.status !== "IDLE") {
    overlayExportStatusBadge.classList.remove("hidden");
    overlayExportStatusBadge.textContent = exportState.status;
    overlayExportStatusBadge.className = "badge " + (BADGE_CLASSES[exportState.status] || "badge-idle");
  } else {
    overlayExportStatusBadge.classList.add("hidden");
  }

  if (exportState.status === "RUNNING" && exportState.progress !== null && exportState.progress !== undefined) {
    overlayExportProgressRow.classList.remove("hidden");
    const pct = exportState.progress;
    overlayExportProgressFill.style.width = pct + "%";
    overlayExportProgressLabel.textContent = pct.toFixed(1) + "%";
  } else {
    overlayExportProgressRow.classList.add("hidden");
  }

  if (exportState.status === "DONE" && exportState.output_filename) {
    overlayExportDownloadRow.classList.remove("hidden");
    overlayExportDownloadLink.href = `/downloads/${encodeDownloadPath(exportState.output_filename)}`;
    overlayExportDownloadLink.textContent = `Download ${exportState.output_filename}`;
  } else {
    overlayExportDownloadRow.classList.add("hidden");
  }

  overlayExportIsRunning = exportState.running;
  updateOverlayExportButtonEnabled();
  overlayExportStopBtn.disabled = !exportState.running;
  overlayExportClearBtn.disabled = exportState.running;
  renderOverlayExportLog(exportState.logs || []);
}

async function refreshOverlayExportStatus() {
  const res = await fetch("/api/overlay-export/status");
  const exportState = await res.json();
  renderOverlayExport(exportState);
}

let overlayExportIsRunning = false;

function updateOverlayExportButtonEnabled() {
  const hasSelection = !!overlayExportListingSelect.value && !overlayExportListingSelect.disabled;
  overlayExportBtn.disabled = overlayExportIsRunning || !hasSelection;
}

function renderOverlayItems(itemsState) {
  if (itemsState.status && itemsState.status !== "IDLE") {
    overlayItemsStatusBadge.classList.remove("hidden");
    overlayItemsStatusBadge.textContent = itemsState.status;
    overlayItemsStatusBadge.className = "badge " + (BADGE_CLASSES[itemsState.status] || "badge-idle");
  } else {
    overlayItemsStatusBadge.classList.add("hidden");
  }

  if (itemsState.status === "RUNNING" && itemsState.progress !== null && itemsState.progress !== undefined) {
    overlayItemsProgressRow.classList.remove("hidden");
    const pct = itemsState.progress;
    overlayItemsProgressFill.style.width = pct + "%";
    overlayItemsProgressLabel.textContent = pct.toFixed(1) + "%";
  } else {
    overlayItemsProgressRow.classList.add("hidden");
  }

  overlayItemsLoadBtn.disabled = itemsState.running;
  overlayItemsStopBtn.disabled = !itemsState.running;
  renderOverlayItemsLog(itemsState.logs || []);

  if (itemsState.status === "DONE" && itemsState.items && itemsState.items.length) {
    const previousValue = overlayExportListingSelect.value;
    overlayExportListingSelect.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = `Select an auction (${itemsState.items.length} found)...`;
    overlayExportListingSelect.appendChild(placeholder);
    for (const item of itemsState.items) {
      const option = document.createElement("option");
      option.value = item.listing_id;
      option.textContent = item.title;
      overlayExportListingSelect.appendChild(option);
    }
    overlayExportListingSelect.disabled = false;
    // Keep the previous selection if it's still in the freshly-loaded list
    // (e.g. after reloading the same event), rather than resetting to the
    // placeholder every time the status just happens to be polled again.
    if (previousValue && itemsState.items.some((i) => String(i.listing_id) === previousValue)) {
      overlayExportListingSelect.value = previousValue;
    }
  } else if (itemsState.status !== "RUNNING") {
    overlayExportListingSelect.innerHTML = '<option value="">Load auctions first...</option>';
    overlayExportListingSelect.disabled = true;
  }

  updateOverlayExportButtonEnabled();
}

async function refreshOverlayItemsStatus() {
  const res = await fetch("/api/overlay-items/status");
  const itemsState = await res.json();
  renderOverlayItems(itemsState);
}

async function startOverlayItems() {
  const eventUrl = overlayExportEventUrlInput.value.trim();
  if (!eventUrl) {
    alert("Please enter an eBay Live event URL.");
    return;
  }
  const res = await fetch("/api/overlay-items", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_url: eventUrl }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to start loading auctions.");
    return;
  }
  renderOverlayItemsLog.reset();
  refreshOverlayItemsStatus();
}

async function stopOverlayItems() {
  const res = await fetch("/api/overlay-items/stop", { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Failed to stop loading auctions.");
    return;
  }
  refreshOverlayItemsStatus();
}

function renderClipResults(clipList) {
  clipResultsListEl.innerHTML = "";
  if (!clipList || clipList.length === 0) {
    const empty = document.createElement("div");
    empty.className = "queue-list-empty";
    empty.textContent = "No clips requested yet.";
    clipResultsListEl.appendChild(empty);
    return;
  }

  for (const clip of clipList) {
    const row = document.createElement("div");
    row.className = "queue-item";

    const label = document.createElement("span");
    label.className = "queue-item-url";
    label.textContent = `${clip.start} - ${clip.end}`;

    const badge = document.createElement("span");
    badge.className = "badge " + (BADGE_CLASSES[clip.status] || "badge-idle");
    badge.textContent = clip.status;

    row.appendChild(label);
    row.appendChild(badge);

    if (clip.status === "DONE" && clip.output_filename) {
      const link = document.createElement("a");
      link.className = "live-event-item-link";
      link.href = `/downloads/${encodeDownloadPath(clip.output_filename)}`;
      link.textContent = "Download";
      link.setAttribute("download", "");
      row.appendChild(link);
    }

    clipResultsListEl.appendChild(row);
  }
}

function renderClipExport(exportState) {
  if (exportState.status && exportState.status !== "IDLE") {
    clipExportStatusBadge.classList.remove("hidden");
    clipExportStatusBadge.textContent = exportState.status;
    clipExportStatusBadge.className = "badge " + (BADGE_CLASSES[exportState.status] || "badge-idle");
  } else {
    clipExportStatusBadge.classList.add("hidden");
  }

  if (exportState.status === "RUNNING" && exportState.progress !== null && exportState.progress !== undefined) {
    clipExportProgressRow.classList.remove("hidden");
    const pct = exportState.progress;
    clipExportProgressFill.style.width = pct + "%";
    clipExportProgressLabel.textContent = pct.toFixed(1) + "%";
  } else {
    clipExportProgressRow.classList.add("hidden");
  }

  renderClipResults(exportState.clips);
  clipExportBtn.disabled = exportState.running;
  clipExportClearBtn.disabled = exportState.running;
  renderClipExportLog(exportState.logs || []);
}

async function refreshClipExportStatus() {
  const res = await fetch("/api/clip-export/status");
  const exportState = await res.json();
  renderClipExport(exportState);
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    refreshStatus();
    refreshQueueStatus();
    refreshOverlayExportStatus();
    refreshOverlayItemsStatus();
    refreshClipExportStatus();
  }, 2000);
}

saveSettingsBtn.addEventListener("click", saveSettings);
checkUpdatesBtn.addEventListener("click", checkForUpdates);
settingsGearBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleSettingsDropdown();
});
startBtn.addEventListener("click", startJob);
stopBtn.addEventListener("click", stopJob);
queueAddBtn.addEventListener("click", addToQueue);
resumeQueueBtn.addEventListener("click", resumeQueue);
clearQueueBtn.addEventListener("click", clearQueue);
overlayExportBtn.addEventListener("click", startOverlayExport);
overlayExportStopBtn.addEventListener("click", stopOverlayExport);
overlayExportClearBtn.addEventListener("click", clearOverlayExport);
overlayItemsLoadBtn.addEventListener("click", startOverlayItems);
overlayItemsStopBtn.addEventListener("click", stopOverlayItems);
overlayExportListingSelect.addEventListener("change", updateOverlayExportButtonEnabled);
clipAddRowBtn.addEventListener("click", () => addClipRow());
clipExportBtn.addEventListener("click", startClipExport);
clipExportClearBtn.addEventListener("click", clearClipExport);

addClipRow();
loadSettings();
loadAppVersion();
startPolling();
refreshStatus();
refreshQueueStatus();
refreshOverlayExportStatus();
refreshOverlayItemsStatus();
refreshClipExportStatus();
