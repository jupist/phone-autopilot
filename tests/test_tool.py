"""routine tool: approval directives and action dispatch, with the engine stubbed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
CODE = "ensure_device()\nopen_app('com.example.app')\ndone(ok=True)\n"
SHA = hashlib.sha256(CODE.encode()).hexdigest()


def _unimplemented(name):
    def stub(*args, **kwargs):
        raise NotImplementedError(f"engine.{name} is not stubbed in this test")
    return stub


def _load_plugin():
    """Load the plugin dir as package `phone_autopilot`, with a stub engine until engine.py exists."""
    if "phone_autopilot" in sys.modules:
        return sys.modules["phone_autopilot"]
    if not (PLUGIN_DIR / "engine.py").exists() and "phone_autopilot.engine" not in sys.modules:
        stub = types.ModuleType("phone_autopilot.engine")
        stub.PLUGIN = "phone-autopilot"
        stub.RoutineError = type("RoutineError", (ValueError,), {})
        for name in ("validate_name", "list_routines", "get_routine", "save_routine", "approve",
                     "is_approved", "code_sha256", "delete_routine", "list_runs", "get_run",
                     "run_routine", "phone_harness_available", "data_dir"):
            setattr(stub, name, _unimplemented(name))
        sys.modules["phone_autopilot.engine"] = stub
    spec = importlib.util.spec_from_file_location(
        "phone_autopilot", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["phone_autopilot"] = module
    spec.loader.exec_module(module)
    return module


_load_plugin()
tool = importlib.import_module("phone_autopilot.tool")
engine = importlib.import_module("phone_autopilot.engine")


def _meta(**overrides):
    meta = {"name": "daily-claim", "title": "Claim daily reward", "version": 2, "code": CODE,
            "sha256": SHA, "approved_sha256": None, "app": "com.example.app", "params": {}}
    meta.update(overrides)
    return meta


@pytest.fixture
def calls(monkeypatch):
    """Stub every engine function the tool uses; each call is recorded in order."""
    log = []

    def fake(name, result=None, exc=None):
        def stub(*args, **kwargs):
            log.append((name, args, kwargs))
            if exc is not None:
                raise exc
            return result(*args, **kwargs) if callable(result) else result
        return stub

    monkeypatch.setattr(engine, "code_sha256", lambda code: hashlib.sha256(code.encode()).hexdigest())
    monkeypatch.setattr(engine, "is_approved", lambda meta: bool(meta.get("approved_sha256"))
                        and meta["approved_sha256"] == meta["sha256"])
    monkeypatch.setattr(engine, "get_routine", fake("get_routine", _meta()))
    monkeypatch.setattr(engine, "validate_name", lambda name: name)
    monkeypatch.setattr(engine, "save_routine", fake(
        "save_routine", lambda name, code, **kw: _meta(name=name, code=code, sha256=SHA, version=3)))
    monkeypatch.setattr(engine, "approve", fake(
        "approve", lambda name, sha: _meta(name=name, approved_sha256=sha)))
    monkeypatch.setattr(engine, "run_routine", fake(
        "run_routine", {"routine": "daily-claim", "version": 2, "run_id": "r1", "ok": True,
                        "failed_checkpoint": None, "events": []}))
    monkeypatch.setattr(engine, "list_routines", fake("list_routines", [_meta()]))
    monkeypatch.setattr(engine, "list_runs", fake("list_runs", []))
    monkeypatch.setattr(engine, "delete_routine", fake("delete_routine", None))
    return log


def _names(log):
    return [entry[0] for entry in log]


def _approved_meta(**kw):
    return _meta(approved_sha256=SHA, **kw)


# ---- approval hook -------------------------------------------------------------------------

def test_save_always_asks_with_sha_prefix_rule_key(calls, monkeypatch):
    monkeypatch.setattr(engine, "get_routine", lambda name: _approved_meta())
    directive = tool.on_pre_tool_call(
        tool_name="routine", args={"action": "save", "name": "daily-claim", "code": CODE},
        task_id="t1", session_id="s1", tool_call_id="c1")
    assert directive["action"] == "approve"
    assert directive["rule_key"] == f"phone-autopilot:save:daily-claim:{SHA[:12]}"
    assert "save routine 'daily-claim' (v3, 3 lines)" in directive["message"]
    assert "ensure_device()" in directive["message"]


def test_save_message_numbers_a_brand_new_routine_v1(calls, monkeypatch):
    def missing(name):
        raise engine.RoutineError("no such routine")
    monkeypatch.setattr(engine, "get_routine", missing)
    directive = tool.on_pre_tool_call(tool_name="routine",
                                      args={"action": "save", "name": "fresh", "code": CODE})
    assert "(v1, 3 lines)" in directive["message"]


def test_save_message_previews_only_first_40_lines(calls):
    long_code = "\n".join(f"line{i}" for i in range(100))
    directive = tool.on_pre_tool_call(tool_name="routine",
                                      args={"action": "save", "name": "x", "code": long_code})
    assert "line39" in directive["message"]
    assert "line40" not in directive["message"]
    assert "60 more lines" in directive["message"]


def test_approval_names_everything_past_the_preview(calls):
    # The risky part sits below the preview: the summary must still surface it.
    long_code = "\n".join([f"step('s{i}')" for i in range(45)] + [
        "open_app('com.example.app')", "shell('pm clear com.example.app')",
        "type_text(PARAMS['query'])", "tap_image('play')", "tap(10, 20)", "import os"])
    message = tool.on_pre_tool_call(tool_name="routine",
                                    args={"action": "save", "name": "x", "code": long_code})["message"]
    assert "Opens apps: 'com.example.app'" in message
    assert "Runs adb shell: 'pm clear com.example.app'" in message
    assert "Types: PARAMS['query']" in message
    assert "Taps by image: 'play'" in message
    assert "1 taps at coordinates" in message
    assert "Imports (runs on this computer): os" in message
    assert "and 39 more" in message  # 45 steps, 6 shown


def test_run_unapproved_routine_asks(calls):
    directive = tool.on_pre_tool_call(tool_name="routine",
                                      args={"action": "run", "name": "daily-claim"})
    assert directive["action"] == "approve"
    assert directive["rule_key"] == f"phone-autopilot:run:daily-claim:{SHA[:12]}"
    assert "run routine 'daily-claim' (v2, 3 lines)" in directive["message"]


def test_run_approved_routine_is_silent(calls, monkeypatch):
    monkeypatch.setattr(engine, "get_routine", lambda name: _approved_meta())
    assert tool.on_pre_tool_call(tool_name="routine", args={"action": "run", "name": "daily-claim"}) is None


def test_run_of_missing_routine_is_silent(calls, monkeypatch):
    def missing(name):
        raise engine.RoutineError("no such routine")
    monkeypatch.setattr(engine, "get_routine", missing)
    assert tool.on_pre_tool_call(tool_name="routine", args={"action": "run", "name": "gone"}) is None


@pytest.mark.parametrize("args", [
    {"action": "list"}, {"action": "show", "name": "daily-claim"},
    {"action": "runs", "name": "daily-claim"}, {"action": "delete", "name": "daily-claim"},
    {"action": "bogus"}, {},
])
def test_other_routine_actions_pass_through(calls, args):
    assert tool.on_pre_tool_call(tool_name="routine", args=args) is None


def test_other_tools_pass_through(calls, monkeypatch):
    monkeypatch.setattr(engine, "configured_allowed_apps", lambda: [])
    assert tool.on_pre_tool_call(tool_name="phone_exec", args={"code": "print(1)"}) is None
    assert tool.on_pre_tool_call(tool_name="terminal", args={"command": "ls"}) is None


def test_hook_accepts_extra_kwargs_and_none_args(calls):
    assert tool.on_pre_tool_call("routine", None, task_id="x", api_request_id="y") is None


# ---- dispatch ------------------------------------------------------------------------------

def _call(args):
    return json.loads(tool.routine(args))


def test_save_stores_then_approves_the_saved_sha(calls):
    result = _call({"action": "save", "name": "daily-claim", "code": CODE, "title": "Claim",
                    "app": "com.example.app", "params": {"account": "a"}})
    assert result["success"] is True
    assert _names(calls) == ["save_routine", "approve"]
    save_args, save_kwargs = calls[0][1], calls[0][2]
    assert save_args == ("daily-claim", CODE)
    assert save_kwargs["title"] == "Claim" and save_kwargs["params"] == {"account": "a"}
    assert calls[1][1] == ("daily-claim", SHA)
    assert result["routine"]["approved_sha256"] == SHA


def test_save_rejects_empty_code_without_writing(calls):
    result = _call({"action": "save", "name": "daily-claim", "code": "   "})
    assert result["success"] is False
    assert "code is required" in result["error"]
    assert calls == []


def test_run_approved_routine_runs_without_reapproving(calls, monkeypatch):
    monkeypatch.setattr(engine, "get_routine", lambda name: _approved_meta())
    result = _call({"action": "run", "name": "daily-claim"})
    assert result["success"] is True
    assert "next_steps" not in result
    assert _names(calls) == ["run_routine"]


def test_run_unapproved_routine_records_approval_before_running(calls):
    result = _call({"action": "run", "name": "daily-claim", "params": {"account": "b"}})
    assert result["success"] is True
    assert _names(calls) == ["get_routine", "approve", "run_routine"]
    assert calls[1][1] == ("daily-claim", SHA)
    assert calls[2][2]["params"] == {"account": "b"}


def test_run_clamps_timeout(calls, monkeypatch):
    monkeypatch.setattr(engine, "get_routine", lambda name: _approved_meta())
    _call({"action": "run", "name": "daily-claim", "timeout_s": 5000})
    assert calls[-1][2]["timeout_s"] == tool.MAX_TIMEOUT_S


def test_failed_run_with_checkpoint_includes_next_steps(calls, monkeypatch):
    failed = {"routine": "daily-claim", "version": 2, "run_id": "r2", "ok": False,
              "failed_checkpoint": {"name": "reward", "expected": "Claim", "seen_text": ["Home"],
                                    "screenshot": "/data/runs/daily-claim/r2.png"},
              "error": None}
    monkeypatch.setattr(engine, "get_routine", lambda name: _approved_meta())
    monkeypatch.setattr(engine, "run_routine", lambda name, **kw: failed)
    result = _call({"action": "run", "name": "daily-claim"})
    assert result["success"] is False
    assert result["failed_checkpoint"]["name"] == "reward"
    assert "checkpoint 'reward'" in result["next_steps"]
    assert "/data/runs/daily-claim/r2.png" in result["next_steps"]
    assert "action=save" in result["next_steps"]


def test_failed_run_without_checkpoint_still_has_next_steps(calls, monkeypatch):
    failed = {"routine": "daily-claim", "version": 2, "run_id": "r3", "ok": False,
              "failed_checkpoint": None, "error": "timed out after 300s"}
    monkeypatch.setattr(engine, "get_routine", lambda name: _approved_meta())
    monkeypatch.setattr(engine, "run_routine", lambda name, **kw: failed)
    result = _call({"action": "run", "name": "daily-claim"})
    assert result["success"] is False
    assert result["next_steps"]


def test_list_show_runs_delete(calls, monkeypatch):
    listing = _call({"action": "list"})
    assert listing["routines"][0]["name"] == "daily-claim"

    monkeypatch.setattr(engine, "list_runs", lambda name, limit=20: [{"run_id": "r1", "limit": limit}])
    shown = _call({"action": "show", "name": "daily-claim"})
    assert shown["routine"]["code"] == CODE
    assert shown["runs"] == [{"run_id": "r1", "limit": 5}]

    runs = _call({"action": "runs", "name": "daily-claim", "limit": 7})
    assert runs["runs"] == [{"run_id": "r1", "limit": 7}]

    deleted = _call({"action": "delete", "name": "daily-claim"})
    assert deleted == {"success": True, "deleted": "daily-claim"}
    assert "delete_routine" in _names(calls)


def test_unknown_action_is_an_error_result(calls):
    result = _call({"action": "explode"})
    assert result["success"] is False
    assert "explode" in result["error"]


def test_engine_errors_come_back_as_error_results(calls, monkeypatch):
    def boom(name):
        raise engine.RoutineError("routine 'x' not found")
    monkeypatch.setattr(engine, "get_routine", boom)
    result = _call({"action": "show", "name": "x"})
    assert result == {"success": False, "error": "routine 'x' not found"}


def test_schema_exposes_one_routine_tool_with_action_enum():
    schema = tool.SCHEMA
    assert schema["name"] == "routine"
    assert schema["parameters"]["properties"]["action"]["enum"] == [
        "save", "run", "list", "show", "runs", "delete"]
    assert schema["parameters"]["required"] == ["action"]


def test_phone_exec_gets_the_guard_prepended_when_an_allowlist_is_set(monkeypatch):
    monkeypatch.setattr(engine, "configured_allowed_apps", lambda: ["com.example.app"], raising=False)
    monkeypatch.setattr(engine, "guard_source", lambda apps: f"GUARD{apps}\n", raising=False)
    directive = tool.on_pre_tool_call("phone_exec", {"code": "print(1)"})
    assert directive == {"action": "modify", "args": {"code": "GUARD['com.example.app']\nprint(1)"}}


def test_phone_exec_is_untouched_without_an_allowlist(monkeypatch):
    monkeypatch.setattr(engine, "configured_allowed_apps", lambda: [], raising=False)
    monkeypatch.setattr(engine, "guard_source", lambda apps: "", raising=False)
    assert tool.on_pre_tool_call("phone_exec", {"code": "print(1)"}) is None


def test_an_unexpected_error_comes_back_as_error_json(monkeypatch):
    def broken():
        raise OSError("disk full")
    monkeypatch.setattr(engine, "list_routines", broken)
    assert _call({"action": "list"}) == {"success": False, "error": "OSError: disk full"}
