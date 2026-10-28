from __future__ import annotations

import re
import subprocess
from typing import Callable, Optional

import imageio_ffmpeg


def probe_video(video_path: str) -> dict:
    """Returns {width, height, fps, duration_seconds, video_bitrate_kbps} for a video file."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run([ffmpeg, "-i", video_path], capture_output=True, text=True)
    stderr = result.stderr

    stream_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})[^,]*,.*?([\d.]+)\s*fps", stderr)
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", stderr)
    # The per-stream bitrate (not the container's overall bitrate, which
    # includes audio) - immediately precedes the fps figure on the same line.
    bitrate_match = re.search(r"Video:.*?(\d+)\s*kb/s,\s*[\d.]+\s*fps", stderr)

    width = int(stream_match.group(1)) if stream_match else None
    height = int(stream_match.group(2)) if stream_match else None
    fps = float(stream_match.group(3)) if stream_match else None
    video_bitrate_kbps = int(bitrate_match.group(1)) if bitrate_match else None

    duration_seconds = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    return {
        "width": width, "height": height, "fps": fps,
        "duration_seconds": duration_seconds, "video_bitrate_kbps": video_bitrate_kbps,
    }


def _parse_progress_block(lines: list) -> Optional[float]:
    """Extracts out_time_ms (seconds) from a batch of `-progress pipe:1` key=value lines."""
    for line in lines:
        if line.startswith("out_time_ms="):
            try:
                return int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                return None
    return None


def export_chroma_key_overlay(
    overlay_frames_dir: str,
    fps: int,
    duration_seconds: Optional[float],
    output_path: str,
    on_log: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Encodes the rendered PNG-sequence overlay (composited over a solid
    chroma-key blue background - see template.html) into its own
    standalone compressed .mp4, for use as a separate layer in external
    editing software via chroma keying. Nothing from the source video is
    baked in here - this is the overlay graphics alone, meant to be keyed
    and layered over the original footage.

    Blue rather than the more common green - the bid pill's own green
    (#4ce160) is close enough to pure chroma green that keying would eat
    into it too (confirmed by testing); nothing else in this design is
    blue-toned, so blue keys cleanly with no collision.

    A real alpha channel (HEVC-with-Alpha, ProRes 4444) was tried first but
    ffmpeg's hevc_videotoolbox encoder did not actually preserve the alpha
    plane through to the output despite accepting the relevant flags
    (confirmed by decoding a frame back out - alpha was gone) - chroma key
    sidesteps that entirely with a universally-supported plain H.264 .mp4.

    Encodes at the overlay's own low render fps rather than upsampling to
    the source video's frame rate - the content only changes about once a
    second regardless, and total duration/timing stays exactly accurate
    either way (what actually matters for lining up against the source
    footage in an editor), so there's no reason to pay for a much larger
    file just for smoother scrubbing.
    """
    log = on_log or (lambda msg: None)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    cmd = [
        ffmpeg, "-y",
        "-framerate", str(fps),
        "-i", f"{overlay_frames_dir}/overlay_%06d.png",
        "-c:v", "h264_videotoolbox",
        "-b:v", "2000k",
        "-loglevel", "error",
        "-progress", "pipe:1",
        output_path,
    ]
    log(f"Running: {' '.join(cmd)}")

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert process.stdout is not None

    block: list = []
    last_logged_pct = -10
    for line in process.stdout:
        line = line.strip()
        block.append(line)
        if line == "progress=continue" or line == "progress=end":
            current_time = _parse_progress_block(block)
            block = []
            if current_time is not None and duration_seconds:
                pct = min(100.0, current_time / duration_seconds * 100)
                if pct - last_logged_pct >= 5:
                    log(f"Export progress: {pct:.0f}% ({current_time:.0f}s / {duration_seconds:.0f}s)")
                    last_logged_pct = pct

    return_code = process.wait()
    stderr_output = process.stderr.read() if process.stderr else ""
    if return_code != 0:
        log(f"ffmpeg failed:\n{stderr_output[-3000:]}")
        return False
    log(f"Chroma-key overlay written to {output_path}")
    return True
