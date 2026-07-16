# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

**Build and setup:**
- `make dev` - Set up development environment with venv and pre-commit hooks
- `make install` - Install production dependencies only
- `make clean` - Remove caches and virtual environment

**Testing:**
- `make test` - Run all tests using pytest
- `make coverage` - Build the HTML coverage report

**Code quality:**
- Pre-commit hooks automatically run ruff for linting and formatting
- `ruff check .` - Manual linting
- `ruff format .` - Manual code formatting

**Distribution:**
- `make docs` - Build the documentation
- `make dist` - Build distributable packages
- `make release` - Publish to PyPI

## Architecture Overview

tvmux is a terminal session recorder that creates asciinema cast files from tmux sessions,
with a client-server architecture and a textual TUI. The longer-term goal is to quantize
recordings (remove spam, reduce frame rate via bittty) and extract computer-use data from them.

### Core Components

**CLI Interface (`src/tvmux/cli/`):**
- `main.py` - Main CLI entry point; running bare `tvmux` launches the TUI. Heavy
  subcommands (`api`, `tui`) are lazy-loaded so ordinary CLI startup stays fast
- `server.py` - Server management commands (start/stop/status)
- `record.py` - Recording control commands (`tvmux rec`, `rec ls`, `rec stop`)
- `config.py` - Config inspection commands (`tvmux config show|defaults`)
- `api_cli.py` - `tvmux api ...`: CLI auto-generated from the FastAPI routes by introspection

**Fast hook poster (`src/tvmux/hook.py`):**
- Invoked by tmux hooks as `python -m tvmux.hook` on every pane switch, so it MUST stay
  stdlib-only (~30ms startup). Never import click/FastAPI/pydantic/textual/httpx here -
  `tests/unit/test_hook_poster.py` has a guard test that fails if anything heavy sneaks in
- POSTs hook events to the server's `/hook` endpoint; always exits 0 so a dead server
  never makes tmux print errors

**FastAPI Server (`src/tvmux/server/`):**
- `main.py` - FastAPI application with lifespan management; installs tmux hooks on startup
  (after unconditionally sweeping stale hooks left by a crashed server) and removes them on shutdown
- `state.py` - Global state: the `recorders` dict keyed by `"session:window_id"`
- `window_monitor.py` - Sweeps recordings whose tmux windows no longer exist
- `routers/` - REST API endpoints:
  - `session.py` - Session management
  - `window.py` - Window management
  - `panes.py` - Pane operations (separate from windows)
  - `recording.py` - Recording control (RESTful with IDs)
  - `callbacks.py` - CRUD for tmux hooks + building/installing the hook commands
  - `hook.py` - Single `POST /hook` endpoint that receives fired tmux hook events

**TUI (`src/tvmux/tui/`):**
- `app.py` - Textual "CRT TV" interface: tmux windows are channels, recordings play
  via textual-asciinema; ctrl+r toggles recording on the selected channel

**Configuration System (`src/tvmux/`):**
- `config.py` - Configuration management with TOML support (`~/.tvmux.conf`) and env overrides
- `api_client.py` - HTTP client for server communication
- `connection.py` - Connection utilities, health checks, and server spawn/stop
- `utils.py` - Common utilities (safe_filename, session dirs, FIFO reader checks)

**Recording Engine (`src/tvmux/`):**
- `models/recording.py` - The Recording model: FIFO + asciinema lifecycle, pane
  dumping/switching. FIFO writes go through `_open_fifo_write()` (O_NONBLOCK first) so a
  dead asciinema can't hang the server's event loop
- `repair.py` - Cast file repair utilities for handling abrupt terminations

**Process Management (`src/tvmux/proc/`):**
- `bg.py` - Background process management and cleanup

**Data Models (`src/tvmux/models/`):**
- Pydantic models for sessions, windows, panes, positions, and recordings
- `remote.py` - Remote connection models

### Key Architecture Decisions

**API as the single source of truth:**
- The FastAPI routes (and their `openapi.json`) are intended to drive every interface:
  the auto-generated `tvmux api` CLI today, the TUI and a web UI eventually
- Don't build side channels around the API. The one sanctioned exception is the tmux hook
  hot path, which bypasses the heavy *CLI* (not the API) via `tvmux/hook.py` for speed

**Client-Server Pattern:**
- REST API at 127.0.0.1:21590 for tmux integration
- Server manages global state and multiple window recordings
- CLI tools communicate via HTTP to the server

**Active Pane Following:**
- Records only the currently active pane rather than entire sessions
- Dramatically reduces file sizes while capturing relevant workflow
- Automatically switches recording focus when user changes panes

**FIFO-based Streaming:**
- Uses named pipes to stream terminal output to asciinema
- Enables real-time recording with proper terminal state preservation
- Handles pane switching by dumping state and redirecting streams

## Recording Flow

1. Server starts, sweeps any stale hooks, and installs tmux hooks whose command is
   `python -m tvmux.hook ... --flag=#{q:...}` (tmux-quoted), which POSTs events to `/hook`
2. When recording starts, creates FIFO and launches asciinema (via `sys.executable -m asciinema`)
3. Dumps initial pane state (content + cursor position) to FIFO
4. Starts `tmux pipe-pane` to stream live output to FIFO
5. On pane switches (`after-select-pane` events), stops old stream, dumps new state, starts new stream
6. On stop, sends terminal reset sequences and repairs cast file

## Output Organization

Recordings are organized by date:
`output_dir/YYYY-MM/{timestamp}_{hostname}_{session}_{window_id}_{window_name}.cast`
(the window id keeps filenames unique when two windows share a name)

## Configuration

tvmux supports flexible configuration through multiple sources:

**Configuration Files:**
- `~/.tvmux.conf` - TOML format user configuration
- `tvmux config defaults` prints a template (add `--format=env` for the env-var forms)

**Environment Variables:**
- Any config field can be overridden as `TVMUX_{SECTION}_{FIELD}`, e.g.
  `TVMUX_OUTPUT_DIRECTORY`, `TVMUX_SERVER_PORT`, `TVMUX_SERVER_AUTO_START`
- `TVMUX_CONFIG_FILE` - Custom config file location
- `TVMUX_LOG_LEVEL` - Log level (also set by the CLI's `--log-level`)

**Configuration Sections:**
- `[output]` - Recording output settings (directory, date format)
- `[server]` - Server settings (port, auto-start, auto-shutdown)
- `[recording]` - Recording behavior (repair, pane following)
- `[annotations]` - Annotation options (cursor state)
- `[logging]` - Log level, HTTP access logs, client log file (`~/.tvmux/client.log`)

Note: `auto_shutdown` is configurable but not currently implemented (see todo.md).

## Development Notes

- Python 3.11+ with type hints and Pydantic models
- Async/await for server operations
- Uses `make test` (pytest) for testing
- Ruff for linting and formatting (120 char line length)
- Pre-commit hooks enforce code quality
- Background process management prevents orphaned processes
- Subprocesses that must survive PATH/venv differences use `sys.executable`
  (server spawn, asciinema, hook commands) - keep it that way

**IMPORTANT:** Do not run the TUI application (`tvmux` or `python -m tvmux.tui.app`) in development - it will interfere with the terminal session.

- you can use tmux capture-pane in this project. The user allows it.
- logs are in /tmp/tvmux-$(whoami)
