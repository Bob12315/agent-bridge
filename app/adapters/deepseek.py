from __future__ import annotations

import json
from pathlib import Path

from app.adapters.base import AgentAdapter, AgentHealth, AgentTurnResult
from app.adapters.transports.base import DeepSeekTransport
from app.bridge.protocol import MessageContent, MessageEnvelope, new_id, utc_now
from app.bridge.session import SessionContext

FALLBACK_EXECUTOR_PROMPT = """你是代码执行器。
只执行 ChatGPT 明确给出的当前任务，只修改 Session Workspace 内的文件。
不得改变验收标准、扩大范围、调用其他 Agent 或进入下一阶段。
完成后返回 Summary、Changed Files、Commit 和 Test Results。"""


class DeepSeekAdapter(AgentAdapter):
    """Translate Bridge messages into one persistent DeepSeek executor session."""

    def __init__(
        self,
        transport: DeepSeekTransport,
        *,
        prompt_path: Path | None = None,
    ) -> None:
        self._transport = transport
        self._prompt_path = prompt_path or (
            Path(__file__).parents[2] / "prompts" / "deepseek_executor.md"
        )
        try:
            self._executor_prompt = self._prompt_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._executor_prompt = FALLBACK_EXECUTOR_PROMPT
        self._sessions: dict[str, str] = {}
        self._active_session_id: str | None = None

    async def start(self, context: SessionContext) -> str:
        existing = self._sessions.get(context.id)
        if existing is not None:
            return existing
        session_id = await self._transport.create_session(context.workspace)
        self._sessions[context.id] = session_id
        return session_id

    async def restore(self, context: SessionContext, external_session_id: str) -> str:
        """Hydrate the process-local adapter cache from Bridge persistence."""
        existing = self._sessions.get(context.id)
        if existing is not None:
            return existing
        restore = getattr(self._transport, "restore_session", None)
        if restore is None:
            raise RuntimeError("configured DeepSeek backend cannot resume persisted sessions")
        session_id = await restore(context.workspace, external_session_id)
        self._sessions[context.id] = session_id
        return session_id

    async def send(
        self, message: MessageEnvelope, context: SessionContext
    ) -> AgentTurnResult:
        if message.receiver != "deepseek":
            raise ValueError("DeepSeekAdapter only accepts messages for deepseek")
        if message.session_id != context.id:
            raise ValueError("message and context session IDs do not match")
        executor_session_id = await self.start(context)
        self._active_session_id = executor_session_id
        try:
            raw_response = await self._transport.send(
                executor_session_id,
                self._render_prompt(message, context),
            )
        finally:
            self._active_session_id = None
        response_type, content = self._parse_response(raw_response)
        response = MessageEnvelope(
            id=new_id("msg"),
            session_id=message.session_id,
            sender="deepseek",
            receiver=message.sender,
            type=response_type,
            task_id=message.task_id,
            stage=message.stage,
            round=message.round,
            reply_to=message.id,
            content=content,
            created_at=utc_now(),
        )
        return AgentTurnResult(
            response=response,
            external_session_id=(
                self._transport.external_session_id(executor_session_id)
                or executor_session_id
            ),
        )

    async def cancel(self) -> None:
        if self._active_session_id is not None:
            await self._transport.cancel(self._active_session_id)

    async def health(self) -> AgentHealth:
        return await self._transport.health()

    async def close(self) -> None:
        await self._transport.close()
        self._sessions.clear()
        self._active_session_id = None

    def _render_prompt(
        self, message: MessageEnvelope, context: SessionContext
    ) -> str:
        payload = {
            "task_id": message.task_id,
            "stage": message.stage,
            "round": message.round,
            "type": message.type,
            "content": message.content.model_dump(mode="json"),
        }
        return (
            f"{self._executor_prompt.strip()}\n\n"
            "Bridge execution context:\n"
            f"- Session Workspace: {context.workspace}\n"
            "- Modify files only inside that workspace.\n"
            "- Perform only this explicit turn; do not invoke another Agent.\n\n"
            "Message JSON:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    @staticmethod
    def _parse_response(raw_response: str) -> tuple[str, MessageContent]:
        try:
            payload = json.loads(raw_response)
        except json.JSONDecodeError:
            return "result", MessageContent(text=raw_response)
        if not isinstance(payload, dict) or payload.get("type") not in {
            "result",
            "question",
            "error",
        }:
            return "result", MessageContent(text=raw_response)
        content_data = payload.get("content", payload.get("text"))
        if isinstance(content_data, str):
            content = MessageContent(text=content_data)
        elif isinstance(content_data, dict):
            content = MessageContent.model_validate(content_data)
        else:
            return "result", MessageContent(text=raw_response)
        return payload["type"], content
