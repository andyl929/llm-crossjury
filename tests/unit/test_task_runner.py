"""Task runner: results, errors, and work that survives a cancelled request."""

from __future__ import annotations

import asyncio

import pytest

from llm_crossjury.core.sessions.tasks import TaskRunner


def test_returns_the_result() -> None:
    async def main() -> object:
        runner = TaskRunner()

        async def work() -> int:
            return 42

        return await runner.run("k", work())

    assert asyncio.run(main()) == 42


def test_exception_reaches_the_waiting_caller() -> None:
    async def main() -> None:
        runner = TaskRunner()

        async def work() -> None:
            raise ValueError("boom")

        await runner.run("k", work())

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(main())


def test_cancelled_caller_does_not_cancel_the_work() -> None:
    finished: list[str] = []

    async def main() -> None:
        runner = TaskRunner()
        release = asyncio.Event()

        async def work() -> str:
            await release.wait()
            finished.append("done")
            return "done"

        caller = asyncio.create_task(runner.run("s/t", work()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert runner.running("s/t")
        caller.cancel()  # the client disconnected
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert runner.running("s/t")
        assert runner.active() == 1
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not runner.running("s/t")
        assert runner.active() == 0

    asyncio.run(main())
    assert finished == ["done"]


def test_failure_after_disconnect_is_retrieved_quietly() -> None:
    async def main() -> None:
        runner = TaskRunner()
        release = asyncio.Event()

        async def work() -> None:
            await release.wait()
            raise RuntimeError("late failure")

        caller = asyncio.create_task(runner.run("k", work()))
        await asyncio.sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not runner.running("k")

    asyncio.run(main())  # no "exception was never retrieved" (warnings are errors)


def test_unknown_key_is_not_running() -> None:
    assert TaskRunner().running("nothing") is False
