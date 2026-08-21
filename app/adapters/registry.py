from __future__ import annotations

from app.adapters.base import AgentAdapter
from app.adapters.codex import CodexAdapter
from app.adapters.deepseek import DeepSeekAdapter
from app.adapters.fallback import FallbackAdapter
from app.adapters.transports.dsh_plugin import DshPluginTransport
from app.adapters.mock import MockAdapter
from app.adapters.transports.codex_cli import CodexCLITransport
from app.adapters.transports.deepseek_cli import DeepSeekCLITransport
from app.config import AppConfig


def build_adapter_registry(config: AppConfig) -> dict[str, AgentAdapter]:
    if not config.deepseek.enabled or config.deepseek.transport == "mock":
        deepseek: AgentAdapter = MockAdapter("deepseek")
    elif config.deepseek.transport == "cli":
        deepseek = DeepSeekAdapter(
            DeepSeekCLITransport(
                executable=config.deepseek.executable,
                command_prefix=tuple(config.deepseek.command_prefix),
                timeout_seconds=config.deepseek.timeout_seconds,
                health_timeout_seconds=config.deepseek.health_timeout_seconds,
            )
        )
    elif config.deepseek.transport == "dsh-plugin":
        primary = DeepSeekAdapter(
            DshPluginTransport(
                config.deepseek.plugin_endpoint,
                config.deepseek.plugin_token,
                config.deepseek.health_timeout_seconds,
            )
        )
        fallback = DeepSeekAdapter(
            DeepSeekCLITransport(
                executable=config.deepseek.executable,
                command_prefix=tuple(config.deepseek.command_prefix),
                timeout_seconds=config.deepseek.timeout_seconds,
                health_timeout_seconds=config.deepseek.health_timeout_seconds,
            )
        )
        deepseek = FallbackAdapter(primary, fallback)
    else:
        raise ValueError(
            f"unsupported DeepSeek transport: {config.deepseek.transport}"
        )
    if not config.codex.enabled or config.codex.transport == "mock":
        codex: AgentAdapter = MockAdapter("codex")
    elif config.codex.transport == "cli":
        codex = CodexAdapter(
            CodexCLITransport(
                executable=config.codex.executable,
                command_prefix=tuple(config.codex.command_prefix),
                timeout_seconds=config.codex.timeout_seconds,
                health_timeout_seconds=config.codex.health_timeout_seconds,
            )
        )
    else:
        raise ValueError(f"unsupported Codex transport: {config.codex.transport}")
    return {"deepseek": deepseek, "codex": codex}
