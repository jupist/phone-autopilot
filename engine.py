"""Storage and runner for Phone Autopilot routines.

Loaded standalone by path (the dashboard does this), so it imports nothing from the
plugin. A run pipes the routine to `python -m phone_harness.run` the same way
phone-harness's own Hermes plugin does, and streams the `@@AUTOPILOT` events it prints.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

PLUGIN = "phone-autopilot"
MAX_RUNS = 50
LOCK_WAIT_S = 120
JOIN_S = 10
EVENT_PREFIX = "@@AUTOPILOT "
TAIL_LINES = 200
TAIL_CHARS = 4000
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_RUN_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
_PRELUDE = Path(__file__).with_name("prelude.py")
_GUARD = Path(__file__).with_name("guard.py")
log = logging.getLogger("phone_autopilot")


class RoutineError(ValueError):
    """A routine is missing, invalid, unapproved, edited behind our back, or the phone is busy."""


# ---------------------------------------------------------------------------
# Paths and small file helpers.


def data_dir() -> Path:
    """<HERMES_HOME>/plugin-data/phone-autopilot/, resolved per call so it follows the active profile."""
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN)


def validate_name(name: str) -> str:
    """Names become folder names, so only the slug alphabet is allowed (no path traversal)."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise RoutineError(f"invalid routine name {name!r}: use 1-48 lowercase letters, digits or hyphens")
    return name


def _routine_dir(name: str) -> Path:
    return data_dir() / "routines" / validate_name(name)


def _meta_path(name: str) -> Path:
    return _routine_dir(name) / "routine.json"


def _runs_dir(name: str) -> Path:
    return data_dir() / "runs" / validate_name(name)


def _write_atomic(path: Path, text: str) -> None:
    """Write to a temp file then rename, so a crash never leaves a half-written JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _read_meta(name: str) -> dict:
    path = _meta_path(name)
    if not path.is_file():
        raise RoutineError(f"no routine named {name!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_meta(name: str, meta: dict) -> None:
    _write_atomic(_meta_path(name), json.dumps(meta, indent=2, ensure_ascii=False))


def code_sha256(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def is_approved(meta: dict) -> bool:
    """Approval is bound to the exact bytes: any edit to the routine revokes it."""
    return meta.get("sha256") is not None and meta.get("approved_sha256") == meta["sha256"]


def _public(meta: dict) -> dict:
    return {**meta, "approved": is_approved(meta)}


# ---------------------------------------------------------------------------
# Routines.


def _new_meta(name: str) -> dict:
    return {"name": name, "version": 0, "sha256": None, "approved_sha256": None,
            "created_at": time.time(), "last_run": None}


_REF_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _region(value) -> tuple[int, int, int, int]:
    if not (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value)):
        raise RoutineError("a ref region is [x, y, w, h] in screenshot pixels")
    x, y, w, h = value
    if w < 8 or h < 8:
        raise RoutineError("a ref region must be at least 8x8 pixels")
    return x, y, w, h


_BLANK_STDDEV = 6.0


@functools.lru_cache(maxsize=1)
def _image_search():
    """prelude.py's image search, so a ref is vetted by the same code that will look for it."""
    spec = importlib.util.spec_from_file_location("phone_autopilot_prelude", _PRELUDE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ref_problem(img, crop, x: int, y: int) -> str | None:
    """Why a crop makes a poor ref: blank, or found in more than one place on its own screen."""
    from PIL import ImageOps, ImageStat

    if max(ImageStat.Stat(crop.convert("L")).stddev) < _BLANK_STDDEV:
        return "is nearly blank, so it would match any plain area"
    masked = img.copy()
    masked.paste(ImageOps.invert(crop), (x, y))  # hide the original so only a second copy can match
    search = _image_search()
    hit = search._locate(masked, crop)
    if hit is not None and hit[0] >= search.MATCH:
        _, ox, oy = hit
        return f"also appears at [{ox}, {oy}] on the same screen, so a search could tap the wrong one"
    return None


def _save_refs(name: str, refs: dict) -> dict:
    """Crop each reference from the screenshot the agent took while exploring.

    Canvas apps (most games) expose no text, so a routine verifies them by comparing a region
    of the live screen with these crops instead.
    """
    from PIL import Image  # Hermes ships Pillow; only routines with image refs need it

    if not isinstance(refs, dict):
        raise RoutineError("refs must be an object of {name: {screenshot, region}}")
    saved = {}
    for ref, spec in refs.items():
        if not _REF_NAME.fullmatch(str(ref)) or not isinstance(spec, dict):
            raise RoutineError(f"bad ref {ref!r}: use a short lowercase name and {{screenshot, region}}")
        x, y, w, h = _region(spec.get("region"))
        shot = Path(str(spec.get("screenshot") or ""))
        if not shot.is_file():
            raise RoutineError(f"ref {ref!r}: screenshot {str(shot)!r} not found")
        with Image.open(shot) as img:
            if x + w > img.width or y + h > img.height:
                raise RoutineError(f"ref {ref!r}: region falls outside the {img.width}x{img.height} screenshot")
            screen = img.convert("RGB")
            crop = screen.crop((x, y, x + w, y + h))
        problem = _ref_problem(screen, crop, x, y)
        if problem:
            raise RoutineError(f"ref {ref!r} {problem}. Pick a region with distinctive content, such as a "
                               "label, logo or icon together with what surrounds it")
        out = _routine_dir(name) / "refs" / f"{ref}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        crop.save(out)
        saved[ref] = {"region": [x, y, w, h]}
    return saved


def save_routine(name, code, *, title="", app="", description="", params=None, refs=None) -> dict:
    """Write a new version of a routine. Saving never approves; the user does that."""
    validate_name(name)
    if not code.strip():
        raise RoutineError("routine code is empty")
    if params is not None and not isinstance(params, dict):
        raise RoutineError("params must be an object")
    meta = _read_meta(name) if _meta_path(name).exists() else _new_meta(name)
    if refs:
        meta["refs"] = {**meta.get("refs", {}), **_save_refs(name, refs)}
    meta.update(
        title=title or meta.get("title") or name,
        app=app or meta.get("app", ""),
        description=description or meta.get("description", ""),
        params=dict(params) if params is not None else meta.get("params", {}),
        version=meta["version"] + 1,
        sha256=code_sha256(code),
        updated_at=time.time(),
    )
    folder = _routine_dir(name)
    _write_atomic(folder / "versions" / f"{meta['version']}.py", code)
    _write_atomic(folder / "routine.py", code)
    _write_meta(name, meta)
    _broadcast("routines.changed", {"routine": name})
    return _public(meta)


def approve(name, sha256: str) -> dict:
    """Approve exactly the version the user was shown. A stale hash is refused."""
    meta = _read_meta(name)
    if sha256 != meta.get("sha256"):
        raise RoutineError(f"routine {name!r} changed since it was shown; current sha256 is {meta.get('sha256')}")
    meta["approved_sha256"] = sha256
    _write_meta(name, meta)
    _broadcast("routines.changed", {"routine": name})
    return _public(meta)


def get_routine(name) -> dict:
    meta = _read_meta(name)
    code = (_routine_dir(name) / "routine.py").read_text(encoding="utf-8")
    return {**_public(meta), "code": code}


def list_routines() -> list[dict]:
    root = data_dir() / "routines"
    if not root.is_dir():
        return []
    return [_public(_read_meta(folder.name)) for folder in sorted(root.iterdir())
            if _NAME_RE.fullmatch(folder.name) and _meta_path(folder.name).is_file()]


def delete_routine(name) -> None:
    _read_meta(name)  # raises when there is nothing to delete
    shutil.rmtree(_routine_dir(name))
    shutil.rmtree(_runs_dir(name), ignore_errors=True)
    _broadcast("routines.changed", {"routine": name})


def phone_harness_available() -> bool:
    return importlib.util.find_spec("phone_harness") is not None


# ---------------------------------------------------------------------------
# Run records.


def list_runs(name, limit=20) -> list[dict]:
    folder = _runs_dir(name)
    if not folder.is_dir():
        return []
    paths = sorted(folder.glob("*.json"), reverse=True)[:limit]  # ids sort by time
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def get_run(name, run_id: str) -> dict:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise RoutineError(f"invalid run id {run_id!r}")
    path = _runs_dir(name) / f"{run_id}.json"
    if not path.is_file():
        raise RoutineError(f"no run {run_id!r} for routine {name!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def _new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _prune_runs(name: str) -> None:
    """Keep the newest MAX_RUNS records and their screenshots."""
    for old in sorted(_runs_dir(name).glob("*.json"))[:-MAX_RUNS]:
        old.unlink(missing_ok=True)
        old.with_suffix(".png").unlink(missing_ok=True)


def _update_last_run(name: str, record: dict) -> None:
    meta = _read_meta(name)
    meta["last_run"] = {k: record[k] for k in ("run_id", "ok", "finished_at", "duration_s")}
    _write_meta(name, meta)


def _keep_failure_screenshot(record: dict) -> None:
    """Copy the checkpoint screenshot into the runs folder, which the engine owns."""
    failed = record["failed_checkpoint"]
    if not failed or not failed.get("screenshot"):
        return
    src = Path(failed["screenshot"])
    if not src.is_file():
        return
    dst = _runs_dir(record["routine"]) / f"{record['run_id']}.png"
    shutil.copyfile(src, dst)
    failed["screenshot"] = str(dst)


def _summary(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "events"}


def _broadcast(event: str, payload: dict) -> None:
    """Push to Hermes's plugin event bus; a no-op outside Hermes."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN, event, payload)
    except Exception:  # not inside Hermes, or the bus is down: the run must not care
        log.debug("plugin event %s not broadcast", event, exc_info=True)


# ---------------------------------------------------------------------------
# Running.


def _read_code(name: str, meta: dict) -> str:
    """The approved bytes are what runs: a routine.py edited on disk is refused."""
    code = (_routine_dir(name) / "routine.py").read_text(encoding="utf-8")
    if code_sha256(code) != meta.get("sha256"):
        raise RoutineError(f"routine {name!r} was edited on disk after it was saved; save it again")
    return code


def _settings() -> dict:
    """plugins.entries.phone-autopilot.settings from the active profile's config."""
    from hermes_cli.config import load_config_readonly
    entry = ((load_config_readonly() or {}).get("plugins", {}).get("entries", {}) or {}).get(PLUGIN) or {}
    settings = entry.get("settings") or {}
    return settings if isinstance(settings, dict) else {}


def configured_allowed_apps() -> list[str]:
    apps = _settings().get("allowed_apps") or []
    return [str(a) for a in apps] if isinstance(apps, list) else []


def configured_allow_all() -> bool:
    """The user turned the app guard off: scripts may open any app without asking."""
    return _settings().get("allow_all_apps") is True


# ---------------------------------------------------------------------------
# App permissions. A script that reaches an app the user has not allowed pauses and asks (see
# guard.py): it writes asking.json, a watcher here turns that into a card, and the script reads the
# user's answer from answers.json. Only a person grants an app: the phone pane's Allow button (saved through the
# Desktop's plugin settings call, like the Settings form) and the /routines allow slash command
# (saved through ctx.set_config). Never a tool, since a tool call is approved automatically under
# /yolo and the agent would grant itself apps.

# Android's "Allow <app> to ...?" dialog. Granting a permission is the user's call, so the
# guard stops on it without pressing Home and asks the user to answer it on the phone.
PERMISSION_DIALOGS = frozenset({"com.google.android.permissioncontroller",
                                "com.android.permissioncontroller"})
_PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")


def validate_package(app: str) -> str:
    app = str(app or "").strip()
    if not _PACKAGE_RE.fullmatch(app) or len(app) > 200:
        raise RoutineError(f"{app!r} is not an Android package name (like com.spotify.music)")
    return app


# How long a paused script waits for the user before it stops. A phone_exec call or a run
# also never waits past its own timeout.
ASK_WAIT_S = 120
_WATCH_POLL_S = 0.3


def _requests_path() -> Path:
    return data_dir() / "app_requests.json"


def _asking_path() -> Path:
    return data_dir() / "asking.json"


def _answers_path() -> Path:
    return data_dir() / "answers.json"


def _record_answer(app: str, answer: str) -> None:
    """Tell a paused script what the user decided (it ignores answers older than its question)."""
    try:
        answers = json.loads(_answers_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        answers = {}
    answers = answers if isinstance(answers, dict) else {}
    answers[app] = {"answer": answer, "at": time.time()}
    _write_atomic(_answers_path(), json.dumps(answers, indent=2))


def app_requests() -> list[dict]:
    """Apps the guard stopped on that the user has not answered yet, newest first.

    An app allowed since (from any surface, the Settings form included) is answered.
    """
    try:
        items = json.loads(_requests_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    allowed = set(configured_allowed_apps())
    return sorted((i for i in items if isinstance(i, dict) and i.get("app") and i["app"] not in allowed),
                  key=lambda i: i.get("at", 0), reverse=True)


def _write_requests(items: list[dict]) -> None:
    _write_atomic(_requests_path(), json.dumps(items, indent=2))


def block_message(app: str, reason: str = "") -> str:
    """What the agent and the user read when the guard stops a script."""
    if app in PERMISSION_DIALOGS:
        return ("stopped: Android is asking for a permission. The user must answer that dialog "
                "on the phone; never tap it from a script")
    if reason == "denied":
        return f"stopped: the user chose Not now for {app}. Do not use that app unless they ask"
    return (f"stopped: nobody answered whether {app} may be used. Ask the user to allow it with "
            f"Allow in the phone pane, or by typing /routines allow {app}")


def note_blocked_app(app: str, *, source: str, routine: str = "", waiting: bool = False) -> dict | None:
    """Queue a request for the user to answer; None when there is nothing to ask.

    `waiting` marks a script that is paused on the app right now, rather than one that stopped.
    """
    try:
        app = validate_package(app)
    except RoutineError:
        return None
    if app in configured_allowed_apps():
        return None
    request = {"app": app, "kind": "permission" if app in PERMISSION_DIALOGS else "app",
               "source": source, "routine": routine, "at": time.time(), "waiting": waiting}
    _write_requests([request] + [i for i in app_requests() if i["app"] != app])
    _broadcast("apps.requested", request)
    return request


def dismiss_app_request(app: str) -> None:
    """Not now: a script paused on the app stops, and the card goes away."""
    _record_answer(app, "denied")
    _write_requests([i for i in app_requests() if i["app"] != app])
    _broadcast("apps.changed", {"app": app})


def allow_app(app: str, save: Callable[[list[str]], None]) -> list[str]:
    """Add an app to the allowlist through `save` (the plugin's settings writer).

    Callers must be acting on a person's own click or command.
    """
    app = validate_package(app)
    if app in PERMISSION_DIALOGS:
        raise RoutineError("Android's permission dialog is answered on the phone, not allowlisted")
    apps = configured_allowed_apps()
    if app not in apps:
        apps = apps + [app]
        save(apps)
    _record_answer(app, "allowed")
    _write_requests([i for i in app_requests() if i["app"] != app])
    _broadcast("apps.changed", {"app": app})
    return apps


def _watch_asking(stop: threading.Event, until: float) -> None:
    """While a script runs: show its question as a card, and pass on an Allow from any surface.

    The pane's Allow button and the Settings form save the allowlist through Hermes's settings
    writer, which runs none of this plugin's code, so the watcher reads the saved list.
    """
    seen = None  # (app, asked_at) of the question on screen
    answered = False
    while not stop.is_set() and time.time() < until:
        try:
            asking = json.loads(_asking_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            asking = None
        key = (str(asking.get("app")), asking.get("at")) if isinstance(asking, dict) else None
        if key != seen:
            if key:
                note_blocked_app(key[0], source=str(asking.get("source") or ""),
                                 routine=str(asking.get("routine") or ""), waiting=True)
            else:  # the script resumed or stopped: an unanswered card stays, no longer waiting
                _write_requests([{**i, "waiting": False} for i in app_requests()])
                _broadcast("apps.changed", {"app": seen[0]})
            seen, answered = key, False
        elif key and not answered and key[0] in configured_allowed_apps():
            _record_answer(key[0], "allowed")
            _broadcast("apps.changed", {"app": key[0]})
            answered = True
        stop.wait(_WATCH_POLL_S)


def start_asking_watch(timeout_s: float) -> threading.Event:
    """Run the watcher for a script that may run up to `timeout_s`; set the event to end it.

    The thread runs in the caller's context, so it reads the caller's profile.
    """
    stop = threading.Event()
    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(_watch_asking, stop, time.time() + timeout_s + JOIN_S),
                     daemon=True, name="phone-autopilot-asking").start()
    return stop


def revoke_app(app: str, save: Callable[[list[str]], None]) -> list[str]:
    app = validate_package(app)
    apps = [a for a in configured_allowed_apps() if a != app]
    save(apps)
    _broadcast("apps.changed", {"app": app})
    return apps


def guard_events(text: str) -> list[dict]:
    """Guard events in a script's output (phone_exec prints them like routine runs do)."""
    found = []
    for line in str(text or "").splitlines():
        if line.startswith(EVENT_PREFIX):
            event = _parse_event(line[len(EVENT_PREFIX):])
            if event and event.get("event") == "guard" and event.get("app"):
                found.append(event)
    return found


def guard_source(allowed_apps, *, script_timeout_s: float, source: str, routine: str = "") -> str:
    """The app guard for a script, or "" when the user lets scripts open any app."""
    if configured_allow_all():
        return ""
    apps = sorted({str(a).strip() for a in (allowed_apps or ()) if str(a).strip()})
    data_dir().mkdir(parents=True, exist_ok=True)  # the paused script writes its question here
    ask = {"asking": str(_asking_path()), "answers": str(_answers_path()), "source": source,
           "routine": routine, "wait_s": ASK_WAIT_S, "script_timeout_s": float(script_timeout_s)}
    return (f"ALLOWED_APPS = {apps!r}\n_GUARD_ASK = {ask!r}\n"
            + _GUARD.read_text(encoding="utf-8") + "\n")


def _build_script(code: str, params: dict, workdir: Path, guard: str = "") -> str:
    """Guard, prelude, then params and folder, then the routine body.

    The marker line tells the prelude where the body starts, so an error that ends the run names
    its routine.py line and leaves a screenshot like a missed checkpoint.

    Params travel as a JSON string literal so any JSON value survives (true/null included).
    """
    prelude = _PRELUDE.read_text(encoding="utf-8")
    params_literal = repr(json.dumps(params))
    head = (f"{guard}{prelude}\nPARAMS.update(json.loads({params_literal}))\n"
            f"ROUTINE_DIR = {str(workdir)!r}\nREFS.update({_refs_for(workdir)!r})\n")
    first_line = head.count("\n") + 2  # the body starts on the line after the marker below
    return f"{head}_ROUTINE_FIRST_LINE = {first_line}\n{code}"


def _refs_for(workdir: Path) -> dict:
    meta_path = workdir / "routine.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    return {ref: {"path": str(workdir / "refs" / f"{ref}.png"), "region": spec["region"]}
            for ref, spec in (meta.get("refs") or {}).items()}


def _child_env() -> dict:
    """Scrub credentials from the child; it only needs the phone's own tools and accounts."""
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(_SECRET_SUFFIXES)}
    env.update(PYTHONUNBUFFERED="1", DO_NOT_TRACK="1")
    # Hermes strips its own packages from child PYTHONPATH; put phone_harness's parent back.
    spec = importlib.util.find_spec("phone_harness")
    if spec is not None and spec.origin:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(spec.origin).parent.parent),
                                                          env.get("PYTHONPATH")]))
    return env


def _spawn(workdir: Path) -> subprocess.Popen:
    # Own process group, so a timeout can kill the runner and everything it started.
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    return subprocess.Popen(
        [sys.executable, "-m", "phone_harness.run"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        env=_child_env(), cwd=str(workdir), **group,
    )


def _kill_group(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:  # already gone
        pass


SUMMARY_VALUE_CHARS = 300


def _trim(value):
    """Keep a done() summary readable: a routine that reports a whole system dump gets its head."""
    if isinstance(value, str) and len(value) > SUMMARY_VALUE_CHARS:
        return value[:SUMMARY_VALUE_CHARS] + f"… ({len(value)} chars)"
    if isinstance(value, (list, dict)) and len(json.dumps(value, ensure_ascii=False)) > SUMMARY_VALUE_CHARS:
        text = json.dumps(value, ensure_ascii=False)
        return text[:SUMMARY_VALUE_CHARS] + f"… ({len(text)} chars)"
    return value


TIME_SPENT_LINES = 6
_COST_FIELDS = ("calls", "total_s", "max_s")


def _helper_cost(helpers) -> dict:
    """The cost event's per-helper totals; anything malformed (the line is script output) is dropped."""
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    if not isinstance(helpers, dict):
        return {}
    return {str(name): cost for name, cost in helpers.items()
            if isinstance(cost, dict) and all(number(cost.get(f)) for f in _COST_FIELDS)
            and number(cost.get("budget_s", 0))}


def _time_spent(record: dict, helpers: dict) -> None:
    """Turn the prelude's per-helper totals into where the run's time went, costliest first.

    Whatever no helper accounts for (starting Python and phone-harness, sleeps, the routine's own
    code) is its own line, so a run cannot hide a cost by doing it outside a helper.
    """
    ranked = sorted(helpers.items(), key=lambda item: -item[1]["total_s"])
    lines = []
    for name, cost in ranked[:TIME_SPENT_LINES]:
        calls = cost["calls"]
        detail = f"{calls} call" if calls == 1 else f"{calls} calls, up to {cost['max_s']:.2f}s each"
        lines.append(f"{name}: {cost['total_s']:.2f}s ({detail})")
    if len(ranked) > TIME_SPENT_LINES:
        rest = sum(cost["total_s"] for _, cost in ranked[TIME_SPENT_LINES:])
        lines.append(f"{len(ranked) - TIME_SPENT_LINES} other helpers: {rest:.2f}s")
    outside = record["duration_s"] - sum(cost["total_s"] for cost in helpers.values())
    if outside >= 0.05:
        lines.append(f"outside helpers (startup, sleeps, the routine's own code): {outside:.2f}s")
    record["time_spent"] = lines
    slow = [f"{name} took up to {cost['max_s']:.2f}s per call; it should answer within {cost['budget_s']:g}s"
            for name, cost in ranked if "budget_s" in cost and cost["max_s"] > cost["budget_s"]]
    if slow:
        record["over_budget"] = slow
        log.warning("routine %s run %s: %s", record["routine"], record["run_id"], "; ".join(slow))


def _parse_event(text: str) -> dict | None:
    try:
        event = json.loads(text)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


class _Run:
    """Collects one run's events and output while the child streams them."""

    def __init__(self, name: str, record: dict, started: float, on_event: Callable[[dict], None] | None):
        self.name = name
        self.record = record
        self.started = started
        self.on_event = on_event
        self.stdout: deque[str] = deque(maxlen=TAIL_LINES)
        self.stderr: deque[str] = deque(maxlen=TAIL_LINES)
        self.helper_cost: dict = {}

    def on_stdout(self, line: str) -> None:
        if line.startswith(EVENT_PREFIX):
            event = _parse_event(line[len(EVENT_PREFIX):])
            if event is not None:
                self._add_event(event)
                return
        self.stdout.append(line)

    def _add_event(self, event: dict) -> None:
        if event.get("event") == "cost":
            self.helper_cost = _helper_cost(event.get("helpers"))
            return
        if event.get("event") == "done":
            event = {k: _trim(v) for k, v in event.items()}
        event = {"t": round(time.time() - self.started, 3), **event}
        self.record["events"].append(event)
        if event.get("event") == "checkpoint" and event.get("status") == "failed":
            self.record["failed_checkpoint"] = {k: event.get(k)
                                                for k in ("name", "expected", "seen_text", "screenshot", "similarity", "raised")}
        if event.get("event") == "guard":
            self.record["error"] = block_message(str(event.get("app")), str(event.get("reason") or ""))
            if event.get("reason") != "denied":
                note_blocked_app(str(event.get("app")), source="routine", routine=self.name)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # a broken UI callback must not kill the run
                log.exception("on_event callback failed")
        _broadcast("run.event", {"routine": self.name, "run_id": self.record["run_id"], **event})


def _drain(stream, handle: Callable[[str], None]) -> None:
    for line in stream:
        handle(line.rstrip("\n"))


def _feed(proc: subprocess.Popen, script: str) -> None:
    try:
        proc.stdin.write(script)
        proc.stdin.close()
    except OSError:  # the child exited before reading; its exit code and stderr say why
        pass


def _wait_or_kill(proc: subprocess.Popen, timeout_s: float) -> bool:
    """True when the timeout killed the child."""
    try:
        proc.wait(timeout=timeout_s)
        return False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.wait()
        return True


def _pump(proc: subprocess.Popen, script: str, run: _Run, timeout_s: float) -> bool:
    """Stream output and feed the script concurrently so no pipe can fill and deadlock."""
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, run.on_stdout), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, run.stderr.append), daemon=True),
        threading.Thread(target=_feed, args=(proc, script), daemon=True),
    ]
    for thread in readers:
        thread.start()
    timed_out = _wait_or_kill(proc, timeout_s)
    for thread in readers[:2]:
        thread.join(JOIN_S)
    return timed_out


def _error_for(record: dict, timed_out: bool, timeout_s: float, stderr: deque) -> str | None:
    if timed_out:
        return f"timed out after {timeout_s}s"
    failed = record["failed_checkpoint"]
    if failed and failed.get("raised"):
        return f"{failed['name']} raised {failed['raised']}"
    if failed:
        return f"checkpoint {failed['name']!r} failed"
    if record["exit_code"] != 0:
        lines = [line for line in stderr if line.strip()]
        return lines[-1] if lines else f"exited with code {record['exit_code']}"
    return None


def _finish(run: _Run, timed_out: bool, timeout_s: float) -> None:
    record = run.record
    name, run_id = record["routine"], record["run_id"]
    record["finished_at"] = time.time()
    record["duration_s"] = round(record["finished_at"] - record["started_at"], 3)
    if run.helper_cost:
        _time_spent(record, run.helper_cost)
    record["output_tail"] = "\n".join(run.stdout)[-TAIL_CHARS:]
    record["stderr_tail"] = "\n".join(run.stderr)[-TAIL_CHARS:]
    if record["error"] is None:
        record["error"] = _error_for(record, timed_out, timeout_s, run.stderr)
    record["ok"] = record["error"] is None and record["exit_code"] == 0
    _runs_dir(name).mkdir(parents=True, exist_ok=True)
    _keep_failure_screenshot(record)
    _write_atomic(_runs_dir(name) / f"{run_id}.json", json.dumps(record, indent=2, ensure_ascii=False))
    _update_last_run(name, record)
    _prune_runs(name)
    _broadcast("run.finished", _summary(record))


def _execute(name: str, meta: dict, code: str, params: dict, timeout_s: float,
             on_event: Callable[[dict], None] | None, allowed_apps=None) -> dict:
    workdir = _routine_dir(name)
    guard = guard_source(allowed_apps, script_timeout_s=timeout_s, source="routine", routine=name)
    script = _build_script(code, params, workdir, guard)
    started = time.time()
    record = {"run_id": _new_run_id(), "routine": name, "version": meta["version"], "ok": False,
              "started_at": started, "finished_at": 0.0, "duration_s": 0.0, "exit_code": None,
              "events": [], "failed_checkpoint": None, "output_tail": "", "stderr_tail": "",
              "error": None}
    run = _Run(name, record, started, on_event)
    _broadcast("run.started", _summary(record))
    timed_out = False
    try:
        proc = _spawn(workdir)
    except OSError as exc:
        record["error"] = f"failed to launch phone-harness: {exc}"
    else:
        watch = start_asking_watch(timeout_s) if guard else None
        try:
            timed_out = _pump(proc, script, run, timeout_s)
        finally:
            if watch is not None:
                watch.set()
        record["exit_code"] = proc.returncode
    _finish(run, timed_out, timeout_s)
    return record


def _try_lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _phone_lock(wait_s: float):
    """One phone, so one routine at a time across every process (the agent, the pane, cron).

    A lock file, not a threading lock: the dashboard API loads its own copy of this module and
    scheduled runs happen in another process. The OS drops the lock if the holder dies.
    """
    path = data_dir() / "phone.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        deadline = time.monotonic() + wait_s
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise RoutineError("phone busy: another routine is still running")
            time.sleep(0.2)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def run_routine(name, *, params=None, timeout_s=300, on_event=None, require_approved=True,
                allowed_apps=None) -> dict:
    """Replay a routine on the phone and return its run record.

    Refuses unapproved routines: approval is the user's consent to this exact code.
    """
    meta = _read_meta(name)
    if require_approved and not is_approved(meta):
        raise RoutineError(f"routine {name!r} v{meta['version']} is not approved; approve it before running")
    code = _read_code(name, meta)
    merged = {**meta.get("params", {}), **(params or {})}
    with _phone_lock(LOCK_WAIT_S):
        apps = configured_allowed_apps() if allowed_apps is None else allowed_apps
        return _execute(name, meta, code, merged, timeout_s, on_event, apps)
