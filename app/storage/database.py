from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from app.bridge.protocol import MessageEnvelope
from app.bridge.session import AgentSession, SessionContext
from app.storage.models import EventRecord, ProjectRecord, RequestRecord, TaskRecord


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
                    project_id TEXT,
                    task_name TEXT,
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
                    backend TEXT,
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
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    repo_path TEXT NOT NULL UNIQUE,
                    default_branch TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    task_name TEXT NOT NULL,
                    bridge_session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, task_name)
                );
                CREATE TABLE IF NOT EXISTS git_operations (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    operation TEXT NOT NULL,
                    confirmation_id TEXT,
                    expected_base_commit TEXT,
                    status TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_external
                    ON agent_sessions(external_session_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_project_status
                    ON tasks(project_id, status);
                CREATE INDEX IF NOT EXISTS idx_requests_session_status
                    ON requests(session_id, status);
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
            if "project_id" not in columns:
                await connection.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT")
            if "task_name" not in columns:
                await connection.execute("ALTER TABLE sessions ADD COLUMN task_name TEXT")
            agent_columns = {
                row[1]
                for row in await (await connection.execute("PRAGMA table_info(agent_sessions)"))
                .fetchall()
            }
            if "backend" not in agent_columns:
                await connection.execute("ALTER TABLE agent_sessions ADD COLUMN backend TEXT")
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
                (id, project_name, project_id, task_name, workspace, base_branch, current_branch,
                 base_commit, access_mode, status, current_task_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.id, session.project_name, session.project_id, session.task_name, str(session.workspace),
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
                (id, project_name, project_id, task_name, workspace, base_branch, current_branch,
                 base_commit, access_mode, status, current_task_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.id,
                    session.project_name,
                    session.project_id,
                    session.task_name,
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
                """INSERT INTO agent_sessions
                (id, bridge_session_id, agent, backend, external_session_id, status)
                VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        item.id,
                        item.bridge_session_id,
                        item.agent,
                        item.backend,
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
                """INSERT INTO agent_sessions
                (id, bridge_session_id, agent, backend, external_session_id, status)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (session.id, session.bridge_session_id, session.agent, session.backend, session.external_session_id, session.status),
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

    async def update_agent_session_external(
        self,
        bridge_session_id: str,
        agent: str,
        external_session_id: str | None,
        *,
        backend: str | None = None,
        status: str = "ready",
    ) -> None:
        """Persist the external mapping after a successful adapter turn.

        The mapping is the recovery source of truth; adapter in-memory caches are
        intentionally treated as disposable.
        """
        async with aiosqlite.connect(self.path) as connection:
            cursor = await connection.execute(
                """UPDATE agent_sessions
                SET external_session_id = ?, backend = COALESCE(?, backend), status = ?
                WHERE bridge_session_id = ? AND agent = ?""",
                (external_session_id, backend, status, bridge_session_id, agent),
            )
            if cursor.rowcount != 1:
                raise LookupError(
                    f"agent session not found: {bridge_session_id}/{agent}"
                )
            await connection.commit()

    async def upsert_project(self, project: ProjectRecord) -> ProjectRecord:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute(
                """INSERT INTO projects
                (id, name, repo_path, default_branch, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  repo_path = excluded.repo_path,
                  default_branch = excluded.default_branch,
                  updated_at = excluded.updated_at""",
                (
                    project.id,
                    project.name,
                    project.repo_path,
                    project.default_branch,
                    project.created_at.isoformat(),
                    project.updated_at.isoformat(),
                ),
            )
            await connection.commit()
            connection.row_factory = aiosqlite.Row
            row = await (
                await connection.execute("SELECT * FROM projects WHERE name = ?", (project.name,))
            ).fetchone()
        return ProjectRecord.model_validate(dict(row))

    async def get_project(self, project_id: str) -> ProjectRecord | None:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (
                await connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
            ).fetchone()
        return ProjectRecord.model_validate(dict(row)) if row else None

    async def list_projects(self, limit: int = 100) -> list[ProjectRecord]:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            rows = await (
                await connection.execute(
                    "SELECT * FROM projects ORDER BY updated_at DESC LIMIT ?", (limit,)
                )
            ).fetchall()
        return [ProjectRecord.model_validate(dict(row)) for row in rows]

    async def insert_task(self, task: TaskRecord) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute(
                """INSERT INTO tasks
                (id, project_id, task_name, bridge_session_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.id,
                    task.project_id,
                    task.task_name,
                    task.bridge_session_id,
                    task.status,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )
            await connection.commit()

    async def get_task(self, task_id: str) -> TaskRecord | None:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (await connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))).fetchone()
        return TaskRecord.model_validate(dict(row)) if row else None

    async def get_task_for_session(self, session_id: str) -> TaskRecord | None:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (
                await connection.execute(
                    "SELECT * FROM tasks WHERE bridge_session_id = ?", (session_id,)
                )
            ).fetchone()
        return TaskRecord.model_validate(dict(row)) if row else None

    async def list_tasks(self, project_id: str, limit: int = 100) -> list[TaskRecord]:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            rows = await (
                await connection.execute(
                    "SELECT * FROM tasks WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (project_id, limit),
                )
            ).fetchall()
        return [TaskRecord.model_validate(dict(row)) for row in rows]

    async def update_task_status(self, task_id: str, status: str, updated_at: str) -> None:
        async with aiosqlite.connect(self.path) as connection:
            cursor = await connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, updated_at, task_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"task not found: {task_id}")
            await connection.commit()

    async def insert_git_operation(
        self,
        operation_id: str,
        session_id: str,
        operation: str,
        status: str,
        detail_json: str,
        created_at: str,
        confirmation_id: str | None = None,
        expected_base_commit: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute(
                """INSERT INTO git_operations
                (id, session_id, operation, confirmation_id, expected_base_commit, status, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (operation_id, session_id, operation, confirmation_id, expected_base_commit, status, detail_json, created_at),
            )
            await connection.commit()

    async def get_git_operation(self, operation_id: str) -> dict[str, object] | None:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            row = await (
                await connection.execute(
                    "SELECT * FROM git_operations WHERE id = ?", (operation_id,)
                )
            ).fetchone()
        return dict(row) if row else None

    async def update_session_status(
        self, session_id: str, status: str, updated_at: str
    ) -> None:
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, updated_at, session_id),
            )
            await connection.commit()

    async def set_session_v2_metadata(
        self, session_id: str, project_id: str, task_name: str, updated_at: str
    ) -> None:
        async with aiosqlite.connect(self.path) as connection:
            cursor = await connection.execute(
                """UPDATE sessions SET project_id = ?, task_name = ?, updated_at = ?
                WHERE id = ?""",
                (project_id, task_name, updated_at, session_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"session not found: {session_id}")
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
