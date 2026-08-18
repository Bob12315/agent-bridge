"""Application metadata for the Foundation stage.

The HTTP and MCP entry points are intentionally introduced in later stages.
"""

from app import __version__


def version() -> str:
    return __version__
