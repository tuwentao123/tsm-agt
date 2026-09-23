"""Conservative local command gate for approved engineering commands.

This Adapter decides whether a structured process request may reach the local
executor. It is deliberately not advertised as OS/container isolation: a
permitted compiler or interpreter still has the host permissions of tsm-agt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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
_TRUSTED_RUNTIME_ORIGINS = tuple(
    Path(origin).expanduser().resolve()
    for origin in (
        "~/.local/share/uv/python",
        "~/.pyenv",
        "/opt/homebrew",
        "/usr/bin",
    )
)
_RUNTIME_TRUST_CACHE = ".tsm/runtime_trust.json"
_RUNTIME_TRUST_SCHEMA_VERSION = 2


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


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
        resolved_executable = None
        if len(path.parts) > 1:
            try:
                resolved_executable = (
                    self._workspace_path.resolve_access_path(
                        request.cwd, executable
                    ).path
                    if not path.is_absolute()
                    else path.expanduser().resolve(strict=True)
                )
            except FileNotFoundError:
                return SandboxDecision(False, "workspace executable does not exist")
            except (PermissionError, ValueError):
                return SandboxDecision(
                    False,
                    "relative executable path escapes the working directory",
                )
            if not resolved_executable.is_file():
                return SandboxDecision(False, "workspace executable does not exist")
            runtime_decision = self._authorize_runtime_origin(
                request.cwd,
                path,
                resolved_executable,
            )
            if runtime_decision is not None:
                return runtime_decision

        return SandboxDecision(
            True,
            "approved structured command passed the local workspace command gate; "
            "host filesystem and network are not OS-isolated",
            diagnostics={
                "resolved_executable": (
                    str(resolved_executable) if resolved_executable else executable
                ),
            },
        )

    def _authorize_runtime_origin(
        self,
        workspace: Path,
        requested_path: Path,
        resolved_path: Path,
    ) -> SandboxDecision | None:
        executable_name = resolved_path.name.casefold()
        if _PYTHON_NAME.fullmatch(executable_name) is None:
            return None
        workspace_root = workspace.resolve()
        if _path_is_relative_to(resolved_path, workspace_root):
            return None
        if not self._is_trusted_runtime_origin(resolved_path):
            fingerprint = self._fingerprint_executable(resolved_path)
            cache = self._load_runtime_trust_cache(workspace_root)
            runtime_key = str(resolved_path)
            runtime_record = cache["runtimes"].get(runtime_key)
            cached_fingerprint = (
                runtime_record.get("fingerprint")
                if isinstance(runtime_record, dict)
                else None
            )
            if cached_fingerprint is None:
                cache["runtimes"][runtime_key] = {
                    "fingerprint": fingerprint,
                    "trust_source": "tofu",
                    "enrolled_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                }
                self._store_runtime_trust_cache(workspace_root, cache)
                return SandboxDecision(
                    True,
                    "runtime trusted via TOFU workspace cache",
                    diagnostics={
                        "requested_executable": str(requested_path),
                        "resolved_executable": str(resolved_path),
                        "trust_mode": "tofu",
                        "fingerprint": fingerprint,
                        "cache_state": "enrolled",
                        "origin_classification": "external-runtime",
                    },
                )
            if cached_fingerprint != fingerprint:
                cache["runtimes"].pop(runtime_key, None)
                self._store_runtime_trust_cache(workspace_root, cache)
                return SandboxDecision(
                    False,
                    "runtime fingerprint changed since TOFU enrollment",
                    diagnostics={
                        "requested_executable": str(requested_path),
                        "resolved_executable": str(resolved_path),
                        "cached_fingerprint": cached_fingerprint,
                        "observed_fingerprint": fingerprint,
                        "cache_state": "revoked-after-drift",
                        "drift_reason": "fingerprint-mismatch",
                        "origin_classification": "external-runtime",
                    },
                )
            runtime_record["updated_at"] = datetime.now().isoformat()
            self._store_runtime_trust_cache(workspace_root, cache)
            return SandboxDecision(
                True,
                "runtime trusted via persisted TOFU fingerprint",
                diagnostics={
                    "requested_executable": str(requested_path),
                    "resolved_executable": str(resolved_path),
                    "trust_mode": "persisted-tofu",
                    "fingerprint": fingerprint,
                    "cache_state": "persisted",
                    "origin_classification": "external-runtime",
                },
            )
        return None

    def _is_trusted_runtime_origin(self, path: Path) -> bool:
        try:
            stat_result = path.stat()
        except OSError:
            return False
        if not os.access(path, os.X_OK):
            return False
        if stat_result.st_mode & stat.S_IWOTH:
            return False
        current_uid = getattr(os, "getuid", lambda: stat_result.st_uid)()
        if stat_result.st_uid not in {0, current_uid}:
            return False
        return any(
            _path_is_relative_to(path, origin)
            for origin in _TRUSTED_RUNTIME_ORIGINS
        )

    def _fingerprint_executable(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as executable:
            digest.update(executable.read())
        return digest.hexdigest()

    def _load_runtime_trust_cache(self, workspace_root: Path) -> dict[str, object]:
        cache_path = workspace_root / _RUNTIME_TRUST_CACHE
        empty_cache = {
            "version": _RUNTIME_TRUST_SCHEMA_VERSION,
            "runtimes": {},
        }
        if not cache_path.exists():
            return empty_cache
        try:
            payload = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return empty_cache
        if not isinstance(payload, dict):
            return empty_cache
        runtimes = payload.get("runtimes")
        if isinstance(runtimes, dict):
            return {
                "version": int(payload.get("version", _RUNTIME_TRUST_SCHEMA_VERSION)),
                "runtimes": runtimes,
            }
        return {
            "version": 1,
            "runtimes": {
                str(key): {
                    "fingerprint": str(value),
                    "trust_source": "legacy-tofu",
                }
                for key, value in payload.items()
            },
        }

    def _store_runtime_trust_cache(
        self,
        workspace_root: Path,
        cache: dict[str, object],
    ) -> None:
        cache_path = workspace_root / _RUNTIME_TRUST_CACHE
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True))
