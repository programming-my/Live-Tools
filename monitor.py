from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Callable, Optional

import imageio_ffmpeg
import requests

# Resolve yt-dlp relative to the running interpreter rather than trusting
# PATH - the app can be launched in ways that don't put the venv's bin/ on
# PATH (e.g. invoking .venv/bin/uvicorn directly instead of through an
# activated shell), which would otherwise make a correctly-installed yt-dlp
# look "not found" to this subprocess. Inside a PyInstaller-packaged app,
# sys.executable is the bundled binary itself (no venv bin/ sibling to find),
# so look for yt-dlp bundled at the frozen app's own root instead (see
# desktop_launcher.py / the PyInstaller build's --add-binary).
_YT_DLP_NAME = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
if getattr(sys, "frozen", False):
    _bundled_yt_dlp = os.path.join(sys._MEIPASS, _YT_DLP_NAME)
    YT_DLP_BIN = _bundled_yt_dlp if os.path.exists(_bundled_yt_dlp) else _YT_DLP_NAME
else:
    _venv_yt_dlp = os.path.join(os.path.dirname(sys.executable), _YT_DLP_NAME)
    YT_DLP_BIN = _venv_yt_dlp if os.path.exists(_venv_yt_dlp) else _YT_DLP_NAME

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

M3U8_PATTERN = re.compile(r'https://[^\s"\']+\.m3u8[^\s"\']*')
YTDLP_PROGRESS_PATTERN = re.compile(r"\[download\]\s+([\d.]+)% of")
DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")

# Some replay manifests declare a fresh #EXT-X-MAP (init segment) after a
# discontinuity later in the playlist - valid per the HLS spec, but yt-dlp's
# native HLS parser assumes only one, ever, and refuses to download rather
# than mishandle it. ffmpeg's own HLS demuxer handles this fine, so this
# specific failure gets a fallback instead of surfacing as a dead end.
YTDLP_INIT_FRAGMENT_BUG_SIGNATURE = "Initialization fragment found after media fragments"

# eBay Live pages embed a normalized GraphQL cache blob with entries like:
#   "LiveEvent:<id>":{"__typename":"LiveEvent","id":"<id>",...,"state":"LIVE", ...}
# `state` is the authoritative status field (observed values include LIVE, ENDED,
# SCHEDULED, CANCELLED). The window bound keeps this from matching an unrelated
# "state" key elsewhere on the page (e.g. a shipping address state).
LIVE_EVENT_STATE_PATTERN = re.compile(
    r'"__typename":"LiveEvent","id":"[^"]+".{0,400}?"state":"([A-Z]+)"', re.DOTALL
)
# The replay manifest is always templated under LiveEventReplay.replayUrl, even
# while the event is still LIVE (the file just isn't ready yet) - so its mere
# presence is NOT evidence the event has ended.
REPLAY_URL_PATTERN = re.compile(r'"replayUrl":"(https://[^"]+\.m3u8[^"]*)"')

# Same normalized LiveEvent blob referenced above also carries the event's
# actual start time, used to date-stamp downloaded filenames.
START_TIME_PATTERN = re.compile(
    r'"__typename":"LiveEvent","id":"[^"]+".{0,600}?"startTime":"([^"]+)"', re.DOTALL
)

# A seller's planned-events page renders one tile per event with this data
# attribute, e.g. <li ... data-event-id=4AG6Tlr5jsmi9Jm4 ...>.
EVENT_ID_PATTERN = re.compile(r"data-event-id=([A-Za-z0-9]+)")
HAS_NEXT_PAGE_PATTERN = re.compile(r'"hasNextPage":(true|false)')


def extract_m3u8(page_content: str) -> Optional[str]:
    """
    Extracts the first non-preview .m3u8 URL anywhere on the page (used for
    logging/fallback only). Preview manifests (query param preview=1) are the
    live-broadcast stream - they get torn down once the event ends, so
    yt-dlp 404s on them if this fallback ever hands one out as if it were
    the replay.
    """
    matches = M3U8_PATTERN.findall(page_content)
    for match in matches:
        if "preview=1" not in match:
            return match
    return None


def extract_live_event_state(page_content: str) -> Optional[str]:
    """Extracts the LiveEvent.state field (LIVE/ENDED/SCHEDULED/CANCELLED/...)."""
    match = LIVE_EVENT_STATE_PATTERN.search(page_content)
    return match.group(1) if match else None


def extract_replay_url(page_content: str) -> Optional[str]:
    """Extracts the replay manifest URL specifically (not the live preview manifest)."""
    match = REPLAY_URL_PATTERN.search(page_content)
    return match.group(1) if match else None


def extract_event_start_time(page_content: str) -> Optional[str]:
    """Extracts the LiveEvent.startTime field (ISO 8601 UTC)."""
    match = START_TIME_PATTERN.search(page_content)
    return match.group(1) if match else None


def fetch_event_start_date(url: str) -> Optional[str]:
    """Fetches the event page and returns its start date as MM-DD-YY (local time), or None on failure."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code != 200:
            return None
        start_time = extract_event_start_time(response.text)
        if not start_time:
            return None
        dt = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%m-%d-%y")
    except Exception:
        return None


def extract_event_ids(page_content: str) -> list[str]:
    """Extracts event ids from a seller's planned-events page, in DOM order, deduped."""
    return list(dict.fromkeys(EVENT_ID_PATTERN.findall(page_content)))


def build_event_url(event_id: str) -> str:
    return f"https://www.ebay.com/ebaylive/events/{event_id}/stream"


def fetch_seller_event_ids(seller_url: str) -> tuple[list[str], Optional[str]]:
    """
    Fetches a seller's planned-events page and extracts the event ids on it.
    Returns (event_ids, error_message).
    """
    try:
        response = requests.get(seller_url, headers=HEADERS, timeout=10)
        if response.status_code != 200:
            return [], f"HTTP Error {response.status_code}"

        content = response.text
        has_next_match = HAS_NEXT_PAGE_PATTERN.search(content)
        if has_next_match and has_next_match.group(1) == "true":
            return extract_event_ids(content), "Warning: seller page reports more events than shown (pagination not supported yet) - some events may be missed."

        return extract_event_ids(content), None

    except Exception as e:
        return [], str(e)


def check_event_status(url: str) -> tuple[str, Optional[str], Optional[str]]:
    """
    Polls the eBay Live URL and determines if the event is active or ended.
    Returns (status, m3u8_url, error_message)
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code != 200:
            return "UNKNOWN", None, f"HTTP Error {response.status_code}"

        content = response.text
        state = extract_live_event_state(content)
        replay_url = extract_replay_url(content)

        # eBay's real terminal state for a finished broadcast with a ready
        # replay is "RECORDED" (confirmed against a live event page) - "ENDED"
        # is kept as a fallback in case eBay uses it in other contexts.
        if state in ("ENDED", "RECORDED") or "Replay available" in content:
            return "ENDED", replay_url or extract_m3u8(content), None
        elif state == "LIVE":
            # Ignore any m3u8 found on the page - it's the live preview manifest,
            # not a finished replay, even though a replayUrl slot already exists.
            return "LIVE", None, None
        elif state:
            # SCHEDULED, UPCOMING, CANCELLED, or any other non-live/non-ended state.
            return state, None, None
        else:
            # Couldn't find the LiveEvent.state marker at all (page structure
            # changed, or we're stuck on eBay's queue-it waiting room). Do NOT
            # infer ENDED just because an m3u8 happens to be present.
            return "WAITING", None, None

    except Exception as e:
        return "ERROR", None, str(e)


def _probe_stream_duration_seconds(url: str) -> Optional[float]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        result = subprocess.run(
            [ffmpeg, "-tls_verify", "0", "-i", url], capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    match = DURATION_PATTERN.search(result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _remux_to_flat_mp4(output_path: str, on_progress: Callable[[Optional[float], str], None]) -> None:
    """
    Remuxes the downloaded file into a standard flat MP4 (single moov index
    followed by one contiguous mdat) - container-only, -c copy, no
    re-encode, no quality loss.

    yt-dlp's native downloader concatenates source HLS fragments directly
    without going through ffmpeg's muxer, so when the source stream is
    itself fMP4-based (increasingly common), the result is a *fragmented*
    MP4 - thousands of separate moof/mdat pairs, one per HLS fragment. This
    is completely valid and ffmpeg/VLC handle it fine, but QuickTime Player
    has a well-known limitation with heavily-fragmented files that can
    cause it to misreport duration or stop playback early even though no
    data is actually missing (confirmed 2026-09-09: a file QuickTime showed
    as 1:15:32 played and seeked correctly through its full 5:03:06 in every
    other tool).

    Best-effort: if the remux fails for any reason, the original (still
    fully valid, just not QuickTime-friendly) file is left in place rather
    than losing the download over a compatibility nicety.
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    temp_path = output_path + ".remux.mp4"
    on_progress(None, "Remuxing to a standard (non-fragmented) MP4 for broad player compatibility...")
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", output_path, "-c", "copy", "-movflags", "+faststart", "-loglevel", "error", temp_path],
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0:
            on_progress(None, f"Remux failed, keeping the original file as-is: {result.stderr[-500:]}")
            return
        os.replace(temp_path, output_path)
        on_progress(None, "Remux complete.")
    except Exception as e:
        on_progress(None, f"Remux failed, keeping the original file as-is: {e}")
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _download_with_ffmpeg(
    m3u8_url: str,
    output_path: str,
    on_progress: Callable[[Optional[float], str], None],
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    """
    Fallback path for manifests yt-dlp's native HLS parser refuses to touch -
    remuxes directly with ffmpeg, which doesn't share that limitation. No
    --concurrent-fragments equivalent here (ffmpeg's HLS demuxer fetches
    segments sequentially), so this is slower than the yt-dlp path, but it's
    only used when that path has already failed.
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    total_duration = _probe_stream_duration_seconds(m3u8_url)

    cmd = [
        ffmpeg, "-y",
        "-tls_verify", "0",
        "-i", m3u8_url,
        "-c", "copy",
        "-loglevel", "error",
        "-progress", "pipe:1",
        output_path,
    ]

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert process.stdout is not None

    block: list = []
    for line in process.stdout:
        if should_stop is not None and should_stop():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            return False

        line = line.strip()
        block.append(line)
        if line == "progress=continue" or line == "progress=end":
            current_time = None
            for entry in block:
                if entry.startswith("out_time_ms="):
                    try:
                        current_time = int(entry.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        pass
            block = []
            if current_time is not None and total_duration:
                percent = min(100.0, current_time / total_duration * 100)
                on_progress(percent, f"[ffmpeg] {percent:.1f}% ({current_time:.0f}s / {total_duration:.0f}s)")

    return_code = process.wait()
    if return_code != 0:
        stderr_output = process.stderr.read() if process.stderr else ""
        on_progress(None, f"ffmpeg fallback failed:\n{stderr_output[-2000:]}")
        return False
    return True


def download_replay_with_progress(
    m3u8_url: str,
    output_path: str,
    on_progress: Callable[[Optional[float], str], None],
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    """
    Downloads the video replay using yt-dlp, reporting progress via
    on_progress(percent, line). If should_stop() becomes true mid-download,
    terminates yt-dlp and returns False. Falls back to a direct ffmpeg remux
    if yt-dlp's native HLS parser rejects the manifest (see
    YTDLP_INIT_FRAGMENT_BUG_SIGNATURE).
    """
    cmd = [
        YT_DLP_BIN,
        "--hls-use-mpegts",
        "--concurrent-fragments", "8",
        "--no-check-certificate",
        "--newline",
        "-o", output_path,
        m3u8_url,
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert process.stdout is not None
    hit_init_fragment_bug = False
    for line in process.stdout:
        if should_stop is not None and should_stop():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            return False

        line = line.rstrip("\n")
        if not line:
            continue
        if YTDLP_INIT_FRAGMENT_BUG_SIGNATURE in line:
            hit_init_fragment_bug = True
        match = YTDLP_PROGRESS_PATTERN.search(line)
        percent = float(match.group(1)) if match else None
        on_progress(percent, line)

    return_code = process.wait()
    if return_code == 0:
        # yt-dlp's native downloader concatenates fragments directly rather
        # than going through ffmpeg's muxer, which can leave the result as a
        # fragmented MP4 when the source HLS stream is itself fMP4-based -
        # see _remux_to_flat_mp4's docstring.
        _remux_to_flat_mp4(output_path, on_progress)
        return True

    if not hit_init_fragment_bug:
        return False

    on_progress(None, "yt-dlp's HLS parser can't handle this manifest (repeated init segment) - retrying with ffmpeg directly...")
    try:
        if os.path.exists(output_path):
            os.remove(output_path)
    except OSError:
        pass
    return _download_with_ffmpeg(m3u8_url, output_path, on_progress, should_stop)


def wait_or_stop(seconds: int, should_stop: Callable[[], bool]) -> bool:
    """Sleeps in small increments so a stop request is picked up quickly."""
    elapsed = 0.0
    step = 0.5
    while elapsed < seconds:
        if should_stop():
            return True
        time.sleep(step)
        elapsed += step
    return False
