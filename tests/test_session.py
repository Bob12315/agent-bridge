from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.bridge.session import AgentSession, SessionContext


def test_session_paths_are_cross_platform_path_objects(tmp_path: Path) -> None:
    context = SessionContext(
        id="ses_1",
        project_name="demo",
        workspace=tmp_path / "repo",
        base_branch="main",
        current_branch="bridge/ses_1",
    )
    assert isinstance(context.workspace, Path)
    assert context.status == "active"


def test_agent_session_uses_fixed_agent_names() -> None:
    session = AgentSession(id="ags_1", bridge_session_id="ses_1", agent="codex")
    assert session.status == "idle"
    with pytest.raises(ValidationError):
        AgentSession(id="ags_2", bridge_session_id="ses_1", agent="other")
