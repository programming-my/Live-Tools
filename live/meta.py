from __future__ import annotations

from typing import Optional


class LiveMeta:
    """
    Event-wide live state that isn't tied to any single listing (unlike
    AuctionState, which is per-listing) - viewer count, host identity, and
    the currently-pinned/featured chat message. Held one per EventSession
    and mutated directly by live/listener.py's _dispatch, mirroring how
    AuctionState is already passed in and mutated there.
    """

    def __init__(self) -> None:
        self.viewer_count: Optional[int] = None
        self.event_title: Optional[str] = None
        self.host_username: Optional[str] = None
        self.host_avatar_url: Optional[str] = None
        # The currently-pinned chat message dict (same shape live/chat.py
        # produces, with featured=True), or None if nothing is pinned.
        # There's no reliable "un-feature" signal (FeaturedMessageRemoved's
        # semantics are unconfirmed and stays a no-op elsewhere in this
        # codebase) - this only ever changes via a newer FeaturedMessageAdded
        # or an explicit ChatMessageDeleted matching its id, so it can persist
        # indefinitely across a long event.
        self.featured_message: Optional[dict] = None

    def apply_bootstrap(
        self, event_title: Optional[str], host_username: Optional[str], host_avatar_url: Optional[str],
    ) -> bool:
        """
        Merges freshly-scraped identity fields rather than replacing wholesale -
        bootstrap_session re-scrapes on every reconnect, and a transient scrape
        failure (returns all-None) must not blank out already-good header data
        for currently-connected clients.
        """
        changed = False
        if event_title and event_title != self.event_title:
            self.event_title = event_title
            changed = True
        if host_username and host_username != self.host_username:
            self.host_username = host_username
            changed = True
        if host_avatar_url and host_avatar_url != self.host_avatar_url:
            self.host_avatar_url = host_avatar_url
            changed = True
        return changed

    def apply_viewer_count(self, count: int) -> bool:
        changed = count != self.viewer_count
        self.viewer_count = count
        return changed

    def set_featured_message(self, message: dict) -> None:
        self.featured_message = message

    def clear_featured_message_if(self, message_id) -> bool:
        if self.featured_message is not None and self.featured_message.get("id") == message_id:
            self.featured_message = None
            return True
        return False

    def snapshot_meta(self) -> Optional[dict]:
        if not (self.event_title or self.host_username or self.host_avatar_url):
            return None
        return {
            "event_title": self.event_title,
            "host_username": self.host_username,
            "host_avatar_url": self.host_avatar_url,
        }
