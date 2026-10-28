from __future__ import annotations

import re
import tempfile
from datetime import datetime
from typing import Callable, Optional

from .compose import export_chroma_key_overlay, probe_video
from .protocol import fetch_history_for_title
from .render import render_frames
from .timeline import build_timeline, find_listing_segment

DEFAULT_FPS = 1
DEFAULT_WORKERS = 10

# Progress budget across the pipeline's stages - rendering and compositing
# both scale with video duration and dominate the runtime for long events,
# so they get the bulk of the range. Fetch/timeline are comparatively quick
# and have no fine-grained progress of their own, so they just jump ahead.
FETCH_DONE_PCT = 5
TIMELINE_DONE_PCT = 10
RENDER_DONE_PCT = 70
COMPOSITE_DONE_PCT = 100

# Extra seconds rendered before the listing's own pinned_offset and after its
# unpinned_offset, so the card's pin/unpin transition isn't cut off at frame
# zero or the very last frame.
ITEM_CLIP_PADDING_SECONDS = 2.0

RENDER_PROGRESS_PATTERN = re.compile(r"Rendered (\d+)/(\d+) frames\.$")
EXPORT_PROGRESS_PATTERN = re.compile(r"Export progress: ([\d.]+)%")


def _clip_bounds_for_listing(
    timeline, listing_id: str, real_duration_seconds: float, video_duration_seconds: float,
    time_offset: float, time_scale: float, log: Callable[[str], None],
) -> Optional[tuple]:
    """Finds listing_id's segment and converts its [pinned_offset,
    unpinned_offset) - in real-event-seconds, like everything in the
    timeline - into the (start, end) *video*-second range render_frames
    actually needs, inverting the same time_offset/time_scale mapping
    render_frames itself applies the other way (real = offset + video*scale).
    For the standalone pipeline (time_offset=0, time_scale=1) this is a
    no-op passthrough. Returns None (after logging why) if nothing usable
    was found."""
    segment = find_listing_segment(timeline, listing_id, on_log=log)
    if segment is None:
        return None

    unpinned = segment.get("unpinned_offset")
    real_end = unpinned if unpinned is not None else real_duration_seconds
    real_start = max(0.0, segment["pinned_offset"] - ITEM_CLIP_PADDING_SECONDS)
    real_end = min(real_duration_seconds, real_end + ITEM_CLIP_PADDING_SECONDS)

    video_start = max(0.0, (real_start - time_offset) / time_scale)
    video_end = min(video_duration_seconds, (real_end - time_offset) / time_scale)
    if video_end <= video_start:
        log(f"Item number {listing_id}'s matched segment doesn't land inside the available video - nothing to export.")
        return None

    log(f"Exporting item {listing_id} (\"{segment['title']}\"): {video_start:.1f}s..{video_end:.1f}s ({video_end - video_start:.1f}s) of the source.")
    return video_start, video_end


def _fetch_history_for_title_and_build_timeline(
    event_url: str, title_query: str,
    on_log: Callable[[str], None], on_progress: Callable[[float], None],
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[tuple]:
    """Searches for title_query (see protocol.fetch_history_for_title -
    stops scanning pin/listing history as soon as a complete match is
    found, then fetches auction/bid events only for that matched segment's
    own narrow range, not the whole event) and builds a timeline from
    whatever that targeted fetch returned. Returns (history, timeline,
    matched_listing_id), or None if nothing matched anywhere in the event."""
    history = fetch_history_for_title(
        event_url, title_query, on_log=on_log, should_stop=should_stop,
        on_progress=lambda frac: on_progress(frac * FETCH_DONE_PCT),
    )
    on_progress(FETCH_DONE_PCT)
    if history is None:
        return None

    on_log("Reconstructing timeline...")
    timeline = build_timeline(history, on_log=on_log)
    return history, timeline, history["matched_listing_id"]


def _fetch_and_calibrate(
    event_url: str, title_query: str, source_video_path: str, log: Callable[[str], None], progress: Callable[[float], None],
    time_offset_override: Optional[float], time_scale_override: Optional[float],
    should_stop: Optional[Callable[[], bool]] = None,
):
    """
    Shared first half of both pipelines: search for title_query and fetch
    just its own segment's data (see
    _fetch_history_for_title_and_build_timeline), probe the source video,
    and compute the time_offset/time_scale mapping from video-seconds to
    real-event-seconds. Returns (timeline, matched_listing_id, video_info,
    time_offset, time_scale), or None if no match was found or the video's
    duration couldn't be determined (nothing usable to render against).
    """
    found = _fetch_history_for_title_and_build_timeline(event_url, title_query, log, progress, should_stop)
    if found is None:
        return None
    history, timeline, matched_listing_id = found
    progress(TIMELINE_DONE_PCT)

    video_info = probe_video(source_video_path)
    duration = video_info["duration_seconds"]
    if not duration:
        log("Could not determine video duration - aborting.")
        return None
    log(f"Source video: {video_info['width']}x{video_info['height']} @ {video_info['fps']}fps, {duration:.0f}s")

    # The downloaded replay's duration rarely matches the real event window
    # (start_time..end_time) exactly - dropped/missing HLS segments are common
    # over a multi-hour recording - so video-second N isn't real-event-second N.
    # Rescale so the timeline lookup tracks the video's actual pace instead of
    # drifting further behind (or ahead) the longer the video runs.
    #
    # This scale-only correction does NOT account for the replay recording
    # simply starting some seconds after the event's official start_time (a
    # fixed lag, not a proportional drift) - the page's startTime and the
    # LiveEventStarted message agree with each other but neither reflects
    # when the video's actual first frame was captured, and nothing in the
    # metadata reveals that gap. When known (from calibrating against a few
    # real-world reference points), pass time_offset_override/
    # time_scale_override to correct for it; otherwise this falls back to
    # the scale-only estimate, which is usually a large improvement over no
    # correction but can still leave a residual constant-ish lag.
    time_offset = 0.0
    time_scale = 1.0
    if history.get("end_time"):
        event_start = datetime.fromisoformat(history["start_time"].replace("Z", "+00:00"))
        event_end = datetime.fromisoformat(history["end_time"].replace("Z", "+00:00"))
        real_duration = (event_end - event_start).total_seconds()
        if real_duration > 0:
            time_scale = real_duration / duration
            log(f"Real event window: {real_duration:.0f}s vs video: {duration:.0f}s -> time_scale={time_scale:.4f}")

    if time_offset_override is not None:
        time_offset = time_offset_override
    if time_scale_override is not None:
        time_scale = time_scale_override
    if time_offset_override is not None or time_scale_override is not None:
        log(f"Using calibrated time mapping: time_offset={time_offset:.2f}, time_scale={time_scale:.4f}")

    return timeline, matched_listing_id, video_info, time_offset, time_scale


def _render_and_export_chroma_key(
    timeline, start_offset: float, end_offset: float, width: int, height: int, output_path: str,
    fps: int, workers: int, time_offset: float, time_scale: float,
    log: Callable[[str], None], progress: Callable[[float], None],
) -> bool:
    """Shared render+export tail for build_chroma_key_overlay_video and
    build_standalone_chroma_key_overlay_video - the only difference between
    them is how (timeline, start_offset/end_offset, width, height,
    time_offset, time_scale) get computed upstream (calibrated against a
    source video, or derived directly from the event's own real start/end
    time). [start_offset, end_offset) are in *video* seconds - just the one
    requested listing's clip, not the whole event (see
    _clip_bounds_for_listing)."""

    def log_and_track_render(msg: str) -> None:
        log(msg)
        match = RENDER_PROGRESS_PATTERN.search(msg)
        if match:
            done, total = int(match.group(1)), int(match.group(2))
            frac = done / total if total else 0
            progress(TIMELINE_DONE_PCT + frac * (RENDER_DONE_PCT - TIMELINE_DONE_PCT))

    def log_and_track_export(msg: str) -> None:
        log(msg)
        match = EXPORT_PROGRESS_PATTERN.search(msg)
        if match:
            frac = float(match.group(1)) / 100
            progress(RENDER_DONE_PCT + frac * (COMPOSITE_DONE_PCT - RENDER_DONE_PCT))

    with tempfile.TemporaryDirectory(prefix="overlay_frames_") as frames_dir:
        log(f"Rendering overlay frames to {frames_dir} (fps={fps}, workers={workers})...")
        render_frames(
            timeline, start_offset, end_offset, fps, frames_dir,
            video_width=width, video_height=height,
            on_log=log_and_track_render, workers=workers, time_scale=time_scale, time_offset=time_offset,
        )
        progress(RENDER_DONE_PCT)

        log("Exporting standalone chroma-key overlay video...")
        success = export_chroma_key_overlay(frames_dir, fps, end_offset - start_offset, output_path, on_log=log_and_track_export)

    if success:
        progress(COMPOSITE_DONE_PCT)
        log(f"Done! Output saved to {output_path}")
    return success


def build_chroma_key_overlay_video(
    event_url: str,
    source_video_path: str,
    output_path: str,
    title_query: str,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    fps: int = DEFAULT_FPS,
    workers: int = DEFAULT_WORKERS,
    time_offset_override: Optional[float] = None,
    time_scale_override: Optional[float] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    """
    Exports just one listing's auction overlay (found by title_query - a
    case-insensitive substring match against its pinned listing title) as
    its own standalone chroma-key video (see compose.export_chroma_key_overlay),
    calibrated against a downloaded replay's actual duration/resolution -
    for use as a separate layer in external editing software via chroma
    keying. source_video_path is only read (probed for duration/resolution/
    calibration), never modified or re-encoded.
    """
    log = on_log or (lambda msg: None)
    progress = on_progress or (lambda pct: None)

    progress(0)
    calibrated = _fetch_and_calibrate(
        event_url, title_query, source_video_path, log, progress, time_offset_override, time_scale_override, should_stop,
    )
    if calibrated is None:
        return False
    if should_stop is not None and should_stop():
        log("Stopped by user.")
        return False
    timeline, matched_listing_id, video_info, time_offset, time_scale = calibrated
    duration = video_info["duration_seconds"]

    real_duration = time_offset + duration * time_scale
    bounds = _clip_bounds_for_listing(timeline, matched_listing_id, real_duration, duration, time_offset, time_scale, log)
    if bounds is None:
        return False
    start_offset, end_offset = bounds

    return _render_and_export_chroma_key(
        timeline, start_offset, end_offset, video_info["width"], video_info["height"], output_path,
        fps, workers, time_offset, time_scale, log, progress,
    )


DEFAULT_STANDALONE_WIDTH = 720
DEFAULT_STANDALONE_HEIGHT = 1280


def build_standalone_chroma_key_overlay_video(
    event_url: str,
    output_path: str,
    title_query: str,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    fps: int = DEFAULT_FPS,
    workers: int = DEFAULT_WORKERS,
    width: int = DEFAULT_STANDALONE_WIDTH,
    height: int = DEFAULT_STANDALONE_HEIGHT,
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    """
    Same idea as build_chroma_key_overlay_video, but for when there's no
    downloaded replay to calibrate against - renders directly against the
    event's own real start/end time (time_offset=0, time_scale=1.0, so
    video-second N is real-event-second N exactly) at a fixed resolution
    instead of probing a source video for one. Requires the event to have
    already ended (a known end_time) since there's no video to derive a
    duration from otherwise.
    """
    log = on_log or (lambda msg: None)
    progress = on_progress or (lambda pct: None)

    progress(0)
    found = _fetch_history_for_title_and_build_timeline(event_url, title_query, log, progress, should_stop)
    if found is None:
        return False
    history, timeline, matched_listing_id = found
    if should_stop is not None and should_stop():
        log("Stopped by user.")
        return False
    progress(TIMELINE_DONE_PCT)

    if not history.get("end_time"):
        log("This event hasn't ended yet (no end time) - can't determine how long to render for.")
        return False

    event_start = datetime.fromisoformat(history["start_time"].replace("Z", "+00:00"))
    event_end = datetime.fromisoformat(history["end_time"].replace("Z", "+00:00"))
    duration = (event_end - event_start).total_seconds()
    if duration <= 0:
        log("Could not determine a valid event duration - aborting.")
        return False
    log(f"Event duration: {duration:.0f}s - no source video, rendering at {width}x{height}.")

    bounds = _clip_bounds_for_listing(timeline, matched_listing_id, duration, duration, 0.0, 1.0, log)
    if bounds is None:
        return False
    start_offset, end_offset = bounds

    return _render_and_export_chroma_key(
        timeline, start_offset, end_offset, width, height, output_path,
        fps, workers, 0.0, 1.0, log, progress,
    )
