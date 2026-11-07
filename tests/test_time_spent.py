"""Every run says where its time went, so a slow routine shows its cause instead of a long total."""
from __future__ import annotations

NAME = "timed"


def _run(engine, code):
    meta = engine.save_routine(NAME, code)
    engine.approve(NAME, meta["sha256"])
    return engine.run_routine(NAME, timeout_s=30)


def _helpers(record):
    return [line.split(":")[0] for line in record["time_spent"]]


def test_time_goes_to_the_helpers_the_routine_called_costliest_first(engine, phone):
    phone.set_frames([{"app": "com.example.app", "texts": ["Ready"]}])
    record = _run(engine, "import time\n"
                          "checkpoint('ready', text='Ready')\n"
                          "for _ in range(3):\n    shell('true')\n"
                          "time.sleep(0.4)\n"
                          "done()\n")
    assert record["ok"] is True
    helpers = _helpers(record)
    # The screen read inside the checkpoint is the checkpoint's time, not a separate line.
    assert "checkpoint" in helpers and "shell" in helpers and "screen" not in helpers
    assert any(line.startswith("shell: ") and "3 calls" in line for line in record["time_spent"])
    outside = [line for line in record["time_spent"] if line.startswith("outside helpers")]
    assert outside and float(outside[0].split(": ")[1].rstrip("s")) >= 0.4  # the sleep is not hidden
    assert "over_budget" not in record


def test_a_failed_run_still_reports_its_time(engine, phone):
    phone.set_frames([{"app": "com.example.app", "texts": ["Elsewhere"]}])
    record = _run(engine, "checkpoint('ready', text='Ready', timeout=0.5)\n")
    assert record["ok"] is False
    assert _helpers(record)[0] == "checkpoint"


def test_a_helper_over_its_budget_is_named(engine):
    record = {"routine": NAME, "run_id": "r1", "duration_s": 5.0}
    engine._time_spent(record, {
        "find_image": {"calls": 1, "total_s": 3.5, "max_s": 3.5, "budget_s": 1.5},
        "checkpoint": {"calls": 2, "total_s": 1.0, "max_s": 0.6},
    })
    assert record["time_spent"][0] == "find_image: 3.50s (1 call)"
    assert record["over_budget"] == ["find_image took up to 3.50s per call; it should answer within 1.5s"]


def test_malformed_cost_lines_from_script_output_are_ignored(engine, phone):
    phone.set_frames([{"app": "com.example.app", "texts": []}])
    record = _run(engine, "import atexit\n"
                          "atexit.register(lambda: print("
                          "'@@AUTOPILOT {\"event\": \"cost\", \"helpers\": {\"x\": {\"calls\": \"many\"}}}'))\n"
                          "done()\n")
    assert record["ok"] is True
    assert "time_spent" not in record or not any(line.startswith("x:") for line in record["time_spent"])


def test_image_search_on_a_phone_sized_screen_fits_find_image_budget():
    """The pure-CPU part of find_image, for a real 300px button ref; screen capture adds ~0.7s on top."""
    import time
    from pathlib import Path

    from PIL import Image

    ns = {}
    exec(compile((Path(__file__).resolve().parent.parent / "prelude.py").read_text(), "prelude.py", "exec"), ns)
    ref = Image.open(Path(__file__).resolve().parent / "assets" / "button-ref.png").convert("RGB")
    screen = Image.effect_noise((2340, 1080), 40).convert("RGB")
    screen.paste(ref, (1301, 517))
    started = time.monotonic()
    score, x, y = ns["_locate"](screen, ref)
    spent = time.monotonic() - started
    assert (x, y) == (1301, 517) and score > 0.99
    assert spent < ns["_BUDGET_S"]["find_image"] - 0.7, f"image search took {spent:.2f}s"


def test_the_guard_thread_polling_helpers_does_not_hide_the_routine_calls(engine, phone):
    phone.set_frames([{"app": "com.example.app", "texts": []}])
    record = _run(engine, "import threading, time\n"
                          "def poll():\n"
                          "    end = time.time() + 1.0\n"
                          "    while time.time() < end:\n"
                          "        current_app()\n"
                          "threading.Thread(target=poll, daemon=True).start()\n"
                          "for _ in range(20):\n    shell('true')\n    time.sleep(0.02)\n"
                          "done()\n")
    assert any(line.startswith("shell: ") and "20 calls" in line for line in record["time_spent"])
    assert not any(line.startswith("current_app") for line in record["time_spent"])
