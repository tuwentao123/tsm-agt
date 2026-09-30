"""The ``Kernel`` facade + mixin-sibling shape (Hermes ``AGENTS.md:232-248``).

``run_agent.py`` is a 1,176-line ``AIAgent`` that inherits 13 ``agent/*Mixin``
classes; ``core/kernel.py`` is following the same decomposition, one mixin per
topic. That shape has three invariants worth pinning, because each of them fails
*silently* rather than loudly:

1. **The mixins are actually in the MRO.** A sibling nobody inherits from is dead
   code that still imports cleanly.
2. **No method name is defined in more than one contributor.** A class's own
   namespace beats its bases, so a name that exists on both ``Kernel`` and a
   mixin means one of the two definitions is unreachable -- and which one wins
   depends only on where the method happened to be moved *from*.
3. **The facade is not bound at module level in its sibling.** ``kernel.py``
   imports its mixins at module level, so a sibling importing ``kernel`` at
   module level is a cycle; the rule (``AGENTS.md:243-244``) is that a sibling
   late-imports the facade *inside* functions instead.

These inspect runtime structure (``__mro__``, module ``__dict__``), not source
text, so a correct relocation never has to touch them.

**Why invariant 3 is not tested through ``sys.modules``.** ``core/__init__.py:55``
does ``from .kernel import Kernel, KernelDependencies``, so importing *any*
``tsm_agt.core.X`` runs the package initialiser and pulls in the facade. An
"``sys.modules`` has no ``core.kernel``" assertion therefore measures the
*package*, not the sibling, and fails for every sibling no matter how it is
written. What is actually checkable is the *binding*: a module-level
``from .kernel import X`` leaves a object whose ``__module__`` is
``tsm_agt.core.kernel`` sitting in the sibling's globals, and a module-level
``from . import kernel`` leaves the facade module object itself there.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from tsm_agt.core.kernel import Kernel

_FACADE_MODULE = "tsm_agt.core.kernel"


def _mixin_classes() -> tuple[type, ...]:
    """Every base of ``Kernel`` that is not ``object`` -- i.e. its mixins."""

    return tuple(base for base in Kernel.__mro__[1:] if base is not object)


class KernelFacadeTest(unittest.TestCase):
    def test_kernel_inherits_at_least_one_mixin(self) -> None:
        self.assertTrue(
            _mixin_classes(),
            "Kernel has no mixin base; the facade+siblings shape has regressed",
        )

    def test_every_mixin_contributes_methods(self) -> None:
        for mixin in _mixin_classes():
            with self.subTest(mixin=mixin.__name__):
                own = {
                    name for name in vars(mixin) if not name.startswith("__")
                }
                self.assertTrue(
                    own, f"{mixin.__name__} contributes nothing to Kernel",
                )

    def test_no_method_name_is_defined_twice_across_kernel_and_its_mixins(
        self,
    ) -> None:
        """The shadowing hazard. Whichever definition wins is an accident of
        method order, so the duplicate is always a bug: one of the two bodies is
        unreachable and nobody notices because both import fine."""

        contributors: dict[str, list[str]] = {}
        for owner in (Kernel, *_mixin_classes()):
            for name in vars(owner):
                if name.startswith("__"):
                    continue
                contributors.setdefault(name, []).append(owner.__name__)
        duplicated = {
            name: owners for name, owners in contributors.items()
            if len(owners) > 1
        }
        self.assertEqual(
            duplicated, {},
            "a method name is defined by more than one contributor; the class "
            "namespace silently wins and the other body is unreachable",
        )

    def test_each_mixin_module_imports_alone_in_a_fresh_interpreter(self) -> None:
        """A hard cycle shows up here: importing the sibling first must work
        without depending on the facade having been imported before it."""

        for mixin in _mixin_classes():
            with self.subTest(mixin=mixin.__name__):
                result = subprocess.run(
                    [sys.executable, "-c", f"import {mixin.__module__}"],
                    capture_output=True, text=True,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"{mixin.__module__} is not independently importable:\n"
                    f"{result.stderr}",
                )

    def test_no_mixin_module_binds_the_facade_at_module_level(self) -> None:
        """Catches both spellings of a module-level facade import."""

        facade = sys.modules[_FACADE_MODULE]
        for mixin in _mixin_classes():
            module = sys.modules[mixin.__module__]
            with self.subTest(mixin=mixin.__name__):
                bound = {
                    name: value for name, value in vars(module).items()
                    if value is facade
                    or getattr(value, "__module__", None) == _FACADE_MODULE
                }
                self.assertEqual(
                    bound, {},
                    f"{mixin.__module__} binds the facade at module level "
                    f"({sorted(bound)}); late-import it inside the method "
                    f"instead (AGENTS.md:243-244)",
                )


if __name__ == "__main__":
    unittest.main()
