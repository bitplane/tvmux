"""Recording model for tvmux."""
import asyncio
import errno
import fcntl
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict

from ..utils import get_session_dir, safe_filename, file_has_readers
from ..repair import repair_cast_file
from ..proc import run_bg
from ..proc import bg
from ..config import get_config

logger = logging.getLogger(__name__)


class Recording(BaseModel):
    """A tmux recording session."""

    # Public API fields
    id: str = Field(..., description="Recording ID (session:window)")
    session_id: str = Field(..., description="Session ID")
    window_id: str = Field(..., description="Window ID")
    active: bool = Field(False, description="Is recording active")
    cast_path: Optional[str] = Field(None, description="Path to cast file")
    active_pane: Optional[str] = Field(None, description="Currently recording pane")

    # Internal fields (excluded from API responses)
    output_dir: Optional[Path] = Field(None, exclude=True, alias="_output_dir")
    hostname: Optional[str] = Field(None, exclude=True, alias="_hostname")
    session_dir: Optional[Path] = Field(None, exclude=True, alias="_session_dir")
    fifo_path: Optional[Path] = Field(None, exclude=True, alias="_fifo_path")
    asciinema_pid: Optional[int] = Field(None, exclude=True, alias="_asciinema_pid")
    running: bool = Field(False, exclude=True, alias="_running")

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, **data):
        super().__init__(**data)

        # Initialize internal fields if needed
        if self.session_id and self.window_id and not self.hostname:
            self.hostname = os.uname().nodename
            self.session_dir = get_session_dir(
                self.hostname,
                self.session_id,
                os.environ.get("TMUX", ""),
                base_dir=f"/tmp/tvmux-{os.getenv('USER', 'nobody')}/sessions"
            )
            self.session_dir.mkdir(parents=True, exist_ok=True)

    async def start(self, active_pane: str, output_dir: Path):
        """Start recording this window."""
        if self.active:
            raise ValueError(f"Already recording {self.id}")

        self.output_dir = output_dir
        self.active_pane = active_pane

        # Create FIFO
        safe_window_id = safe_filename(self.window_id)
        self.fifo_path = self.session_dir / f"window_{safe_window_id}.fifo"
        if self.fifo_path.exists():
            self.fifo_path.unlink()
        os.mkfifo(self.fifo_path)

        # Create output directory with date
        config = get_config()
        date_dir = output_dir / datetime.now().strftime(config.output.date_format)
        date_dir.mkdir(parents=True, exist_ok=True)

        # Generate cast filename: timestamp_hostname_session_windowid_windowname
        # (window_id keeps names unique when two windows share a name)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        display_name = self._get_display_name()
        safe_window_name = safe_filename(display_name)
        cast_filename = (
            f"{timestamp}_{safe_filename(self.hostname)}_{safe_filename(self.session_id)}"
            f"_{safe_filename(self.window_id)}_{safe_window_name}.cast"
        )
        cast_path = date_dir / cast_filename
        self.cast_path = str(cast_path)

        # Start asciinema process
        await self._start_asciinema()

        # Wait for asciinema to be ready
        if not await self._wait_for_reader():
            raise RuntimeError("Asciinema reader not ready")

        self.active = True
        self._dump_pane(active_pane)
        self._start_streaming(active_pane)
        logger.info(f"Started recording window {self.window_id} to {cast_path}")

    def switch_pane(self, new_pane_id: str):
        """Switch recording to a different pane in the window."""
        if not self.active:
            logger.warning(f"Window {self.window_id} not recording")
            return

        if self.active_pane == new_pane_id:
            logger.debug(f"Already recording pane {new_pane_id}")
            return

        logger.info(f"Switching from pane {self.active_pane} to {new_pane_id} in window {self.window_id}")

        # Stop streaming current pane
        self._stop_streaming()

        # Dump new pane state (includes reset)
        self._dump_pane(new_pane_id)

        # Start streaming new pane
        self._start_streaming(new_pane_id)

        self.active_pane = new_pane_id

        # Send SIGWINCH to the new pane's process group to trigger resize handling
        self._send_sigwinch(new_pane_id)

    def _open_fifo_write(self, timeout: float = 2.0):
        """Open the FIFO for writing without risking an indefinite hang.

        A plain open(fifo, "w") blocks until a reader appears; if asciinema
        died that blocks the server's event loop forever. Open with
        O_NONBLOCK to fail fast when there's no reader, then restore
        blocking mode so normal writes behave as before.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as e:
                if e.errno != errno.ENXIO or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        return os.fdopen(fd, "w")

    def _send_sigwinch(self, pane_id: str):
        """Send SIGWINCH signal to asciinema process to handle terminal resize in recording."""
        if not self.asciinema_pid:
            logger.warning("No asciinema process to send SIGWINCH to")
            return

        try:
            subprocess.run([
                "kill", "-SIGWINCH", str(self.asciinema_pid)
            ], check=True)
            logger.debug(f"Sent SIGWINCH to asciinema process {self.asciinema_pid} for pane {pane_id}")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to send SIGWINCH to asciinema process {self.asciinema_pid}: {e}")

    def stop(self):
        """Stop recording."""
        if not self.active:
            return

        logger.info(f"Stopping recording for window {self.window_id}")

        # Stop streaming
        self._stop_streaming()

        # Send final reset sequence and close FIFO
        self._write_reset_sequence()

        # Close the FIFO by writing EOF to it - this will cause tail -f to exit
        try:
            # Write EOF to the FIFO to signal end of data
            with self._open_fifo_write(timeout=0.5):
                pass  # Just opening and closing sends EOF
        except (OSError, IOError):
            pass  # No reader left - asciinema is already gone

        # Stop asciinema
        if self.asciinema_pid:
            logger.debug(f"Terminating asciinema process tree: {self.asciinema_pid}")
            if not bg.terminate(self.asciinema_pid):
                logger.warning(f"Failed to terminate asciinema process tree: {self.asciinema_pid}")

        # Clean up FIFO
        if self.fifo_path and self.fifo_path.exists():
            self.fifo_path.unlink()

        # Repair cast file if configured
        config = get_config()
        if self.cast_path and config.recording.repair_on_stop:
            repair_cast_file(Path(self.cast_path))

        self.active = False
        logger.info(f"Stopped recording for window {self.window_id}")

    def _get_display_name(self) -> str:
        """Get friendly display name for this window."""
        try:
            result = subprocess.run([
                "tmux", "display-message", "-t", f"{self.session_id}:{self.window_id}",
                "-p", "#{window_name}"
            ], capture_output=True, text=True)

            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return self.window_id
        except Exception:
            return self.window_id

    async def _start_asciinema(self):
        """Start asciinema process."""
        # asciinema is a Python dependency; run it via our own interpreter so
        # it works even when the venv's bin dir isn't on PATH
        cmd = [
            sys.executable, "-m", "asciinema", "rec", "--stdin", "--quiet", "--overwrite",
            str(self.cast_path), "--command", f"stdbuf -o0 tail -f {self.fifo_path}"
        ]

        proc = await run_bg(cmd)
        self.asciinema_pid = proc.pid
        logger.info(f"Started asciinema process: {self.asciinema_pid}")

    async def _wait_for_reader(self) -> bool:
        """Wait for asciinema to open the FIFO for reading."""
        for _ in range(100):  # Wait up to 10 seconds
            if file_has_readers(self.fifo_path):
                return True
            await asyncio.sleep(0.1)
        return False

    def _dump_pane(self, pane_id: str):
        """Dump current pane content with proper terminal state handling."""
        try:
            pane_target = f"{self.session_id}:{self.window_id}.{pane_id}"

            # Get all terminal state info in one call
            state_result = subprocess.run([
                "tmux", "display-message", "-t", pane_target,
                "-p", "#{pane_width},#{pane_height},#{cursor_x},#{cursor_y},#{cursor_flag},#{alternate_on},#{alternate_saved_x},#{alternate_saved_y},#{scroll_region_upper},#{scroll_region_lower},#{pane_title}"
            ], capture_output=True, text=True)

            if state_result.returncode != 0:
                logger.warning(f"Failed to get pane state for {pane_id}")
                return

            try:
                state_parts = state_result.stdout.strip().split(',')
                pane_width, pane_height = int(state_parts[0]), int(state_parts[1])
                cursor_x, cursor_y, cursor_flag = int(state_parts[2]), int(state_parts[3]), int(state_parts[4])
                alternate_on = int(state_parts[5]) == 1
                alt_saved_x, alt_saved_y = int(state_parts[6]), int(state_parts[7])
                scroll_upper, scroll_lower = int(state_parts[8]), int(state_parts[9])
                pane_title = state_parts[10] if len(state_parts) > 10 else ""
            except (ValueError, IndexError) as e:
                logger.warning(f"Failed to parse pane state: {state_result.stdout} - {e}")
                return

            # Phase 1: Write reset sequences and close
            with self._open_fifo_write() as f:
                # 1. Full terminal reset first
                f.write("\033c")         # Full terminal reset (ESC c)

                # 2. Set terminal size
                f.write(f"\033[8;{pane_height};{pane_width}t")  # Set window size

                # 3. Ensure we're in normal screen buffer and clear
                f.write("\033[?1049l")   # Ensure we're in normal screen buffer
                f.write("\033[2J\033[H") # Clear screen and move cursor to home

                f.flush()
            # File handle closed, EOF sent to tail

            # Phase 2: Let tmux write normal screen content directly to FIFO
            with self._open_fifo_write() as f:
                subprocess.run([
                    "tmux", "capture-pane", "-t", pane_target, "-e", "-p"
                ], stdout=f, check=False)
            # tmux writes content, we close (EOF sent)

            # Phase 3: Handle alternate screen if needed
            if alternate_on:
                with self._open_fifo_write() as f:
                    f.write("\033[?1049h")   # Switch to alternate screen buffer
                    f.write("\033[2J\033[H") # Clear alternate screen
                    f.flush()
                # File closed, EOF sent

                # Let tmux write alternate screen content
                with self._open_fifo_write() as f:
                    subprocess.run([
                        "tmux", "capture-pane", "-t", pane_target, "-a", "-e", "-p"
                    ], stdout=f, check=False)
                # tmux writes alt content, we close (EOF sent)

                # Update cursor position for alt screen
                cursor_x, cursor_y = alt_saved_x, alt_saved_y

            # Phase 4: Write final terminal setup and close
            with self._open_fifo_write() as f:
                # 5. Set scroll region if not full screen
                if scroll_upper > 0 or scroll_lower < pane_height - 1:
                    # Convert to 1-based for terminal escape sequence
                    f.write(f"\033[{scroll_upper + 1};{scroll_lower + 1}r")

                # 6. Set terminal modes
                f.write("\033[?1000h")   # Enable mouse reporting
                f.write("\033[?1002h")   # Enable button event mouse reporting
                f.write("\033[?1006h")   # Enable SGR extended mouse reporting
                f.write("\033[?7h")      # Enable auto-wrap mode

                # 7. Set window title
                if pane_title:
                    f.write(f"\033]0;{pane_title}\007")

                # 8. Set cursor position and visibility
                config = get_config()
                if config.annotations.include_cursor_state:
                    # Position cursor (convert to 1-based)
                    row = cursor_y + 1
                    col = cursor_x + 1
                    f.write(f"\033[{row};{col}H")

                    # Set cursor visibility
                    if cursor_flag == 1:
                        f.write("\033[?25h")  # Show cursor
                    else:
                        f.write("\033[?25l")  # Hide cursor

                f.flush()
            # Final file handle closed, EOF sent

        except Exception as e:
            logger.warning(f"Failed to dump pane {pane_id}: {e}")

    def _start_streaming(self, pane_id: str):
        """Start streaming pane output."""
        try:
            subprocess.run([
                "tmux", "pipe-pane", "-t", f"{self.session_id}:{self.window_id}.{pane_id}",
                f"stdbuf -o0 cat >> {self.fifo_path}"
            ], check=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to start streaming for pane {pane_id}: {e}")

    def _stop_streaming(self):
        """Stop streaming current pane."""
        if not self.active_pane:
            return

        try:
            subprocess.run([
                "tmux", "pipe-pane", "-t", f"{self.session_id}:{self.window_id}.{self.active_pane}"
            ], check=True)
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to stop streaming: {e}")

    def _write_reset_sequence(self):
        """Write terminal reset sequence to return to known state."""
        try:
            with self._open_fifo_write(timeout=0.5) as f:
                # 1. Disable alt mode (return to main buffer)
                f.write("\033[?1049l")  # Disable alternate screen buffer
                # 2. Clear the screen
                f.write("\033[2J\033[H")  # Clear screen and move cursor to home
                f.flush()
        except Exception as e:
            logger.warning(f"Failed to write reset sequence: {e}")
