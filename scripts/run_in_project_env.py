#!/usr/bin/env python3
"""Unified launcher for local, CI, container, and service environments."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    src_path = str(SRC)
    env["PYTHONPATH"] = (
        f"{src_path}{os.pathsep}{existing}" if existing else src_path
    )
    return env


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: run_in_project_env.py [pytest|web|module] ...",
            file=sys.stderr,
        )
        return 2

    mode, *rest = argv
    env = _build_env()

    if mode == "pytest":
        command = [sys.executable, "-m", "pytest", *rest]
    elif mode == "web":
        command = [sys.executable, "-m", "tsm_agt.web.app", *rest]
    elif mode == "module":
        if not rest:
            print("module mode requires a module name", file=sys.stderr)
            return 2
        command = [sys.executable, "-m", rest[0], *rest[1:]]
    else:
        print(f"unsupported mode: {mode}", file=sys.stderr)
        return 2

    completed = subprocess.run(command, cwd=ROOT, env=env)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
