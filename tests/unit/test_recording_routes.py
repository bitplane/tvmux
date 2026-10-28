"""Recording requests must serialize startup for each window."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response

from tvmux.server.routers import recording


@pytest.fixture(autouse=True)
def clean_state():
    recording.recorders.clear()
    recording._start_locks.clear()
    yield
    recording.recorders.clear()
    recording._start_locks.clear()


def request(window="@1"):
    return recording.RecordingCreate(
        session_id="main", window_id=window, active_pane="%1", output_dir="/tmp",
    )


def test_concurrent_starts_spawn_once(monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        recorder = Mock(active=False)

        async def start(*args):
            entered.set()
            await release.wait()
            recorder.active = True

        recorder.start = AsyncMock(side_effect=start)
        factory = Mock(return_value=recorder)
        monkeypatch.setattr(recording, "Recording", factory)
        responses = [Response(), Response()]
        first = asyncio.create_task(recording.create_recording(request(), responses[0]))
        await entered.wait()
        second = asyncio.create_task(recording.create_recording(request(), responses[1]))
        await asyncio.sleep(0)
        assert factory.call_count == 1
        release.set()
        results = await asyncio.gather(first, second)
        assert results == [recorder, recorder]
        assert [r.status_code for r in responses] == [201, 202]
        assert recording.recorders == {"main:@1": recorder}
        assert not recording._start_locks

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError("failed"), asyncio.CancelledError()])
def test_failed_or_cancelled_start_releases_lock(monkeypatch, failure):
    async def scenario():
        first = Mock(active=False, start=AsyncMock(side_effect=failure))
        second = Mock(active=True, start=AsyncMock())
        monkeypatch.setattr(recording, "Recording", Mock(side_effect=[first, second]))
        error = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else HTTPException
        with pytest.raises(error):
            await recording.create_recording(request(), Response())
        response = Response()
        result = await recording.create_recording(request(), response)
        assert result is second
        assert response.status_code == 201

    asyncio.run(scenario())


def test_different_windows_can_start_concurrently(monkeypatch):
    async def scenario():
        both_entered = asyncio.Event()
        count = 0

        async def start(*args):
            nonlocal count
            count += 1
            if count == 2:
                both_entered.set()
            await both_entered.wait()

        monkeypatch.setattr(recording, "Recording", lambda **kw: Mock(start=AsyncMock(side_effect=start)))
        await asyncio.wait_for(asyncio.gather(
            recording.create_recording(request("@1"), Response()),
            recording.create_recording(request("@2"), Response()),
        ), timeout=2)
        assert len(recording.recorders) == 2

    asyncio.run(scenario())
