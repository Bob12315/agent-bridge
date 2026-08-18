from __future__ import annotations

from datetime import timedelta

import httpx
from fastapi import Request

from app.bridge.protocol import MessageContent, MessageEnvelope, utc_now
from app.bridge.request_manager import BridgeRequestResult
from app.bridge.session import SessionContext
from app.config import AppConfig, WebConfig
from app.main import create_app
from app.storage.database import Database
from app.storage.models import EventRecord, RequestRecord
from app.web.sse import EventStream


class FakeRequestManager:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel(self, request_id: str) -> BridgeRequestResult:
        self.cancelled.append(request_id)
        return BridgeRequestResult(request_id=request_id, status="cancelled")


async def seed_web_data(database: Database, session: SessionContext) -> None:
    declared = MessageEnvelope(
        id="msg_web_task",
        session_id=session.id,
        sender="chatgpt",
        receiver="deepseek",
        type="task",
        stage=4,
        round=2,
        content=MessageContent(text="Build the monitoring dashboard"),
        created_at=utc_now(),
    )
    response = MessageEnvelope(
        id="msg_web_result",
        session_id=session.id,
        sender="deepseek",
        receiver="chatgpt",
        type="result",
        reply_to=declared.id,
        content=MessageContent(text="Dashboard work is running"),
        created_at=utc_now(),
    )
    await database.insert_message(declared)
    await database.insert_message(response)
    now = utc_now()
    await database.insert_request(
        RequestRecord(
            id="req_web",
            message_id=declared.id,
            session_id=session.id,
            agent="deepseek",
            status="running",
            started_at=now - timedelta(seconds=2),
        )
    )
    await database.insert_event(
        EventRecord(
            id="evt_web_started",
            session_id=session.id,
            request_id="req_web",
            agent="deepseek",
            type="AGENT_STARTED",
            message="Dashboard request started",
            created_at=now,
        )
    )
    await database.insert_event(
        EventRecord(
            id="evt_web_progress",
            session_id=session.id,
            request_id="req_web",
            agent="deepseek",
            type="AGENT_PROGRESS",
            message="Rendering templates",
            created_at=now + timedelta(microseconds=1),
        )
    )


async def test_dashboard_session_agent_and_cancel_routes(
    database: Database, session: SessionContext
) -> None:
    await seed_web_data(database, session)
    requests = FakeRequestManager()
    application = create_app(database=database, request_manager=requests)  # type: ignore[arg-type]

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.json() == {"status": "online"}

        dashboard = await client.get("/")
        assert dashboard.status_code == 200
        assert "test-project" in dashboard.text
        assert session.id in dashboard.text

        page = await client.get(f"/sessions/{session.id}")
        assert page.status_code == 200
        assert "Stage" in page.text and ">4<" in page.text
        assert "Round" in page.text and ">2<" in page.text
        assert "Build the monitoring dashboard" in page.text
        assert "Dashboard request started" in page.text
        assert "Cancel request" in page.text

        panel = await client.get(f"/sessions/{session.id}/panel")
        assert panel.status_code == 200
        assert "Message Timeline" in panel.text

        agent = await client.get(f"/sessions/{session.id}/agents/deepseek")
        assert agent.status_code == 200
        assert "Request History" in agent.text
        assert "req_web" in agent.text

        cancelled = await client.post("/requests/req_web/cancel", follow_redirects=False)
        assert cancelled.status_code == 303
        assert cancelled.headers["x-request-status"] == "cancelled"
        assert cancelled.headers["location"] == f"/sessions/{session.id}"
        assert requests.cancelled == ["req_web"]

        assert (await client.get("/static/app.css")).status_code == 200
        assert (await client.get("/sessions/missing")).status_code == 404
        assert (
            await client.get(f"/sessions/{session.id}/agents/system")
        ).status_code == 404
        assert (await client.post("/requests/missing/cancel")).status_code == 404


async def test_empty_dashboard(database: Database) -> None:
    application = create_app(database=database)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert "No sessions yet" in response.text


async def test_app_lifespan_initializes_database_and_web_can_be_disabled(
    tmp_path
) -> None:
    database = Database(tmp_path / "web-lifespan.db")
    application = create_app(database=database)
    async with application.router.lifespan_context(application):
        assert database.path.exists()

    disabled = create_app(
        AppConfig(web=WebConfig(enabled=False)), database=database
    )
    transport = httpx.ASGITransport(app=disabled)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/")).status_code == 404


async def test_sse_fetch_cursor_filter_and_encoding(
    database: Database, session: SessionContext
) -> None:
    await seed_web_data(database, session)
    stream = EventStream(database)
    events = await stream.fetch(None, session.id)
    assert [event.id for event in events] == ["evt_web_started", "evt_web_progress"]

    remaining = await stream.fetch("evt_web_started", session.id)
    assert [event.id for event in remaining] == ["evt_web_progress"]
    payload = stream.encode(remaining[0])
    assert payload.startswith("id: evt_web_progress\nevent: AGENT_PROGRESS\n")
    assert '"message":"Rendering templates"' in payload
    assert payload.endswith("\n\n")

    application = create_app(database=database)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/events",
            "headers": [],
            "app": application,
        }
    )
    response = stream.response(request, None, session.id)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"


async def test_sse_iterator_emits_event_and_heartbeat(
    database: Database, session: SessionContext
) -> None:
    await database.insert_event(
        EventRecord(
            id="evt_stream",
            session_id=session.id,
            agent="system",
            type="SESSION_CREATED",
            message="stream ready",
        )
    )

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    stream = EventStream(database, poll_seconds=0.001, heartbeat_seconds=0.001)
    iterator = stream.iterate(ConnectedRequest(), None, session.id)  # type: ignore[arg-type]
    assert "evt_stream" in await anext(iterator)
    await iterator.aclose()

    heartbeat = stream.iterate(
        ConnectedRequest(), "evt_stream", session.id  # type: ignore[arg-type]
    )
    assert await anext(heartbeat) == ": heartbeat\n\n"
    await heartbeat.aclose()
