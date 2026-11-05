"""The `routine` tool and its approval gate.

Every save and every run of an unapproved routine goes through Hermes's own approval
prompt (the pre_tool_call directive). The handler only runs after the user said yes,
so it records that approval for the exact code the user saw.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from . import engine

PLUGIN = "pocket-autopilot"
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 900
DEFAULT_RUNS_LIMIT = 20
PREVIEW_LINES = 40

DESCRIPTION = (
    "Save, replay and repair phone routines: scripts that drive the user's phone with no model "
    "in the loop. Workflow: 1) explore the task with phone_exec first and keep the steps that "
    "worked; 2) write a clean script: call checkpoint(name, text=...) after every screen change, "
    "read per-run values from PARAMS (never hardcode them), and end with done(). Games and other "
    "canvas apps show no text: take a screenshot() with phone_exec, pass crops of stable UI as "
    "refs on save, and check them with checkpoint(name, image='<ref>'); snap(region) + "
    "changed(before, region) tells whether a move landed; 3) action=save "
    "(the user approves the code); 4) action=run to verify it end to end. A run that fails returns "
    "failed_checkpoint (expected text, what was seen, a screenshot path) and next_steps: look at "
    "the screenshot, fix the screen with phone_exec, then save a new version. Save only scripts "
    "that worked on the phone. Actions: save, run, list, show, runs, delete."
)

_ACTION_NAMES = ("save", "run", "list", "show", "runs", "delete")

SCHEMA = {
    "name": "routine",
    "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_ACTION_NAMES),
                       "description": "What to do with the routine."},
            "name": {"type": "string",
                     "description": "Routine name: lowercase letters, digits and hyphens, up to 48 characters."},
            "code": {"type": "string",
                     "description": "save: the routine.py body (phone-harness helpers pre-imported)."},
            "title": {"type": "string", "description": "save: human title, e.g. 'Claim daily reward'."},
            "app": {"type": "string", "description": "save: app id the routine drives, e.g. com.example.app."},
            "description": {"type": "string", "description": "save: what the routine does."},
            "refs": {"type": "object",
                     "description": "save: image references for canvas apps, "
                                    "{ref_name: {screenshot: <png path from phone_exec>, region: [x, y, w, h]}}. "
                                    "Crop stable UI (a title, a button), not content that changes."},
            "params": {"type": "object",
                       "description": "save: default values for PARAMS. run: values for this run only."},
            "timeout_s": {"type": "integer", "default": DEFAULT_TIMEOUT_S,
                          "description": f"run: max seconds (default {DEFAULT_TIMEOUT_S}, max {MAX_TIMEOUT_S})."},
            "limit": {"type": "integer", "default": DEFAULT_RUNS_LIMIT,
                      "description": "runs: how many run records to return."},
        },
        "required": ["action"],
    },
}

NEXT_STEPS_CHECKPOINT = (
    "Run failed at checkpoint '{name}'. Look at seen_text and the screenshot at {screenshot}. "
    "Use phone_exec to bring the phone to the screen this checkpoint expects, then save the fixed "
    "script as a new version with action=save (the user approves it again) and run it once more."
)
NEXT_STEPS_OTHER = (
    "Run failed without a checkpoint miss: read error and stderr_tail. Use phone_exec to see the "
    "phone's current state, fix the script, save a new version with action=save, and run it again."
)


def _error(message: str) -> dict:
    return {"success": False, "error": message}


def _next_version(name: str) -> int:
    try:
        return int(engine.get_routine(name)["version"]) + 1
    except engine.RoutineError:
        return 1


def _review_message(verb: str, name: str, title: str, version: int, code: str) -> str:
    preview = "\n".join(code.splitlines()[:PREVIEW_LINES])
    return f"{title} — {verb} routine '{name}' (v{version}):\n{preview}"


def _approve_directive(verb: str, name: str, sha: str, message: str) -> dict:
    return {"action": "approve", "message": message,
            "rule_key": f"{PLUGIN}:{verb}:{name}:{sha[:12]}"}


def _save_gate(args: dict) -> dict:
    # Saving always asks, even when this exact code was approved before: the write itself is the
    # thing the user needs to see. Invalid names still get a prompt; the handler rejects them.
    name = str(args.get("name") or "")
    code = str(args.get("code") or "")
    sha = engine.code_sha256(code)
    message = _review_message("save", name, str(args.get("title") or name), _next_version(name), code)
    return _approve_directive("save", name, sha, message)


def _run_gate(args: dict) -> dict | None:
    name = str(args.get("name") or "")
    try:
        meta = engine.get_routine(name)
    except engine.RoutineError:
        return None  # nothing to approve; the handler reports the missing routine
    if engine.is_approved(meta):
        return None
    message = _review_message("run", name, str(meta.get("title") or name), int(meta["version"]), meta["code"])
    return _approve_directive("run", name, meta["sha256"], message)


_GATES: dict[str, Callable[[dict], dict | None]] = {"save": _save_gate, "run": _run_gate}


def _guard_phone_exec(args: dict) -> dict | None:
    # Prepending only restricts the script, so phone-harness's own approval of the original still
    # describes everything the script may do.
    guard = engine.guard_source(engine.configured_allowed_apps())
    code = str(args.get("code") or "")
    if not guard or not code.strip():
        return None
    return {"action": "modify", "args": {"code": guard + code}}


def on_pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs: Any) -> dict | None:
    if tool_name == "phone_exec":
        return _guard_phone_exec(args or {})
    if tool_name != "routine":
        return None
    gate = _GATES.get(str((args or {}).get("action") or ""))
    return gate(args) if gate is not None else None


def _save(args: dict) -> dict:
    code = str(args.get("code") or "")
    if not code.strip():
        raise engine.RoutineError("code is required for save")
    meta = engine.save_routine(
        args.get("name", ""), code, title=str(args.get("title") or ""),
        app=str(args.get("app") or ""), description=str(args.get("description") or ""),
        params=args.get("params"), refs=args.get("refs"),
    )
    # The user approved this exact sha through the pre_tool_call prompt.
    approved = engine.approve(meta["name"], meta["sha256"])
    return {"success": True, "routine": approved,
            "next_steps": "Run it with action=run to verify it on the phone. Then print the "
                          "::routine card line."}


def _run(args: dict) -> dict:
    name = engine.validate_name(args.get("name", ""))
    meta = engine.get_routine(name)
    if not engine.is_approved(meta):
        # Reached only after the user answered the run prompt for this sha.
        engine.approve(name, meta["sha256"])
    timeout_s = max(1, min(int(args.get("timeout_s") or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    record = engine.run_routine(name, params=args.get("params"), timeout_s=timeout_s)
    result = {"success": bool(record.get("ok")), **record}
    if not record.get("ok"):
        result["next_steps"] = _failure_next_steps(record)
    return result


def _failure_next_steps(record: dict) -> str:
    failed = record.get("failed_checkpoint")
    if failed:
        return NEXT_STEPS_CHECKPOINT.format(name=failed.get("name", "?"),
                                            screenshot=failed.get("screenshot") or "(none saved)")
    return NEXT_STEPS_OTHER


def _list(_args: dict) -> dict:
    return {"success": True, "routines": engine.list_routines()}


def _show(args: dict) -> dict:
    name = engine.validate_name(args.get("name", ""))
    return {"success": True, "routine": engine.get_routine(name),
            "runs": engine.list_runs(name, limit=5)}


def _runs(args: dict) -> dict:
    name = engine.validate_name(args.get("name", ""))
    limit = int(args.get("limit") or DEFAULT_RUNS_LIMIT)
    return {"success": True, "routine": name, "runs": engine.list_runs(name, limit=limit)}


def _delete(args: dict) -> dict:
    name = engine.validate_name(args.get("name", ""))
    engine.delete_routine(name)
    return {"success": True, "deleted": name}


_ACTIONS: dict[str, Callable[[dict], dict]] = {
    "save": _save, "run": _run, "list": _list, "show": _show, "runs": _runs, "delete": _delete,
}


def routine(args: dict | None, **kwargs: Any) -> str:
    args = args or {}
    action = str(args.get("action") or "")
    handler = _ACTIONS.get(action)
    if handler is None:
        return json.dumps(_error(f"unknown action {action!r}; expected one of {', '.join(_ACTION_NAMES)}"))
    try:
        return json.dumps(handler(args), ensure_ascii=False)
    except engine.RoutineError as exc:
        return json.dumps(_error(str(exc)), ensure_ascii=False)
