"""Global FIFO queue that allows N heavy jobs at once and reports line position."""
from __future__ import annotations
import asyncio
from collections import deque
from typing import Awaitable, Callable


class JobQueue:
    def __init__(self, max_active: int):
        self.max_active = max_active
        self.active = 0
        self.waiting: deque[object] = deque()
        self.cond = asyncio.Condition()

    def position(self, ticket: object) -> int:
        """1-based place in line (0 = not waiting)."""
        try:
            return list(self.waiting).index(ticket) + 1
        except ValueError:
            return 0

    async def acquire(self, on_position: Callable[[int], Awaitable[None]] | None = None) -> object:
        ticket = object()
        async with self.cond:
            self.waiting.append(ticket)
            last = None
            while not (self.active < self.max_active and self.waiting[0] is ticket):
                pos = self.position(ticket)
                if on_position and pos != last:
                    last = pos
                    try:
                        await on_position(pos)
                    except Exception:
                        pass
                await self.cond.wait()
            self.waiting.popleft()
            self.active += 1
            self.cond.notify_all()
        return ticket

    async def release(self) -> None:
        async with self.cond:
            self.active -= 1
            self.cond.notify_all()

    async def run(self, fn: Callable[[], Awaitable], on_position=None):
        await self.acquire(on_position)
        try:
            return await fn()
        finally:
            await self.release()
