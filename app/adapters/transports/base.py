from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.adapters.base import AgentHealth


class DeepSeekTransport(ABC):
    @abstractmethod
    async def create_session(self, workspace: Path) -> str: ...

    @abstractmethod
    async def send(self, session_id: str, prompt: str) -> str: ...

    @abstractmethod
    async def resume(self, session_id: str, prompt: str) -> str: ...

    @abstractmethod
    def external_session_id(self, session_id: str) -> str | None: ...

    @abstractmethod
    async def cancel(self, session_id: str) -> None: ...

    @abstractmethod
    async def health(self) -> AgentHealth: ...

    @abstractmethod
    async def close(self) -> None: ...
