"""Read-only previews of the same patch plans used by workspace execution."""
from __future__ import annotations

from difflib import unified_diff
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from tsm_agt.ports import WorkspacePathPort
from .v4a import parse_v4a_add, parse_v4a_patch, plan_v4a_update
from .patch_text import plan_text_edits
from .workspace import prepare_workspace_text_write


def patch_diff(path: str, before: str, after: str) -> str:
    parts = unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                         fromfile=f"a/{path}", tofile=f"b/{path}")
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in parts)


def preview_v4a_patch(
    workspace: Path, arguments: Mapping[str, Any], paths: WorkspacePathPort,
) -> str:
    patch = arguments["patch"]
    addition = parse_v4a_add(patch)
    path = addition[0] if addition is not None else parse_v4a_patch(patch)
    if addition is None and arguments["expected_hash"] is None:
        raise ValueError("Update File requires the target's SHA-256 expected_hash")
    prepared = prepare_workspace_text_write(
        workspace, path, "", arguments["expected_hash"], paths,
    )
    before = prepared.before_bytes.decode("utf-8") if prepared.before_bytes is not None else ""
    if addition is not None:
        if arguments["expected_hash"] is not None:
            raise ValueError("Add File requires expected_hash=null")
        after, matches = addition[1], ()
    else:
        plan = plan_v4a_update(path, before, patch)
        after, matches = plan.content, plan.matches
    metadata = {"before_sha256": arguments["expected_hash"],
                "after_sha256": sha256(after.encode()).hexdigest(), "patch_matches": matches}
    return ("Actual workspace diff (all hunks planned before writing):\n"
            + json.dumps(metadata, ensure_ascii=False) + "\n" + patch_diff(path, before, after))


def preview_text_patch(
    workspace: Path, arguments: Mapping[str, Any], paths: WorkspacePathPort,
    *, fallback_only: bool = False,
) -> str | None:
    path, expected_hash = arguments["path"], arguments["expected_hash"]
    edits = tuple(arguments["edits"])
    prepared = prepare_workspace_text_write(workspace, path, "", expected_hash, paths)
    if prepared.before_bytes is None:
        if len(edits) != 1 or edits[0]["old_text"] != "":
            raise ValueError("creating a file requires exactly one edit with empty old_text")
        before, after, matches = "", edits[0]["new_text"], ()
    else:
        before = prepared.before_bytes.decode("utf-8")
        plan = plan_text_edits(path, before, edits)
        after, matches = plan.content, plan.matches
    if fallback_only and not matches:
        # Exact edits already have faithful old/new text in the existing
        # presentation, including its compact large-batch summary.
        return None
    metadata = {"before_sha256": expected_hash,
                "after_sha256": sha256(after.encode()).hexdigest(), "patch_matches": matches}
    return ("Actual workspace diff (all edits planned before writing):\n"
            + json.dumps(metadata, ensure_ascii=False) + "\n" + patch_diff(path, before, after))
