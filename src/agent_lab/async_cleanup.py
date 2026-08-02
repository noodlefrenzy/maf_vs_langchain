"""Bounded asynchronous cleanup helpers for local experiment services."""

from __future__ import annotations

import asyncio
import socket
import sys
from contextlib import suppress
from typing import Any, Protocol


class ExitSignaledServer(Protocol):
    should_exit: bool


async def shutdown_server(
    server: ExitSignaledServer,
    server_task: asyncio.Task[Any],
    listener: socket.socket,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Bound server cleanup without replacing an active experiment exception."""

    server.should_exit = True
    try:
        await asyncio.wait_for(asyncio.shield(server_task), timeout=timeout_seconds)
    except TimeoutError:
        server_task.cancel()
        with suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(server_task, timeout=min(timeout_seconds, 1.0))
    except Exception as exc:  # pragma: no cover - defensive server cleanup boundary
        print(f"local experiment service shutdown failed: {type(exc).__name__}", file=sys.stderr)
    finally:
        listener.close()
