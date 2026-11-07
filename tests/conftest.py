"""Shared fixtures: engine loaded by path, a throwaway data dir, and a scripted fake phone."""
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
FAKE_PH_DIR = Path(__file__).resolve().parent / "fake_ph"


@pytest.fixture
def engine(monkeypatch, tmp_path):
    """A fresh engine module (fresh run lock) with all data under tmp_path."""
    spec = importlib.util.spec_from_file_location("phone_autopilot_engine", PLUGIN_DIR / "engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Hermes's own seams (the profile's plugin-data folder and config.yaml), outside Hermes.
    monkeypatch.setattr(module, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(module, "configured_allowed_apps", lambda: [])
    return module


@pytest.fixture
def data_dir(engine, tmp_path):
    return tmp_path / "data"


class FakePhone:
    """Scripted screens for the fake phone_harness; the child re-reads the file on each call."""

    def __init__(self, path: Path, shot_dir: Path):
        self.path = path
        self.shot_dir = shot_dir
        self.set_frames([{"app": "com.example.app", "texts": []}])

    def set_frames(self, frames):
        self.path.write_text(json.dumps({"shot_dir": str(self.shot_dir), "frames": frames}),
                             encoding="utf-8")


@pytest.fixture
def phone(tmp_path, monkeypatch):
    fake = FakePhone(tmp_path / "screen.json", tmp_path / "shots")
    monkeypatch.setenv("FAKE_PH_SCREEN", str(fake.path))
    monkeypatch.setenv("PYTHONPATH", str(FAKE_PH_DIR))
    return fake
