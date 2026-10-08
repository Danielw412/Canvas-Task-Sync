"""Tools an agent turn may call, described once and adapted to each agent's SDK.

Claude receives them as an in-process MCP server and Codex as dynamic tools; both end in
``AgentToolset.call``. That one entry point owns what every tool shares: a call budget for
the turn, a bound on what a result may return, and errors that reach the agent as text
rather than as an exception that would end the turn. Standard library only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_TOOL_CALLS = 24
DEFAULT_MAX_RESULT_CHARS = 12_000


class ToolRefusal(Exception):
    """A tool declines a request; the message is shown to the agent as the tool's result."""


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    # A JSON Schema object for the tool's arguments.
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]


@dataclass
class AgentToolset:
    tools: list[AgentTool]
    max_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS
    # Checked before every call, so a cancelled run stops spending Canvas requests at once.
    cancelled: Callable[[], bool] = lambda: False
    calls: list[str] = field(default_factory=list)
    # Calls that failed unexpectedly (not a refusal). A verdict reached amid failures is not
    # trusted beyond its own run.
    failures: int = 0
    # Claude may issue tool calls in parallel, each on its own thread. They run one at a time:
    # the tools share a reader (its caches, link ids, and read budget), and Canvas is better
    # served by sequential reads anyway.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("Agent tool names must be unique.")

    @property
    def names(self) -> list[str]:
        return [tool.name for tool in self.tools]

    def reset(self) -> None:
        """Start a new attempt's budget."""
        with self._lock:
            self.calls.clear()
            self.failures = 0

    def call(self, name: str, arguments: Any) -> tuple[str, bool]:
        """Run one tool call and return ``(text, is_error)``. Never raises."""
        tool = next((item for item in self.tools if item.name == name), None)
        if tool is None:
            return f"Unknown tool {name!r}. Available tools: {', '.join(self.names)}.", True
        if self.cancelled():
            return "The run was cancelled. Stop and reply with your verdict.", True
        with self._lock:
            if len(self.calls) >= self.max_calls:
                return (
                    f"The tool budget of {self.max_calls} calls for this check is used up. "
                    "Reply now with your structured verdict from the evidence you have."
                ), True
            self.calls.append(name)
            if not isinstance(arguments, dict):
                return "Tool arguments must be a JSON object.", True
            try:
                text = tool.handler(arguments)
            except ToolRefusal as refusal:
                return str(refusal), True
            except Exception as error:  # A tool failure is the agent's information, not a crash.
                self.failures += 1
                return f"The tool failed ({type(error).__name__}).", True
        if len(text) > self.max_result_chars:
            text = (
                text[: self.max_result_chars]
                + "\n[Result truncated. Narrow the request or read a later part.]"
            )
        return text, False
