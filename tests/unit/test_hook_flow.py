"""Integration tests for the tmux hook flow: command building, installation
cleanup, and event handling through the FastAPI app."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tvmux.server.main import app
from tvmux.server.routers import callbacks
from tvmux.server.state import recorders


@pytest.fixture
def client():
    # No context manager: lifespan (which installs real tmux hooks) must not run
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_recorders():
    recorders.clear()
    yield
    recorders.clear()


@pytest.fixture(autouse=True)
def no_window_cleanup(monkeypatch):
    """Stub the tmux-backed window sweep; tests opt back in by re-patching."""
    monkeypatch.setattr("tvmux.server.routers.hook.cleanup_closed_windows", lambda: None)


class TestBuildHookCommand:
    def test_uses_fast_hook_module(self):
        cmd = callbacks.build_hook_command("after-select-pane", 21590)
        assert "-m tvmux.hook after-select-pane" in cmd
        assert "-m tvmux.cli.main" not in cmd

    def test_uses_tmux_quoting(self):
        cmd = callbacks.build_hook_command("after-select-pane", 21590)
        assert "--session-name=#{q:session_name}" in cmd
        assert "--window-id=#{q:window_id}" in cmd
        assert "--pane-id=#{q:pane_id}" in cmd
        assert '\\"' not in cmd  # old fragile escaping

    def test_embeds_port(self):
        cmd = callbacks.build_hook_command("session-closed", 12345)
        assert "--port=12345" in cmd


class TestRemoveKnownHooks:
    def test_unsets_every_known_hook_regardless_of_registry(self, monkeypatch):
        calls = []
        monkeypatch.setattr(callbacks.proc, "run", lambda cmd, **kw: calls.append(cmd))

        callbacks.installed_hooks.clear()  # simulates fresh start after a crash
        callbacks.remove_known_hooks()

        unset = {cmd[3] for cmd in calls if cmd[:3] == ["tmux", "set-hook", "-gu"]}
        assert unset == set(callbacks.AVAILABLE_HOOKS)


class TestHookEndpoint:
    def test_select_pane_switches_recording(self, client):
        recorder = MagicMock()
        recorders["main:@1"] = recorder

        resp = client.post("/hook", json={
            "hook_name": "after-select-pane",
            "session_name": "main",
            "window_id": "@1",
            "pane_id": "%5",
        })

        assert resp.status_code == 200
        assert resp.json()["action"] == "pane_switched"
        recorder.switch_pane.assert_called_once_with("%5")

    def test_select_pane_without_recording_is_noop(self, client):
        resp = client.post("/hook", json={
            "hook_name": "after-select-pane",
            "session_name": "main",
            "window_id": "@1",
            "pane_id": "%5",
        })

        assert resp.status_code == 200
        assert resp.json()["action"] == "pane_switched"

    def test_session_closed_stops_recordings(self, client):
        recorder = MagicMock()
        other = MagicMock()
        recorders["gone:@1"] = recorder
        recorders["alive:@2"] = other

        resp = client.post("/hook", json={
            "hook_name": "session-closed",
            "session_name": "gone",
        })

        assert resp.status_code == 200
        assert resp.json()["action"] == "session_destroyed"
        recorder.stop.assert_called_once()
        assert "gone:@1" not in recorders
        other.stop.assert_not_called()
        assert "alive:@2" in recorders

    def test_session_closed_with_empty_name_sweeps_windows(self, client, monkeypatch):
        # tmux often can't expand session_name once the session is gone; the
        # window sweep must still run so the recording gets cleaned up
        swept = []
        monkeypatch.setattr("tvmux.server.routers.hook.cleanup_closed_windows",
                            lambda: swept.append(True))

        resp = client.post("/hook", json={"hook_name": "session-closed"})

        assert resp.status_code == 200
        assert swept

    def test_unknown_hook_is_reported(self, client):
        resp = client.post("/hook", json={"hook_name": "made-up-hook"})

        assert resp.status_code == 200
        assert resp.json()["action"] == "unknown_hook_made-up-hook"
