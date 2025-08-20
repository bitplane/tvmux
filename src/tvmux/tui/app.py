"""Main TUI application with CRT TV interface."""
import logging
from pathlib import Path
from typing import Optional, List

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Header, Footer, Static, Label, ListView, ListItem
from textual.reactive import reactive
from textual.screen import Screen
from textual_asciinema import AsciinemaPlayer

from urllib.parse import quote
from ..connection import Connection
from ..config import get_config

logger = logging.getLogger(__name__)


def setup_client_logging():
    """Set up client-side logging."""
    try:
        config = get_config()
        log_level = config.logging.level.upper()
        client_log_file = config.logging.client_log_file

        # Configure handlers
        handlers = []

        # Add file handler if configured (TUI apps should only log to file)
        if client_log_file:
            from pathlib import Path
            log_path = Path(client_log_file).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_path))
        else:
            # If no file configured, use a null handler to avoid console spam
            handlers.append(logging.NullHandler())

        # Configure logging
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=handlers,
            force=True  # Override any existing config
        )

        logger.info("Client logging initialized")

    except Exception:
        # Fallback to basic console logging
        logging.basicConfig(level=logging.INFO)
        logger.exception("Failed to setup client logging, using defaults")


class ChannelTuner(Static):
    """TV channel tuner showing tmux windows as channels."""

    channels: reactive[list] = reactive([])
    selected_index: reactive[int] = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.connection = Connection()
        self.active_recordings = {}

    async def on_mount(self) -> None:
        """Load channels when widget mounts."""
        await self.refresh_channels()

    async def refresh_channels(self) -> None:
        """Refresh the list of available tmux windows/sessions."""
        logger.info("Refreshing channels...")
        try:
            self.channels = []

            logger.info(f"Connection is_running: {self.connection.is_running}")
            if self.connection.is_running:
                try:
                    client = self.connection.client()

                    # Get all sessions
                    logger.info("Making API call to /sessions/")
                    sessions_response = client.get("/sessions/")
                    logger.info(f"Sessions response: {sessions_response.status_code}")
                    if sessions_response.status_code == 200:
                        sessions = sessions_response.json()

                        for session in sessions:
                            # Get windows for this session (use URL-encoded id)
                            session_id_encoded = quote(session['id'], safe='')
                            windows_response = client.get(f"/sessions/{session_id_encoded}/windows")
                            if windows_response.status_code == 200:
                                windows = windows_response.json()

                                for window in windows['windows']:
                                    channel = {
                                        'id': f"{session['name']}:{window['window_id']}",
                                        'name': f"{session['name']}:{window['name']}",
                                        'session': session['name'],
                                        'window': window['window_id'],
                                        'recording': False
                                    }
                                    self.channels.append(channel)

                    # Get active recordings
                    recordings_response = client.get("/recordings/")
                    if recordings_response.status_code == 200:
                        recordings = recordings_response.json()
                        self.active_recordings = {r['id']: r for r in recordings}

                        # Mark channels that are recording
                        for channel in self.channels:
                            if channel['id'] in self.active_recordings:
                                channel['recording'] = True

                except Exception:
                    logger.exception("Could not fetch channels")
                    # Fallback to static message
                    self.channels = [{'name': 'Server running, but no channels found', 'id': None, 'recording': False}]
            else:
                self.channels = [{'name': 'Server not running', 'id': None, 'recording': False}]

        except Exception:
            logger.exception("Error loading channels")
            self.channels = [{'name': 'Error loading channels', 'id': None, 'recording': False}]

    def render(self) -> str:
        """Render the channel tuner."""
        if not self.channels:
            return "📺 No channels available\n\nOpen a tmux session to see channels"

        lines = []

        for i, channel in enumerate(self.channels[:8]):  # Show 8 channels max
            marker = "▶ " if i == self.selected_index else "  "
            status = "🔴" if channel.get('recording') else "⚫"
            name = channel['name']

            lines.append(f"{marker}{status} {name}")

        if len(self.channels) > 8:
            lines.append(f"\n... and {len(self.channels) - 8} more")

        # Show current selection info
        if self.channels and 0 <= self.selected_index < len(self.channels):
            selected = self.channels[self.selected_index]
            status_text = "RECORDING" if selected.get('recording') else "idle"
            lines.append(f"\nSelected: {selected['name']} ({status_text})")

        return "\n".join(lines)

    def action_select_next(self) -> None:
        """Select next channel."""
        if self.channels:
            self.selected_index = (self.selected_index + 1) % len(self.channels)

    def action_select_previous(self) -> None:
        """Select previous channel."""
        if self.channels:
            self.selected_index = (self.selected_index - 1) % len(self.channels)

    def get_selected_channel(self) -> Optional[dict]:
        """Get the currently selected channel."""
        if self.channels and 0 <= self.selected_index < len(self.channels):
            return self.channels[self.selected_index]
        return None

    async def toggle_recording(self) -> None:
        """Start or stop recording for the selected channel."""
        channel = self.get_selected_channel()
        if not channel or not channel.get('id'):
            return

        try:
            client = self.connection.client()

            if channel.get('recording'):
                # Stop recording
                response = client.delete(f"/recordings/{channel['id']}")
                if response.status_code == 200:
                    channel['recording'] = False
                    logger.info(f"Stopped recording {channel['name']}")
            else:
                # Start recording
                session_name = channel['session']
                window_id = channel['window']
                response = client.post("/recordings/", json={
                    'session_id': session_name,
                    'window_id': window_id
                    # active_pane will be auto-detected by server
                })
                if response.status_code in [200, 201]:
                    channel['recording'] = True
                    logger.info(f"Started recording {channel['name']}")

            await self.refresh_channels()

        except Exception:
            logger.exception("Error toggling recording")


class CRTPlayer(Static):
    """CRT-style video player widget."""

    current_file: reactive[Optional[Path]] = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.player: Optional[AsciinemaPlayer] = None

    def compose(self) -> ComposeResult:
        """Compose the CRT player."""
        # Container for the player
        with Container(id="player-container"):
            yield Static("", id="placeholder")

    async def play_recording(self, recording_path: Path) -> None:
        """Play a recording file."""
        try:
            self.current_file = recording_path

            # Remove existing player
            if self.player:
                await self.player.remove()

            # Remove placeholder or blank screen
            try:
                placeholder = self.query_one("#placeholder")
                await placeholder.remove()
            except Exception:
                pass

            try:
                blank_screen = self.query_one("#blank-screen")
                await blank_screen.remove()
            except Exception:
                pass

            # Create new player
            self.player = AsciinemaPlayer(str(recording_path))

            # Mount to the container
            container = self.query_one("#player-container")
            await container.mount(self.player)

            logger.info(f"Playing recording: {recording_path.name}")

        except Exception:
            logger.exception("Error playing recording")

    async def show_blank(self) -> None:
        """Show a blank/static screen for channels not recording."""
        try:
            # Remove existing player
            if self.player:
                await self.player.remove()
                self.player = None

            # Remove placeholder if it exists
            try:
                placeholder = self.query_one("#placeholder")
                await placeholder.remove()
            except Exception:
                pass

            # Add a blank static widget (placeholder for future TV static)
            container = self.query_one("#player-container")
            # Show some indication that it's blank/no signal
            blank_content = "[dim]NO SIGNAL[/dim]"
            blank_screen = Static(blank_content, id="blank-screen")
            await container.mount(blank_screen)

            logger.info("Showing blank screen")

        except Exception:
            logger.exception("Error showing blank screen")


class TVMuxApp(App):
    """Main tvmux TUI application."""

    CSS = """
    /* CRT TV styling */
    .crt-container {
        border: thick white;
        background: black;
        margin: 0;
        padding: 0;
    }

    .crt-screen {
        background: #001100;
        color: #00ff00;
        min-height: 20;
        padding: 0;
    }

    .crt-logo {
        text-align: center;
        color: #00ff00;
        text-style: bold;
        margin-top: 5;
    }

    .crt-subtitle {
        text-align: center;
        color: #666666;
        margin-top: 2;
    }

    /* Control panel styling */
    .controls {
        background: #111111;
        border: solid #333333;
        min-height: 10;
        max-height: 15;
        padding: 1;
    }

    /* Player container - no styling interference */
    #player-container {
        border: none;
        padding: 0;
        margin: 0;
    }

    /* Blank screen - centered text */
    #blank-screen {
        text-align: center;
        content-align: center middle;
        width: 100%;
        height: 100%;
    }

    /* Remove asciinema player borders and padding, fill container */
    AsciinemaPlayer {
        border: none;
        padding: 0;
        width: 100%;
        height: 100%;
    }

    /* Layout */
    .main-layout {
        height: 100%;
    }

    .video-area {
        height: 75%;
    }

    .control-area {
        height: 25%;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("up", "select_previous", "Previous"),
        ("down", "select_next", "Next"),
        ("enter", "play_selected", "Tune"),
        ("ctrl+r", "toggle_playback", "Record"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.player: Optional[CRTPlayer] = None
        self.tuner: Optional[ChannelTuner] = None
        self.connection = Connection()

    def compose(self) -> ComposeResult:
        """Compose the application layout."""
        with Vertical(classes="main-layout"):
            # Top area - CRT TV player
            with Container(classes="video-area"):
                self.player = CRTPlayer()
                yield self.player

            # Bottom area - channel tuner and controls
            with Container(classes="control-area controls"):
                self.tuner = ChannelTuner()
                yield self.tuner

        yield Footer()

    async def on_mount(self) -> None:
        """Initialize player when app starts."""
        if self.player:
            # Check if the initially selected channel is recording
            await self.tune_to_selected_channel()

    async def action_refresh(self) -> None:
        """Refresh channels list."""
        if self.tuner:
            await self.tuner.refresh_channels()

    def action_select_next(self) -> None:
        """Select next channel."""
        if self.tuner:
            self.tuner.action_select_next()
            # Auto-play if channel is recording
            self.schedule_channel_check()

    def action_select_previous(self) -> None:
        """Select previous channel."""
        if self.tuner:
            self.tuner.action_select_previous()
            # Auto-play if channel is recording
            self.schedule_channel_check()

    async def action_play_selected(self) -> None:
        """Tune to the selected channel."""
        await self.tune_to_selected_channel()

    async def action_toggle_playback(self) -> None:
        """Toggle recording for current channel."""
        if self.tuner:
            await self.tuner.toggle_recording()
            # Check if we should start/stop playing
            await self.tune_to_selected_channel()

    def schedule_channel_check(self) -> None:
        """Schedule a channel check for auto-play."""
        # Use call_after to avoid blocking the UI
        self.call_after_refresh(self.tune_to_selected_channel)

    async def tune_to_selected_channel(self) -> None:
        """Auto-play the selected channel if it's recording."""
        if not self.tuner or not self.player:
            return

        channel = self.tuner.get_selected_channel()
        if not channel or not channel.get('recording'):
            # Show blank player for channels not recording
            await self.player.show_blank()
            return

        # Find the recording file for this channel
        try:
            client = self.connection.client()

            # Get the current recording info
            recording_id = channel['id']
            if recording_id in self.tuner.active_recordings:
                recording_info = self.tuner.active_recordings[recording_id]
                cast_file = recording_info.get('cast_path')

                if cast_file and Path(cast_file).exists():
                    logger.info(f"Playing channel: {channel['name']} from {cast_file}")
                    await self.player.play_recording(Path(cast_file))
                else:
                    logger.warning(f"Recording file not found for channel: {channel['name']}")
                    logger.debug(f"Recording info: {recording_info}")
            else:
                logger.warning(f"No active recording found for channel: {channel['name']}")
                logger.debug(f"Available recordings: {list(self.tuner.active_recordings.keys())}")

        except Exception:
            logger.exception(f"Error playing channel: {channel['name']}")


def run_tui():
    """Run the tvmux TUI application."""
    # Set up client logging first
    setup_client_logging()

    app = TVMuxApp()
    app.run()


if __name__ == "__main__":
    run_tui()
