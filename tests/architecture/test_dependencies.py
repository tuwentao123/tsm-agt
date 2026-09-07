from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "tsm_agt"
FORBIDDEN_FROM_CORE = ("tsm_agt.adapters", "tsm_agt.extensions")


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
                if module.startswith("tsm_agt.adapters"):
                    violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {module}")
        self.assertEqual(violations, [])

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
