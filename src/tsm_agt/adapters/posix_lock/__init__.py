"""POSIX implementation of cross-process workspace locks."""

from .lock import PosixCrossProcessLock

__all__ = ["PosixCrossProcessLock"]
