"""Fake `python -m phone_harness.run`: exec stdin with fake helper globals.

The screen script is a JSON file named by FAKE_PH_SCREEN:
  {"shot_dir": "...", "frames": [{"app": "...", "texts": [...]}, ...]}
Each screen() call reads the next frame; the last frame repeats.
"""
import json
import os
import sys
import time
from pathlib import Path

_SCRIPT = Path(os.environ["FAKE_PH_SCREEN"])
_STATE = {"calls": 0}


def _spec():
    return json.loads(_SCRIPT.read_text(encoding="utf-8"))


def _current():
    frames = _spec()["frames"]
    return frames[min(_STATE["calls"], len(frames) - 1)]


def screen(min_confidence=0.3):
    texts = _current()["texts"]
    _STATE["calls"] += 1
    return [{"text": t, "confidence": 1.0, "source": "tree", "x": 0, "y": 0, "w": 1, "h": 1}
            for t in texts]


def find_text(query, exact=False):
    q = query.lower()
    return [o for o in screen() if (o["text"].lower() == q if exact else q in o["text"].lower())]


def current_app():
    return _current()["app"]


def screenshot(path=None):
    shot_dir = Path(_spec()["shot_dir"])
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot = str(shot_dir / f"shot-{time.time_ns()}.png")
    Path(shot).write_bytes(b"\x89PNG fake")
    print(shot)
    return shot


def wait_stable(timeout=6.0, interval=0.5, settle=2):
    return True


def shell(cmd, binary=False, timeout=60):
    if "android.intent.category.HOME" in cmd:
        return "1 activities found:\n  Activity #0:\n    com.example.launcher/.Main\n"
    return ""


def send(op, **kw):
    if op == "screen.capture":
        return _current()["image"], None
    print(f"SEND {op}", flush=True)


_code = sys.stdin.read()
exec(_code, {
    "__name__": "__main__",
    "find_text": find_text,
    "screen": screen,
    "screenshot": screenshot,
    "current_app": current_app,
    "wait_stable": wait_stable,
    "shell": shell,
    "send": send,
})
