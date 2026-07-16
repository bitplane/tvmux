"""Fast, stdlib-only hook poster for tmux callbacks.

tmux hooks fire this via ``python -m tvmux.hook`` on every pane switch, so it
must start fast: no click, no FastAPI, no pydantic — just stdlib. Values are
passed by the server-installed hook command using tmux ``#{q:...}`` quoting.

Always exits 0 so a dead server never makes tmux print errors.
"""
import json
import sys
from http.client import HTTPConnection

DEFAULT_PORT = 21590
SERVER_HOST = "127.0.0.1"
TIMEOUT = 2.0

# CLI flag -> HookEvent field
FLAGS = {
    "--session-name": "session_name",
    "--window-id": "window_id",
    "--pane-id": "pane_id",
    "--window-index": "window_index",
    "--pane-index": "pane_index",
}


def build_payload(argv: list[str]) -> tuple[int, dict]:
    """Parse argv (after the program name) into (port, HookEvent payload).

    The first positional argument is the hook name. Flags use the
    ``--flag=value`` form so that empty tmux expansions can't shift
    arguments around.
    """
    port = DEFAULT_PORT
    payload: dict = {"hook_name": None, "extra": {}}

    for arg in argv:
        if "=" in arg and arg.startswith("--"):
            flag, _, value = arg.partition("=")
            if flag == "--port":
                port = int(value)
            elif flag in FLAGS:
                payload[FLAGS[flag]] = value or None
        elif not arg.startswith("--"):
            payload["hook_name"] = arg

    return port, payload


def post_hook(port: int, payload: dict) -> None:
    """POST the hook event to the local tvmux server."""
    conn = HTTPConnection(SERVER_HOST, port, timeout=TIMEOUT)
    try:
        conn.request(
            "POST",
            "/hook",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        conn.getresponse().read()
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    try:
        port, payload = build_payload(argv)
        if not payload.get("hook_name"):
            return 0
        post_hook(port, payload)
    except Exception:
        # Server down or mid-restart; hooks must never bother tmux.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
