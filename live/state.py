from __future__ import annotations

from datetime import datetime
from typing import Optional

from .auth import parse_iso

STARTED_TYPES = ("AuctionStarted", "AuctionStartedV2")
ENDED_TYPES = ("AuctionEnded", "AuctionEndedV2", "AuctionBiddingClosed")

# Card visibility phases. Driven by the item's pin lifecycle (EventListingsUpdated's
# isPinnedInEvent), not a timer: "pinned" starts as soon as the item is pinned
# (shown as "starting soon", before AuctionStarted fires), "active" is normal
# bidding, "ended" persists indefinitely - the card only disappears once the
# item is explicitly unpinned.
PHASE_PINNED = "pinned"
PHASE_ACTIVE = "active"
PHASE_ENDED = "ended"


def _epoch_ms_to_dt(ms: int, tz) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=tz)


class AuctionState:
    """
    Holds the single currently-displayed listing's state, updated
    incrementally as events arrive one at a time (unlike the offline
    overlay's timeline.py, which reconstructs a full batch of segments
    after the fact).

    Visibility is pin-driven (apply_pin/apply_unpin, from EventListingsUpdated)
    rather than time-based: a card appears the moment its item is pinned and
    stays up - through "starting soon", live bidding, and "ended" - until
    that same item is explicitly unpinned. apply_event handles the
    AuctionStarted/AuctionNewBidLeader/BidTimeExtended/AuctionEnded stream on
    top of whichever listing is currently pinned.

    Mirrors the offline overlay's tricky semantics for BidTimeExtended: it
    resets the countdown to ~10s from ITS OWN firing time (not a fixed
    extension of the original duration), so a rolling deadline history is
    kept rather than a single overwritten value - a late/out-of-order frame
    (e.g. after a reconnect gap) still resolves to whichever entry is
    chronologically newest by the events' own serverCreatedAt, regardless of
    receipt order.

    The countdown itself is tracked as a (remaining_seconds, anchor) pair
    rather than an absolute deadline, and deliberately never compares an
    eBay-supplied timestamp directly against a locally-read "now": the
    ~10s duration is computed purely from eBay's own two timestamps
    (serverCreatedAt -> endingAt, so it's correct regardless of any skew
    between this machine's clock and eBay's), then anchored to *this
    process's own* clock reading at the moment it was applied. snapshot()
    later re-derives remaining_seconds by diffing its `now` against that
    same anchor - both readings come from the same clock, so any constant
    offset that clock has from true time cancels out. (Confirmed
    2026-09-11: comparing eBay's endingAt directly against a locally-read
    now - as this used to do - made a fresh 10s reset display as 12s,
    because whichever clock ends up reading "now" doesn't perfectly agree
    with eBay's.)
    """

    def __init__(self) -> None:
        self.listing_id: Optional[str] = None
        self.title: Optional[str] = None
        self.price: Optional[float] = None
        self.currency: str = "USD"
        self.winner: Optional[str] = None
        # eBay's own suggested next-bid amount, from AuctionNewBidLeader's
        # recommendedBid - intentionally higher than `price` (the current
        # winning bid) to encourage the next bid, not a duplicate of it.
        # None before the first bid (no recommendation exists yet).
        self.recommended_bid: Optional[float] = None
        self.phase: Optional[str] = None
        self._deadline_history: list = []  # [(change_at, deadline), ...] in eBay's own clock frame - sorted by change_at, used only to pick the authoritative entry
        self._chosen_deadline: Optional[datetime] = None  # the winning entry's absolute deadline (eBay's clock) - change-detection only, never compared to a local "now"
        self._remaining_at_anchor: Optional[float] = None  # seconds remaining as of _anchor_at
        self._anchor_at: Optional[datetime] = None  # this process's own clock reading when _remaining_at_anchor was captured

    def apply_pin(self, listing_id: str, title: Optional[str], price: Optional[float], currency: str) -> bool:
        """A listing was just pinned (isPinnedInEvent: true). If it's a new
        listing, resets to the "starting soon" phase. Re-affirming the
        listing already being displayed is a no-op - once AuctionStarted has
        moved it past "pinned", this must not stomp on tracked bid state."""
        if listing_id == self.listing_id:
            return False
        self.listing_id = listing_id
        self.title = title or f"Item {listing_id}"
        self.price = price
        self.currency = currency
        self.winner = None
        self.recommended_bid = None
        self.phase = PHASE_PINNED
        self._deadline_history = []
        self._chosen_deadline = None
        self._remaining_at_anchor = None
        self._anchor_at = None
        return True

    def apply_unpin(self, listing_id: str) -> bool:
        """The currently-displayed listing was unpinned - clear the card
        entirely, regardless of phase. Returns False for an unpin of some
        other (already-past) listing, which changes nothing visible."""
        if listing_id != self.listing_id or self.listing_id is None:
            return False
        self.listing_id = None
        self.title = None
        self.price = None
        self.winner = None
        self.recommended_bid = None
        self.phase = None
        self._deadline_history = []
        self._chosen_deadline = None
        self._remaining_at_anchor = None
        self._anchor_at = None
        return True

    def apply_event(self, event: dict, now: datetime) -> bool:
        """
        Mutates state from ONE incoming auction-lifecycle event. Returns
        True iff the visible card actually changed, so the caller only
        broadcasts on real changes rather than on every message received.

        `now` must be tz-aware (datetime.now(timezone.utc)) - its tzinfo is
        reused to interpret event timestamps.
        """
        typename = event.get("typename")
        listing_id = event.get("listingId")

        if typename in STARTED_TYPES:
            if not listing_id:
                return False
            if listing_id == self.listing_id and self.phase == PHASE_PINNED:
                self._activate(event, now)
                return True
            if listing_id != self.listing_id:
                # AuctionStarted for a listing we never saw pinned - e.g. we
                # joined mid-event before this session existed, or the
                # catch-up window didn't reach far enough back. Fall back to
                # a full reset (with a placeholder title) rather than
                # showing nothing.
                self._reset_from_auction_started(listing_id, event, now)
                return True
            return False  # already active/ended for this listing - AuctionStarted repeated, ignore

        if listing_id != self.listing_id or self.listing_id is None:
            # Stray/late event for a listing we've already moved past.
            return False

        if typename == "AuctionNewBidLeader":
            winning_bid = (event.get("winningBid") or {}).get("original", {})
            bidder = event.get("bidder") or {}
            new_price = winning_bid.get("amount")
            new_winner = bidder.get("userName") or "?"
            new_recommended_bid = (event.get("recommendedBid") or {}).get("original", {}).get("amount")
            changed = (
                new_price != self.price or new_winner != self.winner
                or new_recommended_bid != self.recommended_bid
            )
            self.price = new_price
            self.winner = new_winner
            self.recommended_bid = new_recommended_bid
            return changed

        if typename == "BidTimeExtended":
            ending_at = event.get("endingAt") or event.get("newEndingAt")
            change_at = _server_created_at(event, now)
            new_deadline = parse_iso(ending_at) if ending_at else change_at
            self._deadline_history.append((change_at, new_deadline))
            self._deadline_history.sort(key=lambda pair: pair[0])
            # Always take the chronologically-latest entry (by the event's own
            # serverCreatedAt), so out-of-order delivery can't apply a stale
            # extension over a newer one - regardless of receipt order.
            latest_change_at, latest_deadline = self._deadline_history[-1]
            changed = latest_deadline != self._chosen_deadline
            self._chosen_deadline = latest_deadline
            # Duration is computed from eBay's own two timestamps (skew-free),
            # then anchored to *our* clock's reading right now rather than to
            # eBay's change_at - see the class docstring for why.
            duration = (latest_deadline - latest_change_at).total_seconds()
            self._remaining_at_anchor = max(0.0, duration)
            self._anchor_at = now
            return changed

        if typename in ENDED_TYPES:
            if self.phase == PHASE_ENDED:
                return False
            self.phase = PHASE_ENDED
            return True

        # AuctionWillStart(V2)/AuctionReset(V2): observed, but don't change
        # the visible card.
        return False

    def _activate(self, event: dict, now: datetime) -> None:
        """Transitions an already-pinned listing into live bidding, filling
        in the real deadline (title/starting price already came from the
        pin event, so they're left as-is unless this event overrides them)."""
        self.phase = PHASE_ACTIVE
        started_at = event.get("startedAt")
        ending_at = event.get("endingAt")
        deadline = parse_iso(ending_at) if ending_at else None
        self._deadline_history = []
        self._chosen_deadline = deadline
        if deadline is not None:
            change_at = parse_iso(started_at) if started_at else deadline
            self._deadline_history.append((change_at, deadline))
            self._remaining_at_anchor = max(0.0, (deadline - change_at).total_seconds())
            self._anchor_at = now
        else:
            self._remaining_at_anchor = None
            self._anchor_at = None

        price = (event.get("startingPrice") or {}).get("original", {})
        if price.get("amount") is not None:
            self.price = price.get("amount")
            self.currency = price.get("currencyCode", self.currency)

    def _reset_from_auction_started(self, listing_id: str, event: dict, now: datetime) -> None:
        self.listing_id = listing_id
        self.title = f"Item {listing_id}"
        self.winner = None
        self.recommended_bid = None
        self._deadline_history = []
        self._activate(event, now)

    def snapshot(self, now: datetime) -> Optional[dict]:
        if self.listing_id is None or self.phase is None:
            return None
        remaining_seconds = None
        if self._remaining_at_anchor is not None and self._anchor_at is not None:
            elapsed = (now - self._anchor_at).total_seconds()
            remaining_seconds = max(0.0, self._remaining_at_anchor - elapsed)
        return {
            "listing_id": self.listing_id,
            "title": self.title,
            "price": self.price,
            "currency": self.currency,
            "winner": self.winner,
            "recommended_bid": self.recommended_bid,
            "phase": self.phase,
            "remaining_seconds": remaining_seconds,
        }


def _server_created_at(event: dict, now: datetime) -> datetime:
    server_created_at = event.get("serverCreatedAt")
    if server_created_at is None:
        return now
    return _epoch_ms_to_dt(server_created_at, now.tzinfo)
