from datetime import datetime, timezone

from overlay.timeline import OverlayTimeline, build_auction_segments, find_complete_segment_by_title, find_listing_segment

START_DT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ms(offset_seconds: float) -> int:
    return int((START_DT.timestamp() + offset_seconds) * 1000)


def _bid_event(listing_id: str, offset: float, amount: float, user: str = "bidder1") -> dict:
    return {
        "typename": "AuctionNewBidLeader",
        "listingId": listing_id,
        "serverCreatedAt": _ms(offset),
        "winningBid": {"original": {"amount": amount, "currencyCode": "USD"}},
        "bidder": {"userName": user},
    }


def test_orphaned_listing_with_no_start_event_still_shows_up_via_bids():
    """The real bug: a listing whose AuctionStarted event was lost upstream
    (never landed in any pin segment) used to vanish from the render
    entirely, even with plenty of real bid data for it. It should now be
    reconstructed from the earliest/latest bid offsets instead."""
    listing_id = "111"
    auction_events = [
        _bid_event(listing_id, 100.0, 50.0),
        _bid_event(listing_id, 105.0, 60.0),
        _bid_event(listing_id, 110.0, 73700.0),
    ]
    logs = []
    segments = build_auction_segments([], auction_events, START_DT, on_log=logs.append)

    assert len(segments) == 1
    seg = segments[0]
    assert seg["listing_id"] == listing_id
    assert seg["pinned_offset"] == 100.0
    assert len(seg["bids"]) == 3
    assert seg["bids"][-1]["amount"] == 73700.0
    assert any("reconstructing its segment" in msg for msg in logs)


def test_orphaned_listing_with_truly_nothing_usable_is_dropped():
    """An orphaned event with no start_offset, no bids, and no end_offset -
    e.g. just a stray AuctionWillStart - has nothing to build a segment
    from, so it should still be dropped (not a regression)."""
    listing_id = "222"
    auction_events = [
        {"typename": "AuctionWillStart", "listingId": listing_id, "serverCreatedAt": _ms(50.0)},
    ]
    segments = build_auction_segments([], auction_events, START_DT)
    assert segments == []


def test_orphan_summary_logs_once_per_listing_not_once_per_event():
    """A flood of individual orphaned events (e.g. a contested item with
    hundreds of rapid bids) should produce one summary log line per
    listing, not one line per event - the latter previously evicted far
    more useful log history (including fetch-phase warnings) from the
    export job's bounded log buffer."""
    listing_id = "333"
    auction_events = [_bid_event(listing_id, 10.0 + i, 100.0 + i) for i in range(50)]
    logs = []
    build_auction_segments([], auction_events, START_DT, on_log=logs.append)

    per_event_lines = [m for m in logs if "No pin segment covers" in m]
    assert per_event_lines == []
    summary_lines = [m for m in logs if "50 event(s)" in m]
    assert len(summary_lines) == 1


def test_find_listing_segment_returns_none_for_unknown_item():
    timeline = OverlayTimeline(auctions=[{"listing_id": "111", "pinned_offset": 0, "unpinned_offset": 10, "bids": []}], viewers=[])
    assert find_listing_segment(timeline, "999") is None


def test_find_listing_segment_matches_by_string_comparison():
    """listing_id may come in as an int (from parsed event data) or a str
    (from a user-typed item number) - must match either way."""
    timeline = OverlayTimeline(auctions=[{"listing_id": 111, "pinned_offset": 0, "unpinned_offset": 10, "bids": []}], viewers=[])
    found = find_listing_segment(timeline, "111")
    assert found is not None
    assert found["listing_id"] == 111


def test_find_listing_segment_prefers_the_occurrence_with_real_bids():
    """A listing pinned twice (a pre-start flicker, then the real auction)
    must resolve to the one with actual bidding activity, not whichever
    happens to be longer or come first."""
    flicker = {"listing_id": "111", "pinned_offset": 0, "unpinned_offset": 500, "bids": []}
    real = {"listing_id": "111", "pinned_offset": 600, "unpinned_offset": 650, "bids": [{"amount": 10}, {"amount": 20}]}
    timeline = OverlayTimeline(auctions=[flicker, real], viewers=[])
    logs = []
    found = find_listing_segment(timeline, "111", on_log=logs.append)
    assert found is real
    assert any("pinned 2 separate times" in msg for msg in logs)


def _pin_event(offset: float, listing_id: str, pinned: bool, title: str = None, start_dt: datetime = None) -> dict:
    start_dt = start_dt or START_DT
    listing = {"listingId": listing_id, "isPinnedInEvent": pinned}
    if pinned:
        listing["title"] = title
    return {
        "serverCreatedAt": int((start_dt.timestamp() + offset) * 1000),
        "eventListings": [listing],
    }


def test_find_complete_segment_by_title_requires_both_pin_and_unpin():
    """A listing that's only been pinned so far (no matching unpin event
    yet seen) must NOT match, even if its title matches - its real bounds
    aren't known yet, so stopping here would be premature (the whole point
    of "complete" is to avoid that)."""
    pin_events = [_pin_event(0, "111", True, title="Vintage Watch")]
    assert find_complete_segment_by_title(pin_events, "watch", START_DT) is None


def test_find_complete_segment_by_title_matches_case_insensitive_substring():
    pin_events = [
        _pin_event(0, "111", True, title="Vintage Rolex Watch"),
        _pin_event(10, "111", False),
    ]
    found = find_complete_segment_by_title(pin_events, "ROLEX", START_DT)
    assert found is not None
    assert found["listing_id"] == "111"


def test_find_complete_segment_by_title_returns_none_when_nothing_matches():
    pin_events = [
        _pin_event(0, "111", True, title="Vintage Watch"),
        _pin_event(10, "111", False),
    ]
    assert find_complete_segment_by_title(pin_events, "coin", START_DT) is None


def test_find_complete_segment_by_title_prefers_the_longer_match_when_tied_on_bids():
    """Two different completed listings both match the query (a common
    word in both titles, e.g. two "Vintage Coin" lots) - pick one via the
    same ranking find_listing_segment uses. At this stage (pin segments
    only, before any bid events are attached - see build_auction_segments)
    bid counts are always 0, so the tie-break in practice comes down to
    which pinned segment lasted longer."""
    pin_events = [
        _pin_event(0, "111", True, title="Vintage Coin A"),
        _pin_event(10, "111", False),  # 10s pinned
        _pin_event(20, "222", True, title="Vintage Coin B"),
        _pin_event(90, "222", False),  # 70s pinned - longer
    ]
    found = find_complete_segment_by_title(pin_events, "coin", START_DT)
    assert found["listing_id"] == "222"
