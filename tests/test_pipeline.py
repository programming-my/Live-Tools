from overlay.pipeline import ITEM_CLIP_PADDING_SECONDS, _clip_bounds_for_listing
from overlay.timeline import OverlayTimeline


def _segment(listing_id="111", pinned=100.0, unpinned=150.0, title="Test item"):
    return {"listing_id": listing_id, "pinned_offset": pinned, "unpinned_offset": unpinned, "bids": [], "title": title}


def test_clip_bounds_passthrough_when_time_scale_is_identity():
    """The standalone pipeline (no downloaded replay) always has
    time_offset=0, time_scale=1 - real-event-seconds and video-seconds are
    the same thing, so bounds should just be the segment's own
    [pinned_offset, unpinned_offset) padded on each side."""
    timeline = OverlayTimeline(auctions=[_segment(pinned=100.0, unpinned=150.0)], viewers=[])
    logs = []
    bounds = _clip_bounds_for_listing(timeline, "111", 1000.0, 1000.0, 0.0, 1.0, logs.append)
    assert bounds == (100.0 - ITEM_CLIP_PADDING_SECONDS, 150.0 + ITEM_CLIP_PADDING_SECONDS)


def test_clip_bounds_inverts_time_offset_and_scale():
    """The replay-calibrated pipeline maps video-second N to real-event-
    second (time_offset + N*time_scale) - _clip_bounds_for_listing must
    invert that to get back the right *video*-second range to actually
    render, given the segment's real-event-second boundaries."""
    time_offset, time_scale = 10.0, 2.0
    # Segment spans real-event-seconds [210, 410) -> without padding that's
    # video-seconds [(210-10)/2, (410-10)/2) = [100, 200).
    timeline = OverlayTimeline(auctions=[_segment(pinned=210.0, unpinned=410.0)], viewers=[])
    logs = []
    bounds = _clip_bounds_for_listing(timeline, "111", 100000.0, 5000.0, time_offset, time_scale, logs.append)
    expected_start = 100.0 - ITEM_CLIP_PADDING_SECONDS / time_scale
    expected_end = 200.0 + ITEM_CLIP_PADDING_SECONDS / time_scale
    assert bounds == (expected_start, expected_end)


def test_clip_bounds_clamps_to_available_video_duration():
    """A segment near the very start/end shouldn't request negative or
    out-of-range video seconds just because of the padding."""
    timeline = OverlayTimeline(auctions=[_segment(pinned=0.5, unpinned=5.0)], viewers=[])
    bounds = _clip_bounds_for_listing(timeline, "111", 1000.0, 1000.0, 0.0, 1.0, lambda m: None)
    assert bounds[0] == 0.0  # would've been negative without clamping


def test_clip_bounds_none_for_missing_listing():
    timeline = OverlayTimeline(auctions=[_segment(listing_id="111")], viewers=[])
    logs = []
    assert _clip_bounds_for_listing(timeline, "999", 1000.0, 1000.0, 0.0, 1.0, logs.append) is None
    assert any("No auction found" in msg for msg in logs)


def test_clip_bounds_handles_still_pinned_segment_with_no_unpin_yet():
    """A segment that's still pinned at the moment of export (unpinned_offset
    is None) should clip to the end of the available duration instead of
    crashing on an arithmetic op against None."""
    timeline = OverlayTimeline(auctions=[{"listing_id": "111", "pinned_offset": 900.0, "unpinned_offset": None, "bids": [], "title": "x"}], viewers=[])
    bounds = _clip_bounds_for_listing(timeline, "111", 1000.0, 1000.0, 0.0, 1.0, lambda m: None)
    assert bounds is not None
    assert bounds[1] == 1000.0  # clamped to the real/video duration, not unbounded
