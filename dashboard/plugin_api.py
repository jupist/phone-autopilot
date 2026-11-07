"""Phone Autopilot dashboard API: live phone view, phone controls, routines.

Routes mount at /api/plugins/phone-autopilot/<path>. Handlers are sync so FastAPI
runs them on the threadpool, and every one returns JSON instead of raising.
Device access goes through phone-harness's transport (Android or iPhone); routines
and run records come from engine.py, which is loaded by path so this module never
depends on the plugin's package layout.
"""

import base64
import functools
import importlib.util
import io
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image

try:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse, Response
    router = APIRouter()
except Exception:  # FastAPI only exists inside the dashboard host
    router = None

log = logging.getLogger(__name__)

_ENGINE_PATH = Path(__file__).resolve().parent.parent / "engine.py"
_FRAME_TTL_S = 0.3
_DEFAULT_MAX_W = 480
_DEFAULT_QUALITY = 60
_DEFAULT_TIMEOUT_S = 300
_MAX_TIMEOUT_S = 900
_SWIPE_FRACTION = 0.4
# Finger motion, as phone_harness.helpers.swipe() defines it: "up" drags the thumb up.
_SWIPE_FINGER = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
_KEY_OPS = {"home": "nav.home", "back": "nav.back", "recents": "nav.recents"}
_RUN_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")

_init_lock = threading.Lock()
_engine_mod = None
_device = None
_frame_lock = threading.Lock()
_frame = {"key": None, "at": 0.0, "payload": None}


class _ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _fail(status, message):
    return JSONResponse(status_code=status, content={"ok": False, "error": message})


def _safe(fn):
    """Never-throw envelope: known errors keep their status, anything else is a 500 JSON."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _ApiError as exc:
            return _fail(exc.status, str(exc))
        except Exception as exc:
            log.warning("phone-autopilot %s failed: %s", fn.__name__, exc, exc_info=True)
            return _fail(500, f"{type(exc).__name__}: {exc}")

    return wrapper


def _route(method, path):
    if router is None:
        return lambda fn: fn
    return getattr(router, method)(path)


# --- lazy loaders -----------------------------------------------------------

def _engine():
    global _engine_mod
    with _init_lock:
        if _engine_mod is None:
            spec = importlib.util.spec_from_file_location("phone_autopilot_engine", _ENGINE_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _engine_mod = module
    return _engine_mod


def _device_handle():
    global _device
    with _init_lock:
        if _device is None:
            from phone_harness import transport

            _device = transport.connect()
        return _device


def _send(op, **kw):
    global _device
    try:
        return _device_handle().send(op, **kw)
    except Exception:
        # A dropped adb link leaves a dead handle; the next call reconnects.
        _device = None
        raise


# --- frames -----------------------------------------------------------------

def _clamp(value, lo, hi):
    return max(lo, min(hi, int(value)))


def _downscale(img, max_w):
    if img.width <= max_w:
        return img
    max_h = max(1, round(img.height * max_w / img.width))
    return img.resize((max_w, max_h), Image.LANCZOS)


def _jpeg_data_url(img, quality):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _capture_frame(max_w, quality):
    png_path, _bounds = _send("screen.capture")
    try:
        with Image.open(png_path) as shot:
            src = shot.convert("RGB")
    finally:
        Path(png_path).unlink(missing_ok=True)
    out = _downscale(src, max_w)
    return {
        "ok": True,
        "image": _jpeg_data_url(out, quality),
        "w": out.width,
        "h": out.height,
        "src_w": src.width,
        "src_h": src.height,
        "ts": time.time(),
    }


def _get_frame(max_w=_DEFAULT_MAX_W, quality=_DEFAULT_QUALITY):
    """Recapture only when the cached frame is older than the TTL or was built for other params.

    The lock is held across the capture on purpose: concurrent panes wait and reuse
    one screencap instead of each triggering their own.
    """
    key = (max_w, quality)
    with _frame_lock:
        fresh = time.monotonic() - _frame["at"] < _FRAME_TTL_S
        if fresh and _frame["key"] == key:
            return _frame["payload"]
        payload = _capture_frame(max_w, quality)
        _frame.update(key=key, at=time.monotonic(), payload=payload)
        return payload


def _device_point(x, y, bounds, src_w, src_h):
    """Map screenshot pixels into the device's input space.

    Same scaling as phone_harness.helpers.image_point(): the capture's pixel grid
    spans the window bounds. On Android the bounds equal the capture size, so this
    is the identity; keeping the scale keeps iPhone-style windows correct too.
    """
    return (
        round(bounds["x"] + x * bounds["w"] / src_w),
        round(bounds["y"] + y * bounds["h"] / src_h),
    )


# --- input validation -------------------------------------------------------

def _body_dict(body):
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise _ApiError(400, "body must be a JSON object")
    return body


def _int_field(body, name, default=None):
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ApiError(400, f"{name} must be an integer")
    return value


def _name(name):
    try:
        return _engine().validate_name(name)
    except ValueError as exc:
        raise _ApiError(400, str(exc)) from None


def _run_id(run_id):
    if not _RUN_ID_RE.fullmatch(run_id):
        raise _ApiError(400, "invalid run id")
    return run_id


def _routine_or_404(eng, name):
    try:
        return eng.get_routine(name)
    except eng.RoutineError as exc:
        raise _ApiError(404, str(exc)) from None


# --- device routes ----------------------------------------------------------

@_route("get", "/status")
@_safe
def status():
    info = {"ok": True, "phone_harness": False, "connected": False, "device": None,
            "platform": None, "screen": None, "state": None}
    info["phone_harness"] = _engine().phone_harness_available()
    if not info["phone_harness"]:
        info["error"] = "phone-harness is not installed"
        return info
    try:
        dev = _device_handle()
        state = _send("session.state")
        bounds = _send("screen.bounds") if state == "ready" else None
    except Exception as exc:
        info["error"] = str(exc)
        return info
    info["platform"] = getattr(dev, "name", None)
    info["state"] = state
    info["connected"] = bounds is not None
    if bounds is not None:
        info["serial"] = bounds.get("id")
        info["device"] = _device_label(bounds.get("id"))
        info["screen"] = {"w": bounds["w"], "h": bounds["h"]}
    return info


_LABELS: dict = {}


def _device_label(serial):
    """The phone's model ("SM S911B") instead of its serial, from the adb device listing.

    Cached per serial: the status route polls, and the model never changes under a serial.
    """
    if serial not in _LABELS:
        try:
            detail = str(_send("session.detail"))
        except Exception:
            return serial
        match = re.search(rf"^{re.escape(str(serial))}\s.*\bmodel:(\S+)", detail, re.M)
        _LABELS[serial] = match.group(1).replace("_", " ") if match else serial
    return _LABELS[serial]


@_route("get", "/frame")
@_safe
def frame(max_w: int = _DEFAULT_MAX_W, quality: int = _DEFAULT_QUALITY):
    return _get_frame(_clamp(max_w, 160, 1080), _clamp(quality, 30, 85))


@_route("post", "/tap")
@_safe
def tap(body: Optional[dict] = None):
    data = _body_dict(body)
    x, y = _int_field(data, "x"), _int_field(data, "y")
    # Validate against the same frame the pane shows, so clicks map onto what the user saw.
    shot = _get_frame()
    src_w, src_h = shot["src_w"], shot["src_h"]
    if not (0 <= x < src_w and 0 <= y < src_h):
        raise _ApiError(400, f"tap ({x}, {y}) is outside the {src_w}x{src_h} screen")
    bounds = _send("screen.bounds")
    if bounds is None:
        raise _ApiError(409, "device is not ready")
    dx, dy = _device_point(x, y, bounds, src_w, src_h)
    _send("input.tap", x=dx, y=dy)
    return {"ok": True, "x": dx, "y": dy}


@_route("post", "/swipe")
@_safe
def swipe(body: Optional[dict] = None):
    direction = _body_dict(body).get("direction")
    vector = _SWIPE_FINGER.get(direction) if isinstance(direction, str) else None
    if vector is None:
        raise _ApiError(400, "direction must be up, down, left or right")
    win = _send("screen.require")
    fx, fy = vector
    cx, cy = win["x"] + win["w"] / 2, win["y"] + win["h"] / 2
    half_dx = fx * win["w"] * _SWIPE_FRACTION / 2
    half_dy = fy * win["h"] * _SWIPE_FRACTION / 2
    # A short fast drag is a flick; a slow one barely registers on iOS.
    _send("input.drag", x1=round(cx - half_dx), y1=round(cy - half_dy),
          x2=round(cx + half_dx), y2=round(cy + half_dy), duration=0.12, steps=6)
    return {"ok": True}


@_route("post", "/key")
@_safe
def key_press(body: Optional[dict] = None):
    key = _body_dict(body).get("key")
    op = _KEY_OPS.get(key) if isinstance(key, str) else None
    if op is None:
        raise _ApiError(400, "key must be home, back or recents")
    if not _device_handle().supports(op):
        raise _ApiError(409, f"this device cannot press {key}")
    _send(op)
    return {"ok": True, "op": op}


@_route("post", "/scrcpy")
@_safe
def open_scrcpy():
    exe = shutil.which("scrcpy")
    if exe is None:
        return {"ok": False, "error": "install scrcpy"}
    args = [exe]
    serial = os.environ.get("ANDROID_SERIAL")
    if serial:
        args += ["-s", serial]
    # Detached so the pop-out window outlives this request and never ties up the server's stdio.
    proc = subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "pid": proc.pid}


# --- routine routes ---------------------------------------------------------

@_route("get", "/routines")
@_safe
def list_routines():
    return {"ok": True, "routines": _engine().list_routines()}


@_route("get", "/routines/{name}")
@_safe
def show_routine(name: str):
    eng = _engine()
    name = _name(name)
    return {"ok": True, "routine": _routine_or_404(eng, name),
            "runs": eng.list_runs(name, limit=10)}


def _run_in_background(eng, name, params, timeout_s):
    # Progress reaches the UI through the engine's plugin-event broadcasts, not this response.
    try:
        eng.run_routine(name, params=params, timeout_s=timeout_s, require_approved=True)
    except Exception:
        log.exception("phone-autopilot: background run of %s failed", name)


@_route("post", "/routines/{name}/run")
@_safe
def start_routine(name: str, body: Optional[dict] = None):
    eng = _engine()
    name = _name(name)
    data = _body_dict(body)
    params = data.get("params") or {}
    if not isinstance(params, dict):
        raise _ApiError(400, "params must be an object")
    timeout_s = _clamp(_int_field(data, "timeout_s", _DEFAULT_TIMEOUT_S), 1, _MAX_TIMEOUT_S)
    meta = _routine_or_404(eng, name)
    # Checked here so the UI gets a clear refusal; run_routine re-checks with require_approved.
    if not meta.get("approved"):
        raise _ApiError(403, f"routine '{name}' is not approved yet; ask the agent to run it once to approve")
    threading.Thread(target=_run_in_background, args=(eng, name, params, timeout_s),
                     name=f"phone-autopilot-run-{name}", daemon=True).start()
    return {"ok": True, "started": True, "routine": name}


@_route("get", "/runs/{name}/{run_id}")
@_safe
def get_run(name: str, run_id: str):
    eng = _engine()
    name, run_id = _name(name), _run_id(run_id)
    try:
        return {"ok": True, "run": eng.get_run(name, run_id)}
    except (eng.RoutineError, FileNotFoundError) as exc:
        raise _ApiError(404, str(exc)) from None


@_route("get", "/runs/{name}/{run_id}/screenshot")
@_safe
def run_screenshot(name: str, run_id: str):
    return Response(content=_run_png(name, run_id).read_bytes(), media_type="image/png")


# ctx.rest parses JSON only, so the Desktop pane reads failure thumbnails as a data URL.
@_route("get", "/runs/{name}/{run_id}/thumb")
@_safe
def run_thumb(name: str, run_id: str, max_w: int = 240):
    with Image.open(_run_png(name, run_id)) as shot:
        img = _downscale(shot.convert("RGB"), _clamp(max_w, 80, 480))
    return {"ok": True, "image": _jpeg_data_url(img, 70), "w": img.width, "h": img.height}


def _run_png(name, run_id):
    eng = _engine()
    name, run_id = _name(name), _run_id(run_id)
    root = Path(eng.data_dir()).resolve()
    # resolve() follows symlinks, so a link planted under runs/ cannot escape the data dir.
    path = (root / "runs" / name / f"{run_id}.png").resolve()
    if root not in path.parents or not path.is_file():
        raise _ApiError(404, "no screenshot for this run")
    return path


# --- app permissions --------------------------------------------------------
# These routes sit behind the dashboard session token, which only the web UI holds, so an
# Allow here is a person's click. The agent has no tool that grants apps.

def _app_field(body):
    app = _body_dict(body).get("app")
    if not isinstance(app, str):
        raise _ApiError(400, "app must be a package name")
    return app


@_route("get", "/apps")
@_safe
def list_apps():
    eng = _engine()
    return {"ok": True, "allowed": eng.configured_allowed_apps(), "requests": eng.app_requests()}


@_route("post", "/apps/dismiss")
@_safe
def dismiss_app(body: Optional[dict] = None):
    _engine().dismiss_app_request(_app_field(body))
    return {"ok": True}
