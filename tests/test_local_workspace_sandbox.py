from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
import venv
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.adapters.windows_path import WindowsWorkspacePath
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ApprovalDecision, ApprovalRequired, ProjectTrustLevel, TaskState
from tsm_agt.ports import AdapterContext, SandboxRequest, ToolCall, ToolRisk


class LocalWorkspaceSandboxTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve()
        context = AdapterContext({}, lambda _type, _payload: None)
        self.path_service = (
            WindowsWorkspacePath() if sys.platform == "win32" else PosixWorkspacePath()
        )
        await self.path_service.start(context)
        self.sandbox = LocalWorkspaceSandbox(self.path_service)
        await self.sandbox.start(context)
        self.environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8",
        }

    async def asyncTearDown(self) -> None:
        await self.sandbox.stop(datetime.now())
        self.temporary.cleanup()

    async def test_allows_structured_engineering_executable(self) -> None:
        decision = await self.sandbox.authorize(SandboxRequest(
            (sys.executable, "-m", "unittest"),
            self.workspace, self.environment,
        ))
        self.assertTrue(decision.allowed)
        self.assertIn("not OS-isolated", decision.reason)

    async def test_denies_shells_even_when_shell_is_an_absolute_path(self) -> None:
        for executable in ("sh", "/bin/bash", "zsh", "pwsh"):
            with self.subTest(executable=executable):
                decision = await self.sandbox.authorize(SandboxRequest(
                    (executable, "-c", "echo unsafe"),
                    self.workspace, self.environment,
                ))
                self.assertFalse(decision.allowed)
                self.assertIn("shell", decision.reason)

    async def test_allowed_entry_name_cannot_hide_a_shell_target(self) -> None:
        target = shutil.which("sh") or shutil.which("cmd.exe")
        if target is None:
            self.skipTest("no shell target available")
        uppercase_target = self.workspace / "BASH.EXE"
        uppercase_target.write_text("not executed\n", encoding="utf-8")
        uppercase_target.chmod(0o755)
        launcher = self.workspace / "python"
        for actual_target in (Path(target), uppercase_target):
            launcher.unlink(missing_ok=True)
            try:
                launcher.symlink_to(actual_target)
            except OSError as error:
                self.skipTest(f"cannot create executable symlink: {error}")
            for entry in ("./python", str(launcher)):
                with self.subTest(entry=entry, target=actual_target.name):
                    decision = await self.sandbox.authorize(SandboxRequest(
                        (entry, "-V"), self.workspace, self.environment,
                    ))
                    self.assertFalse(decision.allowed)
                    self.assertIn("resolved shell executable", decision.reason)

    async def test_relative_environment_entry_runs_under_its_own_prefix_after_approval(self) -> None:
        environment = self.workspace / "environment"
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
        launcher = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        probe = self.workspace / "probe.py"
        probe.write_text(
            "import json, sys\nprint(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix}))\n",
            encoding="utf-8",
        )
        application = compose_fixture_application(
            sandbox_adapter=self.sandbox, tool_adapters=(CoreProcessToolProvider(),),
        )
        await application.registry.start_all()
        try:
            kernel = application.kernel
            await kernel.set_project_trust(self.workspace, ProjectTrustLevel.TRUSTED_BUILD)
            task = await kernel.create_task("verify environment entry", self.workspace, "task-environment-entry")
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
            ):
                task = await kernel.transition_task(task.task_id, state, state.value)
            call = ToolCall("call-environment-entry", "core.run_command", {
                "argv": [launcher.relative_to(self.workspace).as_posix(), probe.name], "cwd": ".",
            })
            with self.assertRaises(ApprovalRequired) as caught:
                await kernel.invoke_tool(task.task_id, "turn-environment-entry", call)
            approval = caught.exception.request
            self.assertEqual(approval.execution_identity["launch_path"], str(launcher))
            self.assertEqual(approval.execution_identity["resolved_target"], str(launcher.resolve()))
            started = await kernel.resolve_approval(
                task.task_id, approval.request_id, approval.payload_hash,
                ApprovalDecision.APPROVE, "approve the exact environment entry",
            )
            self.assertTrue(started.ok, started.message)
            waited = await kernel.invoke_tool(task.task_id, "turn-environment-entry", ToolCall(
                "wait-environment-entry", "core.process_wait",
                {"process_id": started.data["process_id"], "wait_seconds": 5},
            ))
            self.assertTrue(waited.ok, waited.message)
            self.assertTrue(waited.data["succeeded"], waited.data)
            output = json.loads(waited.data["stdout"]["text"])
            self.assertEqual(Path(output["prefix"]).resolve(), environment.resolve())
            self.assertNotEqual(output["prefix"], output["base_prefix"])
        finally:
            await application.registry.stop_all()

    async def test_allows_the_github_cli(self) -> None:
        decision = await self.sandbox.authorize(SandboxRequest(
            ("gh", "api", "repos/openai/codex/contents/README.md"),
            self.workspace, self.environment,
        ))
        self.assertTrue(decision.allowed, decision.reason)

    async def test_program_names_do_not_require_an_allowlist(self) -> None:
        for executable in (
            "hbcli", "wget", "company-custom-cli", "company-custom-cli.exe",
            "company-custom-cli.cmd", "company-custom-cli.bat",
        ):
            with self.subTest(executable=executable):
                decision = await self.sandbox.authorize(SandboxRequest(
                    (executable, "--version"), self.workspace, self.environment,
                ))
                self.assertTrue(decision.allowed, decision.reason)

    @unittest.skipIf(os.name == "nt", "POSIX shebang regression")
    async def test_user_installed_cli_and_its_runtime_execute_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            user_bin = Path(directory).resolve()
            (user_bin / "tsm-test-runtime").symlink_to(sys.executable)
            program = user_bin / "company-custom-cli"
            program.write_text(
                "#!/usr/bin/env tsm-test-runtime\n"
                "import json, os\n"
                "print(json.dumps({'result': 'custom CLI ran', 'token_inherited': 'GITHUB_TOKEN' in os.environ}))\n",
                encoding="utf-8",
            )
            program.chmod(0o755)
            application = compose_fixture_application(
                sandbox_adapter=self.sandbox, tool_adapters=(CoreProcessToolProvider(),),
            )
            await application.registry.start_all()
            try:
                kernel = application.kernel
                await kernel.set_project_trust(self.workspace, ProjectTrustLevel.TRUSTED_BUILD)
                task = await kernel.create_task("run an installed CLI", self.workspace, "task-user-cli")
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
                ):
                    task = await kernel.transition_task(task.task_id, state, state.value)
                with patch.dict(os.environ, {"PATH": str(user_bin), "GITHUB_TOKEN": "test-not-forwarded"}):
                    call = ToolCall("call-user-cli", "core.run_command", {"argv": [program.name, "--probe"], "cwd": "."})
                    with self.assertRaises(ApprovalRequired) as caught:
                        await kernel.invoke_tool(task.task_id, "turn-user-cli", call)
                    approval = caught.exception.request
                    self.assertEqual(approval.execution_identity["launch_path"], str(program))
                    started = await kernel.resolve_approval(
                        task.task_id, approval.request_id, approval.payload_hash,
                        ApprovalDecision.APPROVE, "approve exact custom CLI invocation",
                    )
                    self.assertTrue(started.ok, started.message)
                    waited = await kernel.invoke_tool(task.task_id, "turn-user-cli", ToolCall(
                        "wait-user-cli", "core.process_wait",
                        {"process_id": started.data["process_id"], "wait_seconds": 5},
                    ))
                    self.assertTrue(waited.ok, waited.message)
                    self.assertTrue(waited.data["succeeded"], waited.data)
                    self.assertEqual(json.loads(waited.data["stdout"]["text"]), {
                        "result": "custom CLI ran", "token_inherited": False,
                    })
            finally:
                await application.registry.stop_all()

    async def test_allows_curl_at_execution_boundary(self) -> None:
        for executable in ("curl", "/usr/bin/curl"):
            with self.subTest(executable=executable):
                if executable.startswith("/") and not Path(executable).is_file():
                    continue
                decision = await self.sandbox.authorize(SandboxRequest(
                    (executable, "http://localhost:5176/dashboard"),
                    self.workspace, self.environment,
                ))
                self.assertTrue(decision.allowed, decision.reason)

    async def test_approved_curl_starts_with_real_sandbox(self) -> None:
        if shutil.which("curl") is None:
            self.skipTest("curl is not installed")
        application = compose_fixture_application(
            sandbox_adapter=self.sandbox,
            tool_adapters=(CoreProcessToolProvider(),),
        )
        await application.registry.start_all()
        try:
            kernel = application.kernel
            await kernel.set_project_trust(self.workspace, ProjectTrustLevel.TRUSTED_BUILD)
            task = await kernel.create_task("check curl", self.workspace, "task-curl")
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
            ):
                task = await kernel.transition_task(task.task_id, state, state.value)
            with self.assertRaises(ApprovalRequired) as caught:
                await kernel.invoke_tool(task.task_id, "turn-curl", ToolCall(
                    "call-curl", "core.run_command",
                    {"argv": ["curl", "--version"], "cwd": "."},
                ))
            request = caught.exception.request
            self.assertEqual(request.risk, ToolRisk.R3)
            result = await kernel.resolve_approval(
                task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "approved exact curl command",
            )
            self.assertTrue(result.ok, result.message)
            self.assertTrue(result.data["started"])
        finally:
            await application.registry.stop_all()

    async def test_clone_destination_must_stay_inside_the_workspace(self) -> None:
        inside = await self.sandbox.authorize(SandboxRequest(
            ("git", "clone", "--depth", "1", "https://github.com/openai/codex", "vendor/codex"),
            self.workspace, self.environment,
        ))
        self.assertTrue(inside.allowed, inside.reason)

        outside = await self.sandbox.authorize(SandboxRequest(
            ("git", "clone", "https://github.com/openai/codex", "/tmp/elsewhere"),
            self.workspace, self.environment,
        ))
        self.assertFalse(outside.allowed)
        self.assertIn("inside the task workspace", outside.reason)

    async def test_windows_suffixes_are_classified_without_open_shell(self) -> None:
        allowed = await self.sandbox.authorize(SandboxRequest(
            ("gradlew.bat", "test"), self.workspace, self.environment
        ))
        allowed_exe = await self.sandbox.authorize(SandboxRequest(
            ("git.exe", "status"), self.workspace, self.environment
        ))
        denied_shell = await self.sandbox.authorize(SandboxRequest(
            ("cmd.exe", "/c", "echo unsafe"),
            self.workspace, self.environment,
        ))
        denied_meta = await self.sandbox.authorize(SandboxRequest(
            ("gradlew.bat", "test&whoami"),
            self.workspace, self.environment,
        ))
        self.assertTrue(allowed.allowed)
        self.assertTrue(allowed_exe.allowed)
        self.assertFalse(denied_shell.allowed)
        self.assertFalse(denied_meta.allowed)
        for suffix in (".cmd", ".bat"):
            denied_custom_meta = await self.sandbox.authorize(SandboxRequest(
                (f"company-custom-cli{suffix}", "test&whoami"),
                self.workspace, self.environment,
            ))
            self.assertFalse(denied_custom_meta.allowed)

    async def test_denies_credential_like_environment_again(self) -> None:
        decision = await self.sandbox.authorize(SandboxRequest(
            (sys.executable, "-V"), self.workspace,
            {**self.environment, "ACCESS_TOKEN": "secret-value"},
        ))
        self.assertFalse(decision.allowed)
        self.assertIn("credential-like", decision.reason)


    async def test_environment_policy_allows_governed_host_variables(self) -> None:
        decision = await self.sandbox.authorize(SandboxRequest(
            (sys.executable, "-V"),
            self.workspace,
            {**self.environment, "SSH_AUTH_SOCK": "/tmp/agent.sock"},
        ))
        self.assertTrue(decision.allowed)

        decision = await self.sandbox.authorize(SandboxRequest(
            ("../gradlew", "test"), self.workspace, self.environment
        ))
        self.assertFalse(decision.allowed)
        self.assertIn("escapes", decision.reason)

    async def test_denies_untrusted_external_python_runtime(self) -> None:
        other_temporary = tempfile.TemporaryDirectory()

        async def cleanup_runtime() -> None:
            await asyncio.to_thread(other_temporary.cleanup)

        self.addAsyncCleanup(cleanup_runtime)
        external_runtime = Path(other_temporary.name) / "python3"
        external_runtime.write_text("#!/usr/bin/env python3\n")
        external_runtime.chmod(0o755)
        decision = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.diagnostics["trust_mode"], "tofu")
        self.assertIn("fingerprint", decision.diagnostics)

        persisted = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(persisted.allowed)
        self.assertEqual(
            persisted.diagnostics["trust_mode"],
            "persisted-tofu",
        )

    async def test_denies_runtime_when_fingerprint_changes(self) -> None:
        other_temporary = tempfile.TemporaryDirectory()

        async def cleanup_runtime() -> None:
            await asyncio.to_thread(other_temporary.cleanup)

        self.addAsyncCleanup(cleanup_runtime)
        external_runtime = Path(other_temporary.name) / "python3"
        external_runtime.write_text("#!/usr/bin/env python3\n")
        external_runtime.chmod(0o755)

        first = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(first.allowed)

        external_runtime.write_text("#!/usr/bin/env python3\nprint('changed')\n")

        changed = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertFalse(changed.allowed)
        self.assertIn("fingerprint changed", changed.reason)

        decision = await self.sandbox.authorize(SandboxRequest(
            (sys.executable, "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(decision.allowed)
        self.assertIn("resolved_executable", decision.diagnostics)
        self.assertTrue(os.path.isabs(decision.diagnostics["resolved_executable"]))

    async def test_runtime_trust_cache_uses_structured_schema(self) -> None:
        other_temporary = tempfile.TemporaryDirectory()

        async def cleanup_runtime() -> None:
            await asyncio.to_thread(other_temporary.cleanup)

        self.addAsyncCleanup(cleanup_runtime)
        external_runtime = Path(other_temporary.name) / "python3"
        external_runtime.write_text("#!/usr/bin/env python3\n")
        external_runtime.chmod(0o755)

        decision = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(decision.allowed)

        cache_path = self.workspace / ".tsm" / "runtime_trust.json"
        payload = __import__("json").loads(cache_path.read_text())
        self.assertEqual(payload["version"], 2)
        runtime_record = payload["runtimes"][str(external_runtime.resolve())]
        self.assertEqual(runtime_record["trust_source"], "tofu")
        self.assertIn("enrolled_at", runtime_record)

        health = await self.sandbox.health()
        self.assertEqual(health.state, "healthy")
        self.assertIn("no OS isolation", health.message)


if __name__ == "__main__":
    unittest.main()
