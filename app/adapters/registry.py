from __future__ import annotations

from app.adapters.base import AgentAdapter
from app.adapters.deepseek import DeepSeekAdapter
from app.adapters.mock import MockAdapter
from app.adapters.transports.deepseek_cli import DeepSeekCLITransport
from app.config import AppConfig


def build_adapter_registry(config: AppConfig) -> dict[str, AgentAdapter]:
    if not config.deepseek.enabled or config.deepseek.transport == "mock":
        deepseek: AgentAdapter = MockAdapter("deepseek")
    elif config.deepseek.transport == "cli":
        deepseek = DeepSeekAdapter(
            DeepSeekCLITransport(
                executable=config.deepseek.executable,
                timeout_seconds=config.deepseek.timeout_seconds,
                health_timeout_seconds=config.deepseek.health_timeout_seconds,
            )
        )
    else:
        raise ValueError(
            f"unsupported DeepSeek transport: {config.deepseek.transport}"
        )
    return {
        "deepseek": deepseek,
        "codex": MockAdapter("codex"),
    }
