"""Commit notifications are wake-up hints; the underlying log owns the data.

All access is on the owning Runtime loop. A subscriber captures the current
Event *before* reading the log, so a commit between read and wait cannot be lost.
No callback or network operation runs on the transaction's success path.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager


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

    async def commit(self, unit):
        result = await self.inner.commit(unit)
        self.notify(unit.task_id)
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
        self.notify(unit.task.task_id)
        self.notify(unit.session.session_id, scope="session")
        return result


def evented_store(store):
    return store if isinstance(store, EventedRuntimeStore) else EventedRuntimeStore(store)
