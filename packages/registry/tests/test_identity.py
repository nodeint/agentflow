from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_registry.identity import (
    CORRUPT,
    read_service,
    record_is_confirmed_gone,
    service_identity_matches,
    service_lock_path,
    write_service_json,
)
from agentflow_registry.locks import LockNotHeld, hold_start_lock
from tests.support import ScriptedInspector, service_record


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_identity_matches_only_the_lock_holder(self) -> None:
        record = service_record()
        inspector = ScriptedInspector(record)
        lock = service_lock_path(self.home)
        self.assertTrue(service_identity_matches(record, inspector, lock))
        inspector.hold_lock = False
        self.assertFalse(service_identity_matches(record, inspector, lock))

    def test_a_dead_unlocked_pid_is_confirmed_gone(self) -> None:
        record = service_record()
        inspector = ScriptedInspector(record)
        inspector.hold_lock = False
        inspector.alive = False
        lock = service_lock_path(self.home)
        self.assertTrue(record_is_confirmed_gone(record, inspector, lock))
        inspector.hold_lock = True
        self.assertFalse(record_is_confirmed_gone(record, inspector, lock))

    def test_unreadable_service_json_is_corrupt_and_a_missing_file_is_absent(self) -> None:
        self.assertIsNone(read_service(self.home))
        path = self.home / "service.json"
        path.write_bytes(b"{")
        self.assertIs(read_service(self.home), CORRUPT)

    def test_publish_requires_the_start_lock_before_the_hook(self) -> None:
        record = service_record()
        called: list[bool] = []

        def before(home: Path, payload: dict[str, object]) -> None:
            del home, payload
            called.append(True)

        with self.assertRaises(LockNotHeld):
            write_service_json(self.home, record, before)
        self.assertEqual(called, [])
        with hold_start_lock(self.home, 1):
            write_service_json(self.home, record, before)
        self.assertEqual(called, [True])
        self.assertIn(str(record["instance_id"]), (self.home / "service.json").read_text())


if __name__ == "__main__":
    unittest.main()
