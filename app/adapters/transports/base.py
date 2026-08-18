from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class DeepSeekTransport(ABC):
    @abstractmethod
    async def create_session(self, workspace: Path) -> str: ...

    @abstractmethod
    async def send(self, session_id: str, prompt: str) -> str: ...

    @abstractmethod
    async def cancel(self, session_id: str) -> None: ...
