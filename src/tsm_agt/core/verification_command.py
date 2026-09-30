"""One recognizer for "this command can falsify a claim".

Completion verification must be *falsifiable*: a command only counts as
verification evidence when it can fail because the behaviour regressed. Static
syntax checks (``py_compile`` / ``compileall``) and inspection commands
(``git diff --check``) cannot, so they never qualify -- that rule is the point of
this module and it is deliberately project-neutral.

Both sides of the contract use this single function:

* authoring-time validation of a criterion's declared command, and
* runtime acceptance of a recorded execution.

Keeping them on one function is what stops "what the Runtime demands" from
drifting away from "what the Runtime accepts" -- the shape behind
``task-c2d21bce8ea74f0bbcac07aba4aefedf``, where git/state criteria were tagged
``post_mutation_command`` and no allowed command could ever close them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

#: Executables whose presence alone is not enough; the sub-command decides.
_SUBCOMMAND_RULES: Mapping[str, frozenset[str]] = {
    "cargo": frozenset({"test", "check", "build", "clippy"}),
    "go": frozenset({"test", "build", "vet"}),
    "flutter": frozenset({"test", "analyze", "build"}),
    "dart": frozenset({"test", "analyze", "build"}),
}

#: Executables that are verification by themselves.
_DIRECT_EXECUTABLES = frozenset({"pytest", "ruff"})

#: Compilers make a build result observable.
_COMPILERS = frozenset({"javac", "kotlinc", "rustc", "swiftc", "ninja"})

#: Package runners must name a verification script.
_SCRIPT_RUNNERS = ("npm", "pnpm", "yarn", "bun")
_SCRIPT_NAMES = frozenset({"test", "build", "lint", "check"})

#: Build tools whose verification target is spelled as a marker.
_MARKER_TOOLS = frozenset({"gradle", "gradlew", "gradlew.bat", "mvn"})
_MARKERS = ("test", "check", "build", "verify", "lint")


def is_verification_command(argv: Sequence[str]) -> bool:
    """Whether argv is a bounded build/test/lint command.

    Recognizes a closed set of engineering entry points; it never inspects
    project files and never treats an arbitrary command as evidence. Callers
    pass a structured argv, not a shell string.
    """

    if not argv:
        return False
    normalized = tuple(str(item).lower() for item in argv)
    executable = Path(normalized[0]).name
    if executable.endswith(".exe"):
        executable = executable[:-4]
    args = normalized[1:]

    if executable.startswith("python"):
        return (
            len(args) >= 2 and args[0] == "-m"
            and args[1] in {"unittest", "pytest"}
        )
    if executable in _DIRECT_EXECUTABLES:
        return True
    if executable in _MARKER_TOOLS:
        return any(marker in arg for arg in args for marker in _MARKERS)
    if executable in _SCRIPT_RUNNERS:
        return any(arg in _SCRIPT_NAMES for arg in args)
    if executable in _SUBCOMMAND_RULES:
        return bool(args) and args[0] in _SUBCOMMAND_RULES[executable]
    if executable == "xcodebuild":
        return any(arg in {"test", "build", "analyze"} for arg in args)
    if executable in _COMPILERS:
        return True
    if executable == "cmake":
        return "--build" in args
    if executable == "make":
        return not args or any(
            arg in {"test", "check", "build", "lint", "all"} for arg in args
        )
    return False


def is_verification_request(arguments: Mapping[str, object]) -> bool:
    """``is_verification_command`` for a tool-call argument mapping."""

    raw_argv = arguments.get("argv")
    if not isinstance(raw_argv, (list, tuple)):
        return False
    return is_verification_command(tuple(str(item) for item in raw_argv))
