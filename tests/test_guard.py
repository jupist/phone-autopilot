"""App guard: a script that reaches an app nobody allowed pauses and asks, and only a person answers."""
import json
import threading
import time
import types

import pytest

FOREIGN = "com.chat.messenger"
# Three screens in the allowed app, then the foreign app stays in front.
FRAMES = [{"app": "com.example.app", "texts": ["Home"]}] * 3 + [{"app": FOREIGN, "texts": ["Chats"]}]
# Keeps reading until the foreign app is in front and the guard has paused it, then acts there.
CODE = ("import time\n"
        "for _ in range(30):\n    screen()\n    time.sleep(0.1)\n"
        "checkpoint('chats', text='Chats', timeout=1)\n"
        "tap(5, 5)\n"
        "print('FINISHED')\n")


@pytest.fixture
def allowlist(engine, monkeypatch):
    """config.yaml's allowed_apps as a list a test can change, as the Settings form would."""
    store = ["com.example.app"]
    monkeypatch.setattr(engine, "_settings", lambda: {"allowed_apps": list(store)})
    return types.SimpleNamespace(apps=store, save=lambda apps: store.__setitem__(slice(None), apps))


def _start(engine, phone):
    """Run CODE in the background; returns (thread, result holder)."""
    phone.set_frames(FRAMES)
    meta = engine.save_routine("guarded", CODE)
    engine.approve("guarded", meta["sha256"])
    result = {}
    thread = threading.Thread(target=lambda: result.update(engine.run_routine("guarded", timeout_s=60)))
    thread.start()
    return thread, result


def _wait_for_question(engine, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        requests = engine.app_requests()
        if requests and requests[0].get("waiting"):
            return requests[0]
        time.sleep(0.05)
    raise AssertionError("the script never asked")


def test_the_guard_is_on_from_a_fresh_install(engine):
    source = engine.guard_source([], script_timeout_s=60, source="phone_exec")
    assert source.startswith("ALLOWED_APPS = []\n") and "_guard_watch" in source


def test_no_guard_when_the_user_lets_scripts_use_any_app(engine, monkeypatch):
    monkeypatch.setattr(engine, "_settings", lambda: {"allow_all_apps": True})
    assert engine.guard_source([], script_timeout_s=60, source="phone_exec") == ""


def test_allow_resumes_the_paused_run_where_it_was(engine, phone, allowlist, monkeypatch):
    monkeypatch.setattr(engine, "ASK_WAIT_S", 30)
    thread, result = _start(engine, phone)
    request = _wait_for_question(engine)
    assert request["app"] == FOREIGN and request["routine"] == "guarded"
    time.sleep(1.5)  # longer than the checkpoint's timeout: the wait must not count against it
    engine.allow_app(FOREIGN, allowlist.save)
    thread.join(30)
    record = result
    assert record["ok"] is True, record["error"]
    assert [e["event"] for e in record["events"] if e["event"] in ("asking", "allowed")] == ["asking", "allowed"]
    assert "TAP 5 5" in record["output_tail"] and "FINISHED" in record["output_tail"]
    assert "SEND nav.home" not in record["output_tail"]
    assert FOREIGN in allowlist.apps and engine.app_requests() == []
    checkpoint = [line for line in record["time_spent"] if line.startswith("checkpoint")]
    assert checkpoint and float(checkpoint[0].split(": ")[1].split("s")[0]) < 1.0


def test_an_allow_saved_from_settings_also_resumes_the_run(engine, phone, allowlist, monkeypatch):
    monkeypatch.setattr(engine, "ASK_WAIT_S", 30)
    thread, result = _start(engine, phone)
    _wait_for_question(engine)
    allowlist.apps.append(FOREIGN)  # Hermes's settings writer: none of the plugin's code runs
    thread.join(30)
    assert result["ok"] is True, result["error"]


def test_not_now_stops_the_run_without_acting_in_the_app(engine, phone, allowlist, monkeypatch):
    monkeypatch.setattr(engine, "ASK_WAIT_S", 30)
    thread, result = _start(engine, phone)
    _wait_for_question(engine)
    engine.dismiss_app_request(FOREIGN)
    thread.join(30)
    assert result["ok"] is False
    assert result["error"] == f"stopped: the user chose Not now for {FOREIGN}. Do not use that app unless they ask"
    assert "TAP 5 5" not in result["output_tail"] and "FINISHED" not in result["output_tail"]
    assert "SEND nav.home" in result["output_tail"]
    assert engine.app_requests() == []  # Not now is an answer, not a card for later
    assert FOREIGN not in allowlist.apps


def test_no_answer_stops_the_run_and_keeps_the_card(engine, phone, allowlist):
    thread, result = _start(engine, phone)  # the test engine waits 1s for an answer
    thread.join(30)
    assert result["ok"] is False
    assert f"/routines allow {FOREIGN}" in result["error"]
    assert "TAP 5 5" not in result["output_tail"] and "SEND nav.home" in result["output_tail"]
    [request] = engine.app_requests()
    assert request["app"] == FOREIGN and request["waiting"] is False


def test_an_old_answer_does_not_answer_a_new_question(engine, phone, allowlist):
    engine.dismiss_app_request(FOREIGN)  # yesterday's Not now
    thread, result = _start(engine, phone)
    _wait_for_question(engine)  # still asks
    thread.join(30)
    assert "nobody answered" in result["error"]


def test_an_app_left_open_by_the_user_is_left_for_home_without_asking(engine, phone, allowlist):
    phone.set_frames([{"app": FOREIGN, "texts": ["Chats"]}] + [{"app": "com.example.app", "texts": ["Ready"]}] * 3)
    meta = engine.save_routine("starts", "import time\ntime.sleep(1.5)\nfor _ in range(3):\n    screen()\ndone()\n")
    engine.approve("starts", meta["sha256"])
    record = engine.run_routine("starts", timeout_s=30)
    assert record["ok"] is True, record["error"]
    assert record["output_tail"].startswith("SEND nav.home\nphone-autopilot: com.chat.messenger was open")
    assert not any(e["event"] == "asking" for e in record["events"]) and engine.app_requests() == []


def test_launcher_and_allowed_apps_do_not_trip_the_guard(engine, phone, allowlist):
    phone.set_frames([{"app": "com.example.launcher", "texts": []},
                      {"app": "com.example.app", "texts": ["Ready"]}])
    meta = engine.save_routine("calm", "for _ in range(5):\n    screen()\ndone(ok=True)\n")
    engine.approve("calm", meta["sha256"])
    record = engine.run_routine("calm", timeout_s=30)
    assert record["ok"] is True, record["error"]
    assert not any(e["event"] == "asking" for e in record["events"])


def _guard_ns(foreground):
    """guard.py's functions in-process with a scripted foreground app (no watchdog thread)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "guard.py").read_text(encoding="utf-8")
    ns = {"ALLOWED_APPS": [], "current_app": lambda: next(foreground)}
    exec(compile(src, "guard.py", "exec"), ns)
    ns["_GUARD_DIALOG_GRACE_S"] = 0.3
    ns["_GUARD_POLL_S"] = 0.03
    return ns


def test_a_permission_dialog_that_closes_itself_is_not_a_stop():
    # A permanently denied permission: Android opens the dialog activity and it finishes unseen.
    dialog = "com.google.android.permissioncontroller"
    ns = _guard_ns(iter([dialog] + ["com.example.app"] * 100))
    assert ns["_guard_dialog_stays"](dialog) is False


def test_a_permission_dialog_that_stays_up_is_left_for_the_user():
    import itertools
    dialog = "com.google.android.permissioncontroller"
    ns = _guard_ns(itertools.repeat(dialog))
    assert ns["_guard_dialog_stays"](dialog) is True


def test_answers_older_than_the_question_are_ignored(tmp_path):
    ns = _guard_ns(iter(()))
    answers = tmp_path / "answers.json"
    ns["_GUARD_ASK"] = {"answers": str(answers)}
    answers.write_text(json.dumps({FOREIGN: {"answer": "allowed", "at": 100.0}}))
    assert ns["_guard_answer"](FOREIGN, 99.0) == "allowed"
    assert ns["_guard_answer"](FOREIGN, 101.0) is None


LAUNCH = "send('apps.launch', name='com.spotify.music')\nprint('LAUNCHED')\n"


def _start_launch(engine, phone):
    phone.set_frames([{"app": "com.example.app", "texts": ["Home"]}])
    meta = engine.save_routine("launcher", LAUNCH)
    engine.approve("launcher", meta["sha256"])
    result = {}
    thread = threading.Thread(target=lambda: result.update(engine.run_routine("launcher", timeout_s=60)))
    thread.start()
    return thread, result


def test_a_launch_by_package_asks_before_the_app_opens(engine, phone, allowlist, monkeypatch):
    monkeypatch.setattr(engine, "ASK_WAIT_S", 30)
    thread, result = _start_launch(engine, phone)
    assert _wait_for_question(engine)["app"] == "com.spotify.music"
    engine.allow_app("com.spotify.music", allowlist.save)
    thread.join(30)
    assert result["ok"] is True, result["error"]
    out = result["output_tail"]
    assert out.index("phone-autopilot: paused. com.spotify.music") < out.index("SEND apps.launch") < out.index("LAUNCHED")


def test_not_now_before_a_launch_never_opens_the_app(engine, phone, allowlist, monkeypatch):
    monkeypatch.setattr(engine, "ASK_WAIT_S", 30)
    thread, result = _start_launch(engine, phone)
    _wait_for_question(engine)
    engine.dismiss_app_request("com.spotify.music")
    thread.join(30)
    assert result["error"].startswith("stopped: the user chose Not now for com.spotify.music")
    assert "SEND apps.launch" not in result["output_tail"]
    assert "SEND nav.home" not in result["output_tail"]  # it never left the screen it was on


@pytest.mark.parametrize("helper, args, kwargs, target", [
    ("open_app", ("com.spotify.music",), {}, "com.spotify.music"),
    ("open_app", ("Spotify",), {}, None),  # a label: the watchdog catches it once it opens
    ("send", ("apps.launch",), {"name": "com.spotify.music"}, "com.spotify.music"),
    ("send", ("nav.home",), {}, None),
    ("shell", ("am start -n com.spotify.music/.MainActivity",), {}, "com.spotify.music"),
    ("shell", ("monkey -p com.rishukumarcodes.Standbyclock -c android.intent.category.LAUNCHER 1",), {},
     "com.rishukumarcodes.Standbyclock"),
    ("shell", ("am start -a android.intent.action.VIEW -d spotify:playlist:1",), {}, None),
    ("shell", ("settings get global zen_mode",), {}, None),
])
def test_launch_targets_known_up_front(helper, args, kwargs, target):
    assert _guard_ns(iter(()))["_guard_launch_target"](helper, args, kwargs) == target
