from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import load_config


def test_example_config_loads() -> None:
    config = load_config(Path("config/config.example.yaml"))
    assert config.runtime.database == Path("runtime/bridge.db")
    assert config.deepseek.transport == "mock"
    assert config.deepseek.executable == "deepseek"
    assert config.deepseek.timeout_seconds == 1800
    assert config.codex.executable == "codex"
    assert config.codex.timeout_seconds == 1800


def test_unknown_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("unknown: true\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)
