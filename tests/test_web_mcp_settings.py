"""Settings-panel contracts for the read-only MCP status report."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tsm_agt.bootstrap.mcp_status import (
    describe_mcp_servers,
    mcp_configuration_summary,
)
from tsm_agt.web import app as web_app


DEMO_SERVER_MODULE = "tsm_agt.adapters.mcp_demo_server_stdio"


def _document(*, enabled: bool = True) -> dict:
    return {
        "mcp": {
            "enabled": enabled,
            "servers": {
                "demo": {
                    "kind": "executable",
                    "command": [sys.executable, "-m", DEMO_SERVER_MODULE],
                },
                # Fails during initialization, so availability must be False
                # rather than an exception escaping the report.
                "broken": {
                    "kind": "executable",
                    "command": [sys.executable, "-c", "raise SystemExit(3)"],
                },
            },
        },
    }


class McpStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_reports_available_and_unavailable_servers(self) -> None:
        enabled, servers, issues = mcp_configuration_summary(_document())
        self.assertTrue(enabled)
        self.assertEqual(issues, ())
        rows = {
            row["id"]: row
            for row in await describe_mcp_servers(enabled, servers, timeout_seconds=30)
        }
        self.assertTrue(rows["demo"]["available"])
        self.assertIn("echo", rows["demo"]["tools"])
        self.assertFalse(rows["broken"]["available"])
        self.assertTrue(rows["broken"]["detail"])

    async def test_disabled_and_unprobed_reports_are_none(self) -> None:
        _, servers, _ = mcp_configuration_summary(_document())
        disabled = await describe_mcp_servers(False, servers)
        skipped = await describe_mcp_servers(True, servers, probe=False)
        for rows in (disabled, skipped):
            self.assertTrue(all(row["available"] is None for row in rows))

    async def test_malformed_section_becomes_an_issue(self) -> None:
        enabled, servers, issues = mcp_configuration_summary(
            {"mcp": {"enabled": True, "servers": []}},
        )
        self.assertFalse(enabled)
        self.assertEqual(servers, ())
        self.assertTrue(issues)


class McpSettingsRouteTests(unittest.TestCase):
    def test_route_reports_configuration_without_probing(self) -> None:
        document = _document()
        with patch.object(
            web_app, "load_configuration_document",
            lambda path=None: (Path("/tmp/config.yaml"), document),
        ):
            response = TestClient(web_app.app).get("/settings/mcp?probe=false")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body), {"configuration_path", "enabled", "servers", "issues", "probed"},
        )
        self.assertFalse(body["probed"])
        self.assertEqual(
            {row["id"] for row in body["servers"]}, {"demo", "broken"},
        )

    def test_route_probe_reaches_the_configured_server(self) -> None:
        document = _document()
        with patch.object(
            web_app, "load_configuration_document",
            lambda path=None: (Path("/tmp/config.yaml"), document),
        ):
            response = TestClient(web_app.app).get("/settings/mcp?probe=true")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["enabled"])
        self.assertTrue(body["probed"])
        availability = {row["id"]: row["available"] for row in body["servers"]}
        self.assertEqual(availability, {"demo": True, "broken": False})


if __name__ == "__main__":
    unittest.main()
