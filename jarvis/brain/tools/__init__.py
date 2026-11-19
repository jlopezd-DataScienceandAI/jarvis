"""Tools Jarvis can call. Importing this package registers all of them."""

from . import files, memory, modes, music, screen, system, web  # noqa: F401 - imported for registration
from .registry import ToolContext, ToolError, ToolRegistry, ToolResult, registry

__all__ = ["ToolContext", "ToolError", "ToolRegistry", "ToolResult", "registry"]
