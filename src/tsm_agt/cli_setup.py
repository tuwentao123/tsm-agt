"""First-run setup and safe, machine-readable CLI diagnostics."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import platform
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tsm_agt import __version__
from tsm_agt.bootstrap import (
    compose_fixture_application, compose_openai_compatible_readonly_application,
)
from tsm_agt.bootstrap.model_configuration import (
    MODEL_ENV_NAMES, MODEL_ENV_TEMPLATE, ModelConfiguration,
    inspect_model_configuration,
)
from tsm_agt.ports import (
    HealthState, Message, MessageRole, ModelProviderPort, ModelRequest, TextBlock,
)


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    status: str
    summary: str
    required: bool = True
    remedy: str | None = None
    details: Mapping[str, Any] | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "required": self.required,
            "summary": self.summary,
        }
        if self.remedy is not None:
            data["remedy"] = self.remedy
        if self.details is not None:
            data["details"] = dict(self.details)
        return data


def initialize_workspace(workspace: Path, *, print_only: bool = False) -> int:
    """Create one private model template without overwriting existing data."""

    if print_only:
        print(MODEL_ENV_TEMPLATE, end="")
        return 0
    root = workspace.expanduser().resolve()
    if not root.exists():
        raise ValueError(f"workspace does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    target = root / ".env"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as error:
        raise ValueError(
            f"refusing to overwrite existing configuration: {target}"
        ) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(MODEL_ENV_TEMPLATE)
        if os.name != "nt":
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    print(f"created private model configuration: {target}")
    print("next:")
    print(f"  1. edit {target}")
    print(f"  2. tsm-agt doctor --workspace {root} --model-check")
    print(f"  3. tsm-agt chat --workspace {root}")
    if os.name == "nt":
        print("note: verify the .env ACL in Windows; POSIX mode bits do not apply")
    return 0


async def run_doctor(
    workspace: Path, *, as_json: bool = False, model_check: bool = False,
    environment: Mapping[str, str] | None = None,
    model_probe: Callable[[ModelConfiguration], Awaitable[None]] | None = None,
) -> int:
    """Inspect installation, platform adapters, workspace, and model access."""

    root = workspace.expanduser().resolve()
    checks: list[DiagnosticCheck] = []
    checks.append(_python_check())
    checks.append(_platform_check())
    checks.extend(_workspace_checks(root))
    checks.append(await _adapter_check())

    configuration: ModelConfiguration | None = None
    config_error: str | None = None
    missing: tuple[str, ...] = ()
    try:
        configuration, missing = inspect_model_configuration(
            root / ".env", environment if environment is not None else os.environ
        )
    except (OSError, UnicodeError, ValueError) as error:
        config_error = str(error)

    if config_error is not None:
        checks.append(DiagnosticCheck(
            "model_configuration", "fail", "model configuration is invalid",
            remedy=f"fix {root / '.env'}: {config_error}",
        ))
    elif configuration is None:
        checks.append(DiagnosticCheck(
            "model_configuration", "fail", "model configuration is incomplete",
            remedy=(
                f"run `tsm-agt init --workspace {root}` and configure: "
                + ", ".join(missing)
            ),
            details={"missing": list(missing)},
        ))
    else:
        checks.append(DiagnosticCheck(
            "model_configuration", "pass", "model configuration is complete",
            details={
                "endpoint_origin": configuration.endpoint_origin,
                "model": configuration.model,
                "credentials_configured": True,
                "sources": {
                    name: configuration.sources[name] for name in MODEL_ENV_NAMES
                },
            },
        ))
        checks.append(_configuration_file_permission_check(configuration.env_file))

    if model_check:
        if configuration is None:
            checks.append(DiagnosticCheck(
                "model_connectivity", "fail",
                "model check cannot run without complete configuration",
                remedy="fix the model_configuration check first",
            ))
        else:
            try:
                await (model_probe or _probe_model)(configuration)
            except Exception as error:
                checks.append(DiagnosticCheck(
                    "model_connectivity", "fail",
                    f"model endpoint check failed: {_safe_error(
                        error, configuration.api_key, configuration.base_url
                    )}",
                    remedy=(
                        "verify the endpoint, model name, API key, and that the "
                        "provider implements OpenAI-compatible Chat Completions"
                    ),
                ))
            else:
                checks.append(DiagnosticCheck(
                    "model_connectivity", "pass",
                    "provider accepted a fixed, project-free smoke request",
                ))

    healthy = all(
        check.status == "pass" for check in checks if check.required
    )
    report = {
        "schema_version": 1,
        "tool": "tsm-agt",
        "version": __version__,
        "workspace": str(root),
        "model_check_requested": model_check,
        "healthy": healthy,
        "checks": [check.to_data() for check in checks],
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_doctor_report(report)
    return 0 if healthy else 1


def _python_check() -> DiagnosticCheck:
    current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info[:2] == (3, 12):
        return DiagnosticCheck("python", "pass", f"Python {current}")
    return DiagnosticCheck(
        "python", "fail", f"Python {current} is unsupported",
        remedy="install and run tsm-agt with Python 3.12",
    )


def _platform_check() -> DiagnosticCheck:
    details = {"system": platform.system(), "machine": platform.machine()}
    if os.name in {"posix", "nt"}:
        return DiagnosticCheck(
            "platform", "pass",
            f"{details['system']} {details['machine']} platform adapters selected",
            details=details,
        )
    return DiagnosticCheck(
        "platform", "fail", f"unsupported OS interface: {os.name}",
        details=details,
    )


def _workspace_checks(root: Path) -> list[DiagnosticCheck]:
    if not root.exists():
        return [DiagnosticCheck(
            "workspace", "fail", f"workspace does not exist: {root}",
            remedy="create the directory or pass an existing --workspace",
        )]
    if not root.is_dir():
        return [DiagnosticCheck(
            "workspace", "fail", f"workspace is not a directory: {root}",
            remedy="pass a directory to --workspace",
        )]
    checks = [DiagnosticCheck(
        "workspace", "pass", "workspace exists and is a directory"
    )]
    runtime = root / ".agent"
    writable = (
        runtime.is_dir() and os.access(runtime, os.W_OK)
        if runtime.exists() else os.access(root, os.W_OK)
    )
    invalid_runtime = runtime.exists() and not runtime.is_dir()
    if writable and not invalid_runtime:
        checks.append(DiagnosticCheck(
            "runtime_storage", "pass",
            ".agent runtime storage is writable or can be created",
        ))
    else:
        checks.append(DiagnosticCheck(
            "runtime_storage", "fail",
            ".agent runtime storage is unavailable",
            remedy=f"make {runtime} a writable directory",
        ))
    return checks


def _configuration_file_permission_check(path: Path) -> DiagnosticCheck:
    if not path.exists():
        return DiagnosticCheck(
            "model_configuration_file", "pass",
            "model configuration comes from exported environment variables",
            required=False,
        )
    if os.name == "nt":
        return DiagnosticCheck(
            "model_configuration_file", "warn",
            "Windows ACL privacy requires native verification",
            required=False, remedy=f"verify that only your account can read {path}",
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return DiagnosticCheck(
            "model_configuration_file", "warn",
            f"configuration permissions are {mode:04o}, not private",
            required=False, remedy=f"run `chmod 600 {path}`",
        )
    return DiagnosticCheck(
        "model_configuration_file", "pass",
        f"configuration permissions are private ({mode:04o})", required=False,
    )


async def _adapter_check() -> DiagnosticCheck:
    application = compose_fixture_application()
    try:
        await application.registry.start_all()
        unhealthy: list[str] = []
        for adapter in application.registry.runtime_adapters():
            health = await adapter.health()
            if health.state is not HealthState.HEALTHY:
                unhealthy.append(
                    f"{adapter.descriptor.adapter_id}:{health.state.value}"
                )
        if unhealthy:
            return DiagnosticCheck(
                "runtime_adapters", "fail",
                "one or more platform adapters are unhealthy",
                details={"unhealthy": unhealthy},
            )
        return DiagnosticCheck(
            "runtime_adapters", "pass",
            f"{len(application.registry.descriptors())} adapters started and healthy",
        )
    except Exception as error:
        return DiagnosticCheck(
            "runtime_adapters", "fail",
            f"adapter composition failed: {_safe_error(error)}",
        )
    finally:
        await application.registry.stop_all()


async def _probe_model(configuration: ModelConfiguration) -> None:
    with tempfile.TemporaryDirectory(prefix="tsm-agt-doctor-") as directory:
        application = compose_openai_compatible_readonly_application(
            base_url=configuration.base_url, model=configuration.model,
            api_key=configuration.api_key,
            database_path=Path(directory) / "runtime.db",
            model_timeout_seconds=configuration.timeout_seconds,
            model_max_retries=configuration.max_retries,
            model_retry_backoff_seconds=configuration.retry_backoff_seconds,
            model_output_token_parameter=(
                configuration.output_token_parameter
            ),
            model_strict_tool_schema=configuration.strict_tool_schema,
        )
        await application.registry.start_all()
        try:
            provider = application.registry.require(ModelProviderPort)
            response = await asyncio.wait_for(provider.complete(ModelRequest(
                "doctor-smoke",
                (Message(
                    "doctor-user", MessageRole.USER,
                    (TextBlock("Reply with exactly OK. This is a connectivity check."),),
                ),),
                max_output_tokens=8, tools=(), allow_tool_calls=False,
            )), timeout=65)
            if not response.message.text.strip():
                raise RuntimeError("provider returned an empty assistant message")
        finally:
            await application.registry.stop_all()


def _safe_error(error: Exception, *secrets: str) -> str:
    text = " ".join(str(error).split())
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text[:300] if text else type(error).__name__


def _print_doctor_report(report: Mapping[str, Any]) -> None:
    print(f"tsm-agt {report['version']} doctor")
    print(f"workspace: {report['workspace']}")
    for raw in report["checks"]:
        marker = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}[
            raw["status"]
        ]
        print(f"[{marker}] {raw['name']}: {raw['summary']}")
        details = raw.get("details")
        if raw["name"] == "model_configuration" and details:
            print(f"       endpoint: {details['endpoint_origin']}")
            print(f"       model: {details['model']}")
            print("       API key: configured (value hidden)")
        if raw.get("remedy"):
            print(f"       fix: {raw['remedy']}")
    print("result: healthy" if report["healthy"] else "result: action required")
