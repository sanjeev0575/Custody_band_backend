import asyncio
import logging
from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger(__name__)


class TrackingConnections:
    def __init__(self) -> None:
        self._connections: dict[UUID, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, owner_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[owner_id].add(websocket)

    async def disconnect(self, owner_id: UUID, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(owner_id)
            if connections is not None:
                connections.discard(websocket)
                if not connections:
                    self._connections.pop(owner_id, None)

    async def broadcast(self, owner_id: UUID, message: dict[str, Any]) -> None:
        async with self._lock:
            connections = tuple(self._connections.get(owner_id, ()))
        failed: list[WebSocket] = []
        for websocket in connections:
            try:
                await websocket.send_json(message)
            except (WebSocketDisconnect, RuntimeError):
                failed.append(websocket)
                logger.warning("Removing disconnected live-tracking WebSocket")
        for websocket in failed:
            await self.disconnect(owner_id, websocket)


tracking_connections = TrackingConnections()
