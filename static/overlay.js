(function () {
  const auctionCard = document.getElementById("auctionCard");
  const auctionTitle = document.getElementById("auctionTitle");
  const auctionPrice = document.getElementById("auctionPrice");
  const auctionWinner = document.getElementById("auctionWinner");
  const auctionTimer = document.getElementById("auctionTimer");
  const chatMessages = document.getElementById("chatMessages");
  const chatControls = document.getElementById("chatControls");
  const chatFontDec = document.getElementById("chatFontDec");
  const chatFontInc = document.getElementById("chatFontInc");
  const statusPill = document.getElementById("statusPill");
  const bidPillFill = document.getElementById("bidPillFill");
  const bidPillText = document.getElementById("bidPillText");
  const bidPillChevron = document.getElementById("bidPillChevron");
  const fullscreenBtn = document.getElementById("fullscreenBtn");

  const MAX_CHAT_MESSAGES = 100;
  const FONT_SIZE_KEY = "ebayLiveOverlay.chatFontSize";
  const FONT_SIZE_MIN = 10;
  const FONT_SIZE_MAX = 48;

  // Countdown is tracked as "remaining seconds as of a local performance.now()
  // reading" rather than an absolute deadline compared against Date.now() -
  // performance.now() is monotonic and immune to this device's system clock
  // being off from true time (confirmed 2026-09-11: comparing an absolute
  // server-issued deadline against Date.now() made a fresh 10s reset display
  // as 12s, because this device's clock didn't perfectly agree with eBay's).
  let baseRemaining = null;
  let baseAt = null;
  let countdownTimer = null;

  function formatMoney(amount, currency) {
    if (amount === null || amount === undefined) return "";
    const symbol = currency === "USD" ? "$" : (currency || "");
    return symbol + amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatTimer(seconds) {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "";
    const total = Math.max(0, Math.round(seconds));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `⏱ ${m}:${String(s).padStart(2, "0")}`;
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
  // tracking (see live/state.py) - "data" is null once the item is unpinned,
  // otherwise data.phase is "pinned" (starting soon, before AuctionStarted),
  // "active" (live bidding), or "ended" (stays up with the winner's name
  // until the item is unpinned - no client-side timer involved).
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
      auctionWinner.textContent = data.winner ? `🏆 ${data.winner} is winning` : "";
      auctionTimer.textContent = "";
      baseRemaining = data.remaining_seconds;
      baseAt = performance.now();
      updateCountdown();
      bidPillText.textContent = "Bid now";
      bidPillFill.classList.remove("muted");
      bidPillChevron.classList.remove("hidden");
    } else if (data.phase === "ended") {
      auctionWinner.textContent = data.winner ? `🏆 ${data.winner} won` : "Auction ended - no bids";
      auctionTimer.textContent = "";
      baseRemaining = null;
      bidPillText.textContent = "Auction ended";
      bidPillFill.classList.add("muted");
      bidPillChevron.classList.add("hidden");
    } else {
      // "pinned" - item is up next but bidding hasn't started yet.
      auctionWinner.textContent = "";
      auctionTimer.textContent = "";
      baseRemaining = null;
      bidPillText.textContent = "Starting soon";
      bidPillFill.classList.add("muted");
      bidPillChevron.classList.add("hidden");
    }
  }

  function appendChatMessage(data) {
    const div = document.createElement("div");
    div.className = "chat-message";
    if (data.id) {
      div.dataset.messageId = data.id;
    }
    const userSpan = document.createElement("span");
    userSpan.className = "chat-user";
    userSpan.textContent = data.user ? `${data.user}:` : "";
    const textSpan = document.createElement("span");
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
    const el = chatMessages.querySelector(`[data-message-id="${CSS.escape(data.id)}"]`);
    if (el) el.remove();
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
      case "status":
        setStatus(envelope.data.state, envelope.data.message);
        break;
    }
  }

  function connect() {
    const eventId = location.pathname.split("/").filter(Boolean).pop();
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

  function applyFontSize(size) {
    document.documentElement.style.setProperty("--chat-font-size", size + "px");
  }

  function initFontSizeControl() {
    let fontSize = parseInt(localStorage.getItem(FONT_SIZE_KEY), 10);
    if (!Number.isFinite(fontSize)) fontSize = 20;
    applyFontSize(fontSize);

    chatFontDec.addEventListener("click", () => {
      fontSize = Math.max(FONT_SIZE_MIN, fontSize - 2);
      applyFontSize(fontSize);
      localStorage.setItem(FONT_SIZE_KEY, fontSize);
    });
    chatFontInc.addEventListener("click", () => {
      fontSize = Math.min(FONT_SIZE_MAX, fontSize + 2);
      applyFontSize(fontSize);
      localStorage.setItem(FONT_SIZE_KEY, fontSize);
    });
  }

  function applyHideControls() {
    const params = new URLSearchParams(location.search);
    if (params.get("hideControls") === "1") {
      chatControls.classList.add("hidden"); // fullscreenBtn goes with it, being its child
    }
  }

  // iOS Safari has no reliable fullscreen API for arbitrary pages - use
  // "Add to Home Screen" there instead (see the meta tags in overlay.html).
  // This button is a bonus for platforms where requestFullscreen() does
  // work (Android Chrome, most desktop browsers, some iPadOS versions).
  function initFullscreenControl() {
    if (!document.documentElement.requestFullscreen) return;
    fullscreenBtn.classList.remove("hidden");
    fullscreenBtn.addEventListener("click", () => {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        document.documentElement.requestFullscreen().catch(() => {});
      }
    });
  }

  initFontSizeControl();
  initFullscreenControl();
  applyHideControls();
  connect();
  countdownTimer = setInterval(updateCountdown, 250);
})();
