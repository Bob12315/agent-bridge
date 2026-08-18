from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class RuntimeConfig(BaseModel):
    database: Path = Path("./runtime/bridge.db")
    workspace_root: Path = Path("./runtime/workspaces")
    log_root: Path = Path("./runtime/logs")


class BridgeConfig(BaseModel):
    synchronous_wait_seconds: int = Field(default=30, ge=0)


class AgentConfig(BaseModel):
    enabled: bool = False
    transport: str = "mock"


class WebConfig(BaseModel):
    enabled: bool = True


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: ServerConfig = Field(default_factory=ServerConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    deepseek: AgentConfig = Field(default_factory=AgentConfig)
    codex: AgentConfig = Field(default_factory=AgentConfig)
    web: WebConfig = Field(default_factory=WebConfig)


def load_config(path: Path) -> AppConfig:
    """Load YAML configuration without resolving paths against one OS."""
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    return AppConfig.model_validate(data)
