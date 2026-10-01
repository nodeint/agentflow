from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_registry.child import child_main, read_ready_line


class ChildTests(unittest.TestCase):
    def test_child_main_rejects_argv_without_a_complete_handshake(self) -> None:
        self.assertEqual(child_main([]), 2)
        self.assertEqual(child_main(["--child", "--home", "/tmp", "--port", "1"]), 2)
        self.assertEqual(
            child_main(["--child", "--home", "/tmp", "--port", "1", "--handshake-fd", "-1"]),
            2,
        )

    def test_read_ready_line_returns_none_when_the_deadline_passes(self) -> None:
        parent, child = socket.socketpair()
        try:
            self.assertIsNone(read_ready_line(parent, time.monotonic() + 0.05))
        finally:
            parent.close()
            child.close()


if __name__ == "__main__":
    unittest.main()
