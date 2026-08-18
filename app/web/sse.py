from __future__ import annotations

import asyncio

from fastapi import Request
from starlette.responses import StreamingResponse

from app.storage.database import Database
from app.storage.models import EventRecord


class EventStream:
    def __init__(
        self, database: Database, poll_seconds: float = 0.25, heartbeat_seconds: float = 10
    ) -> None:
        self.database = database
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds

    async def fetch(
        self, after_event_id: str | None, session_id: str | None
    ) -> list[EventRecord]:
        return await self.database.list_events_after(
            after_event_id=after_event_id, session_id=session_id
        )

    @staticmethod
    def encode(event: EventRecord) -> str:
        data = event.model_dump_json()
        return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"

    async def iterate(
        self,
        request: Request,
        after_event_id: str | None,
        session_id: str | None,
    ):
        cursor = after_event_id
        elapsed = 0.0
        while not await request.is_disconnected():
            events = await self.fetch(cursor, session_id)
            if events:
                for event in events:
                    cursor = event.id
                    yield self.encode(event)
                elapsed = 0.0
            else:
                await asyncio.sleep(self.poll_seconds)
                elapsed += self.poll_seconds
                if elapsed >= self.heartbeat_seconds:
                    yield ": heartbeat\n\n"
                    elapsed = 0.0

    def response(
        self,
        request: Request,
        after_event_id: str | None,
        session_id: str | None,
    ) -> StreamingResponse:
        return StreamingResponse(
            self.iterate(request, after_event_id, session_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
