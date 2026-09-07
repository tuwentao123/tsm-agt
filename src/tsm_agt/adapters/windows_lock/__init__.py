"""Windows implementation of cross-process workspace locks."""

from .lock import WindowsCrossProcessLock

__all__ = ["WindowsCrossProcessLock"]
