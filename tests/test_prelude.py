"""Prelude helpers exercised in-process with fake helper globals (no subprocess)."""
import json
from pathlib import Path

import pytest

PRELUDE = (Path(__file__).resolve().parent.parent / "prelude.py").read_text(encoding="utf-8")


class FakeScreen:
    """Stands in for phone-harness helpers. `frames` is a list of text lists; the last repeats."""

    def __init__(self, frames, app="com.example.app", shot="shot.png", fail_first=0, broken=False):
        self.frames = frames
        self.app = app
        self.shot = shot
        self.fail_first = fail_first
        self.broken = broken
        self.calls = 0

    def screen(self, min_confidence=0.3):
        self.calls += 1
        if self.broken or self.calls <= self.fail_first:
            raise RuntimeError("tree not ready")
        texts = self.frames[min(self.calls - 1, len(self.frames) - 1)]
        return [{"text": t, "confidence": 1.0, "source": "tree", "x": 0, "y": 0, "w": 1, "h": 1}
                for t in texts]

    def find_text(self, query, exact=False):
        q = query.lower()
        return [o for o in self.screen() if (o["text"].lower() == q if exact else q in o["text"].lower())]

    def current_app(self):
        if self.broken:
            raise RuntimeError("no inventory")
        return self.app

    def screenshot(self, path=None):
        if self.broken:
            raise RuntimeError("capture failed")
        return self.shot


def load_prelude(fake):
    ns = {"find_text": fake.find_text, "screen": fake.screen,
          "screenshot": fake.screenshot, "current_app": fake.current_app}
    exec(compile(PRELUDE, "prelude.py", "exec"), ns)
    ns["POLL_S"] = 0.01  # keep the polling tests fast
    return ns


def events(capsys):
    out = capsys.readouterr().out
    return [json.loads(line[len("@@POCKET "):]) for line in out.splitlines() if line.startswith("@@POCKET ")]


def test_passing_checkpoint_emits_started_then_passed(capsys):
    ns = load_prelude(FakeScreen([["Home", "Claim"]]))
    ns["checkpoint"]("home", text="Home")
    assert [(e["event"], e["status"]) for e in events(capsys)] == [("checkpoint", "started"),
                                                                    ("checkpoint", "passed")]


def test_app_and_any_of_conditions(capsys):
    ns = load_prelude(FakeScreen([["Settings", "Rewards"]]))
    ns["checkpoint"]("rewards", any_of=["Claim", "Rewards"], app="example")
    assert events(capsys)[-1]["status"] == "passed"


def test_wrong_app_is_a_miss(capsys):
    ns = load_prelude(FakeScreen([["Home"]], app="com.other"))
    with pytest.raises(ns["CheckpointMiss"]):
        ns["checkpoint"]("home", text="Home", app="com.example", timeout=0.05)


def test_miss_emits_evidence_and_raises(capsys):
    ns = load_prelude(FakeScreen([["Settings", "Storage"]], shot="/abs/shot.png"))
    with pytest.raises(ns["CheckpointMiss"]):
        ns["checkpoint"]("reward", text="Claim", timeout=0.05)
    failed = events(capsys)[-1]
    assert failed == {"event": "checkpoint", "name": "reward", "status": "failed", "expected": "Claim",
                      "seen_text": ["Settings", "Storage"], "screenshot": "/abs/shot.png"}


def test_helper_error_during_polling_means_not_seen_yet(capsys):
    ns = load_prelude(FakeScreen([["Claim"]], fail_first=2))
    ns["checkpoint"]("reward", text="Claim", timeout=5)
    assert events(capsys)[-1]["status"] == "passed"


def test_miss_is_still_reported_when_evidence_fails(capsys):
    ns = load_prelude(FakeScreen([["Home"]], broken=True))
    with pytest.raises(ns["CheckpointMiss"]):
        ns["checkpoint"]("reward", text="Claim", timeout=0.05)
    failed = events(capsys)[-1]
    assert failed["status"] == "failed"
    assert failed["screenshot"] is None
    assert failed["seen_text"] == ["(screen text unavailable: RuntimeError; use the screenshot)"]


def test_animated_screen_still_keeps_the_screenshot(capsys):
    # A ticking clock never lets the accessibility tree settle: text fails, the capture must not.
    fake = FakeScreen([["Home"]], shot="/abs/shot.png")
    fake.screen = lambda min_confidence=0.3: (_ for _ in ()).throw(RuntimeError("tree unavailable"))
    ns = load_prelude(fake)
    with pytest.raises(ns["CheckpointMiss"]):
        ns["checkpoint"]("settings", text="Focus", timeout=0.05)
    failed = events(capsys)[-1]
    assert failed["screenshot"] == "/abs/shot.png"
    assert "screen text unavailable" in failed["seen_text"][0]


def test_step_and_done_emit_events(capsys):
    ns = load_prelude(FakeScreen([["Home"]]))
    ns["step"]("open")
    ns["done"](claimed=True, count=2)
    assert events(capsys) == [{"event": "step", "name": "open"},
                              {"event": "done", "claimed": True, "count": 2}]


def test_one_text_read_per_poll_however_many_words(capsys):
    fake = FakeScreen([["Collected"]])
    ns = load_prelude(fake)
    ns["checkpoint"]("reward", any_of=["Claim", "Come back", "Collected"])
    assert fake.calls == 1  # a read costs ~2.5s on a phone: never once per word


def test_wrong_app_is_known_without_reading_text(capsys):
    fake = FakeScreen([["Home"]], app="com.other")
    ns = load_prelude(fake)
    with pytest.raises(ns["CheckpointMiss"]):
        ns["checkpoint"]("home", text="Home", app="com.example", timeout=0.05)
    assert fake.calls == 1  # only the failure record's evidence, never a poll


def test_a_screen_without_text_fails_at_once_and_says_to_use_an_image(capsys):
    import time

    class TreeUnavailable(RuntimeError):
        pass

    fake = FakeScreen([["Focus"]], shot="/abs/shot.png")
    fake.screen = lambda min_confidence=0.3: (_ for _ in ()).throw(TreeUnavailable("could not get idle state"))
    ns = load_prelude(fake)
    started = time.time()
    with pytest.raises(ns["CheckpointMiss"]):
        ns["checkpoint"]("settings", text="Focus", timeout=30)
    assert time.time() - started < 5  # not 30s of 11s refusals
    failed = events(capsys)[-1]
    assert "check it with an image ref" in failed["expected"]
    assert failed["screenshot"] == "/abs/shot.png"
    assert failed["seen_text"] == ["(screen text unavailable: TreeUnavailable; use the screenshot)"]


def test_open_app_returns_once_the_app_is_in_front():
    import time
    fronts = iter(["com.launcher", "com.launcher", "com.example.app"])
    current = {"app": "com.launcher"}

    def current_app():
        current["app"] = next(fronts, current["app"])
        return current["app"]

    ns = {"send": lambda op, **kw: kw["name"], "current_app": current_app}
    exec(compile(PRELUDE, "prelude.py", "exec"), ns)
    started = time.time()
    assert ns["open_app"]("com.example.app") == "com.example.app"
    assert time.time() - started < 1


def test_check_condition_passes_once_true(capsys):
    calls = iter([False, False, True])
    ns = load_prelude(FakeScreen([[]]))
    ns["checkpoint"]("dnd-on", check=lambda: next(calls), timeout=5)
    assert events(capsys)[-1]["status"] == "passed"


def test_check_condition_that_never_holds_fails_with_evidence(capsys):
    def dnd_on():
        return False
    ns = load_prelude(FakeScreen([["Home"]], shot="/abs/shot.png"))
    with pytest.raises(ns["CheckpointMiss"]):
        ns["checkpoint"]("dnd-on", check=dnd_on, timeout=0.05)
    failed = events(capsys)[-1]
    assert failed["expected"] == "dnd_on() to be true"
    assert failed["screenshot"] == "/abs/shot.png"


MEDIA_DUMP = """MEDIA SESSION SERVICE (dumpsys media_session)
    package=com.android.server.telecom
    active=false
    metadata: null
      package=com.spotify.music
      active=true
      state=PlaybackState {state=PLAYING(3), position=1193, buffered position=0, speed=1.0}
      metadata: size=18, description=This Is New, Mike Coen, This Is New
"""
NOTES_DUMP = """    NotificationRecord(0x01: pkg=com.chat.app user=UserHandle{0} id=1 tag=null importance=4
                android.title=String (Mum)
                android.text=String (secret message)
    NotificationRecord(0x0f: pkg=com.spotify.music user=UserHandle{0} id=2 tag=null importance=2
      flags=ONGOING_EVENT|NO_CLEAR
                android.title=SpannableString (This Is New)
                android.subText=SpannableString (Jazz for Study)
                android.text=SpannableString (Mike Coen)
"""


def _media_ns(media=MEDIA_DUMP, notes=NOTES_DUMP):
    ns = load_prelude(FakeScreen([[]]))
    ns["shell"] = lambda cmd, **kw: media if "media_session" in cmd else notes
    return ns


def test_media_state_reads_what_is_playing_and_from_where():
    assert _media_ns()["media_state"]() == {
        "app": "com.spotify.music", "state": "PLAYING", "playing": True,
        "title": "This Is New", "artist": "Mike Coen", "source": "Jazz for Study"}


def test_media_state_never_returns_another_apps_notification():
    state = _media_ns()["media_state"]("com.spotify.music")
    assert "secret message" not in json.dumps(state) and "Mum" not in json.dumps(state)


def test_media_state_without_a_session_or_notification():
    assert _media_ns(media="MEDIA SESSION SERVICE\n")["media_state"]() is None
    paused = MEDIA_DUMP.replace("PLAYING(3)", "PAUSED(2)")
    state = _media_ns(media=paused, notes="")["media_state"]("com.spotify.music")
    assert state["playing"] is False and state["title"] == "This Is New" and state["source"] == ""
