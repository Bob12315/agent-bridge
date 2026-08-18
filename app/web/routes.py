from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.bridge.request_manager import RequestManager, RequestManagerError
from app.bridge.session import SessionContext
from app.storage.database import Database
from app.storage.models import RequestRecord
from app.web.sse import EventStream

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _database(request: Request) -> Database:
    return request.app.state.database


def _request_manager(request: Request) -> RequestManager:
    return request.app.state.request_manager


def _duration(record: RequestRecord, now: datetime) -> float:
    start = record.started_at or record.queued_at
    end = record.finished_at or now
    return max(0.0, (end - start).total_seconds())


async def _session_view(database: Database, session: SessionContext) -> dict[str, object]:
    messages = await database.list_messages(session.id)
    events = await database.list_events(session.id)
    requests = await database.list_requests(session.id)
    agents = await database.list_agent_sessions(session.id)
    latest_declaration = next(
        (
            message
            for message in reversed(messages)
            if message.sender == "chatgpt"
            and (message.stage is not None or message.round is not None)
        ),
        None,
    )
    current_request = next(
        (item for item in requests if item.status in {"queued", "running"}), None
    )
    message_by_id = {message.id: message for message in messages}
    now = datetime.now(UTC)
    request_views = [
        {
            "record": item,
            "duration": _duration(item, now),
            "message": message_by_id.get(item.message_id),
        }
        for item in requests
    ]
    return {
        "session": session,
        "messages": messages,
        "events": events,
        "agents": {agent.agent: agent for agent in agents},
        "stage": latest_declaration.stage if latest_declaration else None,
        "round": latest_declaration.round if latest_declaration else None,
        "current_request": current_request,
        "current_message": (
            message_by_id.get(current_request.message_id) if current_request else None
        ),
        "requests": request_views,
    }


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "online"}


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    sessions = await _database(request).list_sessions()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"sessions": sessions},
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_page(request: Request, session_id: str):
    database = _database(request)
    session = await database.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    context = await _session_view(database, session)
    return templates.TemplateResponse(
        request=request, name="session.html", context=context
    )


@router.get("/sessions/{session_id}/panel", response_class=HTMLResponse)
async def session_panel(request: Request, session_id: str):
    database = _database(request)
    session = await database.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    context = await _session_view(database, session)
    return templates.TemplateResponse(
        request=request, name="_session_panel.html", context=context
    )


@router.get(
    "/sessions/{session_id}/agents/{agent}", response_class=HTMLResponse
)
async def agent_page(request: Request, session_id: str, agent: str):
    if agent not in {"deepseek", "codex"}:
        raise HTTPException(status_code=404, detail="Agent not found")
    database = _database(request)
    session = await database.get_session(session_id)
    agent_session = await database.get_agent_session(session_id, agent)
    if session is None or agent_session is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    requests = [
        item for item in await database.list_requests(session_id) if item.agent == agent
    ]
    return templates.TemplateResponse(
        request=request,
        name="agent.html",
        context={"session": session, "agent_session": agent_session, "requests": requests},
    )


@router.post("/requests/{request_id}/cancel")
async def cancel_request(request: Request, request_id: str):
    record = await _database(request).get_request(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Request not found")
    try:
        result = await _request_manager(request).cancel(request_id)
    except RequestManagerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/sessions/{record.session_id}",
        status_code=303,
        headers={"X-Request-Status": result.status},
    )


@router.get("/api/events")
async def events(
    request: Request,
    session_id: str | None = Query(default=None),
    after: str | None = Query(default=None),
):
    cursor = request.headers.get("last-event-id") or after
    stream = EventStream(_database(request))
    return stream.response(request, cursor, session_id)
