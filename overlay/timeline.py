from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Optional

from .protocol import fetch_listing_title

# Kept only for the orphan-fallback path (a listing whose raw auction events
# never landed inside any pin segment's window) - everything else derives
# visibility from the real pin/unpin lifecycle instead of a fixed timer.
AUCTION_END_GRACE_SECONDS = 4
PREVIOUS_ITEM_BANNER_SECONDS = 6  # how long the "previous item" banner fades in for


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _epoch_ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _fetch_titles_parallel(listing_ids: list, log: Callable[[str], None], max_workers: int = 12) -> dict:
    titles: dict = {}
    if not listing_ids:
        return titles
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_id = {pool.submit(fetch_listing_title, lid): lid for lid in listing_ids}
        for future in as_completed(future_to_id):
            listing_id = future_to_id[future]
            try:
                titles[listing_id] = future.result()
            except Exception as e:
                log(f"Could not fetch title for listing {listing_id}: {e}")
                titles[listing_id] = f"Item {listing_id}"
    return titles


def _build_pin_segments(pin_events: list, start_dt: datetime) -> list:
    """
    Replays EventListingsUpdated pin/unpin transitions chronologically as a
    state machine (mirroring live/state.py's apply_pin/apply_unpin, just
    replayed in a batch loop rather than mutating one live object) to
    produce one segment per pin OCCURRENCE - a listing_id can legitimately
    be pinned/unpinned more than once (observed in real data: an item
    flickers pinned-then-unpinned before its real start, or gets briefly
    re-pinned showing its already-sold result after ending).

    Only one item is ever pinned at a time in practice, so "current" is a
    single global slot, not tracked per listing_id.
    """
    segments: list = []
    current: Optional[dict] = None

    for event in sorted(pin_events, key=lambda e: e.get("serverCreatedAt", 0)):
        offset = (_epoch_ms_to_dt(event["serverCreatedAt"]) - start_dt).total_seconds()
        for listing in event.get("eventListings", []):
            listing_id = listing.get("listingId")
            if not listing_id:
                continue
            details = (listing.get("saleInfo") or {}).get("details") or {}
            detail_type = details.get("typename")
            result = details.get("result")

            if listing.get("isPinnedInEvent"):
                if current is not None and current["listing_id"] == listing_id:
                    continue  # re-affirming the already-current listing - no-op
                if current is not None:
                    current["unpinned_offset"] = offset
                price_info = details.get("startingPrice") or listing.get("originalPrice") or {}
                price_original = price_info.get("original") or {}
                current = {
                    "listing_id": listing_id,
                    "title": listing.get("title"),
                    "starting_price": price_original.get("amount"),
                    "currency": price_original.get("currencyCode", "USD"),
                    "pinned_offset": offset,
                    "unpinned_offset": None,
                    "pin_detail_type": detail_type,
                    "pin_result": result,
                    "bids": [],
                    "deadlines": [],
                    "ended": False,
                }
                segments.append(current)
            else:
                if current is not None and current["listing_id"] == listing_id:
                    current["unpinned_offset"] = offset
                    current["pin_detail_type"] = detail_type
                    current["pin_result"] = result
                    current = None
                # else: unpin for a listing that isn't the current one - no-op

    return segments


def _apply_auction_event_to_segment(seg: dict, event: dict, start_dt: datetime) -> None:
    typename = event.get("typename")

    if typename in ("AuctionStarted", "AuctionStartedV2"):
        started_at = event.get("startedAt")
        ending_at = event.get("endingAt")
        if started_at:
            seg["start_offset"] = (_parse_iso(started_at) - start_dt).total_seconds()
        if ending_at:
            seg["end_offset"] = (_parse_iso(ending_at) - start_dt).total_seconds()
            if "start_offset" in seg:
                seg["deadlines"].append((seg["start_offset"], seg["end_offset"]))
        price = (event.get("startingPrice") or {}).get("original", {})
        if price.get("amount") is not None:
            # AuctionStarted's own price wins over the pin's placeholder
            # price once it actually exists (confirmed against real data:
            # the pin's price is a generic pre-auction placeholder in the
            # one case they disagreed).
            seg["starting_price"] = price.get("amount")
            seg["currency"] = price.get("currencyCode", "USD")
    elif typename == "BidTimeExtended":
        # Each bid within the closing window resets the countdown to a
        # fresh ~10s, rather than extending the original duration by a
        # fixed amount - so the deadline that applies varies throughout
        # the auction and must be looked up by the offset it took effect,
        # not just overwritten to a single final value (see deadlines).
        change_offset = (_epoch_ms_to_dt(event["serverCreatedAt"]) - start_dt).total_seconds()
        ending_at = event.get("endingAt") or event.get("newEndingAt")
        if ending_at:
            seg["end_offset"] = (_parse_iso(ending_at) - start_dt).total_seconds()
        else:
            seg["end_offset"] = change_offset
        seg["deadlines"].append((change_offset, seg["end_offset"]))
    elif typename == "AuctionNewBidLeader":
        bid_offset = (_epoch_ms_to_dt(event["serverCreatedAt"]) - start_dt).total_seconds()
        winning_bid = (event.get("winningBid") or {}).get("original", {})
        bidder = event.get("bidder") or {}
        # recommendedBid is eBay's own suggested next bid - intentionally
        # higher than winningBid, to encourage the next bid rather than
        # repeat the amount already shown as "where the bidding is at"
        # (mirrors the same field added to live/state.py this session).
        recommended_bid = (event.get("recommendedBid") or {}).get("original", {})
        seg["bids"].append({
            "offset": bid_offset,
            "amount": winning_bid.get("amount"),
            "user": bidder.get("userName") or "?",
            "recommended_bid": recommended_bid.get("amount"),
        })
    elif typename in ("AuctionEnded", "AuctionEndedV2", "AuctionBiddingClosed"):
        seg["ended"] = True
        ended_at = event.get("endedAt")
        if ended_at:
            seg["end_offset"] = (_parse_iso(ended_at) - start_dt).total_seconds()
        elif "serverCreatedAt" in event:
            seg["end_offset"] = (_epoch_ms_to_dt(event["serverCreatedAt"]) - start_dt).total_seconds()
        seg["deadlines"].append((seg["end_offset"], seg["end_offset"]))


def _find_containing_segment(candidates: list, offset: float) -> Optional[dict]:
    for seg in candidates:
        upper = seg["unpinned_offset"] if seg["unpinned_offset"] is not None else float("inf")
        if seg["pinned_offset"] <= offset < upper:
            return seg
    return None


def build_auction_segments(
    pin_events: list, auction_events: list, start_dt: datetime, on_log: Optional[Callable[[str], None]] = None,
) -> list:
    """
    Reconstructs each pinned item's full lifecycle. Card visibility is
    pin-driven, not timer-based: a segment spans [pinned_offset,
    unpinned_offset) - "starting soon" before AuctionStarted, live bidding
    once it fires, "ended" (showing the final result) afterwards, all the
    way until the item is actually unpinned. Raw auction events are matched
    to whichever pin segment's time window contains them (a listing_id can
    have more than one pin segment - see _build_pin_segments) - any that
    don't land inside any window are orphaned and fall back to the old
    grace-period-based behavior.

    Returns a list of segments sorted by pinned_offset:
    {listing_id, title, pinned_offset, unpinned_offset, start_offset,
     end_offset, starting_price, currency, bids, deadlines, ended,
     pin_detail_type, pin_result}.
    """
    log = on_log or (lambda m: None)

    pin_segments = _build_pin_segments(pin_events, start_dt)
    segments_by_listing: dict = {}
    for seg in pin_segments:
        segments_by_listing.setdefault(seg["listing_id"], []).append(seg)

    orphan_events_by_listing: dict = {}
    for event in sorted(auction_events, key=lambda e: e.get("serverCreatedAt", 0)):
        listing_id = event.get("listingId")
        if not listing_id:
            continue
        offset = (_epoch_ms_to_dt(event["serverCreatedAt"]) - start_dt).total_seconds()
        target = _find_containing_segment(segments_by_listing.get(listing_id, []), offset)
        if target is None:
            orphan_events_by_listing.setdefault(listing_id, []).append((event, offset))
            continue
        _apply_auction_event_to_segment(target, event, start_dt)

    if orphan_events_by_listing:
        total_orphaned = sum(len(events) for events in orphan_events_by_listing.values())
        log(
            f"{total_orphaned} event(s) across {len(orphan_events_by_listing)} listing(s) didn't land inside "
            "any pin segment (the listing's own pin/unpin event was likely lost upstream - see "
            "fetch_full_history's lost_ranges warning, if any) - reconstructing a best-effort segment "
            "for each from whatever was captured instead of logging every individual event:"
        )
        for listing_id, events in orphan_events_by_listing.items():
            offsets = [offset for _event, offset in events]
            log(f"  - listing {listing_id}: {len(events)} orphaned event(s), {min(offsets):.1f}s..{max(offsets):.1f}s")

    orphan_segments = []
    for listing_id, events in orphan_events_by_listing.items():
        seg = {
            "listing_id": listing_id, "title": None, "starting_price": None, "currency": "USD",
            "bids": [], "deadlines": [], "ended": False, "pin_detail_type": None, "pin_result": None,
        }
        for event, _offset in events:
            _apply_auction_event_to_segment(seg, event, start_dt)
        if "start_offset" not in seg:
            # The real AuctionStarted/AuctionStartedV2 event itself never
            # landed in any pin segment either (and wasn't recovered even
            # after retrying - see MIN_CHUNK_RETRY_ATTEMPTS) - but losing the
            # whole auction from the render when we still have other real
            # signal for it (bids, an end event) is worse than showing a
            # best-effort reconstruction with a start time inferred from the
            # earliest event we do have.
            candidate_offsets = [bid["offset"] for bid in seg["bids"]]
            if "end_offset" in seg:
                candidate_offsets.append(seg["end_offset"])
            if not candidate_offsets:
                continue  # truly nothing usable to show
            seg["start_offset"] = min(candidate_offsets)
            log(
                f"Listing {listing_id}: never saw its AuctionStarted event - reconstructing its segment "
                f"from {len(seg['bids'])} bid event(s) instead so it isn't dropped from the render entirely."
            )
        seg["pinned_offset"] = seg["start_offset"]
        seg["unpinned_offset"] = seg.get("end_offset", seg["start_offset"]) + AUCTION_END_GRACE_SECONDS
        orphan_segments.append(seg)

    all_segments = pin_segments + orphan_segments
    need_title_ids = [seg["listing_id"] for seg in all_segments if not seg.get("title")]
    fetched_titles = _fetch_titles_parallel(need_title_ids, log)
    for seg in all_segments:
        if not seg.get("title"):
            seg["title"] = fetched_titles.get(seg["listing_id"], f"Item {seg['listing_id']}")

    all_segments.sort(key=lambda s: s["pinned_offset"])
    return all_segments


def build_viewer_timeline(viewer_stats: list, start_dt: datetime) -> list:
    entries = []
    for event in viewer_stats:
        server_created_at = event.get("serverCreatedAt")
        count = event.get("concurrentViewers")
        if server_created_at is None or count is None:
            continue
        offset = (_epoch_ms_to_dt(server_created_at) - start_dt).total_seconds()
        entries.append({"offset": offset, "count": count})
    entries.sort(key=lambda e: e["offset"])
    return entries


class OverlayTimeline:
    """Queryable view over the reconstructed auction/viewer history."""

    def __init__(self, auctions: list, viewers: list):
        self.auctions = auctions
        self.viewers = viewers

    def viewer_count_at(self, offset_seconds: float) -> Optional[int]:
        latest = None
        for entry in self.viewers:
            if entry["offset"] <= offset_seconds:
                latest = entry["count"]
            else:
                break
        return latest

    def _active_segment_index(self, offset_seconds: float) -> Optional[int]:
        active_index = None
        for i, seg in enumerate(self.auctions):
            if seg["pinned_offset"] <= offset_seconds:
                end = seg.get("unpinned_offset")
                if end is None or offset_seconds < end:
                    active_index = i
        return active_index

    def auction_card_at(self, offset_seconds: float) -> Optional[dict]:
        index = self._active_segment_index(offset_seconds)
        if index is None:
            return None
        active = self.auctions[index]

        price = active.get("starting_price")
        winner = None
        recommended_bid = None
        for bid in active["bids"]:
            if bid["offset"] <= offset_seconds:
                price = bid["amount"]
                winner = bid["user"]
                recommended_bid = bid.get("recommended_bid")
            else:
                break

        # Use whichever deadline was in effect as of this offset - not just the
        # final one - since bid-driven countdown resets only take effect from
        # the moment each bid actually lands (see _apply_auction_event_to_segment).
        remaining = None
        deadline = None
        for change_offset, new_deadline in active.get("deadlines", []):
            if change_offset <= offset_seconds:
                deadline = new_deadline
            else:
                break
        if deadline is not None:
            remaining = max(0.0, deadline - offset_seconds)

        has_started = active.get("start_offset") is not None
        if not has_started:
            # No raw AuctionStarted ever attached to this segment - either
            # it's still waiting to start ("pinned"), or it's the "flicker
            # after end" case: a brief re-pin already showing the sold
            # result, with zero raw events of its own to derive a phase from.
            phase = "ended" if active.get("pin_detail_type") == "EndedAuctionDetails" else "pinned"
        elif offset_seconds < active["start_offset"]:
            phase = "pinned"
        else:
            # Prefer the countdown itself - the "AuctionEnded"-type event has
            # been observed firing at unreliable times (not necessarily real
            # auction close), so only fall back to it when there's no
            # countdown at all.
            countdown_ended = remaining <= 0 if remaining is not None else active.get("ended", False)
            phase = "ended" if countdown_ended else "active"

        if phase == "ended" and not winner:
            # No raw AuctionNewBidLeader ever attached (the flicker-after-end
            # case) - fall back to the pin payload's own final result.
            pin_result = active.get("pin_result") or {}
            result_winner = (pin_result.get("winner") or {}).get("userName")
            result_price = (pin_result.get("winningBid") or {}).get("original", {}).get("amount")
            if result_winner:
                winner = result_winner
            if result_price is not None:
                price = result_price

        return {
            "title": active["title"],
            "price": price,
            "currency": active.get("currency", "USD"),
            "winner": winner,
            "recommended_bid": recommended_bid,
            "remaining_seconds": remaining if phase == "active" else None,
            "phase": phase,
        }

    def previous_item_title_at(self, offset_seconds: float) -> Optional[str]:
        """The item auctioned just before the current one, shown briefly as a fading banner."""
        index = self._active_segment_index(offset_seconds)
        if index is None or index == 0:
            return None
        active = self.auctions[index]
        start_offset = active.get("start_offset")
        if start_offset is None:
            return None  # still in the "pinned" phase - no start transition to announce yet
        time_since_start = offset_seconds - start_offset
        if time_since_start > PREVIOUS_ITEM_BANNER_SECONDS:
            return None
        return self.auctions[index - 1]["title"]

    def full_state_at(self, offset_seconds: float) -> dict:
        """Single composite snapshot of everything the overlay should show at this offset -
        the browser side just applies this dict, no timeline logic duplicated in JS."""
        return {
            "viewer_count": self.viewer_count_at(offset_seconds),
            "auction": self.auction_card_at(offset_seconds),
            "previous_item_title": self.previous_item_title_at(offset_seconds),
        }


def build_timeline(history: dict, on_log: Optional[Callable[[str], None]] = None) -> OverlayTimeline:
    start_dt = _parse_iso(history["start_time"])
    auctions = build_auction_segments(history.get("pin_updates", []), history["auctions"], start_dt, on_log)
    viewers = build_viewer_timeline(history.get("viewer_stats", []), start_dt)
    return OverlayTimeline(auctions, viewers)


def _segment_rank(seg: dict) -> tuple:
    """Ranks a segment by how much it looks like "the real auction" rather
    than a pin flicker - most bids wins, then longest duration. Shared by
    find_listing_segment (pick the best occurrence of one known listing)
    and list_distinct_listings (dedupe a listing pinned more than once down
    to a single dropdown entry)."""
    end = seg.get("unpinned_offset")
    duration = (end - seg["pinned_offset"]) if end is not None else float("inf")
    return (len(seg.get("bids") or []), duration)


def find_listing_segment(
    timeline: OverlayTimeline, listing_id: str, on_log: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """Locates the segment for one specific listing (eBay's own item
    number), for exporting just that auction instead of the whole event.
    A listing can legitimately be pinned more than once (flickers
    pinned-then-unpinned before its real start, or gets briefly re-pinned
    showing its already-sold result after ending - see
    _build_pin_segments) - when that happens, prefers whichever occurrence
    actually has bids (the real auction, not a flicker), falling back to
    the longest one if none do."""
    log = on_log or (lambda m: None)
    listing_id = str(listing_id).strip()
    matches = [seg for seg in timeline.auctions if str(seg.get("listing_id")) == listing_id]
    if not matches:
        log(f"No auction found for item number {listing_id} in this event.")
        return None
    if len(matches) > 1:
        log(f"Item number {listing_id} was pinned {len(matches)} separate times - using the occurrence with the most real activity.")

    return max(matches, key=_segment_rank)


def list_distinct_listings(timeline: OverlayTimeline) -> list:
    """One entry per distinct listing in the timeline - {listing_id, title}
    in the order each was first pinned - for populating a "pick which
    auction to export" dropdown by title instead of requiring the item
    number up front. A listing pinned more than once collapses to a single
    entry via the same "most real activity" ranking find_listing_segment
    uses, so the dropdown doesn't show (confusing) duplicate titles."""
    best_by_listing: dict = {}
    first_seen_offset: dict = {}
    for seg in timeline.auctions:
        listing_id = seg.get("listing_id")
        if listing_id is None:
            continue
        if listing_id not in first_seen_offset:
            first_seen_offset[listing_id] = seg["pinned_offset"]
        current = best_by_listing.get(listing_id)
        if current is None or _segment_rank(seg) > _segment_rank(current):
            best_by_listing[listing_id] = seg

    ordered_ids = sorted(best_by_listing.keys(), key=lambda lid: first_seen_offset[lid])
    return [
        {"listing_id": listing_id, "title": best_by_listing[listing_id].get("title") or f"Item {listing_id}"}
        for listing_id in ordered_ids
    ]
