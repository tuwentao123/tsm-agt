from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "tsm_agt"
FORBIDDEN_FROM_CORE = ("tsm_agt.adapters", "tsm_agt.extensions")

#: Modules under ``adapters/`` that hold protocol-neutral shared infrastructure
#: rather than a provider adapter. Any adapter may depend on these. The rule
#: below exists so provider adapters cannot depend on each *other* (an Anthropic
#: adapter must not import the OpenAI-compatible package to reuse its HTTP/SSE
#: plumbing); a shared transport is not a provider adapter.
SHARED_ADAPTER_MODULES = frozenset({
    "tsm_agt.adapters.http_json",
})


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class DependencyArchitectureTest(unittest.TestCase):
    def test_core_and_ports_do_not_depend_on_implementations(self) -> None:
        violations: list[str] = []
        for layer in ("core", "ports"):
            for path in (PACKAGE_ROOT / layer).rglob("*.py"):
                for module in imported_modules(path):
                    if module.startswith(FORBIDDEN_FROM_CORE):
                        violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {module}")
        self.assertEqual(violations, [])

    def test_adapters_do_not_import_other_adapters(self) -> None:
        violations: list[str] = []
        for path in (PACKAGE_ROOT / "adapters").rglob("*.py"):
            for module in imported_modules(path):
                if not module.startswith("tsm_agt.adapters"):
                    continue
                if module in SHARED_ADAPTER_MODULES:
                    continue
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {module}")
        self.assertEqual(violations, [])

    def test_shared_adapter_modules_are_not_provider_adapters(self) -> None:
        """The allowlist must never legalise provider-to-provider coupling."""
        provider_packages = {
            path.name
            for path in (PACKAGE_ROOT / "adapters").iterdir()
            if path.is_dir() and (path / "__init__.py").exists()
        }
        for module in SHARED_ADAPTER_MODULES:
            leaf = module.rsplit(".", 1)[-1]
            self.assertNotIn(
                leaf, provider_packages,
                f"{module} looks like a provider adapter package",
            )

    def test_anthropic_adapter_does_not_import_the_openai_adapter(self) -> None:
        """The concrete regression: shared plumbing must not drag in a peer."""
        path = PACKAGE_ROOT / "adapters" / "anthropic_messages" / "model.py"
        self.assertNotIn(
            "tsm_agt.adapters.openai_compatible", imported_modules(path)
        )

    def test_core_does_not_import_platform_lock_modules(self) -> None:
        forbidden = {"ctypes", "fcntl", "msvcrt"}
        violations: list[str] = []
        for path in (PACKAGE_ROOT / "core").rglob("*.py"):
            used = imported_modules(path) & forbidden
            if used:
                violations.append(
                    f"{path.relative_to(PACKAGE_ROOT)} -> {', '.join(sorted(used))}"
                )
        self.assertEqual(violations, [])

    def test_workspace_consumers_do_not_resolve_paths_behind_port(self) -> None:
        paths = (
            PACKAGE_ROOT / "core" / "kernel.py",
            PACKAGE_ROOT / "core" / "trust.py",
            PACKAGE_ROOT / "core" / "workspace.py",
            PACKAGE_ROOT / "adapters" / "builtin" / "core_tools.py",
            PACKAGE_ROOT / "adapters" / "local_sandbox" / "sandbox.py",
        )
        violations: list[str] = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "resolve"
                ):
                    violations.append(
                        f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}"
                    )
        self.assertEqual(violations, [])

    def test_workspace_transactions_use_platform_filesystem_boundary(self) -> None:
        violations: list[str] = []
        path = PACKAGE_ROOT / "core" / "workspace.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            owner = node.func.value
            if (
                node.func.attr == "replace"
                and isinstance(owner, ast.Name)
                and owner.id == "os"
            ) or (
                node.func.attr == "unlink"
                and not (isinstance(owner, ast.Name) and owner.id == "filesystem")
            ):
                violations.append(
                    f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}"
                )
        self.assertEqual(violations, [])

    def test_readonly_tools_do_not_resolve_workspace_paths_directly(self) -> None:
        path = PACKAGE_ROOT / "adapters" / "builtin" / "core_tools.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations = [
            node.lineno for node in ast.walk(tree)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "resolve"
            )
        ]
        self.assertEqual(violations, [])

    def test_only_composition_root_imports_concrete_adapters(self) -> None:
        allowed = PACKAGE_ROOT / "bootstrap" / "composition.py"
        violations: list[str] = []
        for path in PACKAGE_ROOT.rglob("*.py"):
            if path == allowed or "adapters" in path.parts:
                continue
            for module in imported_modules(path):
                if module.startswith("tsm_agt.adapters"):
                    violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {module}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()


#: Terminal-state coupling outside the decision authority, measured at the time
#: the convergence started. This is a **ratchet**: the counts may only go down.
#: A new file or a bigger count means fresh "one more place decides the terminal
#: state" -- which is exactly how this codebase accumulated five competing
#: vocabularies and 115 tasks whose ``FAILED`` contradicted their own reason.
#:
#: To lower a number: route that site through the one transition authority
#: and then update this table.
#:
#: **When a cohesive method group moves to a mixin sibling of ``core/kernel.py``
#: (the Hermes facade+siblings shape), redistribute these counts instead of
#: leaving them behind: the moved bodies carry their ``TaskState.X`` references
#: with them. What the ratchet forbids is a *new decision site*, so the rule for
#: a pure move is that the column **sum is conserved** -- 56 before and after
#: every kernel decomposition slice so far. ``kernel_session.py``'s two
#: references came out of ``kernel.py``'s 34; ``kernel_task.py``'s thirteen came
#: out of the remainder (the ``TERMINAL_AUTHORITY`` transition-legality table,
#: which travelled with ``transition_task``). A *reduction* (not a move) tightens
#: the table: removing the proof layer took ``cli.py`` 7 -> 5 and ``sdk/runtime.py``
#: 8 -> 5, because the client entry points no longer map a verdict to a state.
_TERMINAL_STATE_COUPLING_BASELINE = {
    "cli.py": 5,
    "core/kernel.py": 11,
    "core/kernel_checkpoint.py": 3,
    "core/kernel_input.py": 4,
    "core/kernel_session.py": 2,
    "core/kernel_steering.py": 1,
    "core/kernel_task.py": 13,
    "core/task_runtime_projection.py": 7,
    "sdk/runtime.py": 5,
}

#: Modules allowed to name terminal states: the enum itself and the outcome
#: vocabulary. ``core/verification.py`` was dropped when the verdict -> terminal
#: mapping (``TERMINAL_STATE_BY_VERDICT``) was removed -- it no longer names a
#: terminal state at all, so keeping it here would grant a permission nothing
#: uses.
_TERMINAL_STATE_AUTHORITY = {
    "core/task.py",
}


class TerminalStateConvergenceTest(unittest.TestCase):
    """The terminal state must converge on one authority, not spread further."""

    _TERMINAL = {"SUCCEEDED", "FAILED", "NEEDS_REVIEW", "CANCELLED", "CONFLICT"}

    def _coupling(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            relative = str(path.relative_to(PACKAGE_ROOT))
            if relative in _TERMINAL_STATE_AUTHORITY:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            found = sum(
                1 for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and node.attr in self._TERMINAL
                and isinstance(node.value, ast.Name)
                and node.value.id == "TaskState"
            )
            if found:
                counts[relative] = found
        return counts

    def test_terminal_state_coupling_never_grows(self) -> None:
        counts = self._coupling()
        grew = {
            name: (count, _TERMINAL_STATE_COUPLING_BASELINE.get(name, 0))
            for name, count in counts.items()
            if count > _TERMINAL_STATE_COUPLING_BASELINE.get(name, 0)
        }
        self.assertEqual(
            grew, {},
            "terminal-state coupling grew; route it through the single terminal "
            "authority instead of deciding the terminal state locally",
        )

    def test_no_new_module_decides_terminal_states(self) -> None:
        new_modules = set(self._coupling()) - set(_TERMINAL_STATE_COUPLING_BASELINE)
        self.assertEqual(new_modules, set(), "new terminal-state decision site")

    def test_the_baseline_only_shrinks(self) -> None:
        counts = self._coupling()
        for name, baseline in _TERMINAL_STATE_COUPLING_BASELINE.items():
            # A file that dropped below its baseline means the ratchet can be
            # tightened -- keep the table honest instead of leaving slack.
            self.assertLessEqual(
                counts.get(name, 0), baseline,
                f"{name} couples terminal states more than its baseline",
            )
