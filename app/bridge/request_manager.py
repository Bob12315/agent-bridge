from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from app.adapters.base import AgentAdapterTimeout
from app.bridge.protocol import MessageEnvelope, new_id, utc_now
from app.bridge.policy import PolicyError, ReadOnlyViolation
from app.bridge.router import Router
from app.bridge.session import SessionContext
from app.storage.database import Database
from app.storage.models import EventRecord, RequestRecord

PublicRequestStatus = Literal["running", "completed", "failed", "cancelled"]


class RequestManagerError(RuntimeError):
    pass


class AgentTurnTimeout(RuntimeError):
    pass


class BridgeError(BaseModel):
    code: str
    message: str


class BridgeRequestResult(BaseModel):
    request_id: str
    status: PublicRequestStatus
    response: MessageEnvelope | None = None
    error: BridgeError | None = None


class RequestManager:
    """Run one-hop Router calls asynchronously and persist their lifecycle."""

    def __init__(
        self,
        database: Database,
        router: Router,
        agent_timeout_seconds: float | None = None,
    ) -> None:
        if agent_timeout_seconds is not None and agent_timeout_seconds <= 0:
            raise ValueError("agent timeout must be greater than zero")
        self._database = database
        self._router = router
        self._agent_timeout_seconds = agent_timeout_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._running_by_agent: dict[str, str] = {}

    async def send(
        self,
        message: MessageEnvelope,
        context: SessionContext,
        synchronous_wait_seconds: float = 30,
        request_id: str | None = None,
    ) -> BridgeRequestResult:
        if synchronous_wait_seconds < 0:
            raise RequestManagerError("synchronous wait must not be negative")
        if context.status != "active":
            raise RequestManagerError(f"session is not active: {context.id}")
        if message.session_id != context.id:
            raise RequestManagerError("message and context session IDs do not match")
        self._router.adapter_for(message.receiver)

        if request_id is not None:
            existing = await self._database.get_request(request_id)
            if existing is not None:
                if existing.session_id != context.id or existing.agent != message.receiver:
                    raise RequestManagerError(
                        "request_id is already bound to a different task or agent"
                    )
                return await self.status(request_id)

        request = RequestRecord(
            id=request_id or new_id("req"),
            message_id=message.id,
            session_id=context.id,
            agent=message.receiver,
        )
        received_event = EventRecord(
                id=new_id("evt"),
                session_id=context.id,
                request_id=request.id,
                agent=message.receiver,
                type="MESSAGE_RECEIVED",
                message=f"Accepted {message.id}",
        )
        await self._database.create_request(message, request, received_event)

        task = asyncio.create_task(
            self._execute(request, message, context),
            name=f"agent-bridge-{request.id}",
        )
        self._tasks[request.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(request.id, None))
        return await self.wait(request.id, synchronous_wait_seconds)

    async def wait(
        self, request_id: str, timeout_seconds: float = 30
    ) -> BridgeRequestResult:
        if timeout_seconds < 0:
            raise RequestManagerError("wait timeout must not be negative")
        request = await self._require_request(request_id)
        if request.status not in {"completed", "failed", "cancelled"}:
            task = self._tasks.get(request_id)
            if task is not None:
                await asyncio.wait({task}, timeout=timeout_seconds)
        return await self.status(request_id)

    async def status(self, request_id: str) -> BridgeRequestResult:
        request = await self._require_request(request_id)
        response = None
        if request.response_message_id:
            response = await self._database.get_message(request.response_message_id)
        error = None
        if request.error:
            error = BridgeError(
                code=request.error_code or "AGENT_ERROR", message=request.error
            )
        return BridgeRequestResult(
            request_id=request.id,
            status="running" if request.status == "queued" else request.status,
            response=response,
            error=error,
        )

    async def cancel(self, request_id: str) -> BridgeRequestResult:
        request = await self._require_request(request_id)
        if request.status in {"completed", "failed", "cancelled"}:
            return await self.status(request_id)

        task = self._tasks.get(request_id)
        agent_started = self._running_by_agent.get(request.agent) == request_id
        if agent_started:
            await self._router.adapter_for(request.agent).cancel()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        current = await self._require_request(request_id)
        if current.status not in {"completed", "failed", "cancelled"}:
            await self._mark_cancelled(current, agent_started=agent_started)
        return await self.status(request_id)

    async def _execute(
        self,
        request: RequestRecord,
        message: MessageEnvelope,
        context: SessionContext,
    ) -> None:
        lock = self._agent_locks.setdefault(request.agent, asyncio.Lock())
        agent_started = False
        try:
            async with lock:
                request = await self._require_request(request.id)
                if request.status == "cancelled":
                    return
                agent_started = True
                self._running_by_agent[request.agent] = request.id
                request.status = "running"
                request.started_at = utc_now()
                await self._database.update_request(request)
                await self._database.update_agent_session_status(
                    context.id, request.agent, "busy"
                )
                await self._database.insert_event(
                    EventRecord(
                        id=new_id("evt"),
                        session_id=context.id,
                        request_id=request.id,
                        agent=request.agent,
                        type="AGENT_STARTED",
                        message=f"Started {request.id}",
                    )
                )
                route = self._router.route(
                    message,
                    context,
                    store_incoming=False,
                    request_id=request.id,
                )
                if self._agent_timeout_seconds is None:
                    response = await route
                else:
                    try:
                        response = await asyncio.wait_for(
                            route, timeout=self._agent_timeout_seconds
                        )
                    except TimeoutError as exc:
                        raise AgentTurnTimeout from exc
                request.status = "completed"
                request.response_message_id = response.id
                request.finished_at = utc_now()
                await self._database.update_request(request)
                await self._database.update_agent_session_status(
                    context.id, request.agent, "idle"
                )
                await self._database.insert_event(
                    EventRecord(
                        id=new_id("evt"),
                        session_id=context.id,
                        request_id=request.id,
                        agent=request.agent,
                        type="AGENT_FINISHED",
                        message=f"Completed {request.id}",
                    )
                )
        except asyncio.CancelledError:
            await self._mark_cancelled(request, agent_started=agent_started)
        except (AgentTurnTimeout, AgentAdapterTimeout) as exc:
            if isinstance(exc, AgentTurnTimeout):
                timeout_message = (
                    f"{request.agent} did not respond within "
                    f"{self._agent_timeout_seconds} seconds."
                )
            else:
                timeout_message = f"{request.agent} timed out: {exc}"
            try:
                await self._router.adapter_for(request.agent).cancel()
            except Exception as exc:
                timeout_message += f" Adapter cancellation failed: {type(exc).__name__}."
            await self._mark_failed(
                request,
                context,
                code="AGENT_TIMEOUT",
                message=timeout_message,
                agent_started=agent_started,
            )
        except Exception as exc:
            await self._mark_failed(
                request,
                context,
                code=(
                    "READ_ONLY_VIOLATION"
                    if isinstance(exc, ReadOnlyViolation)
                    else "POLICY_DENIED"
                    if isinstance(exc, PolicyError)
                    else "AGENT_ERROR"
                ),
                message=str(exc) or type(exc).__name__,
                agent_started=agent_started,
            )
        finally:
            if self._running_by_agent.get(request.agent) == request.id:
                self._running_by_agent.pop(request.agent, None)

    async def _mark_failed(
        self,
        request: RequestRecord,
        context: SessionContext,
        *,
        code: str,
        message: str,
        agent_started: bool,
    ) -> None:
        request.status = "failed"
        request.error_code = code
        request.error = message
        request.finished_at = utc_now()
        await self._database.update_request(request)
        if agent_started:
            await self._database.update_agent_session_status(
                context.id, request.agent, "error"
            )
        await self._database.insert_event(
            EventRecord(
                id=new_id("evt"),
                session_id=context.id,
                request_id=request.id,
                agent=request.agent,
                type="AGENT_FAILED",
                message=message,
            )
        )

    async def _mark_cancelled(
        self, request: RequestRecord, *, agent_started: bool
    ) -> None:
        current = await self._require_request(request.id)
        if current.status == "cancelled":
            return
        current.status = "cancelled"
        current.error_code = "REQUEST_CANCELLED"
        current.error = "Request was cancelled."
        current.finished_at = utc_now()
        await self._database.update_request(current)
        if agent_started:
            await self._database.update_agent_session_status(
                current.session_id, current.agent, "idle"
            )
        await self._database.insert_event(
            EventRecord(
                id=new_id("evt"),
                session_id=current.session_id,
                request_id=current.id,
                agent=current.agent,
                type="REQUEST_CANCELLED",
                message=f"Cancelled {current.id}",
            )
        )

    async def _require_request(self, request_id: str) -> RequestRecord:
        request = await self._database.get_request(request_id)
        if request is None:
            raise RequestManagerError(f"request not found: {request_id}")
        return request
