from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.adapters.base import AgentAdapter, AgentHealth, AgentTurnResult
from app.adapters.transports.base import CodexTransport
from app.bridge.protocol import (
    MessageContent,
    MessageEnvelope,
    ReviewIssue,
    new_id,
    utc_now,
)
from app.bridge.session import SessionContext

FALLBACK_REVIEWER_PROMPT = """你是独立代码审核员。
你的职责是审核代码，不是修改代码。直接检查实际仓库与 Git Diff。
检查要求、验收标准、约束、范围、Bug、回归风险、测试和错误处理。
最终返回 PASS 或 CHANGES_REQUIRED。"""


class CodexReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["PASS", "CHANGES_REQUIRED"]
    text: str = Field(min_length=1)
    issues: list[ReviewIssue]
    blocking: bool

    @model_validator(mode="after")
    def validate_verdict(self) -> "CodexReviewPayload":
        if self.verdict == "PASS" and (self.issues or self.blocking):
            raise ValueError("PASS review cannot contain issues or be blocking")
        if self.verdict == "CHANGES_REQUIRED" and (
            not self.issues or not self.blocking
        ):
            raise ValueError(
                "CHANGES_REQUIRED review must contain issues and be blocking"
            )
        return self


class CodexAdapter(AgentAdapter):
    """Run every review in a fresh, read-only Codex session."""

    def __init__(
        self,
        transport: CodexTransport,
        *,
        prompt_path: Path | None = None,
    ) -> None:
        self._transport = transport
        self._prompt_path = prompt_path or (
            Path(__file__).parents[2] / "prompts" / "codex_reviewer.md"
        )
        try:
            self._reviewer_prompt = self._prompt_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._reviewer_prompt = FALLBACK_REVIEWER_PROMPT

    async def start(self, context: SessionContext) -> None:
        return None

    async def send(
        self, message: MessageEnvelope, context: SessionContext
    ) -> AgentTurnResult:
        if message.receiver != "codex" or message.type != "review_request":
            raise ValueError("CodexAdapter only accepts review_request messages")
        if message.session_id != context.id:
            raise ValueError("message and context session IDs do not match")
        output = await self._transport.review(
            context.workspace,
            self._render_prompt(message, context),
        )
        payload = CodexReviewPayload.model_validate_json(output.response)
        response = MessageEnvelope(
            id=new_id("msg"),
            session_id=message.session_id,
            sender="codex",
            receiver=message.sender,
            type="review_result",
            task_id=message.task_id,
            stage=message.stage,
            round=message.round,
            reply_to=message.id,
            content=MessageContent(
                text=payload.text,
                issues=payload.issues,
                blocking=payload.blocking,
                verdict=payload.verdict,
            ),
            created_at=utc_now(),
        )
        return AgentTurnResult(
            response=response,
            external_session_id=output.external_session_id,
        )

    async def cancel(self) -> None:
        await self._transport.cancel()

    async def health(self) -> AgentHealth:
        return await self._transport.health()

    async def close(self) -> None:
        await self._transport.close()

    def _render_prompt(
        self, message: MessageEnvelope, context: SessionContext
    ) -> str:
        payload = {
            "task_id": message.task_id,
            "stage": message.stage,
            "round": message.round,
            "content": message.content.model_dump(mode="json"),
        }
        return (
            f"{self._reviewer_prompt.strip()}\n\n"
            "Review execution context:\n"
            f"- Session Workspace: {context.workspace}\n"
            f"- Base branch: {context.base_branch}\n"
            f"- Current branch: {context.current_branch}\n"
            "- Inspect the actual Git repository, commit, git status and git diff.\n"
            "- Do not modify files, run formatters, apply fixes, or commit changes.\n"
            "- Perform only this review; do not invoke another Agent.\n\n"
            "Review request JSON:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
