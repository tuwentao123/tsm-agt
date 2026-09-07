"""Conservative local command gate for approved engineering commands.

This Adapter decides whether a structured process request may reach the local
executor. It is deliberately not advertised as OS/container isolation: a
permitted compiler or interpreter still has the host permissions of tsm-agt.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    SandboxDecision,
    SandboxRequest,
    WorkspacePathPort,
)


_ALLOWED_EXECUTABLES = frozenset(
    {
        "adb", "bun", "bundle", "cargo", "cmake", "dart",
        "flutter", "git", "go", "gradle", "gradlew", "java",
        "javac", "kotlinc", "make", "ninja", "node", "npm",
        "npx", "pnpm", "pytest", "ruby", "ruff", "rustc",
        "swift", "xcodebuild", "yarn",
    }
)
_DENIED_SHELLS = frozenset(
    {
        "ash", "bash", "csh", "dash", "fish", "ksh", "sh",
        "tcsh", "zsh", "cmd", "cmd.exe", "powershell", "pwsh",
    }
)
_PYTHON_NAME = re.compile(r"python(?:3(?:\.\d+)?)?\Z")
_CREDENTIAL_FRAGMENTS = ("secret", "token", "password", "api_key", "apikey")
_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat")
_ALLOWED_BATCH_LAUNCHERS = frozenset(
    {"gradlew", "npm", "npx", "pnpm", "yarn", "bun"}
)
_CMD_METACHARACTERS = frozenset("&|<>^%!?\r\n")


class LocalWorkspaceSandbox:
    """Allow a small engineering executable set after Kernel validation."""

    descriptor = AdapterDescriptor(
        adapter_id="builtin.local-workspace-command-gate",
        adapter_version="0.1.0",
        port_name="SandboxPort",
        port_version="1.0",
        capabilities=frozenset(
            {
                "structured-argv-only", "shell-deny",
                "executable-allowlist", "credential-env-deny",
                "workspace-cwd-precondition",
            }
        ),
    )

    def __init__(self, workspace_path: WorkspacePathPort) -> None:
        self._workspace_path = workspace_path
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            (
                "local workspace command gate ready; no OS isolation"
                if self._started else "not started"
            ),
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def authorize(self, request: SandboxRequest) -> SandboxDecision:
        if not self._started:
            return SandboxDecision(False, "local command gate is not started")
        if not request.argv:
            return SandboxDecision(False, "structured argv must not be empty")
        if not request.cwd.is_absolute() or not request.cwd.is_dir():
            return SandboxDecision(False, "working directory must be an existing absolute directory")
        if any(not item or "\x00" in item for item in request.argv):
            return SandboxDecision(False, "argv contains an empty value or NUL byte")
        for name, value in request.environment.items():
            lowered = name.lower()
            if (
                not name or "=" in name or "\x00" in name or "\x00" in value
                or any(fragment in lowered for fragment in _CREDENTIAL_FRAGMENTS)
            ):
                return SandboxDecision(False, "credential-like or invalid environment entry is denied")

        executable = request.argv[0]
        name = Path(executable).name.lower()
        normalized_name = name
        suffix = Path(name).suffix
        if suffix in _WINDOWS_EXECUTABLE_SUFFIXES:
            normalized_name = name[:-len(suffix)]
        if name in _DENIED_SHELLS or normalized_name in _DENIED_SHELLS:
            return SandboxDecision(False, f"shell executable is denied: {name}")
        if (
            suffix in {".cmd", ".bat"}
            and (
                normalized_name not in _ALLOWED_BATCH_LAUNCHERS
                or any(
                    character in _CMD_METACHARACTERS
                    for argument in request.argv for character in argument
                )
            )
        ):
            return SandboxDecision(
                False, "batch launcher or argument is outside the controlled Windows subset"
            )
        if (
            normalized_name not in _ALLOWED_EXECUTABLES
            and _PYTHON_NAME.fullmatch(normalized_name) is None
        ):
            return SandboxDecision(False, f"executable is not in the local engineering allowlist: {name}")

        path = Path(executable)
        if len(path.parts) > 1 and not path.is_absolute():
            try:
                resolved = self._workspace_path.resolve_access_path(
                    request.cwd, executable
                ).path
            except (PermissionError, ValueError):
                return SandboxDecision(False, "relative executable path escapes the working directory")
            if not resolved.is_file():
                return SandboxDecision(False, "workspace executable does not exist")

        return SandboxDecision(
            True,
            "approved structured command passed the local workspace command gate; "
            "host filesystem and network are not OS-isolated",
        )
