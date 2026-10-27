from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional
from urllib.parse import urljoin

import requests
import urllib3

import clips
from monitor import HEADERS

# The replay CDN's certificate chain is self-signed - see monitor.py's
# --no-check-certificate / -tls_verify 0 fixes for the same reason. Silences
# the resulting per-request warning noise.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_STREAM_INF_PATTERN = re.compile(r"^#EXT-X-STREAM-INF:")
_EXTINF_PATTERN = re.compile(r"^#EXTINF:([\d.]+),")
_MAP_URI_PATTERN = re.compile(r'^#EXT-X-MAP:URI="([^"]+)"')


@dataclass
class Segment:
    start: float
    end: float
    url: str
    init_uri: str


def _fetch_text(url: str) -> str:
    response = requests.get(url, headers=HEADERS, verify=False, timeout=15)
    response.raise_for_status()
    return response.text


def _resolve_media_playlist(manifest_url: str) -> tuple[str, str]:
    """Some replay manifests are themselves an HLS master playlist with one
    #EXT-X-STREAM-INF variant pointing at the real CDN media playlist
    (confirmed on a real eBay Live replay manifest) - follow that redirect-
    like indirection if present. Returns (media_playlist_url, its text)."""
    text = _fetch_text(manifest_url)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _STREAM_INF_PATTERN.match(line):
            for candidate in lines[index + 1:]:
                candidate = candidate.strip()
                if candidate and not candidate.startswith("#"):
                    media_url = urljoin(manifest_url, candidate)
                    return media_url, _fetch_text(media_url)
    return manifest_url, text


def _parse_segments(playlist_url: str, text: str) -> List[Segment]:
    """Walks a media playlist's #EXTINF/segment-URI pairs, tracking the
    *current* #EXT-X-MAP init segment as it goes - some replay manifests
    declare a fresh one partway through at a discontinuity (see
    monitor.py's YTDLP_INIT_FRAGMENT_BUG_SIGNATURE comment for the same
    quirk affecting yt-dlp), so segments after that point belong to the
    later init segment, not the first one seen."""
    segments: List[Segment] = []
    cumulative_time = 0.0
    current_init_uri: Optional[str] = None
    pending_duration: Optional[float] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        map_match = _MAP_URI_PATTERN.match(line)
        if map_match:
            current_init_uri = urljoin(playlist_url, map_match.group(1))
            continue

        extinf_match = _EXTINF_PATTERN.match(line)
        if extinf_match:
            pending_duration = float(extinf_match.group(1))
            continue

        if line.startswith("#"):
            continue

        # A non-comment, non-blank line following #EXTINF is the segment URI.
        if pending_duration is not None and current_init_uri is not None:
            segment_url = urljoin(playlist_url, line)
            segments.append(Segment(
                start=cumulative_time,
                end=cumulative_time + pending_duration,
                url=segment_url,
                init_uri=current_init_uri,
            ))
            cumulative_time += pending_duration
        pending_duration = None

    return segments


def extract_clip_via_segments(
    manifest_url: str, output_path: str, start_seconds: float, end_seconds: float,
    on_log: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Fetches only the HLS segments covering [start_seconds, end_seconds) from
    the replay manifest - typically a handful of ~8-second fragments rather
    than the whole (potentially hours-long) replay - concatenates them with
    their init segment into a small local file, and extracts the clip from
    that via clips.extract_clip's already-fast/reliable local-file path.

    Returns False (rather than raising) on anything that makes the fast path
    unusable for this event - an unparseable playlist, a network error, or
    the requested range spanning a discontinuity (a second #EXT-X-MAP) - so
    the caller can fall back to downloading the full replay instead.
    """
    log = on_log or (lambda msg: None)

    try:
        media_url, media_text = _resolve_media_playlist(manifest_url)
        segments = _parse_segments(media_url, media_text)
    except requests.RequestException as e:
        log(f"Could not fetch the replay manifest for targeted clipping: {e}")
        return False

    if not segments:
        log("Replay manifest has no segments - can't clip without a full download.")
        return False

    selected = [s for s in segments if s.end > start_seconds and s.start < end_seconds]
    if not selected:
        log(f"No segments overlap the requested range ({start_seconds}s-{end_seconds}s).")
        return False

    if len({s.init_uri for s in selected}) > 1:
        log("Requested range spans a stream discontinuity - falling back to a full download.")
        return False

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_file:
            tmp_path = tmp_file.name
            init_response = requests.get(selected[0].init_uri, headers=HEADERS, verify=False, timeout=15)
            init_response.raise_for_status()
            tmp_file.write(init_response.content)

            for segment in selected:
                segment_response = requests.get(segment.url, headers=HEADERS, verify=False, timeout=15)
                segment_response.raise_for_status()
                tmp_file.write(segment_response.content)

        log(f"Fetched {len(selected)} segment(s) covering the requested range.")
        offset = selected[0].start
        return clips.extract_clip(
            tmp_path, output_path, start_seconds - offset, end_seconds - offset, on_log=on_log,
        )
    except requests.RequestException as e:
        log(f"Could not download the needed segments: {e}")
        return False
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
