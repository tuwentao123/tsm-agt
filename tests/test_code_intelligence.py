from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsm_agt.adapters.builtin import CodeIntelligenceToolProvider
from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.adapters.text_code_intelligence import TextCodeIntelligenceProvider
from tsm_agt.ports import (
    AdapterContext, CodeIntelligencePort, ToolCall, ToolInvocationContext,
    ToolRisk,
)


def context() -> AdapterContext:
    return AdapterContext({}, lambda _type, _payload: None)


class CodeIntelligenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.path_adapter = PosixWorkspacePath()
        await self.path_adapter.start(context())
        self.provider = TextCodeIntelligenceProvider(self.path_adapter)
        await self.provider.start(context())
        self.tools = CodeIntelligenceToolProvider(self.provider)
        await self.tools.start(context())

    async def asyncTearDown(self) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=1)
        await self.tools.stop(deadline)
        await self.provider.stop(deadline)
        await self.path_adapter.stop(deadline)
        self.temporary.cleanup()

    def _write_sources(self) -> None:
        (self.workspace / "base.py").write_text(
            "class Service:\n    pass\n\n"
            "class UserService(Service):\n    pass\n\n"
            "def build_service():\n    return UserService()\n",
            encoding="utf-8",
        )
        (self.workspace / "consumer.py").write_text(
            "from base import Service\n\ndef use(item: Service):\n    return item\n",
            encoding="utf-8",
        )
        (self.workspace / "broken.py").write_text(
            "def broken(:\n    pass\n", encoding="utf-8"
        )
        (self.workspace / "Widget.kt").write_text(
            "interface Widget\nclass Button : Widget\nfun render() = Button()\n",
            encoding="utf-8",
        )
        (self.workspace / "JavaService.java").write_text(
            "public class JavaService implements Service {\n"
            "    public String execute(String input) { return input; }\n"
            "}\n", encoding="utf-8",
        )

    async def test_all_queries_return_versioned_bounded_results(self) -> None:
        self._write_sources()
        overview = await self.provider.symbol_overview(
            self.workspace, "base.py"
        )
        self.assertEqual(
            [item["name"] for item in overview["symbols"]],
            ["Service", "UserService", "build_service"],
        )
        self.assertTrue(overview["index"]["refreshed"])
        self.assertTrue(overview["index"]["workspace_fingerprint"])
        self.assertTrue(overview["index"]["index_version"].startswith("text-v1-"))

        definitions = await self.provider.definition(self.workspace, "Service")
        self.assertEqual(len(definitions["definitions"]), 1)
        references = await self.provider.references(
            self.workspace, "Service", include_declaration=False
        )
        self.assertEqual(
            {(item["path"], item["line"]) for item in references["references"]},
            {("base.py", 4), ("consumer.py", 1), ("consumer.py", 3),
             ("JavaService.java", 1)},
        )
        implementations = await self.provider.implementations(
            self.workspace, "Service"
        )
        self.assertEqual(
            [item["name"] for item in implementations["implementations"]],
            ["JavaService", "UserService"],
        )
        symbols = await self.provider.workspace_symbols(self.workspace, "service")
        self.assertEqual(
            [item["name"] for item in symbols["symbols"]],
            ["JavaService", "Service", "UserService", "build_service"],
        )
        diagnostics = await self.provider.diagnostics(self.workspace)
        self.assertEqual(diagnostics["coverage"], "python-syntax")
        self.assertEqual(diagnostics["diagnostics"][0]["code"], "python.syntax")
        preview = await self.provider.rename_preview(
            self.workspace, "Service", "Gateway"
        )
        self.assertFalse(preview["applied"])
        self.assertTrue(preview["preview_complete"])
        self.assertFalse(preview["safe_to_apply"])
        self.assertEqual(len(preview["edits"]), 5)
        java = await self.provider.symbol_overview(
            self.workspace, "JavaService.java"
        )
        self.assertEqual(
            [(item["kind"], item["name"]) for item in java["symbols"]],
            [("class", "JavaService"), ("method", "execute")],
        )

    async def test_index_reuses_then_refreshes_after_source_change(self) -> None:
        self._write_sources()
        first = await self.provider.workspace_symbols(self.workspace, "Service")
        second = await self.provider.workspace_symbols(self.workspace, "Service")
        self.assertTrue(first["index"]["refreshed"])
        self.assertFalse(second["index"]["refreshed"])
        old_version = second["index"]["index_version"]
        (self.workspace / "base.py").write_text(
            (self.workspace / "base.py").read_text(encoding="utf-8")
            + "\ndef ServiceFactory():\n    pass\n", encoding="utf-8"
        )
        refreshed = await self.provider.workspace_symbols(self.workspace, "Service")
        self.assertTrue(refreshed["index"]["refreshed"])
        self.assertNotEqual(refreshed["index"]["index_version"], old_version)
        self.assertIn("ServiceFactory", [item["name"] for item in refreshed["symbols"]])

    async def test_tool_adapter_is_r0_read_only_and_rejects_escapes(self) -> None:
        self._write_sources()
        specs = await self.tools.list_tools()
        self.assertEqual(len(specs), 7)
        self.assertTrue(all(spec.risk is ToolRisk.R0 and spec.is_read_only for spec in specs))
        invocation = ToolInvocationContext(
            "invocation", "task", "turn", self.workspace,
            datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        result = await self.tools.invoke(
            ToolCall("call", "code.definition", {"symbol": "Service"}),
            invocation,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.meta["untrusted_data"])
        escaped = await self.tools.invoke(
            ToolCall("escape", "code.symbol_overview", {"path": "../secret.py"}),
            invocation,
        )
        self.assertFalse(escaped.ok)
        self.assertEqual(escaped.error_code, "INVALID_PARAM")

    async def test_port_is_replaceable_and_provider_declares_full_capability_set(self) -> None:
        self.assertIsInstance(self.provider, CodeIntelligencePort)
        capabilities = self.provider.descriptor.capabilities
        self.assertEqual(
            capabilities,
            frozenset({
                "definition", "diagnostics", "implementations",
                "index-refresh", "references", "rename-preview",
                "symbol-overview", "workspace-symbols",
            }),
        )


if __name__ == "__main__":
    unittest.main()
