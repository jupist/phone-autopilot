"""/routines slash command and plugin registration, with the engine stubbed."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
CODE = "ensure_device()\nopen_app('com.example.app')\ndone(ok=True)\n"
SHA = hashlib.sha256(CODE.encode()).hexdigest()


def _load_plugin():
    """Load the plugin dir as the package `phone_autopilot`, the way Hermes imports it."""
    if "phone_autopilot" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "phone_autopilot", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["phone_autopilot"] = module
    spec.loader.exec_module(module)


_load_plugin()
commands = importlib.import_module("phone_autopilot.commands")
engine = importlib.import_module("phone_autopilot.engine")
plugin = sys.modules["phone_autopilot"]


def _routines(raw_args):
    """/routines as a person types it; these tests never change the allowlist."""
    def no_settings(key, value):
        raise AssertionError(f"unexpected settings write {key}={value!r}")
    return commands.handle_routines(raw_args, set_config=no_settings)


def _meta(**overrides):
    meta = {"name": "daily-claim", "title": "Claim daily reward", "version": 2, "code": CODE,
            "sha256": SHA, "approved_sha256": SHA, "app": "com.example.app",
            "description": "Claims the reward.", "params": {}, "last_run": None, "approved": True}
    meta.update(overrides)
    return meta


def _record(ok=True, **overrides):
    record = {"routine": "daily-claim", "version": 2, "run_id": "20260101-090000-abcd", "ok": ok,
              "finished_at": 1767258000.0, "duration_s": 4.2, "failed_checkpoint": None, "error": None}
    record.update(overrides)
    return record


@pytest.fixture
def ran(monkeypatch):
    """Stub the engine for commands; run_routine calls are recorded."""
    calls = []
    monkeypatch.setattr(engine, "is_approved", lambda meta: bool(meta.get("approved_sha256"))
                        and meta["approved_sha256"] == meta["sha256"])
    monkeypatch.setattr(engine, "get_routine", lambda name: _meta(name=name))
    monkeypatch.setattr(engine, "list_routines", lambda: [_meta()])
    monkeypatch.setattr(engine, "list_runs", lambda name, limit=20: [_record()])
    monkeypatch.setattr(engine, "delete_routine", lambda name: calls.append(("delete", name)))

    def run_routine(name, **kwargs):
        calls.append(("run", name))
        return _record()

    monkeypatch.setattr(engine, "run_routine", run_routine)
    return calls


# ---- /routines list and show ----------------------------------------------------------------

def test_no_args_lists_routines_with_approval_and_last_run(ran, monkeypatch):
    monkeypatch.setattr(engine, "list_routines", lambda: [
        _meta(name="daily-claim", last_run={"ok": True, "finished_at": 1767258000.0}),
        _meta(name="inbox-zero", approved=False, approved_sha256=None, version=1, last_run=None),
    ])
    out = _routines("")
    assert "| daily-claim | 2 | ✓ |" in out
    assert "| inbox-zero | 1 | ✗ | never |" in out
    assert "✓ 20" in out  # last run ok, with its date
    assert out == _routines("list")


def test_empty_list_says_how_to_start(ran, monkeypatch):
    monkeypatch.setattr(engine, "list_routines", lambda: [])
    assert "No routines yet" in _routines("")


def test_show_prints_code_and_recent_runs(ran):
    out = _routines("show daily-claim")
    assert "Claim daily reward (daily-claim), version 2, approved" in out
    assert "```python" in out and "open_app('com.example.app')" in out
    assert "20260101-090000-abcd" in out


# ---- /routines run ---------------------------------------------------------------------------

def test_run_refuses_an_unapproved_routine(ran, monkeypatch):
    monkeypatch.setattr(engine, "get_routine", lambda name: _meta(approved_sha256=None))
    out = _routines("run daily-claim")
    assert "not approved yet" in out
    assert "Ask the agent to run it once" in out
    assert ran == []  # run_routine never called


def test_run_executes_an_approved_routine_and_summarises(ran):
    out = _routines("run daily-claim")
    assert ran == [("run", "daily-claim")]
    assert out.startswith("✓ daily-claim v2 passed in 4.2s (run 20260101-090000-abcd)")


def test_run_summary_names_the_failed_checkpoint_and_screenshot(ran, monkeypatch):
    failed = _record(ok=False, failed_checkpoint={"name": "reward", "expected": "Claim",
                                                  "screenshot": "/data/runs/daily-claim/r.png"})
    monkeypatch.setattr(engine, "run_routine", lambda name, **kw: failed)
    out = _routines("run daily-claim")
    assert out.startswith("✗ daily-claim v2 failed")
    assert "checkpoint 'reward'" in out and "expected: Claim" in out
    assert "/data/runs/daily-claim/r.png" in out
    assert "fix daily-claim" in out


def test_run_surfaces_engine_refusal_as_error_text(ran, monkeypatch):
    def refuse(name, **kwargs):
        raise engine.RoutineError("routine 'daily-claim' is not approved")
    monkeypatch.setattr(engine, "run_routine", refuse)
    assert _routines("run daily-claim") == "Error: routine 'daily-claim' is not approved"


# ---- /routines delete, usage and errors -------------------------------------------------------

def test_delete_removes_the_routine(ran):
    assert _routines("delete daily-claim") == "Deleted routine 'daily-claim'."
    assert ran == [("delete", "daily-claim")]


@pytest.mark.parametrize("raw", ["run", "show", "delete", "run   ", "frobnicate", "frobnicate x"])
def test_usage_for_missing_name_or_unknown_verb(ran, raw):
    assert _routines(raw) == commands.USAGE
    assert ran == []


def test_engine_error_on_show_is_reported(ran, monkeypatch):
    def missing(name):
        raise engine.RoutineError(f"routine '{name}' not found")
    monkeypatch.setattr(engine, "get_routine", missing)
    assert _routines("show nope") == "Error: routine 'nope' not found"


# ---- registration ----------------------------------------------------------------------------

class _FakeCtx:
    def __init__(self):
        self.tools, self.hooks, self.commands, self.blueprints, self.skills = [], [], [], [], []
        self.settings = {}

    def set_config(self, key, value):
        self.settings[key] = value

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_command(self, name, **kwargs):
        self.commands.append((name, kwargs))

    def register_automation_blueprint(self, key, **kwargs):
        self.blueprints.append((key, kwargs))

    def register_skill(self, name, path, **kwargs):
        self.skills.append((name, Path(path), kwargs))


def test_register_wires_tool_hook_command_blueprint_and_skill():
    ctx = _FakeCtx()
    plugin.register(ctx)

    [tool_kwargs] = ctx.tools
    assert tool_kwargs["name"] == "routine" and tool_kwargs["toolset"] == "phone-autopilot"
    assert tool_kwargs["schema"]["name"] == "routine"
    assert callable(tool_kwargs["check_fn"]) and callable(tool_kwargs["handler"])

    tool_mod = importlib.import_module("phone_autopilot.tool")
    assert ctx.hooks == [("pre_tool_call", tool_mod.on_pre_tool_call), ("post_tool_call", tool_mod.on_post_tool_call)]

    [(command_name, command_kwargs)] = ctx.commands
    assert command_name == "routines"

    [(key, blueprint)] = ctx.blueprints
    assert key == "daily-routine"
    assert blueprint["schedule_template"] == "{minute} {hour} * * {dow}"
    slot_names = [slot["name"] for slot in blueprint["slots"]]
    assert slot_names == ["routine", "time", "recurrence", "deliver"]
    assert "{routine}" in blueprint["prompt_template"]

    [(skill_name, skill_path, skill_kwargs)] = ctx.skills
    assert skill_name == "phone-autopilot"
    assert skill_path.name == "SKILL.md" and skill_path.is_file()
    # Hermes lists the skill by what it is given; the skill index shows 60 characters.
    description = skill_kwargs["description"]
    assert f'description: "{description}"' in skill_path.read_text()
    assert len(description) <= 60 and description.endswith(".")


def test_the_registered_command_saves_grants_through_the_plugin_settings_writer(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "_settings", lambda: {"allowed_apps": ["com.example.app"]})
    monkeypatch.setattr(engine, "data_dir", lambda: tmp_path)
    ctx = _FakeCtx()
    plugin.register(ctx)
    [(_, command_kwargs)] = ctx.commands
    assert command_kwargs["handler"]("allow com.spotify.music").startswith("Allowed com.spotify.music")
    assert ctx.settings == {"allowed_apps": ["com.example.app", "com.spotify.music"]}
