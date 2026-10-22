from __future__ import annotations

from functools import partial
from typing import Callable, Optional

from .auth import parse_iso

# Identified 2026-08-31 by watching real traffic on a live event. Real shape:
#   {
#     "typename": "ChatMessageAdded",
#     "chatMessage": {
#       "messageId": "...", "createdAt": "2026-08-31T20:21:20.411Z",
#       "text": "Nice", "userId": "...", "userName": "iwantgeetars",
#       "messageType": "REGULAR", "memberId": "...", "memberName": "...",
#       "mentions": []
#     },
#     "userId": "...", "userName": "...", "serverCreatedAt": 1788207680489
#   }
# Only "REGULAR" messageType has been observed so far - other values (e.g.
# system/moderation messages) may exist and could need filtering once seen.


def _parse_chat_message_added(payload: dict, featured: bool = False) -> dict:
    chat_message = payload.get("chatMessage") or {}
    created_at = chat_message.get("createdAt")
    if created_at:
        ts = int(parse_iso(created_at).timestamp() * 1000)
    else:
        ts = payload.get("serverCreatedAt")
    return {
        "id": chat_message.get("messageId"),
        "user": chat_message.get("userName") or payload.get("userName") or "?",
        "text": chat_message.get("text", ""),
        "ts": ts,
        "featured": featured,
    }


# FeaturedMessageAdded (a host/moderator-highlighted message, i.e. a pinned
# message) carries the identical chatMessage sub-structure plus an extra
# "featuredAt" - the same parser applies, just flagged so the overlay can
# treat it as pinned rather than a regular scrolling message.
#
# Expected parser signature: (payload: dict) -> {"id": str, "user": str, "text": str, "ts": int (epoch ms), "featured": bool}
CHAT_MESSAGE_PARSERS: dict[str, Callable[[dict], dict]] = {
    "ChatMessageAdded": _parse_chat_message_added,
    "FeaturedMessageAdded": partial(_parse_chat_message_added, featured=True),
}


def try_parse_chat(payload: dict) -> Optional[dict]:
    parser = CHAT_MESSAGE_PARSERS.get(payload.get("typename"))
    return parser(payload) if parser else None


# Identified 2026-09-08 by watching real moderation activity on a live
# event. Real shape (deliberately minimal - no chatMessage sub-object):
#   {"typename": "ChatMessageDeleted", "messageId": "...", "serverCreatedAt": ...}
# "FeaturedMessageRemoved" is still deliberately NOT registered here - it
# almost certainly means "un-highlighted" (paired with FeaturedMessageAdded
# above), not "deleted from the chat feed entirely", and there's no evidence
# either way yet.


def _parse_chat_message_deleted(payload: dict) -> dict:
    return {"id": payload.get("messageId")}


# Expected parser signature: (payload: dict) -> {"id": str} (the removed message's id)
CHAT_REMOVAL_PARSERS: dict[str, Callable[[dict], dict]] = {
    "ChatMessageDeleted": _parse_chat_message_deleted,
}


def try_parse_chat_removal(payload: dict) -> Optional[dict]:
    parser = CHAT_REMOVAL_PARSERS.get(payload.get("typename"))
    return parser(payload) if parser else None
