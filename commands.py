"""The /routines slash command. Runs without the model, so it is the human's own shortcut."""
from __future__ import annotations

import time
from typing import Callable

from . import engine

USAGE = ("Usage: /routines [list | show <name> | run <name> | delete <name> | apps | allow <app> "
         "| revoke <app>]")
NOT_APPROVED = ("Routine '{name}' is not approved yet. Ask the agent to run it once; "
                "that run asks you to approve the code.")
NO_ROUTINES = ("No routines yet. Ask the agent to explore a task on the phone and save it as a routine.")


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def _mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def _list(_name: str) -> str:
    routines = engine.list_routines()
    if not routines:
        return NO_ROUTINES
    lines = ["| Routine | Version | Approved | Last run |", "|---|---|---|---|"]
    for item in routines:
        last = item.get("last_run")
        last_text = f"{_mark(bool(last.get('ok')))} {_when(last.get('finished_at'))}" if last else "never"
        lines.append(f"| {item['name']} | {item.get('version')} | {_mark(bool(item.get('approved')))} "
                     f"| {last_text} |")
    return "\n".join(lines)


def _show(name: str) -> str:
    meta = engine.get_routine(name)
    runs = engine.list_runs(name, limit=5)
    header = [f"{meta.get('title') or name} ({name}), version {meta.get('version')}, "
              f"{'approved' if meta.get('approved') else 'not approved'}"]
    if meta.get("app"):
        header.append(f"App: {meta['app']}")
    if meta.get("description"):
        header.append(meta["description"])
    run_lines = [f"- {_when(run.get('finished_at'))} {_mark(bool(run.get('ok')))} {run.get('run_id')}"
                 for run in runs] or ["- no runs yet"]
    return "\n".join(header + ["", "```python", meta.get("code", "").rstrip(), "```", "", "Recent runs:"]
                     + run_lines)


def _run_summary(record: dict) -> str:
    verdict = "passed" if record.get("ok") else "failed"
    lines = [f"{_mark(bool(record.get('ok')))} {record.get('routine')} v{record.get('version')} {verdict} "
             f"in {float(record.get('duration_s') or 0):.1f}s (run {record.get('run_id')})"]
    failed = record.get("failed_checkpoint")
    if failed:
        lines.append(f"Failed at checkpoint '{failed.get('name')}', expected: {failed.get('expected')}")
        if failed.get("screenshot"):
            lines.append(f"Screenshot: {failed['screenshot']}")
    if record.get("error"):
        lines.append(f"Error: {record['error']}")
    if not record.get("ok"):
        lines.append(f"To fix it, ask the agent: fix {record.get('routine')}")
    return "\n".join(lines)


def _run(name: str) -> str:
    meta = engine.get_routine(name)
    if not engine.is_approved(meta):
        return NOT_APPROVED.format(name=name)
    return _run_summary(engine.run_routine(name))


def _delete(name: str) -> str:
    engine.delete_routine(name)
    return f"Deleted routine '{name}'."


def _apps(_name: str) -> str:
    allowed = engine.configured_allowed_apps()
    lines = ["Allowed apps: " + (", ".join(allowed) if allowed else "none (the app guard is off)")]
    for request in engine.app_requests():
        lines.append(f"Waiting for you: {request['app']} ({engine.block_message(request['app'])})")
    return "\n".join(lines)


def _allow(app: str) -> str:
    # A slash command is typed by a person; the agent cannot run one, so this grant is the user's.
    engine.allow_app(app)
    return f"Allowed {app}. Routines and phone_exec scripts may now use it."


def _revoke(app: str) -> str:
    engine.revoke_app(app)
    return f"Removed {app} from the allowed apps."


_HANDLERS: dict[str, Callable[[str], str]] = {
    "": _list, "list": _list, "show": _show, "run": _run, "delete": _delete,
    "apps": _apps, "allow": _allow, "revoke": _revoke,
}
_NEEDS_NAME = frozenset({"show", "run", "delete", "allow", "revoke"})


def handle_routines(raw_args: str) -> str:
    verb, _, rest = (raw_args or "").strip().partition(" ")
    verb = verb.lower()
    handler = _HANDLERS.get(verb)
    name = rest.strip()
    if handler is None or (verb in _NEEDS_NAME and not name):
        return USAGE
    try:
        return handler(name)
    except engine.RoutineError as exc:
        return f"Error: {exc}"
