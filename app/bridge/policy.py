from __future__ import annotations

from app.bridge.protocol import MessageEnvelope
from app.bridge.session import SessionContext


class PolicyError(RuntimeError):
    """A bridge capability policy denied an operation."""


class ReadOnlyViolation(PolicyError):
    """A read-only agent changed its session worktree."""


def validate_agent_turn(message: MessageEnvelope, context: SessionContext) -> None:
    """Enforce role and session capabilities independently of task text."""
    if message.receiver == "deepseek":
        if message.type != "task":
            raise PolicyError("DeepSeek only accepts develop tasks")
        if context.access_mode != "develop":
            raise PolicyError(
                f"session access mode '{context.access_mode}' does not permit development"
            )
        return
    if message.receiver == "codex":
        if message.type != "review_request":
            raise PolicyError("Codex only accepts review requests")
        if context.access_mode not in {"develop", "review"}:
            raise PolicyError(
                f"session access mode '{context.access_mode}' does not permit review"
            )
        return
    raise PolicyError(f"receiver is not locally routable: {message.receiver}")
