from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from app.bridge.protocol import MessageEnvelope
from app.bridge.session import AgentSession, SessionContext
from app.storage.models import EventRecord, RequestRecord


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    project_name TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    current_branch TEXT NOT NULL,
                    base_commit TEXT,
                    access_mode TEXT NOT NULL DEFAULT 'inspect',
                    status TEXT NOT NULL,
                    current_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY,
                    bridge_session_id TEXT NOT NULL REFERENCES sessions(id),
                    agent TEXT NOT NULL,
                    external_session_id TEXT,
                    status TEXT NOT NULL,
                    UNIQUE(bridge_session_id, agent)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    sender TEXT NOT NULL,
                    receiver TEXT NOT NULL,
                    type TEXT NOT NULL,
                    task_id TEXT,
                    stage INTEGER,
                    round INTEGER,
                    reply_to TEXT,
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES messages(id),
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    agent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    response_message_id TEXT,
                    error_code TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    request_id TEXT,
                    agent TEXT,
                    type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_created
                    ON messages(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_session_created
                    ON events(session_id, created_at);
                """
            )
            columns = {
                row[1]
                for row in await (await connection.execute("PRAGMA table_info(sessions)"))
                .fetchall()
            }
            if "base_commit" not in columns:
                await connection.execute("ALTER TABLE sessions ADD COLUMN base_commit TEXT")
            if "access_mode" not in columns:
                await connection.execute(
                    "ALTER TABLE sessions ADD COLUMN access_mode TEXT NOT NULL DEFAULT 'inspect'"
                )
            request_columns = {
                row[1]
                for row in await (await connection.execute("PRAGMA table_info(requests)"))
                .fetchall()
            }
            if "response_message_id" not in request_columns:
                await connection.execute(
                    "ALTER TABLE requests ADD COLUMN response_message_id TEXT"
                )
            if "error_code" not in request_columns:
                await connection.execute("ALTER TABLE requests ADD COLUMN error_code TEXT")
            await connection.commit()

    async def insert_session(self, session: SessionContext) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute(
                """INSERT INTO sessions
                (id, project_name, workspace, base_branch, current_branch,
                 base_commit, access_mode, status, current_task_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.id, session.project_name, str(session.workspace),
                    session.base_branch, session.current_branch, session.base_commit,
                    session.access_mode,
                    session.status, session.current_task_id, session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )
            await connection.commit()

    async def insert_session_bundle(
        self,
        session: SessionContext,
        agent_sessions: list[AgentSession],
        event: EventRecord,
    ) -> None:
        """Persist a newly created session atomically."""
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute(
                """INSERT INTO sessions
                (id, project_name, workspace, base_branch, current_branch,
                 base_commit, access_mode, status, current_task_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.id,
                    session.project_name,
                    str(session.workspace),
                    session.base_branch,
                    session.current_branch,
                    session.base_commit,
                    session.access_mode,
                    session.status,
                    session.current_task_id,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )
            await connection.executemany(
                "INSERT INTO agent_sessions VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        item.id,
                        item.bridge_session_id,
                        item.agent,
                        item.external_session_id,
                        item.status,
                    )
                    for item in agent_sessions
                ],
            )
            await connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.session_id,
                    event.request_id,
                    event.agent,
                    event.type,
                    event.message,
                    event.created_at.isoformat(),
                ),
            )
            await connection.commit()

    async def get_session(self, session_id: str) -> SessionContext | None:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = await cursor.fetchone()
        return SessionContext.model_validate(dict(row)) if row else None

    async def list_sessions(self, limit: int = 100) -> list[SessionContext]:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
        return [SessionContext.model_validate(dict(row)) for row in rows]

    async def insert_agent_session(self, session: AgentSession) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute(
                "INSERT INTO agent_sessions VALUES (?, ?, ?, ?, ?)",
                (session.id, session.bridge_session_id, session.agent, session.external_session_id, session.status),
            )
            await connection.commit()

    async def list_agent_sessions(self, bridge_session_id: str) -> list[AgentSession]:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM agent_sessions WHERE bridge_session_id = ? ORDER BY agent",
                (bridge_session_id,),
            )
            rows = await cursor.fetchall()
        return [AgentSession.model_validate(dict(row)) for row in rows]

    async def get_agent_session(
        self, bridge_session_id: str, agent: str
    ) -> AgentSession | None:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """SELECT * FROM agent_sessions
                WHERE bridge_session_id = ? AND agent = ?""",
                (bridge_session_id, agent),
            )
            row = await cursor.fetchone()
        return AgentSession.model_validate(dict(row)) if row else None

    async def update_agent_session_status(
        self, bridge_session_id: str, agent: str, status: str
    ) -> None:
        async with aiosqlite.connect(self.path) as connection:
            cursor = await connection.execute(
                """UPDATE agent_sessions SET status = ?
                WHERE bridge_session_id = ? AND agent = ?""",
                (status, bridge_session_id, agent),
            )
            if cursor.rowcount != 1:
                raise LookupError(
                    f"agent session not found: {bridge_session_id}/{agent}"
                )
            await connection.commit()

    async def update_session_status(
        self, session_id: str, status: str, updated_at: str
    ) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, updated_at, session_id),
            )
            await connection.commit()

    async def update_agent_session_statuses(
        self, bridge_session_id: str, status: str
    ) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute(
                "UPDATE agent_sessions SET status = ? WHERE bridge_session_id = ?",
                (status, bridge_session_id),
            )
            await connection.commit()

    async def close_session_records(
        self, session_id: str, updated_at: str, event: EventRecord
    ) -> None:
        """Close a session and its agent sessions atomically."""
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute(
                "UPDATE agent_sessions SET status = 'closed' WHERE bridge_session_id = ?",
                (session_id,),
            )
            await connection.execute(
                "UPDATE sessions SET status = 'closed', updated_at = ? WHERE id = ?",
                (updated_at, session_id),
            )
            await connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.session_id,
                    event.request_id,
                    event.agent,
                    event.type,
                    event.message,
                    event.created_at.isoformat(),
                ),
            )
            await connection.commit()

    async def insert_message(self, message: MessageEnvelope) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute(
                """INSERT INTO messages
                (id, session_id, sender, receiver, type, task_id, stage, round,
                 reply_to, content_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.id, message.session_id, message.sender, message.receiver,
                    message.type, message.task_id, message.stage, message.round,
                    message.reply_to, message.content.model_dump_json(),
                    message.created_at.isoformat(),
                ),
            )
            await connection.commit()

    async def get_message(self, message_id: str) -> MessageEnvelope | None:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
            row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        data["content"] = json.loads(data.pop("content_json"))
        return MessageEnvelope.model_validate(data)

    async def list_messages(
        self, session_id: str, limit: int = 200
    ) -> list[MessageEnvelope]:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """SELECT * FROM messages WHERE session_id = ?
                ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit),
            )
            rows = await cursor.fetchall()
        messages: list[MessageEnvelope] = []
        for row in reversed(rows):
            data = dict(row)
            data["content"] = json.loads(data.pop("content_json"))
            messages.append(MessageEnvelope.model_validate(data))
        return messages

    async def insert_request(self, request: RequestRecord) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute(
                """INSERT INTO requests
                (id, message_id, session_id, agent, status, queued_at, started_at,
                 finished_at, response_message_id, error_code, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request.id, request.message_id, request.session_id, request.agent,
                    request.status, request.queued_at.isoformat(),
                    request.started_at.isoformat() if request.started_at else None,
                    request.finished_at.isoformat() if request.finished_at else None,
                    request.response_message_id, request.error_code,
                    request.error,
                ),
            )
            await connection.commit()

    async def create_request(
        self,
        message: MessageEnvelope,
        request: RequestRecord,
        event: EventRecord,
    ) -> None:
        """Persist the incoming message and queued request atomically."""
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute(
                """INSERT INTO messages
                (id, session_id, sender, receiver, type, task_id, stage, round,
                 reply_to, content_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.id,
                    message.session_id,
                    message.sender,
                    message.receiver,
                    message.type,
                    message.task_id,
                    message.stage,
                    message.round,
                    message.reply_to,
                    message.content.model_dump_json(),
                    message.created_at.isoformat(),
                ),
            )
            await connection.execute(
                """INSERT INTO requests
                (id, message_id, session_id, agent, status, queued_at, started_at,
                 finished_at, response_message_id, error_code, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request.id,
                    request.message_id,
                    request.session_id,
                    request.agent,
                    request.status,
                    request.queued_at.isoformat(),
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            )
            await connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.session_id,
                    event.request_id,
                    event.agent,
                    event.type,
                    event.message,
                    event.created_at.isoformat(),
                ),
            )
            await connection.commit()

    async def get_request(self, request_id: str) -> RequestRecord | None:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            )
            row = await cursor.fetchone()
        return RequestRecord.model_validate(dict(row)) if row else None

    async def list_requests(
        self, session_id: str, limit: int = 100
    ) -> list[RequestRecord]:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """SELECT * FROM requests WHERE session_id = ?
                ORDER BY queued_at DESC LIMIT ?""",
                (session_id, limit),
            )
            rows = await cursor.fetchall()
        return [RequestRecord.model_validate(dict(row)) for row in rows]

    async def update_request(self, request: RequestRecord) -> None:
        async with aiosqlite.connect(self.path) as connection:
            cursor = await connection.execute(
                """UPDATE requests SET status = ?, started_at = ?, finished_at = ?,
                    response_message_id = ?, error_code = ?, error = ?
                WHERE id = ?""",
                (
                    request.status,
                    request.started_at.isoformat() if request.started_at else None,
                    request.finished_at.isoformat() if request.finished_at else None,
                    request.response_message_id,
                    request.error_code,
                    request.error,
                    request.id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"request not found: {request.id}")
            await connection.commit()

    async def insert_event(self, event: EventRecord) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id, event.session_id, event.request_id, event.agent,
                    event.type, event.message, event.created_at.isoformat(),
                ),
            )
            await connection.commit()

    async def list_events(self, session_id: str) -> list[EventRecord]:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM events WHERE session_id = ? ORDER BY created_at", (session_id,)
            )
            rows = await cursor.fetchall()
        return [EventRecord.model_validate(dict(row)) for row in rows]

    async def list_events_after(
        self,
        after_event_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[EventRecord]:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            clauses: list[str] = []
            parameters: list[object] = []
            if session_id is not None:
                clauses.append("session_id = ?")
                parameters.append(session_id)
            if after_event_id is not None:
                cursor = await connection.execute(
                    "SELECT created_at, id FROM events WHERE id = ?",
                    (after_event_id,),
                )
                after = await cursor.fetchone()
                if after is not None:
                    clauses.append(
                        "(created_at > ? OR (created_at = ? AND id > ?))"
                    )
                    parameters.extend(
                        [after["created_at"], after["created_at"], after["id"]]
                    )
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            parameters.append(limit)
            cursor = await connection.execute(
                f"""SELECT * FROM events {where}
                ORDER BY created_at, id LIMIT ?""",
                parameters,
            )
            rows = await cursor.fetchall()
        return [EventRecord.model_validate(dict(row)) for row in rows]
