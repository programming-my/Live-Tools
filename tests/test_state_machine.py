import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from live.state import AuctionState

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_auction_events.json")


def load_events():
    with open(FIXTURE_PATH) as f:
        return json.load(f)["auctions"]


def event_time(event):
    return datetime.fromtimestamp(event["serverCreatedAt"] / 1000, tz=timezone.utc)


def test_pin_shows_starting_soon_before_auction_started():
    state = AuctionState()
    changed = state.apply_pin("1", "Rolex Datejust", 100.0, "USD")

    assert changed is True
    snap = state.snapshot(datetime.now(timezone.utc))
    assert snap["phase"] == "pinned"
    assert snap["title"] == "Rolex Datejust"
    assert snap["price"] == 100.0
    assert snap["winner"] is None
    assert snap["remaining_seconds"] is None


def test_auction_started_transitions_pinned_listing_to_active():
    state = AuctionState()
    state.apply_pin("1", "Rolex Datejust", 100.0, "USD")

    now = datetime.now(timezone.utc)
    changed = state.apply_event({
        "typename": "AuctionStarted", "listingId": "1",
        "startedAt": now.isoformat(), "endingAt": (now + timedelta(seconds=30)).isoformat(),
        "startingPrice": {"original": {"amount": 100.0, "currencyCode": "USD"}},
        "serverCreatedAt": int(now.timestamp() * 1000),
    }, now)

    assert changed is True
    snap = state.snapshot(now)
    assert snap["phase"] == "active"
    assert snap["title"] == "Rolex Datejust"  # preserved from the pin event, not reset to a placeholder
    assert snap["remaining_seconds"] == pytest.approx(30.0)


def test_auction_started_without_prior_pin_falls_back_to_placeholder():
    # e.g. joined mid-event before this session existed, or the catch-up
    # window didn't reach back far enough to see the pin.
    state = AuctionState()
    now = datetime.now(timezone.utc)
    changed = state.apply_event({
        "typename": "AuctionStarted", "listingId": "999",
        "startedAt": now.isoformat(), "endingAt": (now + timedelta(seconds=30)).isoformat(),
        "startingPrice": {"original": {"amount": 5.0, "currencyCode": "USD"}},
        "serverCreatedAt": int(now.timestamp() * 1000),
    }, now)

    assert changed is True
    snap = state.snapshot(now)
    assert snap["phase"] == "active"
    assert snap["title"] == "Item 999"


def test_ended_phase_persists_until_unpinned():
    events = [e for e in load_events() if e["listingId"] == "128000000001"]
    state = AuctionState()
    for event in events:
        state.apply_event(event, event_time(event))

    ended_at = event_time(events[-1])
    snap = state.snapshot(ended_at)
    assert snap["phase"] == "ended"
    assert snap["winner"] == "bob"

    # No timer involved - stays "ended" arbitrarily long afterwards...
    snap_later = state.snapshot(ended_at + timedelta(hours=2))
    assert snap_later is not None
    assert snap_later["phase"] == "ended"
    assert snap_later["winner"] == "bob"

    # ...until the listing is explicitly unpinned.
    unpinned = state.apply_unpin("128000000001")
    assert unpinned is True
    assert state.snapshot(ended_at + timedelta(hours=2)) is None


def test_unpin_of_a_different_listing_is_a_no_op():
    state = AuctionState()
    state.apply_pin("1", "Item One", 10.0, "USD")

    changed = state.apply_unpin("some-other-listing")

    assert changed is False
    assert state.snapshot(datetime.now(timezone.utc)) is not None


def test_new_bid_leader_carries_a_recommended_bid_higher_than_the_current_price():
    # recommendedBid is eBay's own suggested next bid - intentionally higher
    # than the current winning price, to encourage the next bid rather than
    # duplicate what's already showing as "where the bidding is at."
    state = AuctionState()
    state.apply_pin("1", "Item One", 10.0, "USD")
    now = datetime.now(timezone.utc)

    changed = state.apply_event({
        "typename": "AuctionNewBidLeader", "listingId": "1",
        "winningBid": {"original": {"amount": 980.0, "currencyCode": "USD"}},
        "recommendedBid": {"original": {"amount": 990.0, "currencyCode": "USD"}},
        "bidder": {"userName": "alice"},
        "serverCreatedAt": 0,
    }, now)

    assert changed is True
    snap = state.snapshot(now)
    assert snap["price"] == 980.0
    assert snap["recommended_bid"] == 990.0
    assert snap["recommended_bid"] > snap["price"]


def test_recommended_bid_is_none_before_any_bid_and_cleared_on_repin():
    state = AuctionState()
    state.apply_pin("1", "Item One", 10.0, "USD")

    assert state.snapshot(datetime.now(timezone.utc))["recommended_bid"] is None

    state.apply_event({
        "typename": "AuctionNewBidLeader", "listingId": "1",
        "winningBid": {"original": {"amount": 15.0}},
        "recommendedBid": {"original": {"amount": 20.0}},
        "bidder": {"userName": "alice"},
        "serverCreatedAt": 0,
    }, datetime.now(timezone.utc))
    assert state.recommended_bid == 20.0

    state.apply_pin("2", "Item Two", 5.0, "USD")
    assert state.snapshot(datetime.now(timezone.utc))["recommended_bid"] is None


def test_pinning_a_new_listing_replaces_the_old_one():
    state = AuctionState()
    state.apply_pin("1", "Item One", 10.0, "USD")
    state.apply_event({
        "typename": "AuctionNewBidLeader", "listingId": "1",
        "winningBid": {"original": {"amount": 15.0}}, "bidder": {"userName": "alice"},
        "serverCreatedAt": 0,
    }, datetime.now(timezone.utc))

    changed = state.apply_pin("2", "Item Two", 20.0, "USD")

    assert changed is True
    snap = state.snapshot(datetime.now(timezone.utc))
    assert snap["listing_id"] == "2"
    assert snap["phase"] == "pinned"
    assert snap["price"] == 20.0
    assert snap["winner"] is None  # item 1's winner must not leak through


def test_repinning_the_same_listing_is_a_no_op():
    state = AuctionState()
    state.apply_pin("1", "Item One", 10.0, "USD")
    now = datetime.now(timezone.utc)
    state.apply_event({
        "typename": "AuctionStarted", "listingId": "1",
        "startedAt": now.isoformat(), "endingAt": (now + timedelta(seconds=30)).isoformat(),
        "startingPrice": {"original": {"amount": 10.0, "currencyCode": "USD"}},
        "serverCreatedAt": int(now.timestamp() * 1000),
    }, now)
    state.apply_event({
        "typename": "AuctionNewBidLeader", "listingId": "1",
        "winningBid": {"original": {"amount": 25.0}}, "bidder": {"userName": "alice"},
        "serverCreatedAt": 0,
    }, now)

    # A second EventListingsUpdated re-affirming the same pinned listing
    # must not wipe out the bid we already tracked.
    changed = state.apply_pin("1", "Item One", 10.0, "USD")

    assert changed is False
    snap = state.snapshot(now)
    assert snap["phase"] == "active"
    assert snap["price"] == 25.0
    assert snap["winner"] == "alice"


def test_bid_time_extended_moves_deadline_forward():
    events = [
        e for e in load_events()
        if e["listingId"] == "128000000001" and e["typename"] != "AuctionEnded"
    ]
    state = AuctionState()
    for event in events:
        state.apply_event(event, event_time(event))

    snap = state.snapshot(event_time(events[-1]))
    # BidTimeExtended fired at 12:00:28 with endingAt 12:00:38 - a fresh ~10s
    # reset from its own firing time, applied with no elapsed time yet since
    # the snapshot is taken at that same instant.
    assert snap["remaining_seconds"] == pytest.approx(10.0)


def test_bid_time_extended_countdown_ticks_down_using_its_own_anchor():
    # Guards against re-deriving remaining time from an absolute deadline
    # compared to `now` (the original bug: any clock disagreement between
    # this process and eBay's servers corrupted the displayed countdown) -
    # remaining_seconds must instead shrink by exactly the elapsed time
    # since the extension was applied, regardless of what "now" is in
    # absolute terms.
    events = [
        e for e in load_events()
        if e["listingId"] == "128000000001" and e["typename"] != "AuctionEnded"
    ]
    state = AuctionState()
    for event in events:
        state.apply_event(event, event_time(event))

    later = event_time(events[-1]) + timedelta(seconds=4)
    snap = state.snapshot(later)
    assert snap["remaining_seconds"] == pytest.approx(6.0)


def test_will_start_does_not_change_card():
    state = AuctionState()
    now = datetime.now(timezone.utc)
    changed = state.apply_event(
        {"typename": "AuctionWillStart", "listingId": "999", "serverCreatedAt": int(now.timestamp() * 1000)}, now,
    )
    assert changed is False
    assert state.listing_id is None


def test_stray_event_for_past_listing_is_ignored():
    state = AuctionState()
    now = datetime.now(timezone.utc)
    state.apply_pin("A", "Item A", 5.0, "USD")
    state.apply_pin("B", "Item B", 1.0, "USD")

    changed = state.apply_event({
        "typename": "AuctionNewBidLeader", "listingId": "A",
        "winningBid": {"original": {"amount": 999}}, "bidder": {"userName": "late"},
        "serverCreatedAt": int(now.timestamp() * 1000),
    }, now)

    assert changed is False
    assert state.listing_id == "B"


def test_apply_event_returns_false_when_nothing_changes():
    events = [e for e in load_events() if e["listingId"] == "128000000001"]
    state = AuctionState()
    for event in events:
        state.apply_event(event, event_time(event))

    # Re-applying the exact same final (AuctionEnded) event again is a no-op.
    changed = state.apply_event(events[-1], event_time(events[-1]))
    assert changed is False
