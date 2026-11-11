"""Run untrusted-file processing in a separate, killable worker process.

A hostile file can make a parser loop, exhaust memory or crash the interpreter. The
work therefore runs in a fresh ``spawn`` process (the only start method on Windows,
and used everywhere for identical behavior); the caller waits at most ``timeout_s``
and kills the worker afterwards. The call blocks: async callers should wrap it in
``asyncio.to_thread``.
"""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import TypeVar, cast

from llm_crossjury.core.attachments.limits import (
    AttachmentError,
    AttachmentExtractionError,
    AttachmentTimeoutError,
    AttachmentTooLargeError,
    UnsupportedAttachmentError,
)

T = TypeVar("T")

_KILL_JOIN_S = 5.0

_ERRORS: dict[str, type[AttachmentError]] = {
    cls.__name__: cls
    for cls in (
        AttachmentError,
        AttachmentExtractionError,
        AttachmentTimeoutError,
        AttachmentTooLargeError,
        UnsupportedAttachmentError,
    )
}


def run_isolated(func: Callable[..., T], args: tuple[object, ...], timeout_s: float) -> T:
    """Call ``func(*args)`` in a new worker process and return its result.

    ``func``, ``args`` and the result must be picklable, and ``func`` must be
    importable by module path (spawn start method).

    Args:
        func: A module-level function.
        args: Positional arguments.
        timeout_s: Wall-clock limit, including worker start-up.

    Returns:
        The function's result.

    Raises:
        AttachmentTimeoutError: The worker did not finish in time (it is killed).
        AttachmentExtractionError: The worker crashed or raised an unexpected error.
        AttachmentError: The worker raised an :class:`AttachmentError` (re-raised
            with the same class and message).
    """
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_worker, args=(writer, func, args), daemon=True)
    process.start()
    writer.close()
    try:
        if not reader.poll(timeout_s):
            raise AttachmentTimeoutError(
                f"extraction took longer than {timeout_s:g} s and was stopped"
            )
        try:
            status, payload, message = reader.recv()
        except EOFError:
            raise AttachmentExtractionError("the extraction worker crashed") from None
    finally:
        reader.close()
        if process.is_alive():
            process.kill()
        process.join(_KILL_JOIN_S)
    if status == "ok":
        return cast("T", payload)
    raise _ERRORS.get(str(payload), AttachmentExtractionError)(str(message))


def _worker(conn: Connection, func: Callable[..., object], args: tuple[object, ...]) -> None:
    """Worker entry point: run ``func`` and send ``(status, payload, message)``."""
    try:
        result = func(*args)
    except AttachmentError as exc:
        conn.send(("error", type(exc).__name__, str(exc)))
    except Exception as exc:  # report the type only, never content
        conn.send(
            (
                "error",
                AttachmentExtractionError.__name__,
                f"could not process the file ({type(exc).__name__})",
            )
        )
    else:
        conn.send(("ok", result, ""))
    finally:
        conn.close()
