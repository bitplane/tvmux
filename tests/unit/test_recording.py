"""Regression tests for recording lifecycle and output preservation."""
import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from tvmux.models.recording import Recording


@pytest.fixture
def recorder(tmp_path):
    return Recording(
        id="main:@1", session_id="main", window_id="@1",
        _hostname="test", _session_dir=tmp_path,
    )


def test_restarting_in_same_minute_preserves_previous_cast(recorder, tmp_path, monkeypatch):
    clock = Mock()
    clock.now.return_value = datetime(2026, 10, 28, 12, 0)
    monkeypatch.setattr("tvmux.models.recording.datetime", clock)
    monkeypatch.setattr(Recording, "_get_display_name", lambda self: "shell")
    monkeypatch.setattr(Recording, "_wait_for_reader", AsyncMock(return_value=True))
    monkeypatch.setattr(Recording, "_dump_pane", Mock())
    monkeypatch.setattr(Recording, "_start_streaming", Mock())

    async def write_cast(self):
        # Like asciinema without --overwrite, refuse to replace an existing file.
        with open(self.cast_path, "x") as cast:
            cast.write("recorded output")

    monkeypatch.setattr(Recording, "_start_asciinema", write_cast)
    asyncio.run(recorder.start("%1", tmp_path))
    first_path = Path(recorder.cast_path)
    recorder.active = False
    asyncio.run(recorder.start("%1", tmp_path))

    assert Path(recorder.cast_path) != first_path
    assert first_path.read_text() == "recorded output"
    assert Path(recorder.cast_path).read_text() == "recorded output"


def test_asciinema_does_not_overwrite_existing_files(recorder, monkeypatch):
    spawn = AsyncMock(return_value=Mock(pid=123))
    monkeypatch.setattr("tvmux.models.recording.run_bg", spawn)
    recorder.cast_path = "/recording.cast"
    asyncio.run(recorder._start_asciinema())
    command = spawn.call_args.args[0]
    assert "--overwrite" not in command
    assert "--append" not in command


@pytest.fixture
def startup_stubs(monkeypatch):
    monkeypatch.setattr(Recording, "_get_display_name", lambda self: "shell")
    monkeypatch.setattr(Recording, "_dump_pane", Mock())
    monkeypatch.setattr(Recording, "_start_streaming", Mock())
    monkeypatch.setattr(Recording, "_stop_streaming", Mock())
    monkeypatch.setattr("tvmux.models.recording.run_bg", AsyncMock(return_value=Mock(pid=123)))
    terminate = Mock(return_value=True)
    monkeypatch.setattr("tvmux.models.recording.bg.terminate", terminate)
    return terminate


@pytest.mark.parametrize("failure", [None, OSError("reader failed"), asyncio.CancelledError()])
def test_failed_start_cleans_process_and_fifo(recorder, tmp_path, monkeypatch, startup_stubs, failure):
    reader = AsyncMock(return_value=False, side_effect=failure)
    monkeypatch.setattr(Recording, "_wait_for_reader", reader)
    expected = type(failure) if failure is not None else RuntimeError
    with pytest.raises(expected):
        asyncio.run(recorder.start("%1", tmp_path))
    startup_stubs.assert_called_once_with(123)
    assert not recorder.fifo_path.exists()
    assert recorder.asciinema_pid is None
    assert not recorder.active
    assert recorder.active_pane is None

    # A fresh start of the same object works after rollback.
    monkeypatch.setattr(Recording, "_wait_for_reader", AsyncMock(return_value=True))
    asyncio.run(recorder.start("%1", tmp_path))
    assert recorder.active
    assert recorder.fifo_path.exists()


def test_failure_before_spawn_removes_fifo(recorder, tmp_path, monkeypatch, startup_stubs):
    output = tmp_path / "not-a-directory"
    output.write_text("existing file")
    with pytest.raises(OSError):
        asyncio.run(recorder.start("%1", output))
    startup_stubs.assert_not_called()
    assert not recorder.fifo_path.exists()
    assert not recorder.active


@pytest.mark.parametrize("cancellations", [1, 3])
def test_cancel_during_spawn_waits_for_pid_then_cleans_up(
    recorder, tmp_path, monkeypatch, startup_stubs, cancellations,
):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def spawn(*args):
            entered.set()
            await release.wait()
            return Mock(pid=456)

        monkeypatch.setattr("tvmux.models.recording.run_bg", spawn)
        task = asyncio.create_task(recorder.start("%1", tmp_path))
        await entered.wait()
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        startup_stubs.assert_called_once_with(456)
        assert recorder.asciinema_pid is None
        assert not recorder.fifo_path.exists()
        assert not recorder.active

    asyncio.run(scenario())
