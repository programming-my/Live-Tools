from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from playwright.sync_api import sync_playwright

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
FRAME_GLOB = "overlay_%06d.png"


def _render_chunk(
    timeline, frame_indices: list, start_offset: float, frame_interval: float,
    output_dir: str, video_width: int, video_height: int, time_scale: float, time_offset: float,
) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": video_width, "height": video_height})
        page.goto(f"file://{TEMPLATE_PATH}")
        page.wait_for_function("typeof window.applyState === 'function'")

        for i in frame_indices:
            t = start_offset + i * frame_interval
            state = timeline.full_state_at(time_offset + t * time_scale)
            page.evaluate("(state) => window.applyState(state)", state)
            page.screenshot(path=os.path.join(output_dir, FRAME_GLOB % i), omit_background=True)

        browser.close()
    return len(frame_indices)


def render_frames(
    timeline,
    start_offset: float,
    end_offset: float,
    fps: int,
    output_dir: str,
    video_width: int = 720,
    video_height: int = 1280,
    on_log: Optional[Callable[[str], None]] = None,
    workers: int = 6,
    time_scale: float = 1.0,
    time_offset: float = 0.0,
) -> int:
    """
    Renders transparent PNG overlay frames for [start_offset, end_offset) at the
    given fps, driven programmatically (not real-time) by the timeline's
    full_state_at(). Splits the frame range across `workers` parallel headless
    browsers - each frame is independent (pure function of its offset), so
    this parallelizes cleanly. Returns the number of frames rendered.

    time_offset/time_scale correct for the source video's timeline not lining
    up 1:1 with the real event window (start_time..end_time) the reconstructed
    timeline is keyed to - video-second N is actually real-event-second
    (time_offset + N*time_scale). A pure scale (time_offset=0) only accounts
    for the video's total duration differing from the real event's; it does
    NOT account for the replay recording simply starting some seconds after
    the event's official start_time (a fixed lag, not a proportional drift) -
    that requires a nonzero time_offset, generally only obtainable by
    calibrating against a few known real-world reference points.
    """
    log = on_log or (lambda msg: None)
    os.makedirs(output_dir, exist_ok=True)
    frame_interval = 1.0 / fps
    total_frames = max(0, int((end_offset - start_offset) * fps))
    if total_frames == 0:
        return 0

    workers = max(1, min(workers, total_frames))
    all_indices = list(range(total_frames))
    chunk_size = (total_frames + workers - 1) // workers
    chunks = [all_indices[i:i + chunk_size] for i in range(0, total_frames, chunk_size)]

    log(f"Rendering {total_frames} frames across {len(chunks)} workers...")
    completed = 0
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        futures = {
            pool.submit(_render_chunk, timeline, chunk, start_offset, frame_interval, output_dir, video_width, video_height, time_scale, time_offset): chunk
            for chunk in chunks
        }
        for future in as_completed(futures):
            completed += future.result()
            log(f"Rendered {completed}/{total_frames} frames.")

    log(f"Rendered {total_frames} frames to {output_dir}")
    return total_frames
