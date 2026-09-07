"""Stable local operating-system subject identity boundary."""

from __future__ import annotations

from typing import Protocol

from .adapter import RuntimeAdapter


class LocalIdentityPort(RuntimeAdapter, Protocol):
    def current_subject(self) -> str: ...
