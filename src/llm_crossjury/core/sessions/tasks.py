"""Work detached from the HTTP request that started it (ADR 0008)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

_log = logging.getLogger(__name__)


class TaskRunner:
    """Runs coroutines as tasks that survive a cancelled (disconnected) request.

    Implements :class:`~llm_crossjury.core.sessions.interfaces.TaskRunnerLike`. The
    runner keeps a strong reference to every task until it finishes, so a task is
    never garbage-collected mid-run, and awaits it under :func:`asyncio.shield`, so
    cancelling the awaiting request leaves the task running. The task's own code
    writes its result into the session.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._by_key: dict[str, asyncio.Task[Any]] = {}

    async def run(self, key: str, coro: Coroutine[Any, Any, Any]) -> Any:
        """Start ``coro`` as a task and wait for its result.

        The result, or the task's exception, is returned to the caller while it is
        still waiting. If the caller is cancelled, the task keeps running.

        Args:
            key: Names the work for :meth:`running` (e.g. ``"<session>/<turn>"``).
            coro: The work.
        """
        task = asyncio.get_running_loop().create_task(coro, name=f"crossjury:{key}")
        self._tasks.add(task)
        self._by_key[key] = task
        task.add_done_callback(lambda t: self._finished(key, t))
        return await asyncio.shield(task)

    def running(self, key: str) -> bool:
        """Whether the work started under ``key`` is still running."""
        task = self._by_key.get(key)
        return task is not None and not task.done()

    def active(self) -> int:
        """Number of tasks still running."""
        return sum(1 for t in self._tasks if not t.done())

    def _finished(self, key: str, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if self._by_key.get(key) is task:
            del self._by_key[key]
        if task.cancelled():
            return
        exc = task.exception()  # marks it retrieved when nobody awaits any more
        if exc is not None:
            # The type only: an exception message is not guaranteed secret-free.
            _log.debug("detached task %s failed (%s)", key, type(exc).__name__)


__all__ = ["TaskRunner"]
