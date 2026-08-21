from __future__ import annotations

from app.adapters.base import AgentAdapter, AgentHealth, AgentTurnResult
from app.bridge.protocol import MessageEnvelope
from app.bridge.session import SessionContext


class FallbackAdapter(AgentAdapter):
    """Use the preferred backend first and keep the V2 core independent of it."""

    def __init__(self, primary: AgentAdapter, fallback: AgentAdapter) -> None:
        self._primary = primary
        self._fallback = fallback
        self._active: AgentAdapter | None = None

    async def start(self, context: SessionContext) -> str | None:
        try:
            return await self._primary.start(context)
        except Exception:
            return await self._fallback.start(context)

    async def send(self, message: MessageEnvelope, context: SessionContext) -> AgentTurnResult:
        try:
            self._active = self._primary
            result = await self._primary.send(message, context)
            result.backend = result.backend or type(self._primary).__name__
            return result
        except Exception:
            self._active = self._fallback
            result = await self._fallback.send(message, context)
            result.backend = result.backend or type(self._fallback).__name__
            return result
        finally:
            self._active = None

    async def cancel(self) -> None:
        if self._active is not None:
            await self._active.cancel()

    async def health(self) -> AgentHealth:
        primary = await self._primary.health()
        if primary.status == "healthy":
            return primary
        fallback = await self._fallback.health()
        if fallback.status == "healthy":
            return AgentHealth(status="degraded", detail=f"primary unavailable: {primary.detail}")
        return primary

    async def close(self) -> None:
        await self._primary.close()
        await self._fallback.close()
