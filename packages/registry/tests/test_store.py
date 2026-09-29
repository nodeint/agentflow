from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_registry.locks import RegistryBusy
from agentflow_registry.store import (
    RegistryCorrupt,
    RegistryStore,
    WorkspaceNotFound,
    workspace_id_for,
)
from tests.support import make_workspace

TOOL = {"name": "agentflow", "version": "0.1.0"}


def _registry_bytes(workspaces: list[dict[str, object]]) -> bytes:
    document = {"schema_version": 1, "workspaces": workspaces}
    return (json.dumps(document) + "\n").encode("utf-8")


class StoreTests(unittest.TestCase):
    def test_register_refresh_list_get_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_workspace(root, "alpha")
            second = make_workspace(root, "beta")
            store = RegistryStore(root / "registry")
            created, was_new = store.register(str(first), None, TOOL)
            self.assertTrue(was_new)
            self.assertEqual(created["name"], "alpha")
            self.assertEqual(created["path"], str(first.resolve()))
            self.assertTrue(created["available"])
            again, was_new = store.register(str(first), "renamed", {"name": "codex", "version": "9"})
            self.assertFalse(was_new)
            self.assertEqual(again["id"], created["id"])
            self.assertEqual(again["registered_at"], created["registered_at"])
            self.assertEqual(again["name"], "renamed")
            self.assertEqual(again["tool"]["name"], "codex")
            other, _ = store.register(str(second), None, TOOL)
            rows = store.list()
            self.assertEqual(
                [row["id"] for row in rows],
                [row["id"] for row in sorted(rows, key=lambda item: (item["registered_at"], item["id"]))],
            )
            self.assertEqual({row["id"] for row in rows}, {created["id"], other["id"]})
            self.assertEqual(store.get(other["id"])["path"], str(second.resolve()))
            (first / ".agentflow" / "config.yaml").unlink()
            self.assertFalse(store.get(created["id"])["available"])
            removed = store.remove(other["id"])
            self.assertEqual(removed["id"], other["id"])
            with self.assertRaises(WorkspaceNotFound):
                store.get(other["id"])
            with self.assertRaises(WorkspaceNotFound):
                store.remove(other["id"])
            mode = stat.S_IMODE((root / "registry" / "registry.json").stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_two_instances_cannot_interleave_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_workspace(root, "one")
            second = make_workspace(root, "two")
            directory = root / "registry"
            left = RegistryStore(directory)
            right = RegistryStore(directory)
            entered = threading.Event()
            release = threading.Event()
            right_entered = threading.Event()

            def hold() -> None:
                entered.set()
                release.wait(timeout=3)

            def mark() -> None:
                right_entered.set()

            left._locked_hook = hold
            right._locked_hook = mark
            errors: list[BaseException] = []

            def run_left() -> None:
                try:
                    left.register(str(first), None, TOOL)
                except BaseException as exc:
                    errors.append(exc)

            def run_right() -> None:
                try:
                    right.register(str(second), None, TOOL)
                except BaseException as exc:
                    errors.append(exc)

            thread_left = threading.Thread(target=run_left)
            thread_left.start()
            self.assertTrue(entered.wait(2))
            thread_right = threading.Thread(target=run_right)
            thread_right.start()
            time.sleep(0.3)
            self.assertFalse(right_entered.is_set())
            release.set()
            thread_left.join(3)
            thread_right.join(3)
            self.assertEqual(errors, [])
            ids = {row["id"] for row in left.list()}
            self.assertEqual(len(ids), 2)

    def test_register_raises_busy_and_leaves_the_first_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_workspace(root, "one")
            second = make_workspace(root, "two")
            directory = root / "registry"
            holder = RegistryStore(directory, timeout_sec=2)
            waiter = RegistryStore(directory, timeout_sec=0.2)
            entered = threading.Event()
            release = threading.Event()

            def hold() -> None:
                entered.set()
                release.wait(timeout=3)

            holder._locked_hook = hold
            errors: list[BaseException] = []

            def run_holder() -> None:
                try:
                    holder.register(str(first), None, TOOL)
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run_holder)
            thread.start()
            self.assertTrue(entered.wait(2))
            with self.assertRaises(RegistryBusy):
                waiter.register(str(second), None, TOOL)
            release.set()
            thread.join(3)
            self.assertEqual(errors, [])
            rows = holder.list()
            self.assertEqual([row["name"] for row in rows], ["one"])

    def test_missing_file_is_empty_and_corrupt_files_are_left_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = make_workspace(root, "demo")
            directory = root / "registry"
            store = RegistryStore(directory)
            self.assertEqual(store.list(), [])
            self.assertFalse((directory / "registry.json").exists())
            for raw in (
                b"",
                b"   \n",
                b"{",
                b"[]",
                b'{"schema_version": 2, "workspaces": []}',
                b'{"schema_version": true, "workspaces": []}',
                b'{"schema_version": 1.0, "workspaces": []}',
            ):
                path = directory / "registry.json"
                path.write_bytes(raw)
                with self.assertRaises(RegistryCorrupt):
                    store.register(str(workspace), None, TOOL)
                self.assertEqual(path.read_bytes(), raw)

    def test_records_outside_the_schema_are_left_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = make_workspace(root, "demo")
            directory = root / "registry"
            directory.mkdir()
            store = RegistryStore(directory)
            stored_path = str(workspace.resolve())
            record = {
                "id": workspace_id_for(stored_path),
                "name": "demo",
                "path": stored_path,
                "registered_at": "2026-09-26T08:07:57Z",
                "updated_at": "2026-09-26T08:07:57Z",
                "tool": {"name": "agentflow", "version": "0.1.0"},
            }
            path = directory / "registry.json"
            path.write_bytes(_registry_bytes([record]))
            self.assertEqual(store.list()[0]["id"], record["id"])

            relative = "relative/demo"
            malformed = [
                {**record, "name": "n" * 201},
                {**record, "path": relative, "id": workspace_id_for(relative)},
                {**record, "id": "ws_000000000000"},
                {**record, "tool": {"name": "bad name", "version": "0.1.0"}},
                {**record, "tool": {"name": "a" * 81, "version": "0.1.0"}},
                {**record, "tool": {"name": "agentflow", "version": "v" * 41}},
                {**record, "registered_at": "2026-09-26 08:07:57"},
            ]
            for item in malformed:
                raw = _registry_bytes([item])
                path.write_bytes(raw)
                with self.assertRaises(RegistryCorrupt):
                    store.list()
                self.assertEqual(path.read_bytes(), raw)
                with self.assertRaises(RegistryCorrupt):
                    store.register(str(workspace), None, TOOL)
                self.assertEqual(path.read_bytes(), raw)

    def test_temp_sibling_is_not_loaded_and_failed_replace_keeps_the_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_workspace(root, "one")
            second = make_workspace(root, "two")
            directory = root / "registry"
            store = RegistryStore(directory)
            created, _ = store.register(str(first), None, TOOL)
            sibling = directory / "registry.json.tmp.leftover"
            sibling.write_text('{"schema_version":1,"workspaces":[]}', encoding="utf-8")
            self.assertEqual([row["id"] for row in store.list()], [created["id"]])
            self.assertTrue(sibling.exists())
            original = (directory / "registry.json").read_bytes()

            def fail_replace(src: Path, dst: Path) -> None:
                raise OSError("replace failed")

            with patch("agentflow_registry.store.os.replace", fail_replace):
                with self.assertRaises(OSError):
                    store.register(str(second), None, TOOL)
            self.assertEqual((directory / "registry.json").read_bytes(), original)
            self.assertEqual(store.get(created["id"])["name"], "one")
            self.assertTrue(sibling.exists())
            self.assertEqual(list(directory.glob("registry.json.tmp.*")), [sibling])

    def test_successful_replace_preserves_an_unrelated_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = make_workspace(root, "one")
            directory = root / "registry"
            store = RegistryStore(directory)
            created, _ = store.register(str(workspace), None, TOOL)
            other = directory / "registry.json.tmp.otherwriter"
            other.write_text("other writer", encoding="utf-8")
            store.register(str(workspace), "renamed", TOOL)
            self.assertEqual(other.read_text(encoding="utf-8"), "other writer")
            self.assertEqual(store.get(created["id"])["name"], "renamed")
            self.assertEqual(list(directory.glob("registry.json.tmp.*")), [other])

    def test_failed_write_or_fsync_removes_the_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = make_workspace(root, "one")
            directory = root / "registry"
            store = RegistryStore(directory)
            created, _ = store.register(str(workspace), None, TOOL)
            original = (directory / "registry.json").read_bytes()

            def fail_fsync(fd: int) -> None:
                raise OSError("fsync failed")

            sibling = directory / "registry.json.tmp.leftover"
            sibling.write_text("partial", encoding="utf-8")
            with patch("agentflow_registry.store.os.fsync", fail_fsync):
                with self.assertRaises(OSError):
                    store.register(str(workspace), "renamed", TOOL)
            self.assertEqual((directory / "registry.json").read_bytes(), original)
            self.assertEqual(store.get(created["id"])["name"], "one")
            self.assertEqual(sibling.read_text(encoding="utf-8"), "partial")
            self.assertEqual(list(directory.glob("registry.json.tmp.*")), [sibling])

            def fail_write(fd: int, data: bytes) -> int:
                raise OSError("write failed")

            with patch("agentflow_registry.store.os.write", fail_write):
                with self.assertRaises(OSError):
                    store.register(str(workspace), "renamed", TOOL)
            self.assertEqual((directory / "registry.json").read_bytes(), original)
            self.assertEqual(store.get(created["id"])["name"], "one")
            self.assertEqual(sibling.read_text(encoding="utf-8"), "partial")
            self.assertEqual(list(directory.glob("registry.json.tmp.*")), [sibling])

    @unittest.skipUnless(os.name == "posix", "record locks are POSIX-only")
    def test_cross_process_lock_returns_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "registry"
            directory.mkdir()
            lock_path = directory / "registry.lock"
            script = textwrap.dedent(
                """
                import fcntl, os, sys, time
                from agentflow_kernel.process_wrapper import fcntl_flock
                fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
                fcntl_flock(fd, fcntl.F_SETLK, fcntl.F_WRLCK)
                sys.stdout.write("locked\\n")
                sys.stdout.flush()
                time.sleep(30)
                """
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script, str(lock_path)],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdout is not None
                self.assertEqual(proc.stdout.readline().strip(), "locked")
                workspace = make_workspace(Path(tmp), "demo")
                store = RegistryStore(directory, timeout_sec=0.2)
                with self.assertRaises(RegistryBusy):
                    store.register(str(workspace), None, TOOL)
            finally:
                proc.kill()
                proc.wait(timeout=2)
                if proc.stdout is not None:
                    proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
