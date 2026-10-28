"""Exercise hook ownership against an isolated tmux server."""
import shutil
import subprocess

import pytest

from tvmux.server.routers import callbacks


@pytest.fixture
def tmux(tmp_path, monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket = str(tmp_path / "tmux.sock")

    def run(*args, **kwargs):
        return subprocess.run(
            ["tmux", "-S", socket, *args], capture_output=True, text=True,
            timeout=5, **kwargs,
        )

    run("-f", "/dev/null", "new-session", "-d", "-s", "review", check=True)
    monkeypatch.setattr(callbacks.proc, "run", lambda cmd, **kw: run(*cmd[1:], **kw))
    try:
        yield run
    finally:
        run("kill-server")


def test_install_and_cleanup_preserve_other_hooks(tmux):
    name = "after-select-pane"
    tmux("set-hook", "-g", f"{name}[0]", 'display-message "user hook"', check=True)
    tmux("set-hook", "-g", f"{name}[21590]", 'display-message "plugin hook"', check=True)
    before = tmux("show-hooks", "-g", name).stdout

    hook = callbacks.Hook(name=name, command="printf '%s' 'custom command'")
    callbacks.install_hook(hook)
    callbacks.install_hook(hook)  # Reinstall replaces our entry, never duplicates it.
    installed = tmux("show-hooks", "-g", name).stdout
    assert installed.count(callbacks.HOOK_MARKER) == 1
    assert f"{name}[21591]" in installed

    callbacks.uninstall_hook(name)
    assert tmux("show-hooks", "-g", name).stdout == before


def test_startup_removes_legacy_hooks_only(tmux):
    tmux("set-hook", "-g", "after-select-pane[0]",
         'run-shell "python -m tvmux.hook after-select-pane"', check=True)
    tmux("set-hook", "-g", "after-select-pane[1]", 'display-message "user hook"', check=True)
    tmux("set-hook", "-g", "client-attached[0]", 'display-message "plugin hook"', check=True)
    before = tmux("show-hooks", "-g", "client-attached").stdout

    callbacks.remove_known_hooks()
    selected = tmux("show-hooks", "-g", "after-select-pane").stdout
    assert "tvmux.hook" not in selected
    assert "user hook" in selected
    assert tmux("show-hooks", "-g", "client-attached").stdout == before


def test_hook_lifecycle_tolerates_tmux_not_running(tmux, monkeypatch):
    monkeypatch.setattr(callbacks, "installed_hooks", {})
    callbacks.setup_default_hooks()
    assert callbacks.installed_hooks
    tmux("kill-server", check=True)

    # Shutdown after the last tmux session closes must still complete.
    callbacks.remove_all_hooks()
    assert not callbacks.installed_hooks
    # Startup outside tmux must not fail or claim hooks were installed.
    callbacks.remove_known_hooks()
    callbacks.setup_default_hooks()
    assert not callbacks.installed_hooks


def test_native_pane_hook_follows_split_kill_and_shell_exit(tmux, monkeypatch):
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from queue import Queue
    from unittest.mock import Mock

    from tvmux.server.routers import hook
    from tvmux.server.state import recorders

    events = Queue()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            events.put(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    recorder = Mock()
    recorders["review:@0"] = recorder
    monkeypatch.setattr(hook, "cleanup_closed_windows", lambda: None)
    monkeypatch.setattr(callbacks, "get_default_command",
                        lambda name: callbacks.build_hook_command(name, server.server_port))
    try:
        # Keep a different window selected to verify the event target is used.
        tmux("new-window", "-t", "review", "-n", "other", check=True)
        callbacks.install_hook(callbacks.Hook(name="window-pane-changed"))

        def assert_followed():
            event = hook.HookEvent(**events.get(timeout=5))
            expected = tmux("display-message", "-p", "-t", "review:@0", "#{pane_id}").stdout.strip()
            assert event.session_name == "review"
            assert event.window_id == "@0"
            assert event.pane_id == expected
            asyncio.run(hook._process_hook_event(event))
            recorder.switch_pane.assert_called_with(expected)

        tmux("split-window", "-t", "review:@0", check=True)
        assert_followed()
        tmux("kill-pane", "-t", "review:@0", check=True)
        assert_followed()
        tmux("split-window", "-t", "review:@0", check=True)
        assert_followed()
        tmux("send-keys", "-t", "review:@0", "exit", "Enter", check=True)
        assert_followed()
        assert recorder.switch_pane.call_count == 4
    finally:
        callbacks.uninstall_hook("window-pane-changed")
        recorders.clear()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
