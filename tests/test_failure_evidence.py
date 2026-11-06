"""A routine that fails anywhere, not only at a checkpoint, leaves what a repair needs."""
from __future__ import annotations

NAME = "fails"


def _run(engine, code):
    meta = engine.save_routine(NAME, code)
    engine.approve(NAME, meta["sha256"])
    return engine.run_routine(NAME, timeout_s=30)


def test_a_helper_that_raises_is_recorded_like_a_missed_checkpoint(engine, phone):
    phone.set_frames([{"app": "com.example.app", "texts": ["Something went wrong.", "Try again"]}])
    record = _run(engine, "checkpoint('open', app='com.example.app')\n"
                          "hits = find_text('Jazz for Study')\n"
                          "hits[1]\n"
                          "done()\n")
    assert record["ok"] is False
    failed = record["failed_checkpoint"]
    assert failed["name"] == "line 3: hits[1]"
    assert failed["raised"] == "IndexError: list index out of range"
    assert "Try again" in failed["seen_text"]
    assert failed["screenshot"]
    assert record["error"] == "line 3: hits[1] raised IndexError: list index out of range"
    assert not any(e["event"] == "done" for e in record["events"])


def test_a_checkpoint_miss_is_reported_once_and_unchanged(engine, phone):
    phone.set_frames([{"app": "com.example.app", "texts": ["Elsewhere"]}])
    record = _run(engine, "checkpoint('ready', text='Ready', timeout=0.3)\n")
    failed = [e for e in record["events"] if e.get("status") == "failed"]
    assert [e["name"] for e in failed] == ["ready"]
    assert record["error"] == "checkpoint 'ready' failed"
