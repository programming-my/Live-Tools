from __future__ import annotations

import re
import subprocess
from typing import Callable, Optional

import imageio_ffmpeg

_BARE_NUMBER_PATTERN = re.compile(r"^\d+(\.\d+)?$")
_REMOTE_SOURCE_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


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
    `source` can be a local file path (already-downloaded replay) or a
    remote URL (a .m3u8 replay manifest, clipped with no download needed).

    Local files are stream-copied (remuxed) - 10-50x faster since there's
    no decode/encode pass, at the cost of only cutting on the nearest
    keyframe rather than the exact requested second. This is only safe for
    local files: `-ss` input-seeking + stream-copy against this project's
    remote fMP4/CMAF HLS sources (seen on real eBay Live replay manifests)
    corrupts the H.264 bitstream while ffmpeg hunts for a copy-safe
    keyframe (a flood of "Invalid NAL unit size" errors), producing a
    near-empty, unplayable output file instead of erroring out cleanly - so
    remote sources fall back to the slower but reliable re-encode path.
    """
    log = on_log or (lambda msg: None)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    duration = max(0.1, end_seconds - start_seconds)
    is_remote = bool(_REMOTE_SOURCE_PATTERN.match(source))

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start_seconds),
    ]
    if is_remote:
        cmd += ["-tls_verify", "0"]
    cmd += ["-i", source, "-t", str(duration)]
    if is_remote:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", output_path]
    log(f"Running: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"ffmpeg failed:\n{result.stderr[-2000:]}")
        return False

    log(f"Clip saved to {output_path}")
    return True
