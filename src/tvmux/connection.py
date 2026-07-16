"""Connection to tvmux server."""
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from .server.state import SERVER_HOST
from .utils import safe_filename
from .config import get_config


class Connection:
    """Manages connection to tvmux server."""

    def __init__(self):
        self.user = os.getenv("USER", "nobody")
        self.server_dir = Path(f"/tmp/tvmux-{safe_filename(self.user)}")
        self.pid_file = self.server_dir / "server.pid"
        self.server_host = SERVER_HOST

        # Use configured port
        config = get_config()
        self.server_port = config.server.port
        self.base_url = f"http://{SERVER_HOST}:{self.server_port}"

    @property
    def server_pid(self) -> Optional[int]:
        """Get server PID if running."""
        try:
            if self.pid_file.exists():
                pid = int(self.pid_file.read_text().strip())
                # Check if process is actually running
                os.kill(pid, 0)
                return pid
        except (ValueError, ProcessLookupError, FileNotFoundError):
            pass
        return None

    @property
    def is_running(self) -> bool:
        """Check if server is running."""
        if self.server_pid is None:
            return False

        # Try to connect to the server
        try:
            response = httpx.get(f"{self.base_url}/", timeout=1.0, follow_redirects=True)
            return response.status_code == 200
        except (httpx.RequestError, httpx.TimeoutException):
            return False

    def start(self) -> bool:
        if self.is_running:
            print(f"Server already running (PID: {self.server_pid})")
            return True

        # Create server directory
        self.server_dir.mkdir(exist_ok=True)

        # Log file for debugging
        log_file = self.server_dir / "server.log"

        # Start server in background with logging; sys.executable rather than
        # bare "python", which may not exist on PATH or point elsewhere
        with open(log_file, "w") as log:
            subprocess.Popen(
                [sys.executable, "-m", "tvmux.server.main"],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True
            )

        # Wait for server to start
        for _ in range(30):  # 3 seconds
            if self.is_running:
                print(f"Server started (PID: {self.server_pid})")
                return True
            time.sleep(0.1)

        # Server failed to start - show the error
        print("Failed to start server")
        if log_file.exists():
            print("\nServer log:")
            print(log_file.read_text())
        return False

    def stop(self) -> bool:
        pid = self.server_pid
        if not pid:
            print("Server not running")
            return True

        try:
            # Send SIGTERM
            os.kill(pid, 15)

            # Wait for graceful shutdown
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    break
            else:
                # Force kill if still running
                os.kill(pid, 9)

            print(f"Server stopped (PID: {pid})")
            return True

        except ProcessLookupError:
            print("Server already stopped")
            return True
        except Exception as e:
            print(f"Error stopping server: {e}")
            return False

    def client(self) -> httpx.Client:
        if not self.is_running:
            raise RuntimeError("Server not running")

        return httpx.Client(base_url=self.base_url, follow_redirects=True)


    def api(self):
        """Get API client with typed methods matching the server routes."""
        if not self.is_running:
            raise RuntimeError("Server not running")

        # Just return the httpx client - we'll use it directly with the API
        # The TestClient doesn't actually give us Python methods, it's still HTTP
        return self.client()
