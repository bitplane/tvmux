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
