"""Several root directories behind one workspace.

A workspace keeps one canonical primary root -- ``TaskSnapshot.workspace`` --
and may attach sibling directories as extra roots. Relative paths keep
resolving against the primary root, so existing behaviour is unchanged; an
absolute path is accepted only when it lives inside one of those roots.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from tsm_agt.ports import WorkspacePathPort


def normalize_roots(
    primary: str | Path,
    extra: Iterable[str | Path] = (),
    path_service: WorkspacePathPort | None = None,
) -> tuple[Path, ...]:
    """Return canonical roots, primary first, without duplicates.

    A root that no longer exists is dropped: a deleted directory must not stay
    selectable, and every containment check needs a real directory.
    """
    roots: list[Path] = []
    for raw in (primary, *extra):
        candidate = Path(raw).expanduser()
        try:
            root = (
                candidate.resolve(strict=True)
                if path_service is None
                else path_service.normalize_workspace(candidate)
            )
        except (OSError, ValueError):
            continue
        if root in roots or not root.is_dir():
            continue
        roots.append(root)
    return tuple(roots)


def select_root_for_path(
    roots: Sequence[Path],
    requested_path: str,
    path_service: WorkspacePathPort | None = None,
) -> tuple[Path, str]:
    """Return ``(root, root_relative_path)`` for a requested path.

    A relative path belongs to the primary root, which keeps every historical
    caller working. An absolute path must be contained by one of the roots;
    anything else raises instead of silently falling back to the primary root.
    """
    if not roots:
        raise ValueError("a workspace needs at least one root")
    primary = Path(roots[0])
    candidate = Path(requested_path).expanduser()
    if not candidate.is_absolute():
        return primary, requested_path
    resolved = candidate.resolve(strict=False)
    for root in roots:
        canonical = Path(root).resolve(strict=False)
        inside = (
            path_service.is_same_or_descendant(resolved, canonical)
            if path_service is not None
            else _is_inside(resolved, canonical)
        )
        if not inside:
            continue
        return canonical, resolved.relative_to(canonical).as_posix()
    raise PermissionError("path is outside every configured workspace root")


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
