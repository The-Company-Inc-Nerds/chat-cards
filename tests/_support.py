"""Shared scaffolding for the tests that need a socket (loopback only, no network).

A stand-in upstream is a real HTTP server on 127.0.0.1, because what these tests
pin down is behaviour at the socket: a refused connection, a server that goes
away mid-session and comes back on the same port.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any, Awaitable, Callable

from aiohttp import web


def free_port() -> int:
    """A loopback port with nothing listening on it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def until(condition: Callable[[], Any], timeout: float = 5.0) -> None:
    """Wait for `condition` to turn true; fail the test if it never does."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition was still false after %.1fs" % timeout)
        await asyncio.sleep(0.01)


class Upstream:
    """An HTTP server on a fixed loopback port that can be taken down and brought back.

    Every request is recorded as (method, path, json-or-None) and answered by
    `respond`, a coroutine the test supplies or swaps mid-run.
    """

    def __init__(self, respond: Callable[[web.Request], Awaitable[web.StreamResponse]] | None = None) -> None:
        self.port = free_port()
        self.requests: list[tuple[str, str, Any]] = []
        self.respond = respond or self._ok
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @staticmethod
    async def _ok(_request: web.Request) -> web.StreamResponse:
        return web.json_response({"ok": True})

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = None
        if request.can_read_body:
            try:
                body = await request.json()
            except ValueError:
                pass
        self.requests.append((request.method, request.path, body))
        return await self.respond(request)

    async def up(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        # A handler still running when the stand-in is taken down is one a test
        # left hanging on purpose; aiohttp's default is to give it a minute.
        self._runner = web.AppRunner(app, shutdown_timeout=0.1)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def down(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
