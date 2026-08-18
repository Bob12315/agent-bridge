from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel

from app.bridge.protocol import MessageEnvelope
from app.bridge.session import SessionContext


class AgentHealth(BaseModel):
    status: Literal["healthy", "degraded", "unavailable"]
    detail: str | None = None


class AgentTurnResult(BaseModel):
    response: MessageEnvelope
    external_session_id: str | None = None


class AgentAdapterTimeout(RuntimeError):
    """An Adapter's own execution deadline expired."""


class AgentAdapter(ABC):
    @abstractmethod
    async def start(self, context: SessionContext) -> str | None: ...

    @abstractmethod
    async def send(
        self, message: MessageEnvelope, context: SessionContext
    ) -> AgentTurnResult: ...

    @abstractmethod
    async def cancel(self) -> None: ...

    @abstractmethod
    async def health(self) -> AgentHealth: ...

    @abstractmethod
    async def close(self) -> None: ...
