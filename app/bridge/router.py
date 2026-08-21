from __future__ import annotations

from collections.abc import Mapping

from app.adapters.base import AgentAdapter
from app.bridge.inspection import WorkspaceInspector
from app.bridge.policy import ReadOnlyViolation, validate_agent_turn
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
        self._inspector = WorkspaceInspector()

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
        validate_agent_turn(message, context)
        adapter = self._adapters.get(message.receiver)
        if adapter is None:
            raise RoutingError(f"no adapter registered for {message.receiver}")

        agent_session = await self._database.get_agent_session(context.id, message.receiver)
        restore = getattr(adapter, "restore", None)
        if agent_session and agent_session.external_session_id and callable(restore):
            try:
                await restore(context, agent_session.external_session_id)
                await self._database.update_agent_session_status(
                    context.id, message.receiver, "resuming"
                )
                await self._database.insert_event(
                    EventRecord(
                        id=new_id("evt"), session_id=context.id, request_id=request_id,
                        agent=message.receiver, type="AGENT_SESSION_RESUMED",
                        message=f"Resumed persisted {message.receiver} session",
                    )
                )
            except Exception as exc:
                await self._database.update_agent_session_status(
                    context.id, message.receiver, "degraded"
                )
                await self._database.insert_event(
                    EventRecord(
                        id=new_id("evt"), session_id=context.id, request_id=request_id,
                        agent=message.receiver, type="SESSION_RESUME_FAILED",
                        message=f"Could not resume persisted session: {exc}",
                    )
                )
                raise RoutingError("SESSION_RESUME_FAILED") from exc

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

        before = None
        if message.receiver == "codex" and (context.workspace / ".git").exists():
            before = await self._inspector.status_snapshot(context)
        try:
            result = await adapter.send(message, context)
        except BaseException:
            await self._raise_if_read_only_changed(context, message, before)
            raise
        await self._raise_if_read_only_changed(context, message, before)
        response = result.response
        if response.session_id != message.session_id:
            raise RoutingError("adapter response belongs to another session")
        if response.sender != message.receiver or response.receiver != message.sender:
            raise RoutingError("adapter response has an invalid direction")
        if response.reply_to != message.id:
            raise RoutingError("adapter response must reply to the routed message")

        if result.external_session_id:
            await self._database.update_agent_session_external(
                context.id,
                message.receiver,
                result.external_session_id,
                backend=result.backend or type(adapter).__name__,
            )
        await self._database.insert_message(response)
        return response

    async def _raise_if_read_only_changed(
        self,
        context: SessionContext,
        message: MessageEnvelope,
        before: str | None,
    ) -> None:
        if before is None:
            return
        after = await self._inspector.status_snapshot(context)
        if after == before:
            return
        violation = "READ_ONLY_VIOLATION: Codex review changed the session workspace."
        await self._database.insert_event(
            EventRecord(
                id=new_id("evt"),
                session_id=context.id,
                agent=message.receiver,
                type="POLICY_VIOLATION",
                message=violation,
            )
        )
        raise ReadOnlyViolation(violation)

    def adapter_for(self, receiver: str) -> AgentAdapter:
        if receiver not in {"deepseek", "codex"}:
            raise RoutingError(f"receiver is not locally routable: {receiver}")
        adapter = self._adapters.get(receiver)
        if adapter is None:
            raise RoutingError(f"no adapter registered for {receiver}")
        return adapter
