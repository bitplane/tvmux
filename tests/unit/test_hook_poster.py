"""Tests for the lightweight tmux hook poster (tvmux.hook)."""
import subprocess
import sys

from tvmux.hook import DEFAULT_PORT, build_payload, main


def test_build_payload_parses_all_fields():
    port, payload = build_payload([
        "after-select-pane",
        "--port=12345",
        "--session-name=main",
        "--window-id=@1",
        "--pane-id=%3",
        "--window-index=0",
        "--pane-index=1",
    ])

    assert port == 12345
    assert payload["hook_name"] == "after-select-pane"
    assert payload["session_name"] == "main"
    assert payload["window_id"] == "@1"
    assert payload["pane_id"] == "%3"
    assert payload["window_index"] == "0"
    assert payload["pane_index"] == "1"


def test_build_payload_defaults():
    port, payload = build_payload(["session-closed"])

    assert port == DEFAULT_PORT
    assert payload["hook_name"] == "session-closed"
    assert payload["extra"] == {}


def test_build_payload_empty_expansions_become_none():
    # tmux expands e.g. #{q:session_name} to nothing once the session is gone
    _, payload = build_payload([
        "session-closed",
        "--session-name=",
        "--window-id=",
    ])

    assert payload["session_name"] is None
    assert payload["window_id"] is None


def test_build_payload_handles_names_with_special_chars():
    _, payload = build_payload([
        "after-select-pane",
        '--session-name=my session:with "quotes"',
    ])

    assert payload["session_name"] == 'my session:with "quotes"'


def test_main_returns_zero_when_server_down():
    # Port 1 should never have a listener; hooks must never fail loudly
    assert main(["after-select-pane", "--port=1", "--session-name=x"]) == 0


def test_main_returns_zero_without_hook_name():
    assert main([]) == 0


def test_hook_module_imports_stay_light():
    """tvmux.hook runs on every pane switch - it must not drag in the app.

    Guards against the 2.7s-per-hook regression where tmux hooks invoked the
    full CLI (click + FastAPI + textual).
    """
    check = (
        "import sys; import tvmux.hook; "
        "banned = {'click', 'fastapi', 'pydantic', 'textual', 'uvicorn', 'httpx', 'requests'}; "
        "loaded = banned & {m.split('.')[0] for m in sys.modules}; "
        "assert not loaded, f'heavy modules imported: {loaded}'"
    )
    subprocess.run([sys.executable, "-c", check], check=True)
