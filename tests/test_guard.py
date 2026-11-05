"""App guard: opt-in allowlist enforced by a watchdog inside the phone script."""
import time


def test_guard_source_is_empty_without_an_allowlist(engine):
    assert engine.guard_source([]) == ""
    assert engine.guard_source(None) == ""
    assert engine.guard_source([" ", ""]) == ""


def test_guard_source_pins_a_sorted_allowlist(engine):
    src = engine.guard_source(["com.b", "com.a", "com.a"])
    assert src.startswith("ALLOWED_APPS = ['com.a', 'com.b']\n")
    assert "_guard_watch" in src


def test_guard_stops_a_run_that_reaches_a_disallowed_app(engine, phone):
    phone.set_frames([{"app": "com.example.app", "texts": ["Home"]}] * 3
                     + [{"app": "com.chat.messenger", "texts": ["Chats"]}])
    code = "for _ in range(40):\n    screen()\n    time.sleep(0.1)\nprint('FINISHED')\n"
    meta = engine.save_routine("guarded", "import time\n" + code)
    engine.approve("guarded", meta["sha256"])
    started = time.time()
    record = engine.run_routine("guarded", timeout_s=30, allowed_apps=["com.example.app"])
    assert record["ok"] is False
    assert record["error"] == "stopped: com.chat.messenger is not on the allowed apps list"
    assert "SEND nav.home" in record["output_tail"]
    assert "FINISHED" not in record["output_tail"]
    assert time.time() - started < 10


def test_launcher_and_allowed_apps_do_not_trip_the_guard(engine, phone):
    phone.set_frames([{"app": "com.example.launcher", "texts": []},
                      {"app": "com.example.app", "texts": ["Ready"]}])
    meta = engine.save_routine("calm", "for _ in range(5):\n    screen()\ndone(ok=True)\n")
    engine.approve("calm", meta["sha256"])
    record = engine.run_routine("calm", timeout_s=30, allowed_apps=["com.example.app"])
    assert record["ok"] is True, record["error"]
