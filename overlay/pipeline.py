from __future__ import annotations

import re
import tempfile
from datetime import datetime
from typing import Callable, Optional

from .compose import composite_overlay, export_chroma_key_overlay, probe_video
from .protocol import fetch_full_history
from .render import render_frames
from .timeline import build_timeline

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

RENDER_PROGRESS_PATTERN = re.compile(r"Rendered (\d+)/(\d+) frames\.$")
COMPOSITE_PROGRESS_PATTERN = re.compile(r"Compositing progress: ([\d.]+)%")
EXPORT_PROGRESS_PATTERN = re.compile(r"Export progress: ([\d.]+)%")


def _fetch_and_calibrate(
    event_url: str, source_video_path: str, log: Callable[[str], None], progress: Callable[[float], None],
    time_offset_override: Optional[float], time_scale_override: Optional[float],
):
    """
    Shared first half of both pipelines: fetch history, build the timeline,
    probe the source video, and compute the time_offset/time_scale mapping
    from video-seconds to real-event-seconds. Returns
    (timeline, video_info, time_offset, time_scale) or None if the video's
    duration couldn't be determined (nothing usable to render against).
    """
    log("Fetching auction/bid history...")
    history = fetch_full_history(event_url, on_log=log)
    progress(FETCH_DONE_PCT)

    log("Reconstructing timeline...")
    timeline = build_timeline(history, on_log=log)
    log(f"Timeline ready: {len(timeline.auctions)} auction segments.")
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

    return timeline, video_info, time_offset, time_scale


def build_overlay_video(
    event_url: str,
    source_video_path: str,
    output_path: str,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    fps: int = DEFAULT_FPS,
    workers: int = DEFAULT_WORKERS,
    time_offset_override: Optional[float] = None,
    time_scale_override: Optional[float] = None,
) -> bool:
    """
    Full pipeline: fetch the event's auction history, reconstruct a
    timeline, render overlay frames, and composite them onto the source
    video. Returns True on success.
    """
    log = on_log or (lambda msg: None)
    progress = on_progress or (lambda pct: None)

    def log_and_track_render(msg: str) -> None:
        log(msg)
        match = RENDER_PROGRESS_PATTERN.search(msg)
        if match:
            done, total = int(match.group(1)), int(match.group(2))
            frac = done / total if total else 0
            progress(TIMELINE_DONE_PCT + frac * (RENDER_DONE_PCT - TIMELINE_DONE_PCT))

    def log_and_track_composite(msg: str) -> None:
        log(msg)
        match = COMPOSITE_PROGRESS_PATTERN.search(msg)
        if match:
            frac = float(match.group(1)) / 100
            progress(RENDER_DONE_PCT + frac * (COMPOSITE_DONE_PCT - RENDER_DONE_PCT))

    progress(0)
    calibrated = _fetch_and_calibrate(
        event_url, source_video_path, log, progress, time_offset_override, time_scale_override,
    )
    if calibrated is None:
        return False
    timeline, video_info, time_offset, time_scale = calibrated
    duration = video_info["duration_seconds"]

    with tempfile.TemporaryDirectory(prefix="overlay_frames_") as frames_dir:
        log(f"Rendering overlay frames to {frames_dir} (fps={fps}, workers={workers})...")
        render_frames(
            timeline, 0, duration, fps, frames_dir,
            video_width=video_info["width"], video_height=video_info["height"],
            on_log=log_and_track_render, workers=workers, time_scale=time_scale, time_offset=time_offset,
        )
        progress(RENDER_DONE_PCT)

        log("Compositing overlay onto video...")
        success = composite_overlay(source_video_path, frames_dir, fps, output_path, on_log=log_and_track_composite)

    if success:
        progress(COMPOSITE_DONE_PCT)
        log(f"Done! Output saved to {output_path}")
    return success


def _render_and_export_chroma_key(
    timeline, duration: float, width: int, height: int, output_path: str,
    fps: int, workers: int, time_offset: float, time_scale: float,
    log: Callable[[str], None], progress: Callable[[float], None],
) -> bool:
    """Shared render+export tail for build_chroma_key_overlay_video and
    build_standalone_chroma_key_overlay_video - the only difference between
    them is how (timeline, duration, width, height, time_offset, time_scale)
    get computed upstream (calibrated against a source video, or derived
    directly from the event's own real start/end time)."""

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
            timeline, 0, duration, fps, frames_dir,
            video_width=width, video_height=height,
            on_log=log_and_track_render, workers=workers, time_scale=time_scale, time_offset=time_offset,
        )
        progress(RENDER_DONE_PCT)

        log("Exporting standalone chroma-key overlay video...")
        success = export_chroma_key_overlay(frames_dir, fps, duration, output_path, on_log=log_and_track_export)

    if success:
        progress(COMPOSITE_DONE_PCT)
        log(f"Done! Output saved to {output_path}")
    return success


def build_chroma_key_overlay_video(
    event_url: str,
    source_video_path: str,
    output_path: str,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    fps: int = DEFAULT_FPS,
    workers: int = DEFAULT_WORKERS,
    time_offset_override: Optional[float] = None,
    time_scale_override: Optional[float] = None,
) -> bool:
    """
    Same fetch/timeline/calibration/render steps as build_overlay_video, but
    exports the rendered overlay as its own standalone chroma-key video
    (see compose.export_chroma_key_overlay) instead of baking it onto a
    copy of the source video - for use as a separate layer in external
    editing software via chroma keying. source_video_path is only read
    (probed for duration/resolution/calibration), never modified or
    re-encoded.
    """
    log = on_log or (lambda msg: None)
    progress = on_progress or (lambda pct: None)

    progress(0)
    calibrated = _fetch_and_calibrate(
        event_url, source_video_path, log, progress, time_offset_override, time_scale_override,
    )
    if calibrated is None:
        return False
    timeline, video_info, time_offset, time_scale = calibrated
    duration = video_info["duration_seconds"]

    return _render_and_export_chroma_key(
        timeline, duration, video_info["width"], video_info["height"], output_path,
        fps, workers, time_offset, time_scale, log, progress,
    )


DEFAULT_STANDALONE_WIDTH = 720
DEFAULT_STANDALONE_HEIGHT = 1280


def build_standalone_chroma_key_overlay_video(
    event_url: str,
    output_path: str,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    fps: int = DEFAULT_FPS,
    workers: int = DEFAULT_WORKERS,
    width: int = DEFAULT_STANDALONE_WIDTH,
    height: int = DEFAULT_STANDALONE_HEIGHT,
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
    log("Fetching auction/bid history...")
    history = fetch_full_history(event_url, on_log=log)
    progress(FETCH_DONE_PCT)

    log("Reconstructing timeline...")
    timeline = build_timeline(history, on_log=log)
    log(f"Timeline ready: {len(timeline.auctions)} auction segments.")
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
    log(f"Event duration: {duration:.0f}s - no source video, rendering at {width}x{height} in real time.")

    return _render_and_export_chroma_key(
        timeline, duration, width, height, output_path,
        fps, workers, 0.0, 1.0, log, progress,
    )
