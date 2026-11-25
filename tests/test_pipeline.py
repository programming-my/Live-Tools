import overlay.pipeline as pipeline
from overlay.pipeline import (
    ITEM_CLIP_PADDING_SECONDS,
    _clip_bounds_for_listing,
    _fetch_history_and_build_timeline,
    build_chroma_key_overlay_video,
    build_standalone_chroma_key_overlay_video,
)
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


def _sample_history(matched_listing_id=None):
    history = {
        "event_id": "evt1",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:16:40Z",  # 1000s event window
        "auctions": [],
        "pin_updates": [],
        "viewer_stats": [],
        "lost_ranges": [],
    }
    if matched_listing_id is not None:
        history["matched_listing_id"] = matched_listing_id
    return history


def test_fetch_history_and_build_timeline_uses_full_history_when_no_title_query(monkeypatch):
    """No title_query -> full-event export: fetches the whole event's
    history (not a narrowed title search) and reports matched_listing_id as
    None, so callers know to render the entire timeline instead of clipping
    to one listing."""
    calls = []
    monkeypatch.setattr(pipeline, "fetch_full_history", lambda *a, **k: calls.append("full") or _sample_history())
    monkeypatch.setattr(pipeline, "fetch_history_for_title", lambda *a, **k: calls.append("title") or _sample_history("999"))

    result = _fetch_history_and_build_timeline("https://example.com/event", None, lambda m: None, lambda p: None)

    assert calls == ["full"]
    assert result is not None
    _history, timeline, matched_listing_id = result
    assert matched_listing_id is None
    assert timeline.auctions == []


def test_fetch_history_and_build_timeline_searches_by_title_when_given(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "fetch_full_history", lambda *a, **k: calls.append("full") or _sample_history())
    monkeypatch.setattr(pipeline, "fetch_history_for_title", lambda *a, **k: calls.append("title") or _sample_history("123"))

    result = _fetch_history_and_build_timeline("https://example.com/event", "Vintage Watch", lambda m: None, lambda p: None)

    assert calls == ["title"]
    assert result is not None
    _history, _timeline, matched_listing_id = result
    assert matched_listing_id == "123"


def test_fetch_history_and_build_timeline_returns_none_when_title_not_found(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_history_for_title", lambda *a, **k: None)
    result = _fetch_history_and_build_timeline("https://example.com/event", "Nonexistent", lambda m: None, lambda p: None)
    assert result is None


def test_build_chroma_key_overlay_video_renders_whole_video_when_no_title_query(monkeypatch):
    """With no title_query, the full-event export should render across the
    entire source video (start=0, end=duration), not clip down to a single
    listing's segment - _clip_bounds_for_listing should never be consulted
    in this path."""
    monkeypatch.setattr(pipeline, "fetch_full_history", lambda *a, **k: _sample_history())
    monkeypatch.setattr(
        pipeline, "probe_video",
        lambda path: {"width": 1920, "height": 1080, "fps": 30.0, "duration_seconds": 500.0, "video_bitrate_kbps": 4000},
    )

    def fail_if_called(*a, **k):
        raise AssertionError("_clip_bounds_for_listing should not be called for a full-event export")
    monkeypatch.setattr(pipeline, "_clip_bounds_for_listing", fail_if_called)

    captured = {}

    def fake_render_and_export(timeline, start_offset, end_offset, width, height, output_path, fps, workers, time_offset, time_scale, log, progress):
        captured["start_offset"] = start_offset
        captured["end_offset"] = end_offset
        return True
    monkeypatch.setattr(pipeline, "_render_and_export_chroma_key", fake_render_and_export)

    success = build_chroma_key_overlay_video("https://example.com/event", "/tmp/fake.mp4", "/tmp/out.mp4", title_query=None)

    assert success is True
    assert captured["start_offset"] == 0.0
    assert captured["end_offset"] == 500.0


def test_build_standalone_chroma_key_overlay_video_renders_whole_event_when_no_title_query(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_full_history", lambda *a, **k: _sample_history())

    def fail_if_called(*a, **k):
        raise AssertionError("_clip_bounds_for_listing should not be called for a full-event export")
    monkeypatch.setattr(pipeline, "_clip_bounds_for_listing", fail_if_called)

    captured = {}

    def fake_render_and_export(timeline, start_offset, end_offset, width, height, output_path, fps, workers, time_offset, time_scale, log, progress):
        captured["start_offset"] = start_offset
        captured["end_offset"] = end_offset
        return True
    monkeypatch.setattr(pipeline, "_render_and_export_chroma_key", fake_render_and_export)

    success = build_standalone_chroma_key_overlay_video("https://example.com/event", "/tmp/out.mp4", title_query=None)

    assert success is True
    assert captured["start_offset"] == 0.0
    assert captured["end_offset"] == 1000.0  # the sample history's 1000s event window
