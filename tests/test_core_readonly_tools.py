from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.adapters.windows_path import WindowsWorkspacePath
from tsm_agt.ports import AdapterContext, ToolCall, ToolInvocationContext


class CoreReadOnlyToolProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text(
            "first line\nclass Kernel:\n    pass\nlast line\n", encoding="utf-8"
        )
        (self.workspace / "README.md").write_text(
            "Kernel documentation\n", encoding="utf-8"
        )
        (self.workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (self.workspace / "secret.pem").write_text(
            "PRIVATE KEY\n", encoding="utf-8"
        )
        (self.workspace / "binary.bin").write_bytes(b"hello\x00world")
        self.provider = CoreReadOnlyToolProvider()
        context = AdapterContext(config={}, emit_event=lambda _type, _payload: None)
        await self.provider.start(context)
        self.path_service = (
            WindowsWorkspacePath() if os.name == "nt" else PosixWorkspacePath()
        )
        await self.path_service.start(context)
        self.context = ToolInvocationContext(
            invocation_id="inv-1",
            task_id="task-1",
            turn_id="turn-1",
            workspace=self.workspace,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
            workspace_path=self.path_service,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_lists_deterministically_and_hides_sensitive_paths(self) -> None:
        result = await self.provider.invoke(
            ToolCall("call-1", "core.list_files", {"recursive": True}),
            self.context,
        )

        self.assertTrue(result.ok)
        paths = [entry["path"] for entry in result.data["entries"]]
        self.assertEqual(paths, sorted(paths))
        self.assertIn("src/app.py", paths)
        self.assertNotIn(".env", paths)
        self.assertNotIn("secret.pem", paths)
        self.assertGreaterEqual(result.data["omitted_sensitive"], 2)
        self.assertTrue(result.meta["untrusted_data"])

    async def test_reads_bounded_line_range(self) -> None:
        result = await self.provider.invoke(
            ToolCall(
                "call-1",
                "core.read_file",
                {"path": "src/app.py", "start_line": 2, "max_lines": 2},
            ),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["content"], "class Kernel:\n    pass\n")
        self.assertEqual(result.data["start_line"], 2)
        self.assertEqual(result.data["end_line"], 3)
        self.assertEqual(
            result.data["sha256"],
            hashlib.sha256(
                b"first line\nclass Kernel:\n    pass\nlast line\n"
            ).hexdigest(),
        )
        self.assertTrue(result.truncated)

    async def test_finds_known_file_names_without_reading_directory_levels(self) -> None:
        nested = self.workspace / "modules" / "feature" / "src"
        nested.mkdir(parents=True)
        target = nested / "Dialog_Call_Confirm.xml"
        target.write_text("<layout />\n", encoding="utf-8")
        generated = self.workspace / "build" / "generated"
        generated.mkdir(parents=True)
        (generated / target.name).write_text("generated\n", encoding="utf-8")

        result = await self.provider.invoke(
            ToolCall(
                "find-known", "core.find_files",
                {"pattern": "dialog_call_confirm.xml"},
            ),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            [item["path"] for item in result.data["matches"]],
            ["modules/feature/src/Dialog_Call_Confirm.xml"],
        )
        self.assertGreaterEqual(
            result.data["generated_directories_skipped"], 1
        )

    async def test_find_files_supports_relative_path_glob(self) -> None:
        nested = self.workspace / "packages" / "service"
        nested.mkdir(parents=True)
        (nested / "settings.gradle").write_text("pluginManagement {}\n")

        result = await self.provider.invoke(
            ToolCall(
                "find-glob", "core.find_files",
                {"pattern": "packages/*/settings.gradle"},
            ),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            [item["path"] for item in result.data["matches"]],
            ["packages/service/settings.gradle"],
        )

    async def test_rejects_workspace_escape_absolute_and_sensitive_paths(self) -> None:
        for index, path in enumerate((
            "../outside.txt", "/etc/passwd", ".env", "src/../.env",
        )):
            with self.subTest(path=path):
                result = await self.provider.invoke(
                    ToolCall(
                        f"call-{index}", "core.read_file", {"path": path}
                    ),
                    self.context,
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "PERMISSION_DENIED")

    async def test_rejects_symlink_that_escapes_workspace(self) -> None:
        outside = Path(self.temp_dir.name).parent / f"outside-{Path(self.temp_dir.name).name}.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.workspace / "outside-link.txt"
        try:
            os.symlink(outside, link)
            result = await self.provider.invoke(
                ToolCall("call-1", "core.read_file", {"path": link.name}),
                self.context,
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    async def test_rejects_binary_file(self) -> None:
        result = await self.provider.invoke(
            ToolCall("call-1", "core.read_file", {"path": "binary.bin"}),
            self.context,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")

    async def test_recursive_list_omits_symlinked_directory_outside_workspace(self) -> None:
        outside = Path(self.temp_dir.name).parent / (
            f"outside-directory-{Path(self.temp_dir.name).name}"
        )
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("outside secret\n", encoding="utf-8")
        link = self.workspace / "linked-outside"
        try:
            os.symlink(outside, link)
            result = await self.provider.invoke(
                ToolCall(
                    "call-linked-list", "core.list_files",
                    {"recursive": True},
                ),
                self.context,
            )
            self.assertTrue(result.ok)
            paths = [entry["path"] for entry in result.data["entries"]]
            self.assertNotIn("linked-outside", paths)
            self.assertNotIn("linked-outside/secret.txt", paths)
            self.assertGreaterEqual(result.data["omitted_sensitive"], 1)
        finally:
            link.unlink(missing_ok=True)
            secret.unlink(missing_ok=True)
            outside.rmdir()

    async def test_searches_literal_and_regex_with_limits(self) -> None:
        literal = await self.provider.invoke(
            ToolCall(
                "call-1",
                "core.search_text",
                {"query": "kernel", "path": "."},
            ),
            self.context,
        )
        regex = await self.provider.invoke(
            ToolCall(
                "call-2",
                "core.search_text",
                {
                    "query": "class\\s+Kernel",
                    "path": "src",
                    "regex": True,
                    "case_sensitive": True,
                    "max_matches": 1,
                },
            ),
            self.context,
        )

        self.assertTrue(literal.ok)
        self.assertEqual(len(literal.data["matches"]), 2)
        self.assertTrue(regex.ok)
        self.assertEqual(regex.data["matches"][0]["path"], "src/app.py")
        self.assertTrue(regex.truncated)

    async def test_search_skips_generated_directories_but_keeps_source(self) -> None:
        generated = self.workspace / "build" / "tmp" / "kapt3"
        generated.mkdir(parents=True)
        (generated / "Generated.java").write_text(
            "class UniqueSearchTarget {}\n", encoding="utf-8"
        )
        source = self.workspace / "src" / "UniqueSearchTarget.kt"
        source.write_text("class UniqueSearchTarget\n", encoding="utf-8")

        result = await self.provider.invoke(
            ToolCall(
                "call-generated-skip", "core.search_text",
                {"query": "UniqueSearchTarget", "path": "."},
            ),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            [item["path"] for item in result.data["matches"]],
            ["src/UniqueSearchTarget.kt"],
        )
        self.assertGreaterEqual(
            result.data["generated_directories_skipped"], 1
        )

    async def test_workspace_search_scans_source_roots_before_docs(self) -> None:
        docs = self.workspace / "docs"
        modules = self.workspace / "modules"
        docs.mkdir()
        modules.mkdir()
        (docs / "Target.md").write_text(
            "Target appears in documentation\n", encoding="utf-8"
        )
        (modules / "Target.kt").write_text(
            "class Target\n", encoding="utf-8"
        )
        result = await self.provider.invoke(
            ToolCall(
                "call-source-first", "core.search_text",
                {"query": "Target", "path": ".", "max_matches": 1},
            ),
            self.context,
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            result.data["matches"][0]["path"], "modules/Target.kt"
        )

    async def test_workspace_search_round_robins_multiple_source_roots(self) -> None:
        code_base = self.workspace / "codeBase"
        modules = self.workspace / "modules"
        code_base.mkdir()
        modules.mkdir()
        for index in range(4):
            (code_base / f"A{index}.kt").write_text(
                "class SharedTarget\n", encoding="utf-8"
            )
        (modules / "ZTarget.kt").write_text(
            "class SharedTarget\n", encoding="utf-8"
        )
        result = await self.provider.invoke(
            ToolCall(
                "call-fair-roots", "core.search_text", {
                    "query": "SharedTarget", "path": ".", "max_matches": 2,
                },
            ),
            self.context,
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            {item["path"].split("/", 1)[0] for item in result.data["matches"]},
            {"codeBase", "modules"},
        )

    async def test_invalid_regex_is_structured_error(self) -> None:
        result = await self.provider.invoke(
            ToolCall(
                "call-1",
                "core.search_text",
                {"query": "[", "regex": True},
            ),
            self.context,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")


if __name__ == "__main__":
    unittest.main()
