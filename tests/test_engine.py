"""Engine behaviour against a fake phone-harness child process (no phone needed)."""
import json
import time
from pathlib import Path

import importlib.util

import pytest

NAME = "daily-claim"
APP = "com.example.app"
HOME_FRAME = {"app": APP, "texts": ["Home", "Claim"]}
ROUTINE_OK = """
step("open app")
checkpoint("home", text="Home", app="com.example.app", timeout=2)
print("claimed for", PARAMS["who"])
done(claimed=PARAMS["who"])
"""


def _approved(engine, code=ROUTINE_OK, name=NAME, **kw):
    meta = engine.save_routine(name, code, app=APP, params={"who": "ann"}, **kw)
    return engine.approve(name, meta["sha256"])


def test_save_bumps_version_and_hashes(engine, data_dir):
    first = engine.save_routine(NAME, "print(1)\n", title="Claim", params={"who": "ann"})
    second = engine.save_routine(NAME, "print(2)\n")
    assert (first["version"], second["version"]) == (1, 2)
    assert second["sha256"] == engine.code_sha256("print(2)\n")
    assert second["title"] == "Claim"
    assert second["params"] == {"who": "ann"}
    assert second["approved"] is False
    folder = data_dir / "routines" / NAME
    assert (folder / "versions" / "1.py").read_text() == "print(1)\n"
    assert (folder / "routine.py").read_text() == "print(2)\n"


def test_approve_requires_current_sha(engine):
    old = engine.save_routine(NAME, "print(1)\n")
    with pytest.raises(engine.RoutineError):
        engine.approve(NAME, "0" * 64)
    assert engine.approve(NAME, old["sha256"])["approved"] is True
    engine.save_routine(NAME, "print(2)\n")
    assert engine.get_routine(NAME)["approved"] is False
    with pytest.raises(engine.RoutineError):
        engine.approve(NAME, old["sha256"])  # the old hash is stale now


def test_unapproved_routine_does_not_run(engine):
    engine.save_routine(NAME, ROUTINE_OK)
    with pytest.raises(engine.RoutineError, match="not approved"):
        engine.run_routine(NAME)
    assert engine.list_runs(NAME) == []


def test_edit_on_disk_after_approval_is_refused(engine, data_dir):
    _approved(engine)
    (data_dir / "routines" / NAME / "routine.py").write_text("print('evil')\n")
    with pytest.raises(engine.RoutineError, match="edited on disk"):
        engine.run_routine(NAME)


def test_passing_run_streams_events_and_persists(engine, phone):
    phone.set_frames([HOME_FRAME])
    _approved(engine)
    seen = []

    def on_event(event):
        seen.append(event)
        raise RuntimeError("a broken UI callback must not stop the run")

    run = engine.run_routine(NAME, params={"who": "bob"}, on_event=on_event)

    assert run["ok"] is True and run["exit_code"] == 0 and run["error"] is None
    assert run["version"] == 1
    kinds = [e["event"] for e in run["events"]]
    assert kinds == ["step", "checkpoint", "checkpoint", "done"]
    assert [e.get("status") for e in run["events"] if e["event"] == "checkpoint"] == ["started", "passed"]
    assert all(isinstance(e["t"], float) for e in run["events"])
    assert [e["event"] for e in seen] == kinds  # the callback saw every event, live
    assert "claimed for bob" in run["output_tail"]
    assert engine.get_run(NAME, run["run_id"]) == run
    assert engine.list_runs(NAME)[0]["run_id"] == run["run_id"]
    assert engine.get_routine(NAME)["last_run"]["run_id"] == run["run_id"]
    assert engine.get_routine(NAME)["last_run"]["ok"] is True


def test_params_default_then_override(engine, phone):
    phone.set_frames([HOME_FRAME])
    _approved(engine)
    assert "claimed for ann" in engine.run_routine(NAME)["output_tail"]
    assert "claimed for zed" in engine.run_routine(NAME, params={"who": "zed"})["output_tail"]


def test_failed_checkpoint_records_expected_text_and_screenshot(engine, phone, data_dir):
    phone.set_frames([{"app": APP, "texts": ["Settings", "Storage"]}])
    _approved(engine, code='step("open")\ncheckpoint("reward", text="Claim", timeout=0.6)\n'
                           'print("never reached")\n')
    run = engine.run_routine(NAME)

    assert run["ok"] is False
    assert run["error"] == "checkpoint 'reward' failed"
    failed = run["failed_checkpoint"]
    assert failed["name"] == "reward"
    assert failed["expected"] == "Claim"
    assert "Settings" in failed["seen_text"]
    shot = Path(failed["screenshot"])
    assert shot == data_dir / "runs" / NAME / f"{run['run_id']}.png"
    assert shot.read_bytes().startswith(b"\x89PNG")
    assert "never reached" not in run["output_tail"]
    assert "CheckpointMiss" in run["stderr_tail"]


def test_timeout_kills_the_script(engine, phone):
    _approved(engine, code="time.sleep(60)\n")
    started = time.time()
    run = engine.run_routine(NAME, timeout_s=1)
    assert time.time() - started < 30
    assert run["ok"] is False
    assert run["error"] == "timed out after 1s"
    assert engine.get_routine(NAME)["last_run"]["ok"] is False


def test_child_env_drops_secrets(engine, phone, monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "sk-secret")
    _approved(engine, code='import os\nprint(f"key={os.environ.get(\'DEMO_API_KEY\')}")\n')
    assert "key=None" in engine.run_routine(NAME)["output_tail"]


def test_runs_are_pruned_to_fifty(engine, phone, data_dir):
    phone.set_frames([HOME_FRAME])
    _approved(engine)
    runs = data_dir / "runs" / NAME
    runs.mkdir(parents=True)
    for i in range(50):
        (runs / f"20000101-000000-{i:04x}.json").write_text(json.dumps({"run_id": f"old-{i}"}))
    run = engine.run_routine(NAME)
    ids = [r["run_id"] for r in engine.list_runs(NAME, limit=100)]
    assert len(ids) == 50
    assert run["run_id"] in ids
    assert not (runs / "20000101-000000-0000.json").exists()  # the oldest went first
    assert (runs / "20000101-000000-0031.json").exists()


def test_a_run_from_another_copy_of_the_engine_waits_then_reports_busy(engine, phone, monkeypatch):
    """The dashboard API loads its own engine module and cron runs elsewhere: one phone still."""
    monkeypatch.setattr(engine, "LOCK_WAIT_S", 0.2)
    _approved(engine)
    spec = importlib.util.spec_from_file_location("pocket_autopilot_engine_copy", engine.__file__)
    other = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(other)
    monkeypatch.setattr(other, "data_dir", engine.data_dir)
    with other._phone_lock(0):
        with pytest.raises(engine.RoutineError, match="busy"):
            engine.run_routine(NAME)
    assert "busy" not in (engine.run_routine(NAME)["error"] or "")  # free once the other run ends


@pytest.mark.parametrize("name", ["", "Daily", "-claim", "../evil", "a/b", "a b", "a.b", "x" * 49, "claim\n"])
def test_bad_names_are_rejected_before_any_write(engine, data_dir, name):
    with pytest.raises(ValueError):
        engine.validate_name(name)
    with pytest.raises(engine.RoutineError):
        engine.save_routine(name, "print(1)\n")
    assert not (data_dir / "routines").exists()


def test_max_length_name_is_accepted(engine):
    assert engine.validate_name("x" * 48) == "x" * 48


def test_list_and_delete(engine):
    engine.save_routine("a-routine", "print(1)\n")
    engine.save_routine("b-routine", "print(2)\n")
    _approved(engine, name="b-routine")
    listed = engine.list_routines()
    assert [m["name"] for m in listed] == ["a-routine", "b-routine"]
    assert [m["approved"] for m in listed] == [False, True]
    engine.delete_routine("a-routine")
    assert [m["name"] for m in engine.list_routines()] == ["b-routine"]
    with pytest.raises(engine.RoutineError):
        engine.get_routine("a-routine")


def test_done_summary_keeps_values_short(engine, phone):
    code = "done(media='x' * 5000, ok=True, playlist='Jazz for Study')\n"
    meta = engine.save_routine("dumpy", code)
    engine.approve("dumpy", meta["sha256"])
    record = engine.run_routine("dumpy", timeout_s=30)
    [done] = [e for e in record["events"] if e["event"] == "done"]
    assert done["ok"] is True and done["playlist"] == "Jazz for Study"
    assert len(done["media"]) < 400 and done["media"].endswith("(5000 chars)")
