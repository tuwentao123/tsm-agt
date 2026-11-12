"""Commit notifications are wake-up hints; the underlying log owns the data.

All access is on the owning Runtime loop. A subscriber captures the current
Event *before* reading the log, so a commit between read and wait cannot be lost.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import contextmanager

from tsm_agt.ports import RuntimeUnitOfWork

logger = logging.getLogger(__name__)


class CommitSignal:
    def __init__(self):
        self.changed = asyncio.Event()
        self.users = 0

    def notify(self):
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()


class EventedRuntimeStore:
    def __init__(self, inner):
        self.inner = inner
        self._signals: dict[tuple[str, str], CommitSignal] = {}
        self.on_task_commit: Callable[[RuntimeUnitOfWork], None] | None = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    @contextmanager
    def watch(self, identity: str, *, scope: str = "task"):
        key = (scope, identity)
        signal = self._signals.setdefault(key, CommitSignal())
        signal.users += 1
        try:
            yield signal
        finally:
            signal.users -= 1
            if not signal.users:
                self._signals.pop(key, None)

    def notify(self, identity: str, *, scope: str = "task"):
        signal = self._signals.get((scope, identity))
        if signal is not None:
            signal.notify()

    def _notify_task_commit(self, unit: RuntimeUnitOfWork):
        self.notify(unit.task_id)
        if self.on_task_commit is not None and unit.events:
            try:
                # Observers may schedule work after the commit and must not block.
                self.on_task_commit(unit)
            except Exception as error:
                logger.warning("Task commit observer failed (%s)", type(error).__name__)

    async def commit(self, unit):
        result = await self.inner.commit(unit)
        self._notify_task_commit(unit)
        return result

    async def commit_session(self, unit):
        result = await self.inner.commit_session(unit)
        self.notify(unit.session_id, scope="session")
        for event in unit.events:
            if task_id := event.payload.get("task_id"):
                self.notify(str(task_id))
        return result

    async def commit_session_and_task(self, unit):
        result = await self.inner.commit_session_and_task(unit)
        self._notify_task_commit(unit.task)
        self.notify(unit.session.session_id, scope="session")
        return result


def evented_store(store):
    return store if isinstance(store, EventedRuntimeStore) else EventedRuntimeStore(store)
