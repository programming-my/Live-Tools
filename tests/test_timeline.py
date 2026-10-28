from datetime import datetime, timezone

from overlay.timeline import build_auction_segments

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
