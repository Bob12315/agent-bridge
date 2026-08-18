from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.bridge.protocol import MessageContent, MessageEnvelope, utc_now
from app.bridge.session import SessionContext
from app.storage.database import Database
from app.storage.models import EventRecord, RequestRecord


async def test_session_round_trip(database: Database, session: SessionContext) -> None:
    restored = await database.get_session(session.id)
    assert restored == session


async def test_all_foundation_records_are_persisted(
    database: Database, session: SessionContext
) -> None:
    message = MessageEnvelope(
        id="msg_1",
        session_id=session.id,
        sender="chatgpt",
        receiver="deepseek",
        type="task",
        content=MessageContent(text="Do one thing"),
        created_at=utc_now(),
    )
    await database.insert_message(message)
    await database.insert_request(
        RequestRecord(
            id="req_1", message_id=message.id, session_id=session.id, agent="deepseek"
        )
    )
    await database.insert_event(
        EventRecord(
            id="evt_1",
            session_id=session.id,
            request_id="req_1",
            agent="deepseek",
            type="AGENT_STARTED",
            message="started",
        )
    )

    restored = await database.get_message(message.id)
    events = await database.list_events(session.id)
    assert restored == message
    assert [event.id for event in events] == ["evt_1"]


async def test_missing_records_return_none(database: Database) -> None:
    assert await database.get_session("missing") is None
    assert await database.get_message("missing") is None


async def test_initialize_migrates_foundation_database(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as connection:
        await connection.execute(
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                project_name TEXT NOT NULL,
                workspace TEXT NOT NULL,
                base_branch TEXT NOT NULL,
                current_branch TEXT NOT NULL,
                status TEXT NOT NULL,
                current_task_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await connection.execute(
            """CREATE TABLE requests (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                status TEXT NOT NULL,
                queued_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error TEXT
            )"""
        )
        await connection.commit()

    database = Database(path)
    await database.initialize()
    async with aiosqlite.connect(path) as connection:
        columns = {
            row[1]
            for row in await (await connection.execute("PRAGMA table_info(sessions)"))
            .fetchall()
        }
        request_columns = {
            row[1]
            for row in await (await connection.execute("PRAGMA table_info(requests)"))
            .fetchall()
        }
    assert "base_commit" in columns
    assert {"response_message_id", "error_code"} <= request_columns
