from __future__ import annotations

import json

from fastapi import WebSocket


class Broadcaster:
    """Fans out one message to every browser client currently watching an event."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    def add(self, ws: WebSocket) -> None:
        self.clients.add(ws)

    def discard(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def send(self, message: dict) -> None:
        text = json.dumps(message)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
