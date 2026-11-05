"""Storage and runner for Pocket Autopilot routines.

Loaded standalone by path (the dashboard does this), so it imports nothing from the
plugin. A run pipes the routine to `python -m phone_harness.run` the same way
phone-harness's own Hermes plugin does, and streams the `@@POCKET` events it prints.
"""

from __future__ import annotations

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

PLUGIN = "pocket-autopilot"
MAX_RUNS = 50
LOCK_WAIT_S = 120
JOIN_S = 10
EVENT_PREFIX = "@@POCKET "
TAIL_LINES = 200
TAIL_CHARS = 4000
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_RUN_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
_PRELUDE = Path(__file__).with_name("prelude.py")
_GUARD = Path(__file__).with_name("guard.py")
# One phone, so one routine at a time across every caller in this process.
_RUN_LOCK = threading.Lock()
log = logging.getLogger("pocket_autopilot")


class RoutineError(ValueError):
    """A routine is missing, invalid, unapproved, edited behind our back, or the phone is busy."""


# ---------------------------------------------------------------------------
# Paths and small file helpers.


def data_dir() -> Path:
    """Plugin data folder: profile-aware inside Hermes, an env override for tests."""
    override = os.environ.get("POCKET_AUTOPILOT_DATA")
    if override:
        return Path(override)
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError:  # only reachable outside Hermes
        return Path.home() / ".hermes" / "plugin-data" / PLUGIN
    return Path(plugin_data_dir(PLUGIN))


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
            crop = img.convert("RGB").crop((x, y, x + w, y + h))
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
    return _public(meta)


def approve(name, sha256: str) -> dict:
    """Approve exactly the version the user was shown. A stale hash is refused."""
    meta = _read_meta(name)
    if sha256 != meta.get("sha256"):
        raise RoutineError(f"routine {name!r} changed since it was shown; current sha256 is {meta.get('sha256')}")
    meta["approved_sha256"] = sha256
    _write_meta(name, meta)
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


def configured_allowed_apps() -> list[str]:
    """plugins.entries.pocket-autopilot.settings.allowed_apps from the active profile's config."""
    try:
        from hermes_cli.config import load_config_readonly
    except ImportError:  # outside Hermes (unit tests): no allowlist configured
        return []
    entry = ((load_config_readonly() or {}).get("plugins", {}).get("entries", {}) or {}).get(PLUGIN) or {}
    apps = (entry.get("settings") or {}).get("allowed_apps") or []
    return [str(a) for a in apps] if isinstance(apps, list) else []


def guard_source(allowed_apps) -> str:
    """The app guard for a script, or "" when no allowlist is set (the guard is opt-in)."""
    apps = sorted({str(a).strip() for a in (allowed_apps or ()) if str(a).strip()})
    if not apps:
        return ""
    return f"ALLOWED_APPS = {apps!r}\n" + _GUARD.read_text(encoding="utf-8") + "\n"


def _build_script(code: str, params: dict, workdir: Path, allowed_apps=None) -> str:
    """Guard, prelude, then params and folder, then the routine body.

    Params travel as a JSON string literal so any JSON value survives (true/null included).
    """
    prelude = _PRELUDE.read_text(encoding="utf-8")
    params_literal = repr(json.dumps(params))
    return (f"{guard_source(allowed_apps)}{prelude}\nPARAMS.update(json.loads({params_literal}))\n"
            f"ROUTINE_DIR = {str(workdir)!r}\nREFS.update({_refs_for(workdir)!r})\n{code}")


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

    def on_stdout(self, line: str) -> None:
        if line.startswith(EVENT_PREFIX):
            event = _parse_event(line[len(EVENT_PREFIX):])
            if event is not None:
                self._add_event(event)
                return
        self.stdout.append(line)

    def _add_event(self, event: dict) -> None:
        event = {"t": round(time.time() - self.started, 3), **event}
        self.record["events"].append(event)
        if event.get("event") == "checkpoint" and event.get("status") == "failed":
            self.record["failed_checkpoint"] = {k: event.get(k)
                                                for k in ("name", "expected", "seen_text", "screenshot", "similarity")}
        if event.get("event") == "guard":
            self.record["error"] = f"stopped: {event.get('app')} is not on the allowed apps list"
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
    script = _build_script(code, params, workdir, allowed_apps)
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
        timed_out = _pump(proc, script, run, timeout_s)
        record["exit_code"] = proc.returncode
    _finish(run, timed_out, timeout_s)
    return record


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
    if not _RUN_LOCK.acquire(timeout=LOCK_WAIT_S):
        raise RoutineError("phone busy: another routine is still running")
    try:
        apps = configured_allowed_apps() if allowed_apps is None else allowed_apps
        return _execute(name, meta, code, merged, timeout_s, on_event, apps)
    finally:
        _RUN_LOCK.release()
