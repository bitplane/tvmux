"""Single endpoint for receiving tmux hook events."""
import logging
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, Dict, Any

from ..state import recorders
from ..window_monitor import cleanup_closed_windows

logger = logging.getLogger(__name__)

router = APIRouter()


class HookEvent(BaseModel):
    """Event data from tmux hooks."""
    hook_name: str
    pane_id: Optional[str] = None
    session_name: Optional[str] = None
    window_id: Optional[str] = None
    window_index: Optional[str] = None
    pane_index: Optional[str] = None
    pane_pid: Optional[int] = None
    extra: Dict[str, Any] = {}


@router.post("")
async def receive_hook(event: HookEvent) -> Dict[str, str]:
    """Receive and process a hook event from tmux."""
    # Log the event using standard Python logging
    logger.info(
        f"Hook {event.hook_name} fired: "
        f"session={event.session_name}, "
        f"window={event.window_id}, "
        f"pane={event.pane_id}"
    )

    # Process the event
    action = await _process_hook_event(event)

    return {"status": "ok", "action": action}


async def _process_hook_event(event: HookEvent) -> str:
    """Process a hook event and return the action taken."""
    hook_name = event.hook_name

    # Log warnings for missing critical values
    if not event.session_name:
        logger.warning(f"Hook {hook_name} fired with empty session_name")
    if not event.window_id:
        logger.warning(f"Hook {hook_name} fired with empty window_id")
    if not event.pane_id and hook_name in ['after-select-pane', 'after-split-window', 'after-kill-pane']:
        logger.warning(f"Hook {hook_name} fired with empty pane_id")

    # Process based on hook type
    if hook_name == "after-new-session":
        logger.debug(f"New session created: {event.session_name}")
        return "session_created"

    elif hook_name == "after-new-window":
        logger.debug(f"New window created: {event.window_id} in session {event.session_name}")
        return "window_created"

    elif hook_name == "after-split-window":
        logger.debug(f"Window split: new pane {event.pane_id}")
        return "pane_created"

    elif hook_name == "after-kill-pane":
        logger.debug(f"Pane killed: {event.pane_id}")
        return "pane_closed"

    elif hook_name == "window-unlinked":
        logger.debug(f"Window unlinked from session {event.session_name}")
        # The cleanup will happen on the next pane switch
        return "window_unlinked"

    elif hook_name == "session-closed":
        # Session died - stop all recordings for this session
        logger.info(f"Session {event.session_name} closed")
        if event.session_name:
            session_recorders = [
                key for key in recorders.keys()
                if key.startswith(f"{event.session_name}:")
            ]
            for recorder_key in session_recorders:
                logger.info(f"Stopping recording {recorder_key} due to session close")
                recorder = recorders[recorder_key]
                recorder.stop()
                del recorders[recorder_key]
        # session_name often expands empty by the time the session is gone;
        # sweep for recordings whose windows no longer exist either way
        cleanup_closed_windows()
        return "session_destroyed"

    elif hook_name == "after-select-pane":
        # Active pane changed within a window
        logger.debug(
            f"Pane selection changed to {event.pane_id} "
            f"in window {event.window_id}"
        )

        # Clean up any recordings for windows that no longer exist
        cleanup_closed_windows()

        if event.session_name and event.window_id:
            recorder_key = f"{event.session_name}:{event.window_id}"

            if recorder_key in recorders:
                # Switch recording to new active pane
                recorder = recorders[recorder_key]
                if event.pane_id:
                    logger.info(f"Switching recording to pane {event.pane_id}")
                    recorder.switch_pane(event.pane_id)
                else:
                    logger.warning("No pane_id in select-pane event")
            else:
                logger.debug(f"No active recording for {recorder_key}")
        else:
            logger.warning("Missing session_name or window_id in select-pane event")
        return "pane_switched"

    elif hook_name == "after-resize-pane":
        logger.debug(f"Pane {event.pane_id} resized")
        return "pane_resized"

    elif hook_name == "after-rename-window":
        logger.debug(f"Window {event.window_id} renamed")
        return "window_renamed"

    elif hook_name == "after-rename-session":
        logger.debug(f"Session {event.session_name} renamed")
        return "session_renamed"

    else:
        logger.warning(f"Unknown hook: {hook_name}")
        return f"unknown_hook_{hook_name}"
