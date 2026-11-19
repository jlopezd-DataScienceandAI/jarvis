"""Tool registry: pydantic models become API schemas and validate model inputs.

Confirmation for sensitive actions is enforced here, in code, so it does not
depend on the model choosing to ask.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from jarvis.config import Config

log = logging.getLogger(__name__)

# A tool returns plain text, or a list of content blocks (text and images).
ToolOutput = str | list[dict[str, Any]]


class ToolError(Exception):
    """An expected failure whose message is safe and useful to show the model."""


@dataclass
class ToolContext:
    """What tools may use from the running app."""

    config: Config
    # Ask the user a yes/no question; returns True only on a clear yes.
    confirm: Callable[[str], bool] = lambda question: False
    # Say something to the user right now (used by mode steps).
    say: Callable[[str], None] = lambda text: None


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    func: Callable[[Any, ToolContext], ToolOutput]
    # Returns the question to ask before running, or None if no confirmation is needed.
    confirm: Callable[[Any, ToolContext], str | None] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _inline_schema(self.args_model.model_json_schema()),
            # Inputs stream as generated; they are validated against args_model before running.
            "eager_input_streaming": True,
        }


def _inline_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve $ref/$defs and drop pydantic's titles so the schema is self-contained."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].rsplit("/", 1)[-1]]
                merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
                return walk(merged)
            return {
                key: walk(value)
                for key, value in node.items()
                if key != "$defs" and not (key == "title" and isinstance(value, str))
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


@dataclass
class ToolResult:
    content: ToolOutput
    is_error: bool = False


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)

    def tool(
        self,
        name: str,
        description: str,
        args: type[BaseModel],
        confirm: Callable[[Any, ToolContext], str | None] | None = None,
    ) -> Callable[[Callable[[Any, ToolContext], ToolOutput]], Callable[[Any, ToolContext], ToolOutput]]:
        """Decorator that registers ``func(args, ctx)`` as a tool."""

        def decorator(func: Callable[[Any, ToolContext], ToolOutput]):
            if name in self._tools:
                raise ValueError(f"Tool '{name}' is already registered")
            self._tools[name] = Tool(name, description, args, func, confirm)
            return func

        return decorator

    def schemas(self) -> list[dict[str, Any]]:
        # Sorted so the tool list is byte-stable between runs (prompt cache prefix).
        return [self._tools[name].schema() for name in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def call(self, name: str, raw_args: Any, ctx: ToolContext, *, skip_confirm: bool = False) -> ToolResult:
        """Validate, confirm if needed, and run a tool. Never raises."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Herramienta desconocida: {name}", is_error=True)
        try:
            args = tool.args_model.model_validate(raw_args)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}" for err in exc.errors()
            )
            return ToolResult(f"Argumentos no válidos para {name}: {problems}", is_error=True)
        try:
            if tool.confirm and not skip_confirm:
                question = tool.confirm(args, ctx)
                if question and not ctx.confirm(question):
                    return ToolResult("El usuario no ha confirmado la acción. No se ha hecho nada.")
            return ToolResult(tool.func(args, ctx))
        except ToolError as exc:
            return ToolResult(str(exc), is_error=True)
        except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the voice loop
            log.exception("Tool %s failed", name)
            return ToolResult(f"Error inesperado en {name}: {exc}", is_error=True)


registry = ToolRegistry()
