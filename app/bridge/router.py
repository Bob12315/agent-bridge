from __future__ import annotations

from collections.abc import Mapping

from app.adapters.base import AgentAdapter
from app.bridge.protocol import MessageEnvelope, new_id
from app.bridge.session import SessionContext
from app.storage.database import Database
from app.storage.models import EventRecord


class RoutingError(RuntimeError):
    pass


class Router:
    """Route one message to exactly one adapter and return one response."""

    def __init__(self, adapters: Mapping[str, AgentAdapter], database: Database) -> None:
        self._adapters = dict(adapters)
        self._database = database

    async def route(
        self,
        message: MessageEnvelope,
        context: SessionContext,
        *,
        store_incoming: bool = True,
        request_id: str | None = None,
    ) -> MessageEnvelope:
        if message.session_id != context.id:
            raise RoutingError("message and context session IDs do not match")
        if message.receiver not in {"deepseek", "codex"}:
            raise RoutingError(f"receiver is not locally routable: {message.receiver}")
        adapter = self._adapters.get(message.receiver)
        if adapter is None:
            raise RoutingError(f"no adapter registered for {message.receiver}")

        if store_incoming:
            await self._database.insert_message(message)
        await self._database.insert_event(
            EventRecord(
                id=new_id("evt"),
                session_id=context.id,
                request_id=request_id,
                agent=message.receiver,
                type="MESSAGE_ROUTED",
                message=f"Routed {message.id} to {message.receiver}",
            )
        )

        result = await adapter.send(message, context)
        response = result.response
        if response.session_id != message.session_id:
            raise RoutingError("adapter response belongs to another session")
        if response.sender != message.receiver or response.receiver != message.sender:
            raise RoutingError("adapter response has an invalid direction")
        if response.reply_to != message.id:
            raise RoutingError("adapter response must reply to the routed message")

        await self._database.insert_message(response)
        return response

    def adapter_for(self, receiver: str) -> AgentAdapter:
        if receiver not in {"deepseek", "codex"}:
            raise RoutingError(f"receiver is not locally routable: {receiver}")
        adapter = self._adapters.get(receiver)
        if adapter is None:
            raise RoutingError(f"no adapter registered for {receiver}")
        return adapter
