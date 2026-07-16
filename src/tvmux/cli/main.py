#!/usr/bin/env python3
"""Main CLI entry point for tvmux."""
import os
import logging
import sys
from pathlib import Path
import click

from .server import server
from .record import rec
from .config import config
from .tui import tui
from ..config import load_config, set_config
from ..connection import Connection
from .. import __version__


class LazyApiGroup(click.Group):
    """Defers importing api_cli (which builds the whole FastAPI app to
    introspect its routes) until an `api` subcommand is actually used."""

    def _real(self) -> click.Group:
        from .api_cli import api as real_api
        return real_api

    def list_commands(self, ctx):
        return sorted(self._real().commands)

    def get_command(self, ctx, name):
        return self._real().get_command(ctx, name)


api = LazyApiGroup(name="api", help="Direct API access for testing (auto-generated from routes).")


def print_version(ctx, param, value):
    """Print version information for client and server."""
    if not value or ctx.resilient_parsing:
        return

    click.echo(f"tvmux client version: {__version__}")

    # Try to get server version if running
    conn = Connection()
    if conn.is_running:
        try:
            api = conn.client()
            response = api.get("/version")
            if response.status_code == 200:
                server_version = response.json().get("version", "unknown")
                click.echo(f"tvmux server version: {server_version}")
            else:
                click.echo("tvmux server version: unable to retrieve")
        except Exception:
            click.echo("tvmux server version: error connecting")
    else:
        click.echo("tvmux server: not running")

    ctx.exit()


def setup_client_logging(config):
    """Set up client-side logging for all CLI commands."""
    try:
        log_level = config.logging.level.upper()
        client_log_file = config.logging.client_log_file

        # Configure handlers
        handlers = []

        # Add file handler if configured
        if client_log_file:
            log_path = Path(client_log_file).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_path))

        # Always use file logging only for CLI - no console spam
        if not handlers:
            handlers.append(logging.NullHandler())

        # Configure logging
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=handlers,
            force=True  # Override any existing config
        )

        logger = logging.getLogger(__name__)
        logger.debug(f"Client logging initialized, command: {' '.join(sys.argv)}")

    except Exception:
        # Fallback to basic console logging
        logging.basicConfig(level=logging.INFO)
        logging.getLogger(__name__).exception("Failed to setup client logging, using defaults")


@click.group(invoke_without_command=True)
@click.option('--log-level', default='INFO', type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR']),
              help='Set logging level')
@click.option('--config-file', type=click.Path(exists=True),
              help='Path to configuration file')
@click.option('--version', is_flag=True, callback=print_version,
              expose_value=False, is_eager=True, help='Show version information')
@click.pass_context
def cli(ctx, log_level, config_file):
    """Per-window recorder for tmux."""
    os.environ['TVMUX_LOG_LEVEL'] = log_level

    # Load configuration
    config = load_config(config_file)
    set_config(config)

    # Set up logging for all CLI commands
    setup_client_logging(config)

    # If no command specified, launch TUI
    if ctx.invoked_subcommand is None:
        ctx.invoke(tui)


cli.add_command(server)
cli.add_command(rec)
cli.add_command(config)
cli.add_command(api)
cli.add_command(tui)


if __name__ == "__main__":
    cli()
