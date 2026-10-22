WS_URL = "wss://fanout.ebay.com/livecommerce/auction/v1/fastly/socket"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

AUCTION_MESSAGE_TYPES = [
    "AuctionWillStart", "AuctionWillStartV2", "AuctionStarted", "AuctionStartedV2",
    "AuctionNewBidLeader", "BidTimeExtended", "AuctionReset", "AuctionResetV2",
    "AuctionEnded", "AuctionEndedV2", "AuctionBiddingClosed",
]

# Carries each listing's pin state (isPinnedInEvent) plus its title/price -
# this is what drives card visibility: a listing pins ("starting soon"),
# eventually the AUCTION_MESSAGE_TYPES stream moves it through active/ended,
# and the card only disappears once this same listing is unpinned.
PIN_EVENT_TYPE = "EventListingsUpdated"

# Concurrent-viewer-count updates - carries {"concurrentViewers": <int>},
# confirmed against real live traffic on 2026-09-21. Actively handled (see
# _dispatch), not in KNOWN_NON_CHAT_TYPES below.
VIEWER_STATS_TYPE = "LiveEventViewerStatsUpdatedV2"

# Identified by watching real live traffic on 2026-08-31 - high-frequency
# housekeeping/telemetry types, not chat. Silently dropped rather than
# logged as "unknown", so that buffer stays useful for spotting the real
# chat typename instead of filling up with these within minutes.
# - ServerTimeBeacon: clock-sync ping, fires every few seconds
# - StreamReactionReceived: tap-to-react (heart) taps, not text chat
# - EventListingsUpdatedPatch: catalog/price metadata patch for a listing
# - StreamHealthNotice: seller-side stream quality warnings (e.g. low bitrate)
# - SubtitleCueReceived: closed-caption/subtitle text, fires very frequently -
#   flooded the 200-entry unknown-frame buffer within seconds when first seen
#   (2026-09-08), evicting a genuinely-unknown ChatMessageDeleted entry before
#   it could be inspected.
KNOWN_NON_CHAT_TYPES = {
    "ServerTimeBeacon",
    "StreamReactionReceived",
    "EventListingsUpdatedPatch",
    "StreamHealthNotice",
    "SubtitleCueReceived",
}

# On (re)connect, fetch this many trailing seconds of history so joining
# mid-auction doesn't show a blank card until the *next* listing starts.
# Generous default since a single auction can run well past its base
# duration via repeated BidTimeExtended resets.
CATCHUP_WINDOW_SECONDS = 600

# Reconnect backoff (seconds): starts at RECONNECT_BACKOFF_INITIAL, doubles
# each failed attempt, capped at RECONNECT_BACKOFF_MAX.
RECONNECT_BACKOFF_INITIAL = 1
RECONNECT_BACKOFF_MAX = 60

# Longer than the 8s heartbeat interval (and the 10s STOMP heart-beat we
# negotiate) so a real hang is distinguishable from a quiet lull.
READ_TIMEOUT_SECONDS = 30

HEARTBEAT_INTERVAL_SECONDS = 8
