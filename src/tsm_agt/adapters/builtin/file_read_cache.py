"""Bounded, task-scoped reuse of file pages after checking an open file's version.

Cached pages remain full tool results. Whether their text can be omitted from a
particular model request belongs to core.read_result_reuse, which can see that
request's retained history. This cache must never manufacture an orphan stub.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import os
import stat
import threading
from typing import Any, Iterator


FileKey = tuple[str, str, str]  # task, approved root, canonical path
PageKey = tuple[FileKey, int, int, int]  # file, line, column, requested line limit


def file_version(info: os.stat_result) -> tuple[int, ...] | None:
    """Use POSIX change time as well as mtime; otherwise perform a normal read.

    Windows ctime may be creation time, and coarse/missing change timestamps
    cannot establish that a same-length rewrite preserving mtime was observed.
    Opening the file remains mandatory even on a hit, to recheck read access.
    """
    if os.name != "posix" or not stat.S_ISREG(info.st_mode):
        return None
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns",
              "st_mode", "st_uid", "st_gid")
    values = tuple(getattr(info, name, None) for name in fields)
    if (any(type(value) is not int for value in values) or not info.st_ino
            or info.st_ctime_ns <= 0 or info.st_ctime_ns % 1_000_000_000 == 0):
        return None
    return values


@dataclass(frozen=True)
class _Page:
    version: tuple[int, ...]
    data: dict[str, Any]
    content_bytes: int


class FileReadCache:
    """LRU pages with bounded content bytes and per-file concurrent read coalescing."""

    def __init__(self, *, max_pages: int = 256, max_content_bytes: int = 2 * 1024 * 1024):
        self._max_pages = max_pages
        self._max_content_bytes = max_content_bytes
        self._pages: OrderedDict[PageKey, _Page] = OrderedDict()
        self._content_bytes = 0
        self._mutex = threading.Lock()
        self._active: dict[FileKey, tuple[Any, int]] = {}

    @contextmanager
    def reading(self, key: FileKey) -> Iterator[None]:
        # Waiters retain the lock's identity. Unrelated files never hold a
        # global lock while doing disk I/O; idle lock entries are removed.
        with self._mutex:
            lock, users = self._active.get(key, (threading.Lock(), 0))
            self._active[key] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._mutex:
                _, users = self._active[key]
                if users == 1:
                    del self._active[key]
                else:
                    self._active[key] = (lock, users - 1)

    def get(self, key: PageKey, version: tuple[int, ...] | None) -> dict[str, Any] | None:
        with self._mutex:
            page = self._pages.pop(key, None)
            if page is not None and version is not None and page.version == version:
                self._pages[key] = page
                return deepcopy(page.data)
            if page is not None:
                self._content_bytes -= page.content_bytes
            if version is not None and key[1:3] == (1, 1):
                # A larger line limit doesn't require rereading a small file
                # already returned in full. Partial/other windows stay distinct.
                for source_key, source in reversed(self._pages.items()):
                    data = source.data
                    if (source_key[0] == key[0] and source.version == version
                            and source_key[1:3] == (1, 1) and data["next_cursor"] is None
                            and key[3] >= data["total_lines"]):
                        self._pages.move_to_end(source_key)
                        return deepcopy(data)
            return None

    def put(self, key: PageKey, version: tuple[int, ...], data: dict[str, Any]) -> None:
        size = len(data["content"].encode("utf-8"))
        with self._mutex:
            # Discard all old windows when a new version is observed.
            for old_key in tuple(self._pages):
                if old_key[0] == key[0] and (old_key == key or self._pages[old_key].version != version):
                    self._content_bytes -= self._pages.pop(old_key).content_bytes
            if size > self._max_content_bytes:
                return
            self._pages[key] = _Page(version, deepcopy(data), size)
            self._content_bytes += size
            while len(self._pages) > self._max_pages or self._content_bytes > self._max_content_bytes:
                _, page = self._pages.popitem(last=False)
                self._content_bytes -= page.content_bytes

    def clear(self) -> None:
        with self._mutex:
            self._pages.clear()
            self._content_bytes = 0
