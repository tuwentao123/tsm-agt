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
from tsm_agt.core.configuration import ripgrep_binary
from tsm_agt.core.workspace import EXCLUDED_DIRECTORIES
from tsm_agt.ports import (
    AdapterContext,
    ToolCall,
    ToolFactCapability,
    ToolInvocationContext,
    ToolResultMaterialization,
)


class CoreReadOnlyToolProviderTest(unittest.IsolatedAsyncioTestCase):
    def _point_ripgrep_at(self, binary: Path) -> None:
        """Make the search tools use a stub ripgrep for this test."""

        previous = os.environ.get("TSM_AGT_RIPGREP")
        os.environ["TSM_AGT_RIPGREP"] = str(binary)
        ripgrep_binary.cache_clear()

        def restore() -> None:
            if previous is None:
                os.environ.pop("TSM_AGT_RIPGREP", None)
            else:
                os.environ["TSM_AGT_RIPGREP"] = previous
            ripgrep_binary.cache_clear()

        self.addCleanup(restore)

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
        self.assertEqual(
            result.fact_descriptor.materialization,
            ToolResultMaterialization.METADATA,
        )
        self.assertEqual(
            result.fact_descriptor.capabilities,
            (ToolFactCapability.REPORT_METADATA,),
        )
        self.assertEqual(result.data["requested_path"], ".")
        canonical_workspace = self.workspace.resolve()
        self.assertEqual(result.data["resolved_path"], str(canonical_workspace))
        self.assertEqual(result.data["resolved_root"], str(canonical_workspace))
        self.assertEqual(result.data["root_alias"], canonical_workspace.name)
        self.assertEqual(result.data["root_kind"], "PRIMARY_WORKSPACE")

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
                "find-known", "core.search_files",
                {"pattern": "dialog_call_confirm.xml", "target": "files"},
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
        self.assertEqual(result.data["search_target"], "file_names_and_paths")
        self.assertIn("File-name matches are candidates", result.hint)
        self.assertIn("target='content'", result.hint)

    async def test_search_files_defaults_to_content_and_switches_to_file_names(self) -> None:
        src = self.workspace / "src"
        src.mkdir(exist_ok=True)
        (src / "ui.py").write_text(
            "button_id = 'cancel-btn'\n", encoding="utf-8"
        )
        (src / "cancel_helpers.py").write_text("pass\n", encoding="utf-8")

        content = await self.provider.invoke(
            ToolCall(
                "search-content", "core.search_files",
                {"pattern": "cancel-btn", "path": "src"},
            ),
            self.context,
        )
        files = await self.provider.invoke(
            ToolCall(
                "search-files", "core.search_files",
                {"pattern": "*cancel*", "target": "files", "path": "src"},
            ),
            self.context,
        )

        self.assertTrue(content.ok)
        self.assertEqual(content.data["search_target"], "content")
        self.assertEqual(
            [item["path"] for item in content.data["matches"]], ["src/ui.py"]
        )
        self.assertTrue(files.ok)
        self.assertEqual(files.data["search_target"], "file_names_and_paths")
        self.assertEqual(
            [item["path"] for item in files.data["matches"]],
            ["src/cancel_helpers.py"],
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

    async def test_external_read_root_supports_all_read_tools_but_hides_secrets(self) -> None:
        external = self.workspace.parent / f"external-{self.workspace.name}"
        external.mkdir()
        try:
            (external / "service.py").write_text(
                "class ExternalService:\n    pass\n", encoding="utf-8"
            )
            (external / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            context = ToolInvocationContext(
                invocation_id="inv-external", task_id="task-1",
                turn_id="turn-1", workspace=self.workspace,
                deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
                workspace_path=self.path_service,
                additional_read_roots=(external,),
            )
            read = await self.provider.invoke(ToolCall(
                "read-external", "core.read_file",
                {"path": str(external / "service.py")},
            ), context)
            listed = await self.provider.invoke(ToolCall(
                "list-external", "core.list_files",
                {"path": str(external), "recursive": True},
            ), context)
            found = await self.provider.invoke(ToolCall(
                "find-external", "core.find_files",
                {"path": str(external), "pattern": "service.py"},
            ), context)
            searched = await self.provider.invoke(ToolCall(
                "search-external", "core.search_text",
                {"path": str(external), "query": "ExternalService"},
            ), context)
            secret = await self.provider.invoke(ToolCall(
                "secret-external", "core.read_file",
                {"path": str(external / ".env")},
            ), context)
            self.assertTrue(read.ok)
            self.assertEqual(read.data["path"], "service.py")
            self.assertEqual([item["path"] for item in listed.data["entries"]], ["service.py"])
            self.assertEqual(found.data["matches"][0]["path"], "service.py")
            self.assertEqual(searched.data["matches"][0]["path"], "service.py")
            canonical_external = external.resolve()
            for result, requested in (
                (read, str(external / "service.py")),
                (listed, str(external)),
                (found, str(external)),
                (searched, str(external)),
            ):
                with self.subTest(call_id=result.call_id):
                    self.assertEqual(result.data["requested_path"], requested)
                    self.assertEqual(
                        result.data["resolved_root"], str(canonical_external)
                    )
                    self.assertEqual(
                        result.data["root_alias"], canonical_external.name
                    )
                    self.assertEqual(
                        result.data["root_kind"], "TASK_APPROVED_READ_ROOT"
                    )
                    self.assertTrue(
                        result.data["resolved_path"].startswith(
                            str(canonical_external)
                        )
                    )
            self.assertFalse(secret.ok)
            self.assertEqual(secret.error_code, "PERMISSION_DENIED")
        finally:
            for child in external.iterdir():
                child.unlink()
            external.rmdir()

    async def test_external_root_symlink_cannot_escape_approved_directory(self) -> None:
        external = self.workspace.parent / f"external-link-{self.workspace.name}"
        unapproved = self.workspace.parent / f"unapproved-{self.workspace.name}.txt"
        external.mkdir()
        unapproved.write_text("not approved\n", encoding="utf-8")
        link = external / "escape.txt"
        try:
            os.symlink(unapproved, link)
            context = ToolInvocationContext(
                invocation_id="inv-link", task_id="task-1", turn_id="turn-1",
                workspace=self.workspace,
                deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
                workspace_path=self.path_service,
                additional_read_roots=(external,),
            )
            result = await self.provider.invoke(ToolCall(
                "read-link", "core.read_file", {"path": str(link)}
            ), context)
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
        finally:
            link.unlink(missing_ok=True)
            unapproved.unlink(missing_ok=True)
            external.rmdir()

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

    async def test_content_search_locates_with_ripgrep_when_available(self) -> None:
        # ripgrep answers in exactly the walker's shape: it is a locator, not a
        # second result format, so evidence accounting cannot tell them apart.
        stub = self.workspace / "fake-rg"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdout.write('src/app.py\\0' + '2:class Kernel:\\n')\n"
            "sys.stdout.write('README.md\\0' + '1:Kernel documentation\\n')\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self._point_ripgrep_at(stub)

        result = await self.provider.invoke(
            ToolCall("call-rg", "core.search_files", {"pattern": "kernel"}),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["search_engine"], "ripgrep")
        self.assertEqual(
            [(item["path"], item["line"], item["text"]) for item in result.data["matches"]],
            [
                ("README.md", 1, "Kernel documentation"),
                ("src/app.py", 2, "class Kernel:"),
            ],
        )

    async def test_a_failing_ripgrep_falls_back_to_the_walker(self) -> None:
        stub = self.workspace / "broken-rg"
        stub.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit(2)\n", encoding="utf-8",
        )
        stub.chmod(0o755)
        self._point_ripgrep_at(stub)

        result = await self.provider.invoke(
            ToolCall("call-rg2", "core.search_files", {"pattern": "kernel"}),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertNotIn("search_engine", result.data)
        self.assertEqual(len(result.data["matches"]), 2)

    async def test_search_output_modes_answer_without_line_text(self) -> None:
        # Force the in-tree walker so the answer shape does not depend on
        # whether ripgrep happens to be installed.
        self._point_ripgrep_at(self.workspace / "no-such-ripgrep")

        counts = await self.provider.invoke(
            ToolCall("c-count", "core.search_files", {
                "pattern": "kernel", "output_mode": "count",
            }),
            self.context,
        )
        self.assertTrue(counts.ok)
        self.assertEqual(counts.data["output_mode"], "count")
        self.assertEqual(
            counts.data["counts"], {"src/app.py": 1, "README.md": 1}
        )
        self.assertEqual(counts.data["total_matches"], 2)
        self.assertNotIn("matches", counts.data)

        paths = await self.provider.invoke(
            ToolCall("c-paths", "core.search_files", {
                "pattern": "kernel", "output_mode": "paths",
            }),
            self.context,
        )
        self.assertTrue(paths.ok)
        self.assertCountEqual(paths.data["paths"], ["src/app.py", "README.md"])
        self.assertEqual(paths.data["files_matched"], 2)
        self.assertNotIn("matches", paths.data)

    async def test_search_result_pages_with_offset(self) -> None:
        self._point_ripgrep_at(self.workspace / "no-such-ripgrep")

        first = await self.provider.invoke(
            ToolCall("c-page-1", "core.search_files", {
                "pattern": "kernel", "limit": 1,
            }),
            self.context,
        )
        second = await self.provider.invoke(
            ToolCall("c-page-2", "core.search_files", {
                "pattern": "kernel", "limit": 1, "offset": 1,
            }),
            self.context,
        )

        self.assertEqual(len(first.data["matches"]), 1)
        self.assertEqual(len(second.data["matches"]), 1)
        self.assertTrue(first.truncated)
        self.assertNotEqual(
            first.data["matches"][0]["path"], second.data["matches"][0]["path"]
        )

    async def test_read_file_reports_a_continuation_offset(self) -> None:
        long_file = self.workspace / "src" / "long.py"
        long_file.write_text(
            "".join(f"line {number}\n" for number in range(1, 41)),
            encoding="utf-8",
        )

        partial = await self.provider.invoke(
            ToolCall("c-read-1", "core.read_file", {
                "path": "src/long.py", "start_line": 1, "max_lines": 10,
            }),
            self.context,
        )
        final = await self.provider.invoke(
            ToolCall("c-read-2", "core.read_file", {
                "path": "src/long.py", "start_line": 1, "max_lines": 100,
            }),
            self.context,
        )

        self.assertTrue(partial.truncated)
        self.assertEqual(partial.data["next_offset"], 11)
        self.assertIn("start_line=11", partial.hint)
        self.assertFalse(final.truncated)
        self.assertIsNone(final.data["next_offset"])
        self.assertEqual(
            final.fact_descriptor.materialization,
            ToolResultMaterialization.CONTENT,
        )
        self.assertEqual(
            final.fact_descriptor.capabilities,
            (ToolFactCapability.DESCRIBE_CONTENT,),
        )

    async def test_read_file_character_limit_continues_without_missing_lines(self) -> None:
        source = "".join(f"{number:04d}:" + "字" * 100 + "\n" for number in range(110))
        (self.workspace / "src" / "long.py").write_text(source, encoding="utf-8")
        first = await self.provider.invoke(
            ToolCall("first", "core.read_file", {"path": "src/long.py"}), self.context
        )
        self.assertTrue(first.truncated)
        self.assertEqual(first.data["next_cursor"]["start_line"], first.data["end_line"] + 1)
        second = await self.provider.invoke(
            ToolCall("second", "core.read_file", {
                "path": "src/long.py", **first.data["next_cursor"],
            }), self.context
        )
        self.assertFalse(second.truncated)
        self.assertEqual(first.data["content"] + second.data["content"], source)

    async def test_read_file_long_single_line_cursor_preserves_every_character(self) -> None:
        source = "开头\n" + "字" * 17000 + "\n末尾\n"
        (self.workspace / "src" / "long.py").write_text(source, encoding="utf-8")
        cursor = {"start_line": 1, "start_column": 1}
        parts = []
        for page in range(10):
            result = await self.provider.invoke(
                ToolCall(f"page-{page}", "core.read_file", {
                    "path": "src/long.py", **cursor,
                }), self.context
            )
            self.assertTrue(result.ok)
            parts.append(result.data["content"])
            if not result.truncated:
                break
            cursor = result.data["next_cursor"]
            self.assertIn(f"start_column={cursor['start_column']}", result.hint)
        else:
            self.fail("pagination did not terminate")
        self.assertEqual("".join(parts), source)

    async def test_read_file_rejects_invalid_column(self) -> None:
        result = await self.provider.invoke(
            ToolCall("bad-column", "core.read_file", {
                "path": "src/app.py", "start_line": 1, "start_column": 999,
            }), self.context
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")

    async def test_read_file_exact_character_boundary_and_line_limit(self) -> None:
        source = "a" * 7999 + "\n" + "tail\n"
        (self.workspace / "src" / "long.py").write_text(source, encoding="utf-8")
        first = await self.provider.invoke(
            ToolCall("boundary", "core.read_file", {
                "path": "src/long.py", "max_lines": 1,
            }), self.context
        )
        self.assertEqual(first.data["content"], source[:8000])
        self.assertEqual(first.data["next_cursor"], {"start_line": 2, "start_column": 1})
        second = await self.provider.invoke(
            ToolCall("tail", "core.read_file", {
                "path": "src/long.py", **first.data["next_cursor"],
            }), self.context
        )
        self.assertFalse(second.truncated)
        self.assertEqual(first.data["content"] + second.data["content"], source)

    async def test_searches_literal_and_regex_with_limits(self) -> None:
        # This test asserts the walker's conservative page/truncation contract;
        # real rg versions differ in whether they can prove another match
        # exists after a one-hit page. Keep the implementation under test
        # deterministic rather than depending on the developer machine.
        self._point_ripgrep_at(self.workspace / "no-such-ripgrep")
        literal = await self.provider.invoke(
            ToolCall(
                "call-1",
                "core.search_files",
                {"pattern": "kernel", "path": "."},
            ),
            self.context,
        )
        regex = await self.provider.invoke(
            ToolCall(
                "call-2",
                "core.search_files",
                {
                    "pattern": "class\\s+Kernel",
                    "target": "content",
                    "path": "src",
                    "regex": True,
                    "case_sensitive": True,
                    "limit": 1,
                },
            ),
            self.context,
        )

        self.assertTrue(literal.ok)
        self.assertEqual(literal.data["search_target"], "content")
        self.assertEqual(len(literal.data["matches"]), 2)
        self.assertIn("core.read_file", literal.hint)
        self.assertTrue(regex.ok)
        self.assertEqual(regex.data["matches"][0]["path"], "src/app.py")
        self.assertTrue(regex.truncated)
        self.assertEqual(
            literal.fact_descriptor.materialization,
            ToolResultMaterialization.LOCATOR,
        )

    async def test_search_supports_grep_style_filters_and_context(self) -> None:
        # Generated-directory accounting belongs to the in-tree walker. rg
        # intentionally delegates ignore handling to the repository, so force
        # the walker for this contract test.
        self._point_ripgrep_at(self.workspace / "no-such-ripgrep")
        src = self.workspace / "src"
        docs = self.workspace / "docs"
        src.mkdir(exist_ok=True)
        docs.mkdir(exist_ok=True)
        (src / "service.py").write_text(
            "class Service\n"
            "def target_call():\n"
            "    return 'ok'\n",
            encoding="utf-8",
        )
        (docs / "service.md").write_text(
            "target_call appears in docs\n",
            encoding="utf-8",
        )

        result = await self.provider.invoke(
            ToolCall(
                "call-grep-search",
                "core.grep_search",
                {
                    "query": "target_call",
                    "path": ".",
                    "include": "src/*.py",
                    "exclude": "docs/*",
                    "before_context": 1,
                    "after_context": 1,
                },
            ),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["matches"]), 1)
        match = result.data["matches"][0]
        self.assertEqual(match["path"], "src/service.py")
        self.assertEqual(match["before_context"], ["class Service"])
        self.assertEqual(match["after_context"], ["    return 'ok'"])

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

    async def test_ripgrep_cap_is_source_first_and_fair_across_peer_roots(self) -> None:
        files = ["docs/Target.md", "modules/A.kt", "modules/B.kt", "packages/C.py"]
        for name in files:
            path = self.workspace / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("Target\n", encoding="utf-8")
        stub = self.workspace / "fake-rg-order"
        stub.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            + "for path in " + repr(files) + ":\n"
            + "    sys.stdout.write(path + '\\0' + '1:Target\\n')\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self._point_ripgrep_at(stub)
        result = await self.provider.invoke(
            ToolCall("ordered", "core.search_text", {"query": "Target", "path": ".", "max_matches": 2}),
            self.context,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["search_engine"], "ripgrep")
        self.assertEqual([row["path"] for row in result.data["matches"]], ["modules/A.kt", "packages/C.py"])

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


class ExcludedDirectoryPolicyTest(unittest.IsolatedAsyncioTestCase):
    """One exclusion list for every walker.

    ``core.list_files(recursive=True)`` used to return the Runtime's own state
    (``.agent/``, including its SQLite ledger) and ``.git/`` as project entries:
    its walker applied no filter at all, while the workspace layer skipped a list
    the tool layer did not share. A real run showed the model reading
    ``.agent/runtime.db`` back as project content.

    Every excluded directory is given the same marker file, so a test can prove
    the walker never *entered* it rather than merely never mentioned it.
    """

    MARKER = "exclusion-marker-8f2c"

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text(
            f"{self.MARKER}\n", encoding="utf-8"
        )
        for name in sorted(EXCLUDED_DIRECTORIES):
            directory = self.workspace / name
            directory.mkdir()
            (directory / "inside.txt").write_text(
                f"{self.MARKER} inside {name}\n", encoding="utf-8"
            )
        self.provider = CoreReadOnlyToolProvider()
        context = AdapterContext(config={}, emit_event=lambda _type, _payload: None)
        await self.provider.start(context)
        self.path_service = (
            WindowsWorkspacePath() if os.name == "nt" else PosixWorkspacePath()
        )
        await self.path_service.start(context)
        self.context = ToolInvocationContext(
            invocation_id="inv-excl",
            task_id="task-excl",
            turn_id="turn-excl",
            workspace=self.workspace,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
            workspace_path=self.path_service,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_every_entry_is_lowercase(self) -> None:
        """The walkers compare with ``str.casefold()``, so entries must be lower."""

        self.assertEqual(
            {name.casefold() for name in EXCLUDED_DIRECTORIES},
            set(EXCLUDED_DIRECTORIES),
        )

    async def test_recursive_list_omits_every_excluded_directory(self) -> None:
        result = await self.provider.invoke(
            ToolCall("list-excl", "core.list_files", {"recursive": True}),
            self.context,
        )

        self.assertTrue(result.ok)
        paths = [entry["path"] for entry in result.data["entries"]]
        self.assertIn("src/app.py", paths)
        for name in sorted(EXCLUDED_DIRECTORIES):
            self.assertNotIn(name, paths)
            self.assertFalse(
                any(path.startswith(f"{name}/") for path in paths),
                f"{name}/ leaked into the recursive listing: {paths}",
            )

    async def test_find_files_does_not_report_files_inside_them(self) -> None:
        result = await self.provider.invoke(
            ToolCall("find-excl", "core.find_files", {"pattern": "inside.txt"}),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["matches"], [])

    async def test_search_never_enters_them(self) -> None:
        result = await self.provider.invoke(
            ToolCall(
                "search-excl", "core.search_text",
                {"query": self.MARKER, "path": "."},
            ),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            [match["path"] for match in result.data["matches"]],
            ["src/app.py"],
            "the marker exists only in src/app.py and inside excluded directories",
        )

    async def test_an_explicit_path_into_one_still_lists(self) -> None:
        """The filter prunes recursion; it must not forbid targeted access."""

        result = await self.provider.invoke(
            ToolCall("list-agent", "core.list_files", {"path": ".agent"}),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            [entry["path"] for entry in result.data["entries"]],
            [".agent/inside.txt"],
        )



class GitignoreIsNotAVisibilityFilterTest(unittest.IsolatedAsyncioTestCase):
    """``.gitignore`` must not hide a file that exists in the selected workspace.

    Real report: ``task-292644cf`` on an iOS workspace asked for ``Podfile.local``.
    The file exists at the workspace root, but the parent repository's
    ``.gitignore`` lists it, and the file tools used ``rg --files`` -- which
    honours ``.gitignore`` -- as a visibility filter. ``core.search_files``
    therefore returned zero matches, which is indistinguishable from "the file
    does not exist", and the Agent concluded the workspace was not mounted.

    Codex and reference agents both honour ``.gitignore`` for enumeration, but in both it
    is a default of a command the model chooses and can escape (``rg -u``,
    ``ls``, plain reads). Here it was baked into the tool, with no override and
    no signal. Noise is pruned by ``EXCLUDED_DIRECTORIES`` instead, which is the
    job that actually needs doing.
    """

    MARKER = "visibility-marker-3d17"

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / ".gitignore").write_text(
            "generated/\nPodfile.local\n", encoding="utf-8"
        )
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text(
            f"{self.MARKER}\n", encoding="utf-8"
        )
        (self.workspace / "generated").mkdir()
        (self.workspace / "generated" / "artifact.py").write_text(
            f"{self.MARKER}\n", encoding="utf-8"
        )
        (self.workspace / "node_modules").mkdir()
        (self.workspace / "node_modules" / "dep.py").write_text(
            f"{self.MARKER}\n", encoding="utf-8"
        )
        (self.workspace / "Podfile.local").write_text("platform :ios\n", encoding="utf-8")
        self.provider = CoreReadOnlyToolProvider()
        context = AdapterContext(config={}, emit_event=lambda _type, _payload: None)
        await self.provider.start(context)
        self.path_service = (
            WindowsWorkspacePath() if os.name == "nt" else PosixWorkspacePath()
        )
        await self.path_service.start(context)
        self.context = ToolInvocationContext(
            invocation_id="inv-gitignore",
            task_id="task-gitignore",
            turn_id="turn-gitignore",
            workspace=self.workspace,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
            workspace_path=self.path_service,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_a_gitignored_file_is_still_discoverable(self) -> None:
        result = await self.provider.invoke(
            ToolCall(
                "search-ignored", "core.search_text",
                {"query": self.MARKER, "path": "."},
            ),
            self.context,
        )

        self.assertTrue(result.ok)
        paths = {match["path"] for match in result.data["matches"]}
        self.assertIn("src/app.py", paths)
        self.assertIn(
            "generated/artifact.py", paths,
            "the only thing hiding this file was .gitignore",
        )

    async def test_a_gitignored_file_is_found_by_name(self) -> None:
        """The exact call the real Task made, which returned zero matches."""

        result = await self.provider.invoke(
            ToolCall(
                "find-ignored", "core.find_files",
                {"pattern": "Podfile.local", "path": "."},
            ),
            self.context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            [match["path"] for match in result.data["matches"]],
            ["Podfile.local"],
        )

    async def test_a_gitignored_file_is_listed(self) -> None:
        result = await self.provider.invoke(
            ToolCall(
                "list-ignored", "core.list_files",
                {"path": ".", "recursive": True, "limit": 200},
            ),
            self.context,
        )

        self.assertTrue(result.ok)
        names = {entry["path"] for entry in result.data["entries"]}
        self.assertIn("Podfile.local", names)
        self.assertIn("src", names)

    async def test_excluded_directories_are_still_pruned(self) -> None:
        """Dropping the ignore layer must not cost the noise pruning."""

        result = await self.provider.invoke(
            ToolCall(
                "search-noise", "core.search_text",
                {"query": self.MARKER, "path": "."},
            ),
            self.context,
        )

        paths = {match["path"] for match in result.data["matches"]}
        self.assertNotIn(
            "node_modules/dep.py", paths,
            "EXCLUDED_DIRECTORIES, not .gitignore, is what prunes generated noise",
        )
