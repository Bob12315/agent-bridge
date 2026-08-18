from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from app.adapters.base import AgentAdapter, AgentHealth, AgentTurnResult
from app.bridge.protocol import MessageContent, MessageEnvelope, new_id, utc_now
from app.bridge.session import SessionContext

MockMode = Literal["success", "error", "timeout", "question", "changes_required"]


@dataclass(slots=True)
class MockBehavior:
    delay_seconds: float = 0
    mode: MockMode = "success"


class MockAdapter(AgentAdapter):
    def __init__(self, agent: Literal["deepseek", "codex"], behavior: MockBehavior | None = None):
        self.agent = agent
        self.behavior = behavior or MockBehavior()
        self._cancel_event = asyncio.Event()
        self.turn_count = 0

    async def start(self, context: SessionContext) -> str | None:
        return f"mock-{self.agent}-{context.id}"

    async def send(
        self, message: MessageEnvelope, context: SessionContext
    ) -> AgentTurnResult:
        self.turn_count += 1
        self._cancel_event.clear()
        if self.behavior.mode == "error":
            raise RuntimeError(f"simulated {self.agent} error")

        delay = self.behavior.delay_seconds
        if self.behavior.mode == "timeout" and delay <= 0:
            delay = 3600
        if delay:
            try:
                await asyncio.wait_for(self._cancel_event.wait(), timeout=delay)
            except TimeoutError:
                pass
            else:
                raise asyncio.CancelledError(f"mock {self.agent} turn cancelled")

        if self.behavior.mode == "question":
            response_type = "question"
            content = MessageContent(text=f"{self.agent.upper()}_MOCK_QUESTION", blocking=True)
        elif self.agent == "codex":
            response_type = "review_result"
            verdict = "CHANGES_REQUIRED" if self.behavior.mode == "changes_required" else "PASS"
            content = MessageContent(text=f"CODEX_MOCK_{verdict}", verdict=verdict)
        else:
            response_type = "result"
            content = MessageContent(text="DEEPSEEK_MOCK_OK")

        response = MessageEnvelope(
            id=new_id("msg"),
            session_id=message.session_id,
            sender=self.agent,
            receiver=message.sender,
            type=response_type,
            task_id=message.task_id,
            stage=message.stage,
            round=message.round,
            reply_to=message.id,
            content=content,
            created_at=utc_now(),
        )
        return AgentTurnResult(response=response, external_session_id=f"mock-{self.agent}-{context.id}")

    async def cancel(self) -> None:
        self._cancel_event.set()

    async def health(self) -> AgentHealth:
        return AgentHealth(status="healthy", detail="mock adapter")

    async def close(self) -> None:
        self._cancel_event.set()
