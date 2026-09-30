"""Extract one cohesive ``Kernel`` method group into a mixin sibling.

One of the steps of the facade+siblings decomposition recorded in
``tsm-agt-按Hermes重构kernel映射.md``: ``core/kernel.py`` is a god file and the
Hermes shape (``AGENTS.md:232-248``, ``run_agent.py`` + ``agent/*Mixin``) is a
façade that inherits one mixin per topic.

Why a script rather than by hand: the move is mechanical but has three
assertions that are easy to skip and expensive to skip, because each failure is
*silent* -- the code still imports and the suite still runs, just with a body
that is unreachable or attached to the wrong function.

1. **Decorator-aware ranges.** ``ast.FunctionDef.lineno`` does NOT include the
   decorators; they carry their own ``lineno`` in ``decorator_list``. Deleting
   or copying from ``lineno`` leaves ``@staticmethod`` behind, where it attaches
   to whatever definition follows. The first slice hit exactly this:
   ``_session_task_summary``'s ``@staticmethod`` landed on ``get_task`` and turned
   ``self.get_task(task_id)`` into a static call.
2. **Method-set conservation.** ``before == after`` as *sets*: nothing lost,
   nothing duplicated, nothing invented.
3. **No cross-contributor name collision.** A class's own namespace beats its
   bases, so a name defined on both ``Kernel`` and a mixin means one body is
   silently unreachable.

``--dry-run`` prints the dependency report and the collisions without writing
anything; run it first and read it.

Usage::

    python scripts/extract_kernel_mixin.py \\
        --module src/tsm_agt/core/kernel_tool.py --class ToolMixin \\
        --regex 'tool|invoke' --expect 28 --dry-run

This script is migration tooling, not product code: it goes away with the last
slice (see the mapping doc's order table).
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import textwrap
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "src/tsm_agt/core/kernel.py"
PACKAGE_PREFIX = "tsm_agt."


def _origin_map(tree: ast.Module) -> dict[str, tuple]:
    """Module-level name -> where it comes from.

    ``('importfrom', module, level, original)`` / ``('import', module, ...)`` /
    ``('local', lineno, None, None)``.
    """

    origin: dict[str, tuple] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                origin[alias.asname or alias.name] = (
                    "importfrom", node.module, node.level, alias.name,
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                origin[alias.asname or alias.name] = (
                    "import", alias.name, None, None,
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            origin[node.name] = ("local", node.lineno, None, None)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    origin[target.id] = ("local", node.lineno, None, None)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            origin[node.target.id] = ("local", node.lineno, None, None)
    return origin


def _kernel_class(tree: ast.Module) -> ast.ClassDef:
    matches = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Kernel"
    ]
    assert len(matches) == 1, f"expected exactly one Kernel class, got {len(matches)}"
    return matches[0]


def _method_span(node: ast.AST) -> tuple[int, int]:
    """Inclusive 1-based line range, **including decorators**."""

    decorators = [d.lineno for d in getattr(node, "decorator_list", [])]
    return min(decorators + [node.lineno]), node.end_lineno


def _module_nodes(tree: ast.Module) -> dict[str, ast.AST]:
    """Module-level definitions a moved method might reference by bare name.

    Assignments count as well as ``def``/``class``: a module-level table or
    constant is exactly as unmovable-without-the-method as a function.
    """

    nodes: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    nodes[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            nodes[node.target.id] = node
    return nodes


def _encloses(node: ast.AST, spans: list[tuple[int, int]]) -> bool:
    """Whether ``node``'s span contains any of ``spans``.

    A container is never movable: the facade class encloses every method, so
    without this the fixpoint happily decides ``Kernel`` itself has no user left
    outside the moved set and schedules it for extraction.
    """

    start, end = _method_span(node)
    return any(start <= low and high <= end for low, high in spans)


def _selected(class_node: ast.ClassDef, pattern: re.Pattern[str]) -> list[str]:
    names = [
        node.name for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return [name for name in names if pattern.search(name)]


def _bare_loads(node: ast.AST) -> set[str]:
    """Bare names the node actually *evaluates*.

    Annotation subtrees are skipped. ``kernel: Kernel`` inside a moved class is a
    type annotation, and ``from __future__ import annotations`` (which every
    sibling carries) makes it a string that is never evaluated -- so it is not a
    dependency, and counting it drags the whole facade in. Hermes' mixins annotate
    the facade the same way without importing it.
    """

    found: set[str] = set()

    def visit(current: ast.AST) -> None:
        for child in ast.iter_child_nodes(current):
            if isinstance(child, ast.arg):
                if child.annotation is not None:
                    continue  # skip the annotation, keep walking nothing else
                continue
            if isinstance(child, ast.AnnAssign):
                if child.value is not None:
                    visit(child.value)
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Keep the body and defaults; drop the return annotation.
                for inner in ast.iter_child_nodes(child):
                    if inner is child.returns:
                        continue
                    if inner in child.decorator_list:
                        visit(inner)
                        continue
                    visit(inner)
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                found.add(child.id)
                continue
            visit(child)

    visit(node)
    return found


def _render_imports(
    needs: Counter[str], origin: dict[str, tuple],
) -> str:
    from_groups: dict[tuple[int, str], list[str]] = defaultdict(list)
    plain: list[str] = []
    for name in sorted(needs):
        source = origin.get(name)
        if source is None:
            continue
        kind, module, level, original = source
        if kind == "importfrom":
            from_groups[(level or 0, module)].append(original or name)
        elif kind == "import":
            plain.append(f"import {name}")
    blocks: list[str] = []
    if plain:
        blocks.append("\n".join(plain))
    for (level, module), originals in sorted(from_groups.items(), key=lambda kv: str(kv[0])):
        dotted = "." * level + (module or "")
        names = ", ".join(sorted(set(originals)))
        if len(dotted) + len(names) <= 62:
            blocks.append(f"from {dotted} import {names}")
        else:
            wrapped = "\n".join(
                f"    {part}"
                for part in _wrap(names, width=68)
            )
            blocks.append(f"from {dotted} import (\n{wrapped}\n)")
    return "\n".join(blocks)


def _wrap(names: str, width: int) -> list[str]:
    parts = [part.strip() for part in names.split(",")]
    lines: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}, {part}" if current else part
        if len(candidate) > width and current:
            lines.append(current + ",")
            current = part
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _kernel_members(path: Path, class_name: str = "Kernel") -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    matches = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if not matches:
        return {}
    return {
        node.name: node for node in matches[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, help="target sibling path")
    parser.add_argument("--class", dest="class_name", required=True)
    parser.add_argument("--regex", required=True, help="method-name selection")
    parser.add_argument("--expect", type=int, required=True, help="asserted count")
    parser.add_argument("--import-after", default=None,
                        help="insert the facade import before this anchor line")
    parser.add_argument("--summary", required=True,
                        help="one-line module docstring, e.g. "
                             "'Tool invocation and batch lifecycle for Kernel.'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    source = KERNEL.read_text(encoding="utf-8")
    lines = source.split("\n")
    tree = ast.parse(source)
    class_node = _kernel_class(tree)
    origin = _origin_map(tree)
    pattern = re.compile(args.regex)

    names = _selected(class_node, pattern)
    methods = {
        node.name: node for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    # --- assertion: the selection is exactly what was expected -----------------
    if len(names) != args.expect:
        print(f"SELECTION MISMATCH: --expect {args.expect}, selected {len(names)}")
        for name in names:
            print(f"  {name}  ({methods[name].end_lineno - methods[name].lineno + 1} lines)")
        return 2

    spans = {name: _method_span(methods[name]) for name in names}
    ranges = sorted(spans.values())

    # --- assertion: no collision with what stays behind ------------------------
    staying = set(methods) - set(names)
    collisions = sorted(set(names) & staying)
    if collisions:
        print(f"NAME COLLISION with remaining Kernel methods: {collisions}")
        return 2

    # --- dependency report -----------------------------------------------------
    nodes = _module_nodes(tree)
    direct_needs: Counter[str] = Counter()
    for name in names:
        direct_needs.update(_bare_loads(methods[name]))

    def _has_outside_reference(name: str, moved_spans: list[tuple[int, int]]) -> bool:
        start, end = _method_span(nodes[name])
        covered = ranges + moved_spans
        for number in range(1, len(lines) + 1):
            if start <= number <= end:
                continue
            if any(low <= number <= high for low, high in covered):
                continue
            if lines[number - 1].lstrip().startswith("#"):
                continue
            if re.search(rf"\b{re.escape(name)}\b", lines[number - 1]):
                return True
        return False

    # Fixpoint: a module-level symbol travels only while NOTHING outside the moved
    # set references it. Adding first and checking afterwards would drag in the
    # whole module -- ``Kernel`` itself is one reference away from any moved class.
    moved_locals: dict[str, ast.AST] = {}
    while True:
        moved_spans = [_method_span(node) for node in moved_locals.values()]
        needed = Counter()
        for name in names:
            needed.update(_bare_loads(methods[name]))
        for node in moved_locals.values():
            needed.update(_bare_loads(node))
        added = [
            name for name in needed
            if name in nodes and name not in moved_locals
            and not _encloses(nodes[name], ranges + moved_spans)
            and not _has_outside_reference(name, moved_spans)
        ]
        if not added:
            break
        for name in added:
            moved_locals[name] = nodes[name]

    # Imports must cover the moved module-level symbols too, not only the methods:
    # a moved `@dataclass` class needs `dataclass` exactly as much as a method does.
    all_needs = Counter(direct_needs)
    for node in moved_locals.values():
        all_needs.update(_bare_loads(node))

    local_spans = {name: _method_span(node) for name, node in moved_locals.items()}
    # Symbols the moved set needs that are NOT allowed to travel, because they
    # have users outside it. These are late-imported from the facade inside the
    # methods that read them -- ``from .kernel import X`` executed per call, which
    # is the shape ``AGENTS.md:243-244`` prescribes. It resolves at *call* time
    # from the facade's namespace, so it also keeps ``patch(<facade>.<name>)``
    # working, unlike a module-level import here.
    #
    # Computed from ``all_needs``, not ``direct_needs``: a symbol can be reachable
    # only through another symbol that is travelling, and missing that case leaves
    # an unbound name in a branch nothing exercises until production.
    stuck = sorted(
        name for name in all_needs
        if name in nodes and name not in moved_locals
    )

    total_lines = sum(end - start + 1 for start, end in ranges)
    print(f"selected {len(names)} methods / {total_lines} lines")
    print(f"decorated: {[n for n in names if methods[n].decorator_list]}")
    print(
        "module-level symbols that must move too: "
        + (
            ", ".join(
                f"{name} ({start}-{end})"
                for name, (start, end) in sorted(
                    local_spans.items(), key=lambda kv: kv[1][0],
                )
            )
            or "none"
        )
    )
    if stuck:
        print("late-imported from the facade inside the methods that read them:")
        for name in stuck:
            start, end = _method_span(nodes[name])
            print(f"  {name} (defined at lines {start}-{end})")
    print("imports for the new module:")
    print(_render_imports(all_needs, origin))
    if args.dry_run:
        print("\n(dry run: nothing written)")
        return 0

    # --- build the sibling -----------------------------------------------------
    method_bodies = ["\n".join(lines[start - 1:end]) for start, end in ranges]
    local_bodies = [
        (
            name,
            "\n".join(lines[start - 1:end]),
        )
        for name, (start, end) in sorted(
            local_spans.items(), key=lambda kv: kv[1][0],
        )
    ]

    # A stuck symbol can be read from a travelling *module-level* definition too,
    # not only from a travelling method: ``_required_effects_from_outcomes`` is
    # reached through ``_runtime_authored_criteria``, which travels with the task
    # slice. Missing that case writes a sibling whose name is unbound in the one
    # branch nobody exercises -- which is the failure this whole script exists to
    # prevent.
    late_sites: dict[str, list[str]] = {}
    for index, body in enumerate(method_bodies):
        needed = _reads_stuck_globals(body, set(stuck), wrapped=True)
        if needed:
            method_bodies[index] = _insert_late_imports(body, needed)
            late_sites[names[index]] = needed
    for index, (owner, body) in enumerate(local_bodies):
        needed = _reads_stuck_globals(body, set(stuck), wrapped=False)
        if not needed:
            continue
        if not re.match(r"^\s*(async\s+)?def ", textwrap.dedent(body)):
            # A travelling class would need the import inside each of its own
            # methods that read the name, and this script only injects into
            # functions. Refusing beats writing a file that only fails on the
            # branch that first runs it.
            print("REFUSING: travelling definition reads a name that cannot "
                  "travel, and only functions can receive a late import:")
            print(f"  {owner} needs {needed}")
            return 2
        local_bodies[index] = (owner, _insert_late_imports(body, needed))
        late_sites[owner] = needed
    for name, readers in sorted(late_sites.items()):
        print(f"  late-import injected into {name}: {readers}")

    header = _sibling_header(
        args.class_name, args.summary, names, sorted(local_spans),
    )
    sections = [header, _render_imports(all_needs, origin)]
    if local_bodies:
        sections.append("\n\n\n".join(body for _, body in local_bodies))
    sections.append(
        f"class {args.class_name}:\n" + "\n\n".join(method_bodies)
    )
    Path(args.module).write_text("\n\n\n".join(sections) + "\n", encoding="utf-8")

    # --- mutate the facade, descending so ranges stay valid --------------------
    for start, end in sorted(
        ranges + list(local_spans.values()), reverse=True,
    ):
        del lines[start - 1:end]
    mutated = "\n".join(lines)
    old_class = "class Kernel"
    assert mutated.count(old_class + ":") == 1 or mutated.count(old_class + "(") == 1
    mutated = _add_base(mutated, args.class_name)
    mutated = _add_import(mutated, args.module, args.class_name, args.import_after)
    KERNEL.write_text(mutated, encoding="utf-8")

    return _verify(args.module, args.class_name, set(names), names)


def _sibling_header(
    class_name: str, summary: str, names: list[str], moved_locals: list[str],
) -> str:
    table = "\n".join(f"* ``{name}``" for name in sorted(names))
    moved = ""
    if moved_locals:
        moved = (
            "\nModule-level symbols that moved with them, transitively. They had no\n"
            "user outside the moved set, and this module must not import the facade\n"
            "to reach them, so their home is here now:\n\n"
            + "\n".join(f"* ``{name}``" for name in moved_locals)
            + "\n"
        )
    return (
        f'"""{summary}\n'
        "\n"
        "A mixin module in the Hermes facade+siblings shape (``run_agent.py`` +\n"
        "``agent/session_persistence.py``): the facade ``core/kernel.py`` imports\n"
        f"this at module level and adds ``{class_name}`` to ``Kernel``'s bases, so\n"
        "**every method resolves through ``Kernel``'s MRO unchanged** --\n"
        "``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class\n"
        "attributes all still resolve. Moved bodies are verbatim: nothing is renamed\n"
        "and no signature changes.\n"
        "\n"
        "This module must never bind the facade at module level; late-import it\n"
        "inside a method instead (Hermes ``AGENTS.md:243-244``).\n"
        "\n"
        "Moved from ``core/kernel.py``:\n"
        "\n"
        f"{table}\n"
        f"{moved}"
        '"""\n'
        "\n"
        "from __future__ import annotations"
    )


def _add_base(source: str, class_name: str) -> str:
    if re.search(rf"^class Kernel\({class_name}(,|\))", source, re.M):
        return source
    if "class Kernel:\n" in source:
        return source.replace("class Kernel:\n", f"class Kernel({class_name}):\n")
    match = re.search(r"^class Kernel\(([^)]*)\):", source, re.M)
    assert match, "cannot find the Kernel class declaration"
    bases = match.group(1).strip()
    return source[:match.start()] + (
        f"class Kernel({class_name}, {bases}):"
    ) + source[match.end():]


def _add_import(
    source: str, module: str, class_name: str, anchor: str | None,
) -> str:
    statement = f"from .{Path(module).stem} import (\n    {class_name},\n)\n"
    if statement in source:
        return source
    if anchor:
        assert source.count(anchor) == 1, f"anchor {anchor!r} is not unique"
        index = source.index(anchor)
    else:
        match = re.search(r"^from \.[a-z_]+ import \($", source, re.M)
        assert match, "no import anchor found"
        index = match.start()
    return source[:index] + statement + source[index:]


def _verify(
    module: str, class_name: str, expected: set[str], ordered: list[str],
) -> int:
    """Post-write checks: set conservation, MRO, collision, clean import, closure.

    The closure check uses ``symtable`` -- the interpreter's own scope analysis --
    rather than a hand-rolled walker, because the whole point of this script is
    that hand-rolled analysis is what silently produces an unreachable body.
    """

    before = subprocess.run(
        ["git", "show", f"HEAD:{KERNEL.relative_to(REPO)}"],
        capture_output=True, text=True, cwd=REPO,
    ).stdout
    if not before:
        print("WARNING: no committed kernel.py to compare against; using in-memory set")
        expected_before = expected
    else:
        expected_before = set(_kernel_members_from_source(before))
    after = set(_kernel_members(KERNEL)) | set(_kernel_members(Path(module), class_name))
    missing = sorted(expected_before - after)
    extra = sorted(after - expected_before)
    duplicates = sorted(
        set(_kernel_members(KERNEL)) & set(_kernel_members(Path(module), class_name))
    )
    print(f"members before={len(expected_before)} after={len(after)}")
    print(f"  missing: {missing or 'none'}")
    print(f"  extra:   {extra or 'none'}")
    print(f"  cross-contributor duplicates: {duplicates or 'none'}")

    dotted = f"{PACKAGE_PREFIX}core.{Path(module).stem}"
    probe = subprocess.run(
        [
            sys.executable, "-c",
            f"import {dotted} as sibling; "
            f"from tsm_agt.core.kernel import Kernel; "
            f"assert sibling.{class_name} in Kernel.__mro__",
        ],
        capture_output=True, text=True, cwd=REPO,
    )
    print(f"  import + MRO: {'ok' if probe.returncode == 0 else 'FAILED'}")
    if probe.returncode != 0:
        print(probe.stderr)

    unresolved = _unresolved_global_names(Path(module), dotted)
    print(f"  unresolved global names in moved bodies: {unresolved or 'none'}")
    if unresolved:
        print("  (a name here would be a NameError the first time that branch runs)")

    seams = seam_names(Path(module), KERNEL)
    print(f"  patch seams that moved out of the facade: {seams or 'none'}")
    if seams:
        print("  A test or tool doing patch(<facade>.<name>) now binds the FACADE's "
              "copy while the moved body reads the sibling's, so the patch silently "
              "stops intercepting. Repoint it at the sibling module (Hermes "
              "AGENTS.md:244-245). No check above can see this: the name still "
              "resolves, just somewhere else.")

    detached = _detached_symbol_imports(module)
    print(f"  imports of moved symbols still pointing at the facade: {detached or 'none'}")
    if detached:
        print("  The other half of the same problem, and the loud half: an import of "
              "a symbol that left the facade fails at import time. Repoint it at this "
              "sibling, because an import follows the *definition* while a patch "
              "target follows wherever the moved body *reads* from.")

    ok = (
        not missing and not extra and not duplicates
        and probe.returncode == 0 and not unresolved and not detached
    )
    print("VERIFY:", "ok" if ok else "FAILED")
    return 0 if ok else 1


def _detached_symbol_imports(module: str) -> list[str]:
    """Repo-wide ``from tsm_agt.core.kernel import X`` where ``X`` has left.

    Asked of the real module in a subprocess rather than of the source text, so
    re-exports and conditional definitions cannot fool it.
    """

    imported: dict[str, list[str]] = {}
    for path in sorted(REPO.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.endswith("core.kernel")
            ):
                for alias in node.names:
                    imported.setdefault(alias.name, []).append(
                        f"{path.relative_to(REPO)}:{node.lineno}"
                    )
    if not imported:
        return []
    script = (
        "import tsm_agt.core.kernel as K\n"
        f"print('|'.join(n for n in {sorted(imported)!r} if not hasattr(K, n)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO,
    )
    if result.returncode != 0:
        return [f"<could not inspect the facade: {result.stderr.strip().splitlines()[-1]}>"]
    names = [name for name in result.stdout.strip().split("|") if name]
    return [f"{name} (imported at {', '.join(imported[name][:3])})" for name in names]


def _reads_stuck_globals(text: str, stuck: set[str], *, wrapped: bool) -> list[str]:
    """Which of ``stuck`` this fragment reads as a global.

    ``wrapped`` says whether ``text`` is a method body (indented, so it needs a
    dummy class to become parseable) or a module-level definition (already valid
    on its own). Guessing from the text was wrong for a module-level assignment
    such as ``TERMINAL_AUTHORITY: ... = {...}``.
    """

    import symtable

    source = "class _Wrapper:\n" + text if wrapped else textwrap.dedent(text)
    table = symtable.symtable(source, "<fragment>", "exec")
    found: set[str] = set()

    def walk(node) -> None:
        for child in node.get_children():
            if child.get_type() == "function":
                for symbol in child.get_symbols():
                    if symbol.is_global() and symbol.get_name() in stuck:
                        found.add(symbol.get_name())
            walk(child)

    walk(table)
    return sorted(found)


def _insert_late_imports(text: str, names: list[str]) -> str:
    """Insert ``from .kernel import ...`` after the docstring.

    After the docstring, not before: the moved body's docstring is part of the
    verbatim original and stays first. The original indentation is restored, so
    this works for a method body and a module-level function alike.
    """

    prefix = text[: len(text) - len(text.lstrip())]
    dedented = textwrap.dedent(text)
    lines = dedented.split("\n")
    node = ast.parse(dedented).body[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise ValueError("late imports can only be injected into a function")
    first = node.body[0]
    is_docstring = (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )
    anchor = first.end_lineno if is_docstring else first.lineno
    indent = re.match(r"\s*", lines[anchor]).group(0)
    statements = [f"{indent}from .kernel import {name}" for name in names]
    patched = "\n".join(lines[:anchor] + statements + lines[anchor:])
    return textwrap.indent(patched, prefix) if prefix else patched


def seam_names(sibling: Path, facade: Path) -> list[str]:
    """Names the sibling's moved bodies read, that were also reachable on the facade.

    Narrowed to names actually read as globals -- listing every import would bury
    the two or three that matter among the shared type imports nobody patches.
    """

    import symtable

    def top_level(module_path: Path) -> set[str]:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
        return names

    text = sibling.read_text(encoding="utf-8")
    table = symtable.symtable(text, str(sibling), "exec")
    read: set[str] = set()

    def functions(node) -> list:
        found = []
        for child in node.get_children():
            if child.get_type() == "function":
                found.append(child)
            found.extend(functions(child))
        return found

    for function in functions(table):
        for symbol in function.get_symbols():
            if symbol.is_global():
                read.add(symbol.get_name())

    return sorted(read & top_level(sibling) & top_level(facade))


def _unresolved_global_names(path: Path, dotted: str) -> list[str]:
    """Global names inside the sibling that resolve to nothing.

    Only ``is_global()`` counts. ``is_free()`` is a closure reference -- ``self``
    inside a nested helper, or a comprehension variable -- and is bound by an
    enclosing function scope, so it is not a missing import. Only the module's own
    functions are inspected: imports and module-level definitions are bound by
    construction.
    """

    import builtins
    import importlib
    import symtable

    module = importlib.import_module(dotted)
    bound = set(vars(module)) | set(dir(builtins))
    table = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    unresolved: set[str] = set()

    def functions(node) -> list:
        found = []
        for child in node.get_children():
            if child.get_type() == "function":
                found.append(child)
            found.extend(functions(child))
        return found

    for function in functions(table):
        for symbol in function.get_symbols():
            if symbol.is_global() and symbol.get_name() not in bound:
                unresolved.add(symbol.get_name())
    return sorted(unresolved)


def _kernel_members_from_source(source: str, class_name: str = "Kernel") -> list[str]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                member.name for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
