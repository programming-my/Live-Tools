(function () {
  const statusPill = document.getElementById("statusPill");

  const headerAvatar = document.getElementById("headerAvatar");
  const headerAvatarFallback = document.getElementById("headerAvatarFallback");
  const headerUsername = document.getElementById("headerUsername");

  const viewerCountEl = document.getElementById("viewerCount");

  const chatMessages = document.getElementById("chatMessages");
  const pinnedMessage = document.getElementById("pinnedMessage");
  const pinnedUser = document.getElementById("pinnedUser");
  const pinnedText = document.getElementById("pinnedText");

  const auctionCard = document.getElementById("auctionCard");
  const auctionTitle = document.getElementById("auctionTitle");
  const auctionPrice = document.getElementById("auctionPrice");
  const auctionStatusRow = document.getElementById("auctionStatusRow");
  const auctionWinner = document.getElementById("auctionWinner");
  const auctionTimer = document.getElementById("auctionTimer");
  const bidPillContainer = document.getElementById("bidPillContainer");
  const bidPillFill = document.getElementById("bidPillFill");
  const bidPillText = document.getElementById("bidPillText");
  const bidPillChevron = document.getElementById("bidPillChevron");
  const maxBidBtn = document.getElementById("maxBidBtn");

  const MAX_CHAT_MESSAGES = 100;

  // Same technique as the minimal overlay's countdown: track "remaining
  // seconds as of a local performance.now() reading" rather than comparing
  // an absolute deadline against Date.now() - performance.now() is monotonic
  // and immune to this device's system clock being off from true time.
  let baseRemaining = null;
  let baseAt = null;
  let pinnedMessageId = null;

  function formatMoney(amount, currency) {
    if (amount === null || amount === undefined) return "";
    const symbol = currency === "USD" ? "$" : (currency || "");
    return symbol + amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatTimer(seconds) {
    // AuctionState never anchors remaining_seconds if a listing ends while
    // still "pinned" (e.g. a very short auction) - treat that None the same
    // as 0 here rather than showing a blank timer.
    const value = (seconds === null || seconds === undefined || Number.isNaN(seconds)) ? 0 : seconds;
    const total = Math.max(0, Math.round(value));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `🕐 ${m}:${String(s).padStart(2, "0")}`;
  }

  function updateCountdown() {
    if (baseRemaining === null) {
      auctionTimer.textContent = "";
      return;
    }
    const elapsed = (performance.now() - baseAt) / 1000;
    auctionTimer.textContent = formatTimer(baseRemaining - elapsed);
  }

  // Card visibility/content is entirely driven by the server's pin-lifecycle
  // tracking (see live/state.py) - "data" is null once the item is unpinned.
  // eBay's real mobile UI (per the reference screenshots) doesn't show a
  // "winner" line during active bidding, but showing who's currently
  // winning is more useful for monitoring than pure look-alike fidelity, so
  // it's shown here too (matching the original minimal overlay's behavior).
  function applyAuctionState(data) {
    if (!data) {
      auctionCard.classList.add("hidden");
      baseRemaining = null;
      return;
    }
    auctionCard.classList.remove("hidden");
    auctionTitle.textContent = data.title || "";
    auctionPrice.textContent = formatMoney(data.price, data.currency);

    if (data.phase === "active") {
      auctionStatusRow.classList.remove("hidden");
      auctionWinner.textContent = data.winner ? `🏆 ${data.winner} is winning` : "";
      baseRemaining = data.remaining_seconds;
      baseAt = performance.now();
      updateCountdown();
      // The top price is "where the bidding is at" (current winning bid);
      // the pill suggests the *next* bid, which eBay computes as
      // recommended_bid - always higher than price, to actually encourage a
      // bid rather than repeat the number already shown above. Falls back
      // to price itself before the first bid (no recommendation exists yet).
      const suggestedBid = data.recommended_bid ?? data.price;
      bidPillText.textContent = `Bid ${formatMoney(suggestedBid, data.currency)}`;
      bidPillFill.classList.remove("muted");
      bidPillContainer.classList.remove("muted");
      bidPillChevron.classList.remove("hidden");
      maxBidBtn.classList.remove("muted");
    } else if (data.phase === "ended") {
      auctionStatusRow.classList.remove("hidden");
      auctionWinner.textContent = data.winner ? `🏆 ${data.winner} won` : "No bids";
      // Frozen at 00:00 rather than ticking down from whatever residual
      // remaining_seconds it ended with (which may legitimately be null -
      // see formatTimer's null handling).
      baseRemaining = null;
      auctionTimer.textContent = formatTimer(0);
      bidPillText.textContent = "Sold out";
      bidPillFill.classList.add("muted");
      bidPillContainer.classList.add("muted");
      bidPillChevron.classList.add("hidden");
      maxBidBtn.classList.add("muted");
    } else {
      // "pinned" - item is up next but bidding hasn't started yet.
      auctionStatusRow.classList.add("hidden");
      baseRemaining = null;
      auctionTimer.textContent = "";
      bidPillText.textContent = "Starting soon";
      bidPillFill.classList.add("muted");
      bidPillContainer.classList.add("muted");
      bidPillChevron.classList.add("hidden");
      maxBidBtn.classList.add("muted");
    }
  }

  function setPinnedMessage(data) {
    pinnedMessageId = data.id || null;
    pinnedUser.textContent = data.user || "";
    pinnedText.textContent = data.text || "";
    pinnedMessage.classList.remove("hidden");
  }

  function clearPinnedMessage() {
    pinnedMessageId = null;
    pinnedMessage.classList.add("hidden");
  }

  function appendChatMessage(data) {
    if (data.featured) {
      setPinnedMessage(data);
      return;
    }
    const div = document.createElement("div");
    div.className = "chat-message";
    if (data.id) {
      div.dataset.messageId = data.id;
    }
    const userSpan = document.createElement("span");
    userSpan.className = "chat-user";
    userSpan.textContent = data.user || "";
    const textSpan = document.createElement("span");
    textSpan.className = "chat-text";
    textSpan.textContent = data.text || "";
    div.appendChild(userSpan);
    div.appendChild(textSpan);
    chatMessages.appendChild(div);
    while (chatMessages.children.length > MAX_CHAT_MESSAGES) {
      chatMessages.removeChild(chatMessages.firstChild);
    }
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  function removeChatMessage(data) {
    if (!data || !data.id) return;
    if (data.id === pinnedMessageId) {
      clearPinnedMessage();
    }
    const el = chatMessages.querySelector(`[data-message-id="${CSS.escape(data.id)}"]`);
    if (el) el.remove();
  }

  function applyViewerCount(data) {
    viewerCountEl.textContent = (data && data.count !== undefined && data.count !== null) ? data.count : "0";
  }

  function applyMeta(data) {
    if (!data) return;
    if (data.event_title) {
      document.title = data.event_title;
    }
    if (data.host_username) {
      headerUsername.textContent = data.host_username;
    }
    if (data.host_avatar_url) {
      headerAvatar.src = data.host_avatar_url;
      headerAvatar.classList.remove("hidden");
      headerAvatarFallback.classList.add("hidden");
    } else {
      headerAvatar.classList.add("hidden");
      headerAvatarFallback.classList.remove("hidden");
    }
  }

  function setStatus(state, message) {
    if (state === "live") {
      statusPill.classList.add("hidden");
      return;
    }
    statusPill.classList.remove("hidden");
    statusPill.className = "status-pill " + (state || "");
    statusPill.textContent = message || state || "";
  }

  function handleMessage(envelope) {
    switch (envelope.type) {
      case "auction":
        applyAuctionState(envelope.data);
        break;
      case "chat":
        appendChatMessage(envelope.data);
        break;
      case "chat_removed":
        removeChatMessage(envelope.data);
        break;
      case "viewer_count":
        applyViewerCount(envelope.data);
        break;
      case "meta":
        applyMeta(envelope.data);
        break;
      case "status":
        setStatus(envelope.data.state, envelope.data.message);
        break;
    }
  }

  function connect() {
    // Path is always /overlay/{event_id}/mobile - index 1, not the last
    // segment (which would be "mobile").
    const eventId = location.pathname.split("/").filter(Boolean)[1];
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${location.host}/ws/${eventId}`);
    let backoff = 1000;

    ws.onopen = () => {
      backoff = 1000;
      setStatus("live", "");
    };
    ws.onmessage = (event) => {
      try {
        handleMessage(JSON.parse(event.data));
      } catch (e) {
        // ignore malformed frames
      }
    };
    ws.onclose = () => {
      setStatus("reconnecting", "Reconnecting…");
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30000);
    };
    ws.onerror = () => ws.close();
  }

  connect();
  setInterval(updateCountdown, 250);
})();
