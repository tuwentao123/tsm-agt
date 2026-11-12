"""A workspace may attach extra roots; resolution must follow the owning root."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.posix_path.path import PosixWorkspacePath
from tsm_agt.core.workspace import (
    MutationOperation,
    MutationRecord,
    prepare_workspace_file_delete,
    prepare_workspace_text_write,
    workspace_mutation_lock_key,
)
from tsm_agt.core.workspace_roots import normalize_roots, select_root_for_path


def _path_service() -> PosixWorkspacePath:
    """Start the real POSIX path adapter without an adapter context."""
    service = PosixWorkspacePath()
    service._started = True
    return service


class WorkspaceRootSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _path_service()
        self._directory = tempfile.TemporaryDirectory()
        base = Path(self._directory.name).resolve()
        self.primary = base / "primary"
        self.extra = base / "extra"
        self.primary.mkdir()
        self.extra.mkdir()
        self.base = base
        self.roots = (self.primary, self.extra)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_relative_path_stays_on_the_primary_root(self):
        root, relative = select_root_for_path(self.roots, "src/app.py", self.service)
        self.assertEqual(root, self.primary)
        self.assertEqual(relative, "src/app.py")

    def test_absolute_path_inside_an_extra_root_selects_that_root(self):
        target = self.extra / "lib" / "mod.py"
        root, relative = select_root_for_path(self.roots, str(target), self.service)
        self.assertEqual(root, self.extra)
        self.assertEqual(relative, "lib/mod.py")

    def test_absolute_path_inside_the_primary_root_also_selects_it(self):
        target = self.primary / "deep" / "mod.py"
        root, relative = select_root_for_path(self.roots, str(target), self.service)
        self.assertEqual(root, self.primary)
        self.assertEqual(relative, "deep/mod.py")

    def test_absolute_path_outside_every_root_is_rejected(self):
        with self.assertRaises(PermissionError):
            select_root_for_path(
                self.roots, str(self.base / "outside.py"), self.service
            )

    def test_normalize_roots_drops_duplicates_and_missing_directories(self):
        roots = normalize_roots(
            self.primary,
            (self.extra, self.extra, self.base / "missing"),
            self.service,
        )
        self.assertEqual(roots, (self.primary, self.extra))

    def test_extra_root_write_keeps_the_owning_root(self):
        prepared = prepare_workspace_text_write(
            self.primary, str(self.extra / "notes.md"), "hello\n", None,
            self.service, roots=(self.extra,),
        )
        self.assertEqual(prepared.workspace, self.extra)
        self.assertEqual(prepared.relative_path, "notes.md")
        self.assertEqual(prepared.operation, MutationOperation.CREATE)

    def test_extra_root_delete_resolves_inside_that_root(self):
        target = self.extra / "gone.txt"
        target.write_text("bye", encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        prepared = prepare_workspace_file_delete(
            self.primary, str(target), digest, self.service, roots=(self.extra,),
        )
        self.assertEqual(prepared.path, target)

    def test_primary_root_write_is_unchanged(self):
        prepared = prepare_workspace_text_write(
            self.primary, "notes.md", "hi\n", None,
            self.service, roots=(self.extra,),
        )
        self.assertEqual(prepared.workspace, self.primary)
        self.assertEqual(prepared.relative_path, "notes.md")

    def test_lock_key_uses_the_root_that_owns_the_target(self):
        key = workspace_mutation_lock_key(
            self.primary, str(self.extra / "notes.md"), self.service,
            roots=(self.extra,),
        )
        self.assertEqual(key, str(self.extra / "notes.md"))


class MutationRecordRootTest(unittest.TestCase):
    def _record(self, **overrides):
        fields = {
            "mutation_id": "mutation-1",
            "path": "notes.md",
            "operation": MutationOperation.CREATE,
            "before_hash": None,
            "after_hash": "a" * 64,
            "step_id": "step-1",
            "backup_ref": None,
            "created_at": datetime.now(timezone.utc),
        }
        fields.update(overrides)
        return MutationRecord(**fields)

    def test_root_survives_a_round_trip(self):
        record = self._record(root="/elsewhere/extra")
        self.assertEqual(MutationRecord.from_data(record.to_data()), record)

    def test_legacy_record_without_root_still_loads(self):
        data = self._record().to_data()
        data.pop("root")
        self.assertIsNone(MutationRecord.from_data(data).root)


if __name__ == "__main__":
    unittest.main()
