"""Narrow tool-call trace for the synthetic e2e eval.

The trace wraps the tools inside the registry owned by one eval container. It
does not modify application code, the graph, or any shared process registry.
Each record keeps only the dispatched tool name, schema-validated and bounded
arguments, and a fixed status. Tool context, result payloads, exception text,
stack traces, environment values and compiler output never reach a record.

Business-connector invocations are outside this trace; they are not agent tool
calls and are reported separately as not measured.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.core.exceptions import (
    InvalidRequestError,
    ToolExecutionError,
    ToolPermissionError,
)
from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext
from app.tools.registry import ToolRegistry

_MAX_STRING = 200
_MAX_ITEMS = 20
_MAX_KEYS = 20
_MAX_DEPTH = 4
_DEPTH_MARKER = "<max-depth>"
_EXPECTED_FIELDS = frozenset({"name", "arguments", "count"})


class ToolTrace:
    """Per-container wrapper store and per-case record buffer."""

    def __init__(
        self,
        *,
        sanitize: Callable[[Any], str] | None = None,
        max_string: int = _MAX_STRING,
        max_items: int = _MAX_ITEMS,
        max_keys: int = _MAX_KEYS,
        max_depth: int = _MAX_DEPTH,
    ) -> None:
        self._sanitize = sanitize or str
        self._max_string = max_string
        self._max_items = max_items
        self._max_keys = max_keys
        self._max_depth = max_depth
        self._registry: ToolRegistry | None = None
        self._originals: dict[str, Tool] = {}
        self._active_case: str | None = None
        self._records: list[dict[str, Any]] = []
        self.installed = False

    def install(self, registry: ToolRegistry) -> None:
        """Replace every tool in a fresh eval registry with a tracing wrapper."""
        if self.installed:
            raise RuntimeError("tool trace is already installed")
        originals = {tool.name: tool for tool in registry.all()}
        if any(isinstance(tool, _TracedTool) for tool in originals.values()):
            raise RuntimeError("registry already contains a tracing wrapper")
        custom_run = sorted(
            name for name, tool in originals.items() if type(tool).run is not Tool.run
        )
        if custom_run:
            # A custom run may do more than parse+execute; refuse rather than
            # silently reimplement or bypass it.
            raise RuntimeError(
                "refusing instrumentation for tools with custom Tool.run: " + ", ".join(custom_run)
            )
        try:
            for _name, tool in originals.items():
                registry.register(_TracedTool(tool, self))
        except BaseException:
            for _name, tool in originals.items():
                registry.register(tool)
            raise
        self._registry = registry
        self._originals = originals
        self.installed = True

    def uninstall(self) -> None:
        """Restore the original tool objects and drop any active case buffer."""
        registry = self._registry
        if registry is not None:
            for name, original in self._originals.items():
                try:
                    current = registry.get(name)
                except InvalidRequestError:
                    continue
                if isinstance(current, _TracedTool) and current._trace is self:
                    registry.register(original)
        self._registry = None
        self._originals = {}
        self._active_case = None
        self._records = []
        self.installed = False

    def begin_case(self, case_id: str) -> None:
        """Start a fresh record buffer; earlier case records are discarded."""
        self._active_case = case_id
        self._records = []

    def end_case(self) -> list[dict[str, Any]]:
        """Return this case's records and deactivate recording."""
        records = self._records
        self._active_case = None
        self._records = []
        return records

    def bound_params(self, arguments: Any) -> tuple[Any, bool, bool]:
        """Return a redacted, size-bounded copy plus (truncated, sanitized) flags.

        ``sanitized`` means the sanitizer changed a value or key, so the bounded
        copy is not safe for exact comparison even when no truncation occurred.
        """
        return self._bound(arguments, 0)

    def record(
        self,
        name: str,
        *,
        arguments: Any | None,
        status: str,
        truncated: bool = False,
        sanitized: bool = False,
    ) -> None:
        """Record one dispatch only while an eval case is active."""
        if self._active_case is None:
            return
        record: dict[str, Any] = {
            "name": name,
            "arguments": arguments,
            "status": status,
        }
        if truncated:
            record["params_truncated"] = True
        if sanitized:
            record["params_sanitized"] = True
        self._records.append(record)

    def _bound(self, value: Any, depth: int) -> tuple[Any, bool, bool]:
        if depth > self._max_depth:
            return _DEPTH_MARKER, True, False
        if value is None or isinstance(value, bool | int | float):
            return value, False, False
        if isinstance(value, str):
            text = str(self._sanitize(value))
            sanitized = text != value
            if len(text) > self._max_string:
                return text[: self._max_string], True, sanitized
            return text, False, sanitized
        if isinstance(value, dict):
            items = list(value.items())
            truncated = len(items) > self._max_keys
            sanitized = False
            bounded: dict[str, Any] = {}
            for key, item in sorted(items[: self._max_keys], key=lambda pair: str(pair[0])):
                original_key = str(key)
                key_text = str(self._sanitize(original_key))
                sanitized = sanitized or key_text != original_key
                if len(key_text) > self._max_string:
                    key_text = key_text[: self._max_string]
                    truncated = True
                bounded[key_text], item_truncated, item_sanitized = self._bound(item, depth + 1)
                truncated = truncated or item_truncated
                sanitized = sanitized or item_sanitized
            return bounded, truncated, sanitized
        if isinstance(value, list | tuple):
            truncated = len(value) > self._max_items
            sanitized = False
            bounded_items = []
            for item in value[: self._max_items]:
                bounded_item, item_truncated, item_sanitized = self._bound(item, depth + 1)
                bounded_items.append(bounded_item)
                truncated = truncated or item_truncated
                sanitized = sanitized or item_sanitized
            return bounded_items, truncated, sanitized
        original_text = str(value)
        text = str(self._sanitize(original_text))
        sanitized = text != original_text
        if len(text) > self._max_string:
            return text[: self._max_string], True, sanitized
        return text, True, sanitized


class _TracedTool(Tool):
    """Transparent tool proxy preserving name, schema, risk, parse and errors."""

    def __init__(self, tool: Tool, trace: ToolTrace) -> None:
        self._tool = tool
        self._trace = trace
        self.name = tool.name
        self.description = tool.description
        self.risk = tool.risk
        self.input_model = tool.input_model

    def parse(self, arguments: dict[str, Any]) -> BaseModel:
        return self._tool.parse(arguments)

    def openai_schema(self) -> dict[str, Any]:
        return self._tool.openai_schema()

    async def execute(self, parsed: BaseModel, context: ToolContext) -> ToolResult:
        return await self._tool.execute(parsed, context)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            parsed = self._tool.parse(arguments)
        except InvalidRequestError:
            # Do not retain raw rejected arguments: they may contain secrets.
            self._trace.record(self.name, arguments=None, status="invalid_input")
            raise
        except BaseException:
            self._trace.record(self.name, arguments=None, status="exception")
            raise

        params, truncated, sanitized = self._trace.bound_params(parsed.model_dump(mode="json"))
        try:
            # Match base Tool.run exactly: one parse, then execute that parsed
            # object. Never call a second parse or the original run here.
            result = await self._tool.execute(parsed, context)
        except InvalidRequestError:
            self._trace.record(
                self.name,
                arguments=params,
                status="tool_error",
                truncated=truncated,
                sanitized=sanitized,
            )
            raise
        except Exception as exc:
            status = "permission_denied" if isinstance(exc, ToolPermissionError) else "tool_error"
            self._trace.record(
                self.name,
                arguments=params,
                status=status,
                truncated=truncated,
                sanitized=sanitized,
            )
            # Same mapping and message as Tool.run.
            raise ToolExecutionError(f"{self.name} failed") from exc
        except BaseException:
            self._trace.record(
                self.name,
                arguments=params,
                status="cancelled",
                truncated=truncated,
                sanitized=sanitized,
            )
            raise

        status = "ok" if result.success else "tool_failure"
        self._trace.record(
            self.name,
            arguments=params,
            status=status,
            truncated=truncated,
            sanitized=sanitized,
        )
        return result


def _canonical(arguments: Any) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_expected(value: Any, trace: ToolTrace) -> tuple[list[dict[str, Any]], bool, bool]:
    if not isinstance(value, list):
        raise ValueError("expect_tools must be a list")
    entries: list[dict[str, Any]] = []
    truncated_any = False
    sanitized_any = False
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each expect_tools entry must be an object")
        unknown = set(item) - _EXPECTED_FIELDS
        if unknown:
            raise ValueError("unsupported expect_tools field")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("expect_tools entry needs a non-empty name")
        arguments = item.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("expect_tools arguments must be an object")
        count = item.get("count", 1)
        if type(count) is not int or count < 1:
            raise ValueError("expect_tools count must be a positive integer")
        bounded, truncated, sanitized = trace.bound_params(arguments)
        entry: dict[str, Any] = {"name": name, "arguments": bounded, "count": count}
        if truncated:
            entry["params_truncated"] = True
            truncated_any = True
        if sanitized:
            entry["params_sanitized"] = True
            sanitized_any = True
        entries.append(entry)
    return entries, truncated_any, sanitized_any


def compare_tool_expectation(
    expected: Any,
    actual: list[dict[str, Any]],
    trace: ToolTrace,
) -> dict[str, Any]:
    """Compare exact tool name/argument/count multisets.

    Missing ``expect_tools`` means no assertion. An empty list asserts zero
    calls. Unknown fields are rejected rather than interpreted.

    Bounded arguments cannot be compared exactly. Any truncated or sanitized
    actual/expected value makes the case unscorable instead of matching a
    shared prefix or shared redaction marker.
    """
    entries, expected_truncated, expected_sanitized = _parse_expected(expected, trace)
    actual_truncated = any(record.get("params_truncated") is True for record in actual)
    actual_sanitized = any(record.get("params_sanitized") is True for record in actual)
    unscorable = []
    if actual_sanitized or expected_sanitized:
        unscorable.append("sanitized")
    if actual_truncated or expected_truncated:
        unscorable.append("truncated")
    if unscorable:
        return {
            "matched": False,
            "reason": "arguments_unscorable_" + "_and_".join(unscorable),
            "expected": entries,
        }
    expected_counts: Counter[tuple[str, str]] = Counter()
    for entry in entries:
        expected_counts[(entry["name"], _canonical(entry["arguments"]))] += int(entry["count"])
    actual_counts: Counter[tuple[str, str]] = Counter(
        (record["name"], _canonical(record["arguments"])) for record in actual
    )
    return {
        "matched": actual_counts == expected_counts,
        "expected": entries,
    }
