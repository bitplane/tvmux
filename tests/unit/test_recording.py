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
