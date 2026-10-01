"""Evidence capabilities are derived from a tool's declared behaviour.

These lock the live ``ports.tool`` contract: ``ToolSpec.evidence_capabilities``
is derived from ``(effect, result_authority)`` plus the two explicit extras, and
an adapter may not declare a capability it could have derived. Recovered from
``tests/test_proof_closure.py`` when the P0 proof layer was removed -- the tests
below never touched that layer, only the port contract it happened to live beside.
"""

from __future__ import annotations

import unittest

from tsm_agt.ports import (
    EvidenceCapability, ToolEffect, ToolResultAuthority, ToolRisk, ToolSpec,
)


def tool(
    name: str,
    effect: ToolEffect,
    authority: ToolResultAuthority,
    **extra: object,
) -> ToolSpec:
    return ToolSpec(
        name=name, description="test tool",
        parameters={"type": "object", "properties": {}},
        risk=ToolRisk.R0, effect=effect, result_authority=authority, **extra,
    )


class EvidenceCapabilityTest(unittest.TestCase):
    """§3.3: capabilities are derived, never a second adapter-maintained list."""

    def test_derivation_table(self) -> None:
        cases = {
            (ToolEffect.OBSERVE, ToolResultAuthority.WORKSPACE_FACT): "READ_ARTIFACT",
            (ToolEffect.MUTATE, ToolResultAuthority.MUTATION_FACT): "MUTATE_ARTIFACT",
            (ToolEffect.OBSERVE, ToolResultAuthority.PROCESS_FACT): "OBSERVE_EFFECT",
            (ToolEffect.EXECUTE, ToolResultAuthority.PROCESS_FACT): "EXECUTE_VERIFICATION",
            (ToolEffect.OBSERVE, ToolResultAuthority.RUNTIME_FACT): "OBSERVE_RUNTIME",
            (ToolEffect.OBSERVE, ToolResultAuthority.EXTERNAL_SERVICE): "OBSERVE_EXTERNAL",
        }
        for (effect, authority), expected in cases.items():
            with self.subTest(effect=effect, authority=authority):
                spec = tool("core.x", effect, authority)
                self.assertEqual(
                    {item.value for item in spec.evidence_capabilities}, {expected},
                )

    def test_existing_tools_need_no_change(self) -> None:
        # The whole P0 migration story: declaring only the two existing fields
        # is enough, so no adapter has to change.
        spec = tool("core.read", ToolEffect.OBSERVE, ToolResultAuthority.WORKSPACE_FACT)
        self.assertEqual(spec.extra_evidence_capabilities, frozenset())
        self.assertIn(EvidenceCapability.READ_ARTIFACT, spec.evidence_capabilities)

    def test_explicit_declaration_adds_non_derivable_capabilities(self) -> None:
        spec = tool(
            "core.enumerate", ToolEffect.OBSERVE, ToolResultAuthority.WORKSPACE_FACT,
            extra_evidence_capabilities=frozenset({
                EvidenceCapability.ENUMERATE_SCOPE, EvidenceCapability.OBSERVE_RELATION,
            }),
        )
        self.assertIn(EvidenceCapability.ENUMERATE_SCOPE, spec.evidence_capabilities)
        self.assertIn(EvidenceCapability.READ_ARTIFACT, spec.evidence_capabilities)

    def test_declaring_a_derivable_capability_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "derived from effect"):
            tool(
                "core.bad", ToolEffect.OBSERVE, ToolResultAuthority.WORKSPACE_FACT,
                extra_evidence_capabilities=frozenset({
                    EvidenceCapability.READ_ARTIFACT,
                }),
            )

    def test_explicit_capability_requires_an_observable_pair(self) -> None:
        with self.assertRaisesRegex(ValueError, "observable"):
            tool(
                "core.bad", ToolEffect.UNSPECIFIED, ToolResultAuthority.NONE,
                extra_evidence_capabilities=frozenset({
                    EvidenceCapability.ENUMERATE_SCOPE,
                }),
            )


class CapabilityDeclarationContractTest(unittest.IsolatedAsyncioTestCase):
    """§7 P1: declarations must match what a tool can actually establish.

    These lock the two non-derivable capabilities. A tool may only claim
    ``ENUMERATE_SCOPE`` when it reports whether its scope was covered, and only
    relation producers may claim ``OBSERVE_RELATION``.
    """

    async def _tools(self):
        from tsm_agt.bootstrap import compose_readonly_application

        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            return {
                spec.name: spec for spec in await application.kernel.list_tools()
            }
        finally:
            await application.registry.stop_all()

    async def test_read_only_tools_derive_their_capability(self) -> None:
        tools = await self._tools()
        self.assertTrue(tools)
        for name, spec in tools.items():
            with self.subTest(tool=name):
                # Derived from effect + result_authority -- no per-tool rule.
                self.assertIn(
                    EvidenceCapability.READ_ARTIFACT, spec.evidence_capabilities,
                )

    async def test_only_truncation_reporting_tools_claim_enumeration(self) -> None:
        tools = await self._tools()
        self.assertNotIn(
            EvidenceCapability.ENUMERATE_SCOPE,
            tools["core.read_file"].evidence_capabilities,
            "reading one file cannot certify that a scope was enumerated",
        )
        for name in (
            "core.list_files", "core.find_files", "core.search_text",
            "core.grep_search",
        ):
            with self.subTest(tool=name):
                self.assertIn(
                    EvidenceCapability.ENUMERATE_SCOPE,
                    tools[name].evidence_capabilities,
                )

    def test_relation_capability_is_declared_only_by_relation_producers(self) -> None:
        from tsm_agt.adapters.builtin.code_intelligence_tools import (
            CodeIntelligenceToolProvider,
        )

        specs = {
            spec.name: spec
            for spec in CodeIntelligenceToolProvider(None)._tools
        }
        for name in (
            "code.definition", "code.references", "code.implementations",
        ):
            with self.subTest(tool=name):
                self.assertIn(
                    EvidenceCapability.OBSERVE_RELATION,
                    specs[name].evidence_capabilities,
                )
        for name in ("code.workspace_symbols", "code.symbol_overview"):
            with self.subTest(tool=name):
                self.assertNotIn(
                    EvidenceCapability.OBSERVE_RELATION,
                    specs[name].evidence_capabilities,
                    "a name search is not a parsed relation",
                )


class EngineeringCapabilityContractTest(unittest.IsolatedAsyncioTestCase):
    """§7 P1: the real engineering composition derives truthful capabilities.

    Nothing here reads a tool name to decide evidence strength -- the values all
    come from ``effect`` + ``result_authority`` plus the two explicit extras.
    The point of this test is the *negative* space: tools that must prove
    nothing, and capabilities no tool may over-claim.
    """

    async def _tools(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from tsm_agt.bootstrap import (
            compose_openai_compatible_engineering_application_from_env,
        )

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_MODEL_BASE_URL=https://models.example.test/v1\n"
                "TSM_AGT_MODEL=open\nTSM_AGT_OPEN_MODEL=test-model\n"
                "TSM_AGT_MODEL_API_KEY=test-secret\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_env(
                        env_file
                    )
                )
        await application.registry.start_all()
        try:
            return {
                spec.name: spec for spec in await application.kernel.list_tools()
            }, application
        except Exception:
            await application.registry.stop_all()
            raise

    async def test_derived_capabilities_match_the_tool_kind(self) -> None:
        tools, application = await self._tools()
        try:
            self.assertTrue(tools)
            for name in ("core.apply_patch", "core.rollback_mutation"):
                with self.subTest(tool=name):
                    self.assertIn(
                        EvidenceCapability.MUTATE_ARTIFACT,
                        tools[name].evidence_capabilities,
                    )
            self.assertIn(
                EvidenceCapability.EXECUTE_VERIFICATION,
                tools["core.run_command"].evidence_capabilities,
            )
            self.assertIn(
                EvidenceCapability.OBSERVE_RELATION,
                tools["code.definition"].evidence_capabilities,
            )
        finally:
            await application.registry.stop_all()

    async def test_derived_only_tools_prove_nothing(self) -> None:
        tools, application = await self._tools()
        try:
            # A model-derived summary, an interaction and internal state tools
            # establish no evidence fact at all (INV-9: non-authoritative text is
            # never proof).
            for name in (
                "content.summarize", "core.request_input",
                "core.working_memory_update", "core.memory_remember",
            ):
                with self.subTest(tool=name):
                    self.assertEqual(tools[name].evidence_capabilities, frozenset())
        finally:
            await application.registry.stop_all()

    async def test_only_local_enumerations_claim_enumeration(self) -> None:
        tools, application = await self._tools()
        try:
            enumeration_claims = {
                name for name, spec in tools.items()
                if EvidenceCapability.ENUMERATE_SCOPE in spec.evidence_capabilities
            }
            self.assertEqual(enumeration_claims, {
                "core.list_files", "core.find_files", "core.search_text",
                "core.grep_search", "code.diagnostics",
            })
            # A web search is bounded by result count and is not a defensible
            # enumeration of its scope, so it must not claim one.
            self.assertNotIn(
                EvidenceCapability.ENUMERATE_SCOPE,
                tools["web.search"].evidence_capabilities,
            )
        finally:
            await application.registry.stop_all()
