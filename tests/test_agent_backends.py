"""Claude and Codex as extraction backends, and the global agent setting that picks one.

No test here starts a real agent: turns are faked below ``AgentBackend._run_turn``, and the
SDK option builders are checked as data. A live turn would spend plan usage.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from canvas_task_sync.agent_backends import (
    AgentBackend,
    AgentExtractionError,
    ClaudeAgentBackend,
    _claude_result,
    agent_output_schema,
    codex_config_overrides,
    create_agent_backend,
    parse_agent_candidates,
)
from canvas_task_sync.agent_status import (
    agent_environment,
    agent_environment_overrides,
    quick_agent_status,
)
from canvas_task_sync.configuration import (
    ExtractionAgentSettings,
    ProjectSettings,
    ResolvedExtractionAgent,
    load_settings,
)
from canvas_task_sync.gemini import GeminiExtractor

CANDIDATE = {
    "source_anchor": "a1",
    "source_text": "Read chapter 3",
    "row_label": None,
    "classification": "homework",
    "task_type": "assignment",
    "action_kind": "complete",
    "title": "Read chapter 3",
    "details": "Read chapter 3.",
    "due_relation": "next_class",
    "explicit_due_date": None,
    "due_offset_days": None,
    "due_offset_unit": None,
    "confidence": "high",
    "warnings": [],
}


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_output_schema_is_strict_and_keeps_every_candidate_field():
    schema = agent_output_schema()
    assert schema["required"] == ["tasks"]
    candidate = schema["properties"]["tasks"]["items"]
    # A field may be named "title" even though the keyword is stripped.
    assert set(candidate["properties"]) == set(CANDIDATE)
    for node in _walk(schema):
        assert "$ref" not in node and "default" not in node
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])


def test_agent_replies_parse_from_objects_strings_and_bare_lists():
    assert parse_agent_candidates({"tasks": [CANDIDATE]})[0].title == "Read chapter 3"
    assert parse_agent_candidates('{"tasks": []}') == []
    assert len(parse_agent_candidates([CANDIDATE])) == 1
    for broken in ("not json", {"tasks": "nope"}, {"tasks": [{"title": "x"}]}):
        with pytest.raises(AgentExtractionError) as caught:
            parse_agent_candidates(broken)
        assert caught.value.retryable


def test_agents_never_see_secrets_or_pay_per_token_keys(monkeypatch):
    for key in (
        "CANVAS_TOKEN",
        "GEMINI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
    ):
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription")
    monkeypatch.setenv("PATH", "/usr/bin")
    overrides = agent_environment_overrides()
    assert overrides["CANVAS_TOKEN"] == overrides["ANTHROPIC_API_KEY"] == ""
    assert overrides["OPENAI_API_KEY"] == overrides["CODEX_API_KEY"] == ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in overrides
    assert "PATH" not in overrides
    environment = agent_environment()
    assert "GEMINI_API_KEY" not in environment and "ANTHROPIC_API_KEY" not in environment
    assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription"
    assert environment["PATH"] == "/usr/bin"


def test_claude_turns_get_no_tools_settings_sessions_or_secrets(monkeypatch):
    monkeypatch.setenv("CANVAS_TOKEN", "secret")
    options = ClaudeAgentBackend("claude-sonnet-5-5", "high").options("/tmp/run", print)
    assert options.model == "claude-sonnet-5-5" and options.effort == "high"
    assert options.tools == [] and options.allowed_tools == []
    assert options.mcp_servers == {} and options.strict_mcp_config
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == [] and options.skills == []
    assert options.verbatim_prompts
    assert "no-session-persistence" in options.extra_args
    assert options.env["CANVAS_TOKEN"] == ""
    assert options.output_format == {"type": "json_schema", "schema": agent_output_schema()}


def test_codex_turns_disable_tools_search_history_and_configured_mcp_servers(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[mcp_servers.docs]\nurl = "https://example.invalid"\n'
        '[mcp_servers."bad name"]\ncommand = "x"\n',
        encoding="utf-8",
    )
    overrides = codex_config_overrides(config)
    assert "features.shell_tool=false" in overrides
    assert "features.plugins=false" in overrides
    assert 'web_search="disabled"' in overrides
    assert 'history.persistence="none"' in overrides
    assert "mcp_servers.docs.enabled=false" in overrides
    assert not any("bad name" in item for item in overrides)


class ScriptedBackend(AgentBackend):
    provider = "claude"
    provider_label = "Claude"

    def __init__(self, replies, **kwargs) -> None:
        super().__init__("claude-sonnet-5-5", "medium", **kwargs)
        self.replies = list(replies)
        self.turns: list[tuple[str, list[tuple[bytes, str]]]] = []

    def _run_turn(self, prompt, images):
        self.turns.append((prompt, images))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _generate(backend: AgentBackend, **kwargs):
    return backend.generate(prompt="p", image_bytes=None, image_mime_type=None, **kwargs)


def test_a_transient_failure_is_retried_once_and_recorded():
    backend = ScriptedBackend(
        [AgentExtractionError("overloaded", retryable=True), {"tasks": [CANDIDATE]}]
    )
    assert len(_generate(backend)) == 1
    assert len(backend.turns) == 2
    assert backend.used_model == "claude-sonnet-5-5"
    assert "retried once" in backend.fallback_reasons[0]


def test_usage_limits_and_sign_in_failures_are_not_retried():
    backend = ScriptedBackend([AgentExtractionError("The Claude plan's usage limit was reached.")])
    with pytest.raises(AgentExtractionError, match="usage limit"):
        _generate(backend)
    assert len(backend.turns) == 1
    assert backend.used_model is None


def test_images_reach_the_agent_in_source_order():
    from canvas_task_sync.models import SourceImage

    backend = ScriptedBackend([{"tasks": []}])
    images = [
        SourceImage(id="b", order=2, data=b"second", mime_type="image/png", sha256="b"),
        SourceImage(id="a", order=1, data=b"first", mime_type="image/jpeg", sha256="a"),
    ]
    _generate(backend, images=images)
    assert backend.turns[0][1] == [(b"first", "image/jpeg"), (b"second", "image/png")]


def test_turns_beyond_the_slot_limit_wait_and_report_it():
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    waits: list[str] = []
    backend = ScriptedBackend([{"tasks": []}], slots=slots)
    backend.on_slot_wait = lambda: waits.append("waiting")
    threading.Timer(0.8, slots.release).start()
    assert _generate(backend) == []
    assert waits == ["waiting"]


def test_a_cancelled_run_stops_waiting_for_a_slot():
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    backend = ScriptedBackend([{"tasks": []}], slots=slots)
    started = time.monotonic()
    backend.cancelled = lambda: time.monotonic() - started > 0.2
    with pytest.raises(AgentExtractionError, match="cancelled"):
        _generate(backend)
    assert backend.turns == []
    slots.release()


def _result(**overrides):
    values = {
        "subtype": "success",
        "is_error": False,
        "structured_output": {"tasks": []},
        "result": "",
        "errors": None,
        "api_error_status": None,
    }
    return SimpleNamespace(**{**values, **overrides})


def test_claude_results_map_to_output_or_a_classified_error():
    assert _claude_result(_result(), None) == {"tasks": []}
    with pytest.raises(AgentExtractionError) as limited:
        _claude_result(_result(is_error=True, api_error_status=429), "rate_limit")
    assert "usage limit" in str(limited.value) and not limited.value.retryable
    with pytest.raises(AgentExtractionError) as overloaded:
        _claude_result(_result(is_error=True, api_error_status=529, result="Overloaded"), None)
    assert overloaded.value.retryable
    with pytest.raises(AgentExtractionError) as schema:
        _claude_result(_result(subtype="error_max_structured_output_retries"), None)
    assert schema.value.retryable


def test_the_extractor_names_the_agent_in_uncertain_items(spanish_capture, spanish_course):
    candidate = {**CANDIDATE, "source_anchor": "missing", "source_text": "Nothing like this"}
    backend = ScriptedBackend([{"tasks": [candidate]}])
    course = spanish_course.model_copy(deep=True)
    course.source.extraction.mode = "text"
    outcome = GeminiExtractor(backend).extract(spanish_capture, course)
    assert outcome.model_name == "claude-sonnet-5-5"
    assert outcome.uncertain[0].reason.startswith("Could not map Claude evidence")


def test_quick_status_reports_a_missing_sign_in(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert quick_agent_status("claude").ready is False
    assert "/login" in quick_agent_status("claude").detail
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "auth.json").write_text("{}", encoding="utf-8")
    assert quick_agent_status("codex").ready is True


# --- The global setting -----------------------------------------------------------------


def _settings(spanish_course, **agent) -> ProjectSettings:
    return ProjectSettings(
        gemini_model="gemini-3.7-flash",
        gemini_fallback_models=["gemini-3.6-flash", "gemini-3.5-flash"],
        extraction_agent=ExtractionAgentSettings(**agent),
        courses={"spanish": spanish_course.model_copy(deep=True)},
    )


def test_the_default_keeps_each_courses_gemini_chain_and_its_cache_key(spanish_course):
    settings = _settings(spanish_course)
    course = settings.courses["spanish"]
    course.gemini_reasoning = "high"
    resolved = settings.extraction_agent_for(course)
    assert resolved.provider == "gemini"
    assert resolved.models == ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
    # Byte-for-byte the key used before agents existed, so cached extractions still hit.
    assert resolved.cache_key == (
        "gemini-3.7-flash -> gemini-3.6-flash -> gemini-3.5-flash|reasoning:high"
    )


def test_a_global_gemini_model_replaces_every_courses_chain(spanish_course):
    settings = _settings(spanish_course, provider="gemini", model="gemini-3.5-flash", effort="low")
    resolved = settings.extraction_agent_for(settings.courses["spanish"])
    assert resolved.models == ["gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
    assert resolved.effort == "low"


def test_claude_and_codex_use_one_model_for_every_course(spanish_course):
    claude = _settings(spanish_course, provider="claude", model="claude-opus-5-5", effort="max")
    resolved = claude.extraction_agent_for(claude.courses["spanish"])
    assert resolved.models == ["claude-opus-5-5"]
    assert resolved.cache_key == "claude:claude-opus-5-5|effort:max"
    codex = _settings(spanish_course, provider="codex")
    assert codex.extraction_agent.model == "gpt-6-luna"
    assert codex.extraction_agent.describe() == "Codex · GPT-6 Luna · medium effort"


def test_haiku_takes_no_effort_setting(spanish_course):
    settings = _settings(
        spanish_course, provider="claude", model="claude-haiku-4-5-20251001", effort="max"
    )
    resolved = settings.extraction_agent_for(settings.courses["spanish"])
    assert resolved.effort is None
    assert resolved.cache_key == "claude:claude-haiku-4-5-20251001|effort:none"
    backend = create_agent_backend(resolved)
    assert isinstance(backend, ClaudeAgentBackend) and backend.effort is None


@pytest.mark.parametrize(
    "agent",
    [
        {"provider": "claude", "model": "gpt-6-luna"},
        {"provider": "codex", "model": "claude-opus-5-5"},
        {"provider": "gemini", "model": "gemini-3.7-flash", "effort": "max"},
        {"provider": "openai"},
    ],
)
def test_mismatched_models_and_efforts_are_rejected(agent):
    with pytest.raises(ValueError):
        ExtractionAgentSettings(**agent)


def test_the_agent_block_loads_from_yaml(tmp_path):
    config = tmp_path / "config" / "courses.yaml"
    config.parent.mkdir()
    config.write_text(
        "version: 1\nextraction_agent:\n  provider: codex\n  model: gpt-6.1-sol\n"
        "  effort: xhigh\ncourses: {}\n",
        encoding="utf-8",
    )
    agent = load_settings(config).extraction_agent
    assert (agent.provider, agent.model, agent.effort) == ("codex", "gpt-6.1-sol", "xhigh")


def test_only_sdk_agents_have_an_agent_backend():
    with pytest.raises(ValueError):
        create_agent_backend(
            ResolvedExtractionAgent(provider="gemini", models=["gemini-3.7-flash"], effort="low")
        )


def test_codex_overrides_survive_a_missing_or_broken_config(tmp_path):
    assert "features.apps=false" in codex_config_overrides(tmp_path / "missing.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("[mcp_servers", encoding="utf-8")
    assert not any("mcp_servers" in item for item in codex_config_overrides(Path(broken)))


# --- Tool turns (the agenda verifier) ---------------------------------------------------


def _toolset(calls=None, **kwargs):
    from canvas_task_sync.agent_tools import AgentTool, AgentToolset

    calls = [] if calls is None else calls

    def lookup(arguments):
        calls.append(arguments)
        return f"read {arguments.get('id')}"

    schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    }
    return AgentToolset(
        tools=[
            AgentTool("read_page", "Read a page.", schema, lookup),
            AgentTool("search", "Search.", schema, lookup),
        ],
        **kwargs,
    )


def _turn(toolset=None):
    from canvas_task_sync.agent_backends import AgentTurnSpec

    return AgentTurnSpec(
        system_prompt="Verify.",
        output_schema={"type": "object", "properties": {}, "additionalProperties": False},
        toolset=toolset,
        max_turns=12,
    )


def test_claude_tool_turns_get_only_their_own_in_process_tools(monkeypatch):
    monkeypatch.setenv("CANVAS_TOKEN", "secret")
    toolset = _toolset()
    options = ClaudeAgentBackend("claude-sonnet-5-5", "high").options(
        "/tmp/run", print, _turn(toolset)
    )
    assert options.tools == []
    assert options.allowed_tools == ["mcp__canvas__read_page", "mcp__canvas__search"]
    assert list(options.mcp_servers) == ["canvas"]
    assert options.mcp_servers["canvas"]["type"] == "sdk"
    assert options.strict_mcp_config and options.permission_mode == "dontAsk"
    assert options.setting_sources == [] and options.skills == []
    assert options.system_prompt == "Verify." and options.max_turns == 12
    assert options.output_format["schema"] == _turn().output_schema
    assert options.verbatim_prompts and "no-session-persistence" in options.extra_args
    assert options.env["CANVAS_TOKEN"] == ""


def test_claude_tool_calls_run_through_the_toolset(monkeypatch):
    import asyncio

    import claude_agent_sdk

    from canvas_task_sync.agent_backends import _claude_tool_server

    captured: dict[str, object] = {}

    def fake_tool(name, description, schema):
        def wrap(handler):
            captured[name] = (description, schema, handler)
            return name

        return wrap

    def fake_server(name, tools):
        return {"name": name, "tools": tools}

    monkeypatch.setattr(claude_agent_sdk, "tool", fake_tool)
    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server", fake_server)
    calls: list[dict] = []
    server = _claude_tool_server(_toolset(calls, max_calls=1))
    assert server == {"name": "canvas", "tools": ["read_page", "search"]}
    handler = captured["read_page"][2]
    assert asyncio.run(handler({"id": "home"})) == {
        "content": [{"type": "text", "text": "read home"}],
        "is_error": False,
    }
    exhausted = asyncio.run(captured["search"][2]({"id": "x"}))
    assert exhausted["is_error"] and "budget" in exhausted["content"][0]["text"]
    assert calls == [{"id": "home"}]


def test_codex_answers_tool_calls_and_refuses_everything_else():
    from canvas_task_sync.agent_backends import codex_request_handler

    calls: list[dict] = []
    handle = codex_request_handler(_toolset(calls))
    assert handle(
        "item/tool/call",
        {"threadId": "t", "turnId": "u", "callId": "c", "tool": "read_page",
         "arguments": {"id": "home"}},
    ) == {"contentItems": [{"type": "inputText", "text": "read home"}], "success": True}
    namespaced = handle("item/tool/call", {"tool": "read_page", "namespace": "x", "arguments": {}})
    assert namespaced["success"] is False
    assert handle("item/tool/call", {"tool": "shell", "arguments": {}})["success"] is False
    assert calls == [{"id": "home"}]
    assert handle("item/commandExecution/requestApproval", {}) == {"decision": "decline"}
    assert handle("item/fileChange/requestApproval", {}) == {"decision": "decline"}
    assert handle("execCommandApproval", {}) == {"decision": "denied"}
    assert handle("applyPatchApproval", {}) == {"decision": "denied"}
    assert handle("item/permissions/requestApproval", {}) == {"permissions": {}}
    assert handle("mcpServer/elicitation/request", {}) == {"action": "decline"}
    assert handle("item/tool/requestUserInput", {}) == {"answers": {}}
    assert handle("account/chatgptAuthTokens/refresh", {}) == {}
    # Extraction has no tools, and a tool call during it gets nothing.
    assert codex_request_handler(None)("item/tool/call", {"tool": "read_page"})["success"] is False


def test_codex_threads_are_read_only_ephemeral_and_carry_only_the_turns_tools():
    from canvas_task_sync.agent_backends import CodexAgentBackend, extraction_turn

    backend = CodexAgentBackend("gpt-6-luna", "low")
    extraction = backend._thread_params("/tmp/run", extraction_turn())
    assert extraction == {
        "approvalPolicy": "never",
        "baseInstructions": extraction_turn().system_prompt,
        "cwd": "/tmp/run",
        "ephemeral": True,
        "model": "gpt-6-luna",
        "sandbox": "read-only",
    }
    verification = backend._thread_params("/tmp/run", _turn(_toolset()))
    assert verification["baseInstructions"] == "Verify."
    assert [tool["name"] for tool in verification["dynamicTools"]] == ["read_page", "search"]
    assert all(tool["type"] == "function" for tool in verification["dynamicTools"])
    assert verification["sandbox"] == "read-only" and verification["approvalPolicy"] == "never"


class ToolTurnBackend(AgentBackend):
    provider = "codex"
    provider_label = "Codex"

    def __init__(self, replies, toolset) -> None:
        super().__init__("gpt-6-luna", "low")
        self.replies = list(replies)
        self.toolset = toolset
        self.turns: list[object] = []

    def _run_turn(self, prompt, images, turn=None):
        self.turns.append((prompt, images, turn))
        self.toolset.call("read_page", {"id": "home"})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_structured_turns_retry_once_with_a_fresh_tool_budget():
    toolset = _toolset(max_calls=1)
    turn = _turn(toolset)

    def parse(payload):
        if payload == "bad":
            raise AgentExtractionError("did not match", retryable=True)
        return payload

    backend = ToolTurnBackend(["bad", {"ok": True}], toolset)
    assert backend.run_structured("check", turn, parse) == {"ok": True}
    assert [entry[2] for entry in backend.turns] == [turn, turn]
    assert backend.turns[0][1] == []
    # Each attempt could spend the whole budget: the retry started from zero.
    assert toolset.calls == ["read_page"]
    assert backend.used_model == "gpt-6-luna" and "retried once" in backend.fallback_reasons[0]

    failing = ToolTurnBackend([AgentExtractionError("usage limit")], _toolset())
    with pytest.raises(AgentExtractionError, match="usage limit"):
        failing.run_structured("check", _turn(failing.toolset), parse)
    assert len(failing.turns) == 1
