"""Behaviour tests for dashboard/plugin_api.py with a stub engine and a fake device."""

import base64
import importlib.util
import io
import re
import threading
import types
from pathlib import Path

import pytest
from PIL import Image

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
API_PATH = ROOT / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/pocket-autopilot"
RUN_ID = "20260101-120000-abcd"
SRC_W, SRC_H = 540, 1200
DEV_W, DEV_H = 1080, 2400


class FakeDevice:
    name = "android"

    def __init__(self, shot_dir):
        self.shot_dir = shot_dir
        self.calls = []
        self.captures = 0
        self.fail_ops = {}
        self.unsupported = set()

    def supports(self, op):
        return op not in self.unsupported

    def send(self, op, **kw):
        self.calls.append((op, kw))
        if op in self.fail_ops:
            raise self.fail_ops.pop(op)
        if op == "session.state":
            return "ready"
        if op == "screen.bounds" or op == "screen.require":
            return {"x": 0, "y": 0, "w": DEV_W, "h": DEV_H, "id": "R3CN"}
        if op == "screen.capture":
            self.captures += 1
            path = self.shot_dir / f"shot-{self.captures}.png"
            Image.new("RGB", (SRC_W, SRC_H), (30, 90, 200)).save(path)
            return str(path), self.send("screen.bounds")
        return None

    def ops(self, name):
        return [kw for op, kw in self.calls if op == name]


def _stub_engine(data_dir):
    eng = types.ModuleType("stub_engine")

    class RoutineError(ValueError):
        pass

    eng.RoutineError = RoutineError
    eng.approved = {"daily": True, "fresh": False}
    eng.run_calls = []
    eng.run_done = threading.Event()

    def validate_name(name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", name or ""):
            raise RoutineError(f"invalid routine name: {name!r}")
        return name

    def get_routine(name):
        validate_name(name)
        if name not in eng.approved:
            raise RoutineError(f"no routine named {name!r}")
        return {"name": name, "version": 1, "code": "pass", "approved": eng.approved[name]}

    def run_routine(name, *, params=None, timeout_s=300, on_event=None, require_approved=True):
        eng.run_calls.append({"name": name, "params": params, "timeout_s": timeout_s,
                              "require_approved": require_approved})
        eng.run_done.set()
        return {"run_id": RUN_ID, "ok": True}

    def get_run(name, run_id):
        path = data_dir / "runs" / name / f"{run_id}.json"
        if not path.exists():
            raise FileNotFoundError(run_id)
        return {"run_id": run_id, "routine": name, "ok": True}

    eng.validate_name = validate_name
    eng.get_routine = get_routine
    eng.list_routines = lambda: [{"name": "daily", "approved": True}]
    eng.list_runs = lambda name, limit=20: [{"run_id": RUN_ID, "routine": name}]
    eng.run_routine = run_routine
    eng.get_run = get_run
    eng.data_dir = lambda: data_dir
    eng.phone_harness_available = lambda: True
    return eng


@pytest.fixture
def env(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("pocket_autopilot_api_under_test", API_PATH)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    shots = tmp_path / "shots"
    shots.mkdir()
    eng = _stub_engine(data_dir)
    device = FakeDevice(shots)
    monkeypatch.setattr(api, "_engine_mod", eng)
    monkeypatch.setattr(api, "_device", device)
    monkeypatch.setattr(api, "_frame", {"key": None, "at": 0.0, "payload": None})
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return types.SimpleNamespace(api=api, eng=eng, device=device, data=data_dir,
                                 client=TestClient(app))


# --- status -----------------------------------------------------------------

def test_status_reports_connected_android(env):
    body = env.client.get(f"{PREFIX}/status").json()
    assert body["ok"] is True
    assert body["phone_harness"] is True
    assert body["connected"] is True
    assert body["platform"] == "android"
    assert body["device"] == "R3CN"
    assert body["screen"] == {"w": DEV_W, "h": DEV_H}
    assert ("session.state", {}) in env.device.calls


def test_status_without_phone_harness(env, monkeypatch):
    monkeypatch.setattr(env.eng, "phone_harness_available", lambda: False)
    body = env.client.get(f"{PREFIX}/status").json()
    assert body["ok"] is True and body["connected"] is False
    assert "phone-harness" in body["error"]


def test_status_error_is_json_and_device_resets(env):
    env.device.fail_ops["session.state"] = RuntimeError("adb offline")
    resp = env.client.get(f"{PREFIX}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False and "adb offline" in body["error"]


# --- frame ------------------------------------------------------------------

def test_frame_returns_scaled_jpeg_data_url(env):
    body = env.client.get(f"{PREFIX}/frame", params={"max_w": 480}).json()
    assert body["ok"] is True
    assert body["image"].startswith("data:image/jpeg;base64,")
    assert (body["src_w"], body["src_h"]) == (SRC_W, SRC_H)
    assert body["w"] == 480
    assert body["h"] == round(SRC_H * 480 / SRC_W)
    raw = base64.b64decode(body["image"].split(",", 1)[1])
    assert Image.open(io.BytesIO(raw)).format == "JPEG"


def test_frame_clamps_max_w(env):
    body = env.client.get(f"{PREFIX}/frame", params={"max_w": 50}).json()
    assert body["w"] == 160


def test_frame_is_cached_for_300ms(env):
    env.client.get(f"{PREFIX}/frame")
    env.client.get(f"{PREFIX}/frame")
    assert env.device.captures == 1
    env.api._frame["at"] -= 1.0  # age the cache past the TTL
    env.client.get(f"{PREFIX}/frame")
    assert env.device.captures == 2


def test_frame_capture_failure_resets_device(env):
    env.device.fail_ops["screen.capture"] = RuntimeError("screencap failed")
    resp = env.client.get(f"{PREFIX}/frame")
    assert resp.json()["ok"] is False
    assert env.api._device is None


# --- tap --------------------------------------------------------------------

def test_tap_maps_source_pixels_to_device_points(env):
    body = env.client.post(f"{PREFIX}/tap", json={"x": 100, "y": 200}).json()
    assert body["ok"] is True
    # Source is 540x1200 over a 1080x2400 device window: the scale is exactly 2.
    assert env.device.ops("input.tap") == [{"x": 200, "y": 400}]
    assert (body["x"], body["y"]) == (200, 400)


def test_tap_outside_source_is_rejected_without_sending(env):
    resp = env.client.post(f"{PREFIX}/tap", json={"x": SRC_W, "y": 0})
    assert resp.status_code == 400
    assert env.device.ops("input.tap") == []


@pytest.mark.parametrize("payload", [{"x": "10", "y": 5}, {"x": True, "y": 5}, {"y": 5}, {"x": 1.5, "y": 2}])
def test_tap_rejects_non_integer_fields(env, payload):
    resp = env.client.post(f"{PREFIX}/tap", json=payload)
    assert resp.status_code == 400
    assert env.device.ops("input.tap") == []


# --- swipe and keys ---------------------------------------------------------

@pytest.mark.parametrize("direction,check", [
    ("up", lambda d: d["y2"] < d["y1"] and d["x1"] == d["x2"]),
    ("down", lambda d: d["y2"] > d["y1"] and d["x1"] == d["x2"]),
    ("left", lambda d: d["x2"] < d["x1"] and d["y1"] == d["y2"]),
    ("right", lambda d: d["x2"] > d["x1"] and d["y1"] == d["y2"]),
])
def test_swipe_drags_in_finger_direction(env, direction, check):
    resp = env.client.post(f"{PREFIX}/swipe", json={"direction": direction})
    assert resp.json()["ok"] is True
    (drag,) = env.device.ops("input.drag")
    assert check(drag)


def test_swipe_rejects_unknown_direction(env):
    assert env.client.post(f"{PREFIX}/swipe", json={"direction": "diagonal"}).status_code == 400
    assert env.client.post(f"{PREFIX}/swipe", json={"direction": ["up"]}).status_code == 400
    assert env.device.ops("input.drag") == []


@pytest.mark.parametrize("key,op", [("home", "nav.home"), ("back", "nav.back"), ("recents", "nav.recents")])
def test_key_maps_to_nav_ops(env, key, op):
    body = env.client.post(f"{PREFIX}/key", json={"key": key}).json()
    assert body == {"ok": True, "op": op}
    assert (op, {}) in env.device.calls


def test_key_rejects_unknown_and_unsupported(env):
    assert env.client.post(f"{PREFIX}/key", json={"key": "power"}).status_code == 400
    env.device.unsupported.add("nav.back")
    assert env.client.post(f"{PREFIX}/key", json={"key": "back"}).status_code == 409
    assert not any(op == "nav.back" for op, _ in env.device.calls)


# --- routines ---------------------------------------------------------------

def test_list_routines(env):
    body = env.client.get(f"{PREFIX}/routines").json()
    assert body == {"ok": True, "routines": [{"name": "daily", "approved": True}]}


def test_show_routine_includes_code_and_runs(env):
    body = env.client.get(f"{PREFIX}/routines/daily").json()
    assert body["routine"]["code"] == "pass"
    assert body["runs"][0]["run_id"] == RUN_ID


def test_show_missing_routine_is_404_and_bad_name_400(env):
    assert env.client.get(f"{PREFIX}/routines/ghost").status_code == 404
    assert env.client.get(f"{PREFIX}/routines/Bad_Name").status_code == 400


def test_run_refuses_unapproved_routine(env):
    resp = env.client.post(f"{PREFIX}/routines/fresh/run", json={})
    assert resp.status_code == 403
    assert resp.json()["ok"] is False
    assert env.eng.run_calls == []


def test_run_starts_background_thread_for_approved(env):
    env.eng.run_done.clear()
    resp = env.client.post(f"{PREFIX}/routines/daily/run", json={"params": {"key": "x"}, "timeout_s": 60})
    assert resp.json() == {"ok": True, "started": True, "routine": "daily"}
    assert env.eng.run_done.wait(2)
    (call,) = env.eng.run_calls
    assert call == {"name": "daily", "params": {"key": "x"}, "timeout_s": 60, "require_approved": True}


def test_run_rejects_non_object_params(env):
    resp = env.client.post(f"{PREFIX}/routines/daily/run", json={"params": ["a"]})
    assert resp.status_code == 400


# --- run records and screenshots -------------------------------------------

def test_get_run_validates_run_id(env):
    run_dir = env.data / "runs" / "daily"
    run_dir.mkdir(parents=True)
    (run_dir / f"{RUN_ID}.json").write_text("{}")
    assert env.client.get(f"{PREFIX}/runs/daily/{RUN_ID}").json()["run"]["run_id"] == RUN_ID
    assert env.client.get(f"{PREFIX}/runs/daily/not-an-id").status_code == 400
    assert env.client.get(f"{PREFIX}/runs/daily/20260101-120000-abce").status_code == 404


def test_screenshot_serves_png_under_data_dir(env):
    run_dir = env.data / "runs" / "daily"
    run_dir.mkdir(parents=True)
    (run_dir / f"{RUN_ID}.png").write_bytes(b"\x89PNG-test")
    resp = env.client.get(f"{PREFIX}/runs/daily/{RUN_ID}/screenshot")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"\x89PNG-test"


def test_screenshot_rejects_traversal(env):
    assert env.client.get(f"{PREFIX}/runs/Bad_Name/{RUN_ID}/screenshot").status_code == 400
    assert env.client.get(f"{PREFIX}/runs/daily/..secret/screenshot").status_code == 400


def test_screenshot_refuses_symlink_escaping_data_dir(env, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    run_dir = env.data / "runs" / "daily"
    run_dir.mkdir(parents=True)
    (run_dir / f"{RUN_ID}.png").symlink_to(outside)
    assert env.client.get(f"{PREFIX}/runs/daily/{RUN_ID}/screenshot").status_code == 404


def test_screenshot_missing_is_404(env):
    assert env.client.get(f"{PREFIX}/runs/daily/{RUN_ID}/screenshot").status_code == 404


def test_thumb_returns_downscaled_jpeg_data_url(env):
    from PIL import Image
    run_dir = env.data / "runs" / "daily"
    run_dir.mkdir(parents=True)
    Image.new("RGB", (1080, 2340), "white").save(run_dir / f"{RUN_ID}.png")
    body = env.client.get(f"{PREFIX}/runs/daily/{RUN_ID}/thumb?max_w=200").json()
    assert body["ok"] is True
    assert body["image"].startswith("data:image/jpeg;base64,")
    assert (body["w"], body["h"]) == (200, 433)
    assert env.client.get(f"{PREFIX}/runs/daily/..x/thumb").status_code == 400


# --- scrcpy -----------------------------------------------------------------

def test_scrcpy_missing_reports_install_hint(env, monkeypatch):
    monkeypatch.setattr(env.api.shutil, "which", lambda name: None)
    body = env.client.post(f"{PREFIX}/scrcpy").json()
    assert body == {"ok": False, "error": "install scrcpy"}


def test_scrcpy_launches_detached(env, monkeypatch):
    launched = {}

    class FakeProc:
        pid = 4242

    def fake_popen(args, **kw):
        launched["args"] = args
        launched["kw"] = kw
        return FakeProc()

    monkeypatch.setattr(env.api.shutil, "which", lambda name: "/usr/bin/scrcpy")
    monkeypatch.setattr(env.api.subprocess, "Popen", fake_popen)
    body = env.client.post(f"{PREFIX}/scrcpy").json()
    assert body == {"ok": True, "pid": 4242}
    assert launched["args"][0] == "/usr/bin/scrcpy"
    assert launched["kw"]["start_new_session"] is True
