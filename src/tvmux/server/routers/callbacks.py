"""CRUD endpoints for managing tmux hooks."""
import logging
import sys
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, List

from ...config import get_config
from ... import proc

logger = logging.getLogger(__name__)

router = APIRouter()


class Hook(BaseModel):
    """Configuration for a tmux hook."""
    name: str
    enabled: bool = True
    command: Optional[str] = None  # If None, use default command
    description: Optional[str] = None


class HookCreate(BaseModel):
    """Request to create/install a hook."""
    name: str
    enabled: bool = True
    command: Optional[str] = None
    description: Optional[str] = None


class HookUpdate(BaseModel):
    """Request to update a hook."""
    enabled: Optional[bool] = None
    command: Optional[str] = None
    description: Optional[str] = None


# Available tmux hooks with descriptions
AVAILABLE_HOOKS = {
    "after-new-session": "Fired when a new session is created",
    "after-new-window": "Fired when a new window is created",
    "after-split-window": "Fired when a window is split into panes",
    "after-kill-pane": "Fired when a pane is killed",
    "after-resize-pane": "Fired when a pane is resized",
    "after-rename-window": "Fired when a window is renamed",
    "after-rename-session": "Fired when a session is renamed",
    "after-select-pane": "Fired when the active pane changes",
    "window-unlinked": "Fired when a window is unlinked from a session",
    "session-closed": "Fired when a session ends",
    "pane-mode-changed": "Fired when pane mode changes",
    "client-attached": "Fired when a client attaches",
    "client-detached": "Fired when a client detaches",
    "client-session-changed": "Fired when a client switches sessions",
}

# In-memory storage for hook configurations
# In a production system, this would be persisted
installed_hooks: Dict[str, Hook] = {}


def build_hook_command(hook_name: str, port: int) -> str:
    """Build the shell command tmux runs when a hook fires.

    Uses the lightweight ``tvmux.hook`` module (stdlib-only, ~30ms startup)
    rather than the full CLI, since after-select-pane fires on every pane
    switch. Values use tmux ``#{q:...}`` quoting and ``--flag=value`` form so
    names containing spaces or quotes survive, and empty expansions can't
    shift arguments.
    """
    # sys.executable ensures the server's own interpreter (and venv) is used
    # even though tmux runs the hook outside it.
    python_exe = sys.executable

    # The #{...} parts are tmux format strings, expanded when the hook fires.
    return (
        f'{python_exe} -m tvmux.hook {hook_name} '
        f'--port={port} '
        '--session-name=#{q:session_name} '
        '--window-id=#{q:window_id} '
        '--pane-id=#{q:pane_id} '
        '--window-index=#{q:window_index} '
        '--pane-index=#{q:pane_index} '
        '>/dev/null 2>&1'
    )


def get_default_command(hook_name: str) -> str:
    """Get the default command for a hook."""
    config = get_config()
    return build_hook_command(hook_name, config.server.port)


def install_hook(hook: Hook) -> None:
    """Install a tmux hook."""
    if not hook.enabled:
        return

    command = hook.command or get_default_command(hook.name)

    logger.info(f"Installing hook {hook.name}")
    logger.debug(f"Hook command: {command}")

    # Set the hook using tmux
    # Use single quotes for run-shell to avoid conflicts with double quotes in command
    proc.run(["tmux", "set-hook", "-g", hook.name, f"run-shell '{command}'"])


def uninstall_hook(hook_name: str) -> None:
    """Uninstall a tmux hook."""
    logger.info(f"Uninstalling hook {hook_name}")
    proc.run(["tmux", "set-hook", "-gu", hook_name])


@router.get("")
async def list_hooks() -> List[Hook]:
    """List all available hooks and their status."""
    hooks = []

    for hook_name, description in AVAILABLE_HOOKS.items():
        if hook_name in installed_hooks:
            hook = installed_hooks[hook_name]
        else:
            hook = Hook(
                name=hook_name,
                enabled=False,
                description=description
            )
        hooks.append(hook)

    return hooks


@router.get("/{hook_name}")
async def get_hook(hook_name: str) -> Hook:
    """Get details about a specific hook."""
    if hook_name not in AVAILABLE_HOOKS:
        raise HTTPException(status_code=404, detail=f"Hook '{hook_name}' not recognized")

    if hook_name in installed_hooks:
        return installed_hooks[hook_name]
    else:
        return Hook(
            name=hook_name,
            enabled=False,
            description=AVAILABLE_HOOKS[hook_name]
        )


@router.post("")
async def create_hook(hook_data: HookCreate) -> Hook:
    """Create/install a new hook."""
    if hook_data.name not in AVAILABLE_HOOKS:
        raise HTTPException(status_code=400, detail=f"Hook '{hook_data.name}' not recognized")

    if hook_data.name in installed_hooks and installed_hooks[hook_data.name].enabled:
        raise HTTPException(status_code=409, detail=f"Hook '{hook_data.name}' already installed")

    hook = Hook(
        name=hook_data.name,
        enabled=hook_data.enabled,
        command=hook_data.command,
        description=hook_data.description or AVAILABLE_HOOKS[hook_data.name]
    )

    if hook.enabled:
        install_hook(hook)

    installed_hooks[hook.name] = hook

    return hook


@router.put("/{hook_name}")
async def update_hook(hook_name: str, update_data: HookUpdate) -> Hook:
    """Update an existing hook."""
    if hook_name not in AVAILABLE_HOOKS:
        raise HTTPException(status_code=404, detail=f"Hook '{hook_name}' not recognized")

    # Get existing hook or create default
    if hook_name in installed_hooks:
        hook = installed_hooks[hook_name]
    else:
        hook = Hook(
            name=hook_name,
            enabled=False,
            description=AVAILABLE_HOOKS[hook_name]
        )

    # Update fields if provided
    if update_data.enabled is not None:
        was_enabled = hook.enabled
        hook.enabled = update_data.enabled

        # Install/uninstall based on state change
        if not was_enabled and hook.enabled:
            install_hook(hook)
        elif was_enabled and not hook.enabled:
            uninstall_hook(hook.name)

    if update_data.command is not None:
        hook.command = update_data.command
        # Reinstall if enabled and command changed
        if hook.enabled:
            install_hook(hook)

    if update_data.description is not None:
        hook.description = update_data.description

    installed_hooks[hook_name] = hook

    return hook


@router.delete("/{hook_name}")
async def delete_hook(hook_name: str) -> Dict[str, str]:
    """Remove/uninstall a hook."""
    if hook_name not in AVAILABLE_HOOKS:
        raise HTTPException(status_code=404, detail=f"Hook '{hook_name}' not recognized")

    if hook_name not in installed_hooks:
        raise HTTPException(status_code=404, detail=f"Hook '{hook_name}' not installed")

    hook = installed_hooks[hook_name]
    if hook.enabled:
        uninstall_hook(hook_name)

    del installed_hooks[hook_name]

    return {"status": "deleted", "hook": hook_name}


def setup_default_hooks():
    """Set up default tmux hooks for tvmux operation."""
    logger.info("Setting up default tmux hooks...")

    # Default hooks needed for tvmux to function
    default_hooks = [
        "after-select-pane",  # Essential for pane switching
        "session-closed",     # Essential for cleanup
        "window-unlinked",    # Helpful for cleanup
    ]

    for hook_name in default_hooks:
        if hook_name not in installed_hooks:
            hook = Hook(
                name=hook_name,
                enabled=True,
                description=AVAILABLE_HOOKS[hook_name]
            )
            install_hook(hook)
            installed_hooks[hook_name] = hook
            logger.debug(f"Installed default hook: {hook_name}")


def remove_all_hooks():
    """Remove all installed tmux hooks."""
    logger.info("Removing all tmux hooks...")

    for hook_name in installed_hooks:
        if installed_hooks[hook_name].enabled:
            uninstall_hook(hook_name)

    installed_hooks.clear()


def remove_known_hooks():
    """Unconditionally unset every hook tvmux could ever have installed.

    Unlike remove_all_hooks() this doesn't consult the in-memory registry,
    which is always empty at startup — it exists to clear hooks left in tmux
    by a previous server that crashed without cleaning up.
    """
    logger.info("Removing any stale tvmux hooks from tmux...")

    for hook_name in AVAILABLE_HOOKS:
        proc.run(["tmux", "set-hook", "-gu", hook_name])
