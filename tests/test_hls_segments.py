from hls_segments import _parse_segments

PLAYLIST_URL = "https://resource-us-east-1.shoplive.cloud/videos/fmp4/archive/example/index.m3u8"

# Shape matches a real eBay Live replay manifest captured during development
# (~8.3s segments, a single #EXT-X-MAP).
SIMPLE_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:8
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXTINF:8.333008,
index_000000.m4s
#EXTINF:8.333008,
index_000001.m4s
#EXTINF:8.333984,
index_000002.m4s
#EXTINF:8.333008,
index_000003.m4s
#EXT-X-ENDLIST
"""

# A second #EXT-X-MAP partway through, at a discontinuity - monitor.py's
# YTDLP_INIT_FRAGMENT_BUG_SIGNATURE comment notes real replay manifests do
# this.
DISCONTINUITY_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MAP:URI="init.mp4"
#EXTINF:8.0,
index_000000.m4s
#EXTINF:8.0,
index_000001.m4s
#EXT-X-DISCONTINUITY
#EXT-X-MAP:URI="init2.mp4"
#EXTINF:8.0,
index_000002.m4s
#EXTINF:8.0,
index_000003.m4s
#EXT-X-ENDLIST
"""


def test_parses_segments_with_correct_cumulative_timing():
    segments = _parse_segments(PLAYLIST_URL, SIMPLE_PLAYLIST)
    assert len(segments) == 4
    assert segments[0].start == 0.0
    assert segments[0].end == 8.333008
    assert segments[1].start == 8.333008
    assert segments[3].url == (
        "https://resource-us-east-1.shoplive.cloud/videos/fmp4/archive/example/index_000003.m4s"
    )


def test_all_segments_share_the_single_init_segment():
    segments = _parse_segments(PLAYLIST_URL, SIMPLE_PLAYLIST)
    init_uris = {s.init_uri for s in segments}
    assert init_uris == {
        "https://resource-us-east-1.shoplive.cloud/videos/fmp4/archive/example/init.mp4"
    }


def test_segments_after_a_discontinuity_get_the_later_init_segment():
    segments = _parse_segments(PLAYLIST_URL, DISCONTINUITY_PLAYLIST)
    assert len(segments) == 4
    assert segments[0].init_uri.endswith("init.mp4")
    assert segments[1].init_uri.endswith("init.mp4")
    assert segments[2].init_uri.endswith("init2.mp4")
    assert segments[3].init_uri.endswith("init2.mp4")


def test_overlap_selection_for_a_range_spanning_a_discontinuity_would_be_rejected():
    # This is what extract_clip_via_segments checks before downloading -
    # verifying the raw ingredient here (mixed init_uris in the selection).
    segments = _parse_segments(PLAYLIST_URL, DISCONTINUITY_PLAYLIST)
    start_seconds, end_seconds = 10.0, 20.0  # spans segments 1 (init.mp4) and 2 (init2.mp4)
    selected = [s for s in segments if s.end > start_seconds and s.start < end_seconds]
    assert len({s.init_uri for s in selected}) > 1


def test_overlap_selection_for_a_range_within_one_segment():
    segments = _parse_segments(PLAYLIST_URL, SIMPLE_PLAYLIST)
    start_seconds, end_seconds = 10.0, 15.0  # entirely within segment 1 (8.33-16.67)
    selected = [s for s in segments if s.end > start_seconds and s.start < end_seconds]
    assert len(selected) == 1
    assert selected[0].url.endswith("index_000001.m4s")
