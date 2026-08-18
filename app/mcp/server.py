from __future__ import annotations

import argparse
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server import MCPServer

from app import __version__
from app.adapters.base import AgentAdapter
from app.adapters.registry import build_adapter_registry
from app.bridge.request_manager import RequestManager
from app.bridge.router import Router
from app.bridge.session_manager import SessionManager
from app.config import AppConfig, load_config
from app.mcp.tools import BridgeToolService
from app.runtime.workspace import WorkspaceManager
from app.storage.database import Database


def build_service(
    config: AppConfig,
    *,
    database: Database | None = None,
    adapters: Mapping[str, AgentAdapter] | None = None,
) -> BridgeToolService:
    storage = database or Database(config.runtime.database)
    registered_adapters = dict(adapters or build_adapter_registry(config))
    router = Router(registered_adapters, storage)
    requests = RequestManager(storage, router)
    sessions = SessionManager(storage, WorkspaceManager(config.runtime.workspace_root))
    return BridgeToolService(
        storage,
        sessions,
        requests,
        synchronous_wait_seconds=config.bridge.synchronous_wait_seconds,
    )


def create_mcp_server(
    config: AppConfig | None = None,
    *,
    service: BridgeToolService | None = None,
) -> MCPServer:
    settings = config or AppConfig()
    bridge = service or build_service(settings)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        await bridge.database.initialize()
        yield

    server = MCPServer(
        "Agent Bridge",
        version=__version__,
        instructions=(
            "One-hop bridge controlled by ChatGPT. Each bridge_send call invokes "
            "exactly one DeepSeek or Codex turn; no workflow step is automatic."
        ),
        lifespan=lifespan,
    )
    server.tool()(bridge.bridge_create_session)
    server.tool()(bridge.bridge_send)
    server.tool()(bridge.bridge_wait)
    server.tool()(bridge.bridge_status)
    server.tool()(bridge.bridge_cancel)
    server.tool()(bridge.bridge_close_session)
    return server


mcp = create_mcp_server()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Agent Bridge MCP server")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=None,
    )
    arguments = parser.parse_args()
    config = load_config(arguments.config) if arguments.config else AppConfig()
    transport = arguments.transport or config.mcp.transport
    server = create_mcp_server(config)
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run(
            "streamable-http",
            host=config.mcp.host,
            port=config.mcp.port,
            streamable_http_path=config.mcp.path,
            stateless_http=True,
            json_response=True,
        )


if __name__ == "__main__":
    main()
