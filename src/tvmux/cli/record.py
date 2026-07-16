"""Recording management commands."""
import os
import subprocess

import click

from ..connection import Connection
from ..config import get_config


@click.group(invoke_without_command=True)
@click.pass_context
def rec(ctx):
    """Manage window recordings."""
    if ctx.invoked_subcommand is None:
        # Default to start command
        ctx.invoke(start)


@rec.command("start")
def start():
    """Start recording the current tmux window."""
    conn = Connection()
    config = get_config()

    if not conn.is_running:
        if config.server.auto_start:
            click.echo("Server not running, starting automatically...")
            if not conn.start():
                click.echo("Failed to start server", err=True)
                raise SystemExit(1)
            click.echo(f"Server started at {conn.base_url}")
        else:
            click.echo("Server not running", err=True)
            raise SystemExit(1)

    # Check if we're in tmux
    if not os.environ.get("TMUX"):
        click.echo("Not in a tmux session", err=True)
        raise click.Abort()

    # Get current tmux session and window info
    try:
        info = subprocess.run(
            ["tmux", "display-message", "-p",
             "#{session_name}:#{window_id}:#{pane_id}"],
            capture_output=True,
            text=True,
            check=True
        )
        # rsplit: window/pane ids are tmux-generated (@N/%N) and can't
        # contain ":", but session names can
        session_name, window_id, pane_id = info.stdout.strip().rsplit(":", 2)
    except (subprocess.CalledProcessError, ValueError):
        click.echo("Failed to get tmux info", err=True)
        raise click.Abort()

    # Call API to start recording (fields match the RecordingCreate model;
    # not imported here to keep FastAPI out of CLI startup)
    try:
        api = conn.client()
        response = api.post("/recordings/", json={
            "session_id": session_name,
            "window_id": window_id,
            "active_pane": pane_id,
        })

        if response.status_code in [201, 202]:
            recording_data = response.json()
            recording_id = recording_data['id']

            if response.status_code == 201:
                click.echo(f"Started recording {recording_id}")
            else:  # 202
                click.echo(f"Recording already active {recording_id}")

            click.echo(f"Recording to: {recording_data['cast_path']}")
        else:
            click.echo(f"Failed to start recording: {response.text}", err=True)
            raise click.Abort()

    except Exception as e:
        click.echo(f"Error starting recording: {e}", err=True)
        raise click.Abort()


@rec.command("ls")
@click.option("-q", "--quiet", is_flag=True, help="Only output recording IDs (one per line)")
def ls(quiet):
    """List active recordings."""
    conn = Connection()
    if not conn.is_running:
        click.echo("Server not running", err=True)
        raise SystemExit(1)

    # Call API to list recordings
    try:
        api = conn.client()
        response = api.get("/recordings/")

        if response.status_code == 200:
            recordings = response.json()
            if recordings:
                if quiet:
                    # Quiet mode: output only recording IDs, one per line
                    for rec in recordings:
                        click.echo(rec['id'])
                else:
                    # Verbose mode: full details
                    click.echo("Active recordings:")
                    for rec in recordings:
                        rec_id = rec['id']
                        session = rec['session_id']
                        window = rec['window_id']
                        active_pane = rec.get('active_pane', 'unknown')

                        click.echo(f"  ID: {rec_id}")
                        click.echo(f"      Session: {session}, Window: {window}, Pane: {active_pane}")

                        if rec.get('cast_path'):
                            click.echo(f"      Recording to: {rec['cast_path']}")
                        click.echo()  # Blank line between recordings
            else:
                if not quiet:
                    click.echo("No active recordings")
        else:
            click.echo(f"Failed to list recordings: {response.text}", err=True)
            raise click.Abort()

    except Exception as e:
        click.echo(f"Error listing recordings: {e}", err=True)
        raise click.Abort()


@rec.command("stop")
@click.argument("recording_ids", nargs=-1)
def stop(recording_ids):
    """Stop recording(s). Stop all recordings if no IDs specified."""
    conn = Connection()
    if not conn.is_running:
        click.echo("Server not running", err=True)
        raise SystemExit(1)

    # Call API to stop recording(s)
    try:
        api = conn.client()

        if recording_ids:
            # Stop specific recording(s)
            stopped_count = 0
            failed_count = 0

            for recording_id in recording_ids:
                response = api.delete(f"/recordings/{recording_id}", timeout=10.0)

                if response.status_code == 200:
                    data = response.json()
                    click.echo(f"Stopped recording '{recording_id}'")
                    if 'cast_path' in data and data['cast_path']:
                        click.echo(f"Recording saved to: {data['cast_path']}")
                    stopped_count += 1
                elif response.status_code == 404:
                    click.echo(f"Recording '{recording_id}' not found", err=True)
                    failed_count += 1
                else:
                    click.echo(f"Failed to stop recording '{recording_id}': {response.text}", err=True)
                    failed_count += 1

            # Summary message for multiple IDs
            if len(recording_ids) > 1:
                if stopped_count > 0:
                    click.echo(f"Successfully stopped {stopped_count} recording(s)")
                if failed_count > 0:
                    click.echo(f"Failed to stop {failed_count} recording(s)", err=True)
                    raise SystemExit(1)
        else:
            # Stop all recordings (default behavior)
            # First get list of active recordings
            list_response = api.get("/recordings/")

            if list_response.status_code != 200:
                click.echo(f"Failed to list recordings: {list_response.text}", err=True)
                raise click.Abort()

            recordings = list_response.json()

            if not recordings:
                click.echo("No active recordings to stop")
                return

            # Stop each recording
            stopped_count = 0
            for recording in recordings:
                rec_id = recording['id']
                response = api.delete(f"/recordings/{rec_id}", timeout=10.0)
                if response.status_code == 200:
                    stopped_count += 1
                    data = response.json()
                    click.echo(f"Stopped recording '{rec_id}'")
                    if 'cast_path' in data and data['cast_path']:
                        click.echo(f"Recording saved to: {data['cast_path']}")
                else:
                    click.echo(f"Failed to stop recording '{rec_id}': {response.text}", err=True)

            if stopped_count > 0:
                click.echo(f"Stopped {stopped_count} recording(s)")

    except Exception as e:
        click.echo(f"Error stopping recording: {e}", err=True)
        raise click.Abort()
