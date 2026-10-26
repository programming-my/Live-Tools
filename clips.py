from __future__ import annotations

import re
import subprocess
from typing import Callable, Optional

import imageio_ffmpeg

_BARE_NUMBER_PATTERN = re.compile(r"^\d+(\.\d+)?$")


def parse_timestamp_to_seconds(raw_value: str) -> float:
    """Accepts mm:ss, hh:mm:ss, or a bare number of seconds."""
    value = (raw_value or "").strip()
    if not value:
        raise ValueError("Timestamp is required")

    if _BARE_NUMBER_PATTERN.match(value):
        return float(value)

    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Unsupported timestamp format: {value}")

    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        raise ValueError(f"Invalid timestamp: {value}")

    if any(n < 0 for n in numbers):
        raise ValueError(f"Timestamp must be 0 or greater: {value}")

    if len(numbers) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            raise ValueError(f"Invalid timestamp {value}. Use mm:ss or hh:mm:ss.")
        return minutes * 60 + seconds

    hours, minutes, seconds = numbers
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Invalid timestamp {value}. Use hh:mm:ss or mm:ss.")
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_timecode(total_seconds: float) -> str:
    """Dashes rather than colons - filesystem-safe, matches this project's
    existing MM-DD-YY dash convention for filenames."""
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}-{minutes:02d}-{seconds:02d}"


def extract_clip(
    source: str, output_path: str, start_seconds: float, end_seconds: float,
    on_log: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Extracts [start_seconds, end_seconds) from `source` into output_path.
    `source` can be a local file path or a remote URL (e.g. a .m3u8 replay
    manifest) - ffmpeg reads both the same way, so no separate branching is
    needed for "already downloaded" vs. "clip straight from the live stream".

    Stream-copies (remuxes) rather than re-encoding - 10-50x faster since
    there's no decode/encode pass, at the cost of only being able to cut on
    the source's keyframe boundaries rather than the exact requested second
    (typically within a few seconds, depending on the source's keyframe
    interval). `-ss` before `-i` (input seek) is what actually skips to
    that nearest keyframe; ffmpeg can't start a copied stream mid-frame.
    """
    log = on_log or (lambda msg: None)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    duration = max(0.1, end_seconds - start_seconds)

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start_seconds),
        "-i", source,
        "-t", str(duration),
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    log(f"Running: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"ffmpeg failed:\n{result.stderr[-2000:]}")
        return False

    log(f"Clip saved to {output_path}")
    return True
