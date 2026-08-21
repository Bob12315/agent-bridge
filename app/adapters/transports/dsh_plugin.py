from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.adapters.base import AgentHealth
from app.adapters.transports.deepseek_cli import DeepSeekTransportError


class DshPluginTransport:
    """Thin local protocol adapter for the agent-bridge-dsh plugin.

    The plugin owns DeepSeek history and UI. Bridge only exchanges task metadata,
    messages, status, cancellation, and the stable external session identifier.
    """

    def __init__(self, endpoint: str, token: str | None = None, timeout_seconds: float = 30) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self._sessions: dict[str, tuple[Path, str | None]] = {}

    async def create_session(self, workspace: Path) -> str:
        payload = {"execution_workspace": str(workspace)}
        response = await self._post("/sessions", payload)
        session_id = self._required_string(response, "session_id")
        self._sessions[session_id] = (workspace, self._optional_string(response, "external_session_id"))
        return session_id

    async def restore_session(self, workspace: Path, external_session_id: str) -> str:
        response = await self._post("/sessions/resume", {
            "execution_workspace": str(workspace), "external_session_id": external_session_id,
        })
        session_id = self._required_string(response, "session_id")
        self._sessions[session_id] = (workspace, external_session_id)
        return session_id

    async def send(self, session_id: str, prompt: str) -> str:
        workspace, external = self._sessions[session_id]
        response = await self._post(f"/sessions/{session_id}/turns", {
            "prompt": prompt, "execution_workspace": str(workspace), "external_session_id": external,
        })
        external = self._optional_string(response, "external_session_id") or external
        self._sessions[session_id] = (workspace, external)
        return self._required_string(response, "text")

    async def resume(self, session_id: str, prompt: str) -> str:
        return await self.send(session_id, prompt)

    def external_session_id(self, session_id: str) -> str | None:
        return self._sessions[session_id][1]

    async def cancel(self, session_id: str) -> None:
        await self._post(f"/sessions/{session_id}/cancel", {})

    async def health(self) -> AgentHealth:
        try:
            result = await self._get("/health")
        except DeepSeekTransportError as exc:
            return AgentHealth(status="unavailable", detail=str(exc))
        return AgentHealth(status="healthy" if result.get("status") == "healthy" else "degraded", detail=str(result.get("detail") or "DSH plugin reachable"))

    async def close(self) -> None:
        self._sessions.clear()

    async def _get(self, path: str) -> dict[str, object]:
        return await asyncio.to_thread(self._request, path, None)

    async def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        return await asyncio.to_thread(self._request, path, payload)

    def _request(self, path: str, payload: dict[str, object] | None) -> dict[str, object]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with urlopen(Request(self.endpoint + path, data=data, headers=headers), timeout=self.timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (URLError, OSError, json.JSONDecodeError) as exc:
            raise DeepSeekTransportError(f"DSH plugin request failed: {exc}") from exc
        if not isinstance(parsed, dict):
            raise DeepSeekTransportError("DSH plugin returned a non-object response")
        return parsed

    @staticmethod
    def _required_string(data: dict[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise DeepSeekTransportError(f"DSH plugin response missing {key}")
        return value

    @staticmethod
    def _optional_string(data: dict[str, object], key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None
