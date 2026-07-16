"""FastAPI server that manages tmux connections."""
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

import uvicorn

from .state import server_dir, recorders, SERVER_HOST
from .routers import session, window, panes, callbacks, hook, recording
from ..config import get_config
from .. import __version__


def setup_logging():
    """Configure logging for the application."""
    # Get config first, then check environment variable as fallback
    try:
        from ..config import get_config
        config = get_config()
        log_level = config.logging.level.upper()
        include_access_logs = config.logging.include_access_logs
    except Exception:
        # Fallback to environment variable if config fails
        log_level = os.getenv('TVMUX_LOG_LEVEL', 'INFO').upper()
        include_access_logs = False

    # Configure root logger
    handlers = [logging.StreamHandler()]  # Console output

    # Also log to file if running as daemon
    log_file = server_dir / "server.log"
    if log_file.parent.exists():
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers
    )

    # Set uvicorn loggers based on config
    if include_access_logs:
        logging.getLogger('uvicorn.access').setLevel(logging.INFO)
    else:
        logging.getLogger('uvicorn.access').setLevel(logging.WARNING)  # Reduce HTTP noise
    logging.getLogger('uvicorn.error').setLevel(logging.INFO)

    # Our application loggers
    logging.getLogger('tvmux').setLevel(getattr(logging, log_level, logging.INFO))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan."""
    # Set up logging first
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Starting tvmux server...")

    # Startup
    server_dir.mkdir(exist_ok=True)
    # Write PID file
    (server_dir / "server.pid").write_text(str(os.getpid()))

    # Clean up hooks left in tmux by a previously crashed server; the
    # in-memory registry is empty here so remove_all_hooks() wouldn't work
    callbacks.remove_known_hooks()

    # Set up default tmux hooks
    callbacks.setup_default_hooks()
    logger.info("Default tmux hooks configured")

    # TODO: Discover existing panes and start tracking them

    yield

    # Shutdown
    # Remove tmux hooks
    callbacks.remove_all_hooks()

    # Remove PID file
    (server_dir / "server.pid").unlink(missing_ok=True)

    # Clean up recorders
    for recorder in recorders.values():
        recorder.stop()


app = FastAPI(title="tvmux server", lifespan=lifespan)

# Include routers
app.include_router(session.router, prefix="/sessions", tags=["sessions"])
app.include_router(window.router, prefix="/windows", tags=["windows"])
app.include_router(panes.router, prefix="/panes", tags=["panes"])
app.include_router(callbacks.router, prefix="/callbacks", tags=["callbacks"])
app.include_router(hook.router, prefix="/hook", tags=["hook"])
app.include_router(recording.router, prefix="/recordings", tags=["recordings"])


@app.get("/")
async def root():
    """Server info."""
    return {
        "status": "running",
        "pid": os.getpid(),
        "recorders": len(recorders),
        "version": __version__
    }


@app.get("/version")
async def version():
    """Get server version."""
    return {"version": __version__}




def cleanup_and_exit(signum=None, frame=None):
    """Clean up and exit gracefully."""
    print("\nCleaning up...")

    # Stop all recorders first (kills asciinema processes)
    print(f"Stopping {len(recorders)} active recordings...")
    for recorder in recorders.values():
        try:
            recorder.stop()
        except Exception as e:
            print(f"Error stopping recorder: {e}")

    # Remove tmux hooks
    callbacks.remove_all_hooks()

    # Remove PID file
    (server_dir / "server.pid").unlink(missing_ok=True)

    sys.exit(0)


def run_server():
    """Run the server on HTTP port."""
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, cleanup_and_exit)
    signal.signal(signal.SIGTERM, cleanup_and_exit)

    # Use configured port
    config = get_config()
    server_port = config.server.port

    try:
        uvicorn.run(app, host=SERVER_HOST, port=server_port)
    except KeyboardInterrupt:
        pass  # cleanup_and_exit will be called by signal handler
    finally:
        # Ensure cleanup happens even on unexpected exits
        cleanup_and_exit()


if __name__ == "__main__":
    run_server()
