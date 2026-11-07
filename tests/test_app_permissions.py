"""Asking the user for apps: requests queue when the guard stops, and only a person grants them."""
from __future__ import annotations

import importlib
import json
import types

import pytest

from test_commands import _load_plugin

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PREFIX = "/api/plugins/phone-autopilot"
DIALOG = "com.google.android.permissioncontroller"


def _store_allowlist(module, monkeypatch, initial=()):
    """The allowlist as a list: config.yaml's read, and the settings writer a surface passes in."""
    store = list(initial)
    monkeypatch.setattr(module, "_settings", lambda: {"allowed_apps": list(store)})
    events = []
    monkeypatch.setattr(module, "_broadcast", lambda event, payload: events.append((event, payload)))
    return store, events


@pytest.fixture
def allowlist(engine, monkeypatch):
    store, events = _store_allowlist(engine, monkeypatch, ["com.example.app"])
    return types.SimpleNamespace(apps=store, events=events,
                                 save=lambda apps: store.__setitem__(slice(None), apps))


def _guarded_run(engine, phone, foreign_app):
    phone.set_frames([{"app": "com.example.app", "texts": ["Home"]}] * 3
                     + [{"app": foreign_app, "texts": []}])
    meta = engine.save_routine("deep-work", "import time\nfor _ in range(40):\n    screen()\n    time.sleep(0.1)\n")
    engine.approve("deep-work", meta["sha256"])
    return engine.run_routine("deep-work", timeout_s=30)


def test_a_blocked_app_becomes_a_request_and_tells_the_agent_how_to_get_it(engine, phone, allowlist):
    record = _guarded_run(engine, phone, "com.android.settings")
    assert record["ok"] is False
    assert "/routines allow com.android.settings" in record["error"]
    [request] = engine.app_requests()
    assert request | {"at": 0} == {"app": "com.android.settings", "kind": "app", "source": "routine",
                                   "routine": "deep-work", "at": 0, "waiting": False}
    assert ("apps.requested", request) in allowlist.events


def test_the_permission_dialog_is_left_for_the_user_and_never_allowlisted(engine, phone, allowlist):
    record = _guarded_run(engine, phone, DIALOG)
    assert record["ok"] is False
    assert "must answer that dialog on the phone" in record["error"]
    assert "SEND nav.home" not in record["output_tail"]  # pressing Home would dismiss the dialog
    assert engine.app_requests()[0]["kind"] == "permission"
    with pytest.raises(engine.RoutineError, match="answered on the phone"):
        engine.allow_app(DIALOG, allowlist.save)
    assert DIALOG not in allowlist.apps


def test_allowing_saves_the_app_and_clears_its_request(engine, allowlist):
    engine.note_blocked_app("com.spotify.music", source="phone_exec")
    assert engine.allow_app("com.spotify.music", allowlist.save) == ["com.example.app", "com.spotify.music"]
    assert allowlist.apps == ["com.example.app", "com.spotify.music"]
    assert engine.app_requests() == []
    assert engine.note_blocked_app("com.spotify.music", source="phone_exec") is None


@pytest.mark.parametrize("bad", ["", "spotify", "com.spotify.music; rm -rf /", "../etc", "com..x"])
def test_only_package_names_can_be_allowed(engine, allowlist, bad):
    with pytest.raises(engine.RoutineError, match="package name"):
        engine.allow_app(bad, allowlist.save)
    assert allowlist.apps == ["com.example.app"]


def test_revoke_and_dismiss(engine, allowlist):
    engine.revoke_app("com.example.app", allowlist.save)
    assert allowlist.apps == []
    engine.note_blocked_app("com.other.app", source="phone_exec")
    engine.dismiss_app_request("com.other.app")
    assert engine.app_requests() == []
    assert allowlist.apps == []


# --- surfaces: phone_exec hook, slash command, pane API ----------------------

@pytest.fixture
def plugin(tmp_path, monkeypatch):
    _load_plugin()
    eng = importlib.import_module("phone_autopilot.engine")
    monkeypatch.setattr(eng, "data_dir", lambda: tmp_path / "pdata")
    store, _ = _store_allowlist(eng, monkeypatch)
    return types.SimpleNamespace(engine=eng, apps=store, tool=importlib.import_module("phone_autopilot.tool"),
                                 commands=importlib.import_module("phone_autopilot.commands"))


def test_a_guarded_phone_exec_result_queues_a_request(plugin):
    output = 'hi\n@@AUTOPILOT {"event": "guard", "status": "blocked", "app": "com.android.settings"}\n'
    plugin.tool.on_post_tool_call(tool_name="phone_exec", result=json.dumps({"success": False, "output": output}))
    plugin.tool.on_post_tool_call(tool_name="terminal", result=output)
    assert [r["app"] for r in plugin.engine.app_requests()] == ["com.android.settings"]


def test_slash_command_grants_lists_and_revokes(plugin):
    def handle(raw):
        return plugin.commands.handle_routines(
            raw, set_config=lambda key, apps: plugin.apps.__setitem__(slice(None), apps))
    assert handle("allow com.spotify.music").startswith("Allowed com.spotify.music")
    assert plugin.apps == ["com.spotify.music"]
    assert "com.spotify.music" in handle("apps")
    assert handle("allow not-a-package").startswith("Error:")
    handle("revoke com.spotify.music")
    assert plugin.apps == []


def test_the_routine_tool_has_no_way_to_grant_an_app(plugin):
    actions = plugin.tool.SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert not any("allow" in a or "app" in a for a in actions)


def test_pane_api_lists_and_dismisses_and_an_allowed_app_stops_asking(engine, allowlist):
    from test_plugin_api import API_PATH
    spec = importlib.util.spec_from_file_location("phone_autopilot_api_apps", API_PATH)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    api._engine_mod = engine
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    client = TestClient(app)
    engine.note_blocked_app("com.spotify.music", source="routine", routine="deep-work")
    engine.note_blocked_app("com.other.app", source="phone_exec")
    body = client.get(f"{PREFIX}/apps").json()
    assert [r["app"] for r in body["requests"]] == ["com.other.app", "com.spotify.music"]
    # The pane allows through Hermes's plugin settings call; here the Settings form did it.
    allowlist.apps.append("com.spotify.music")
    assert [r["app"] for r in client.get(f"{PREFIX}/apps").json()["requests"]] == ["com.other.app"]
    assert client.post(f"{PREFIX}/apps/dismiss", json={"app": "com.other.app"}).json() == {"ok": True}
    assert client.get(f"{PREFIX}/apps").json() == {"ok": True, "allowed": ["com.example.app", "com.spotify.music"],
                                                    "requests": []}
    assert client.post(f"{PREFIX}/apps/allow", json={"app": "com.x.y"}).status_code in (404, 405)
