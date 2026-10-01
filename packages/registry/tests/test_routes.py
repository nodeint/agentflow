from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_registry.routes import Call, Result, build_router
from agentflow_registry.store import RegistryBusy, RegistryStore
from tests.support import make_workspace


TOOL = {"name": "agentflow", "version": "0.1.0"}
TOKEN = "token-under-test-token-under-test-token-01"
INSTANCE = "instance-under-test-instance-under-test-01"


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "registry"
        self.home.mkdir()
        self.store = RegistryStore(self.home)
        self.router = build_router()
        self._cwd = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_health_reports_the_instance_only_to_the_bearer(self) -> None:
        absent = self._dispatch("GET", "/v1/health", authorization=None)
        self.assertEqual(absent, Result(200, {"ok": True}))
        authed = self._dispatch("GET", "/v1/health")
        self.assertEqual(authed.status, 200)
        self.assertEqual(authed.payload["instance_id"], INSTANCE)
        denied = self._dispatch("GET", "/v1/health", authorization="Bearer nope")
        self.assertEqual(denied.status, 401)
        self.assertEqual(denied.payload["error"], "unauthorized")

    def test_health_rejects_another_method_before_the_body(self) -> None:
        self.assertFalse(self.router.should_read_body("POST", "/v1/health"))
        self.assertTrue(self.router.should_read_body("POST", "/v1/workspaces"))
        self.assertFalse(self.router.should_read_body("GET", "/v1/workspaces"))
        denied = self._dispatch("POST", "/v1/health", authorization=None)
        self.assertEqual(denied.status, 401)
        rejected = self._dispatch("POST", "/v1/health")
        self.assertEqual(rejected.status, 405)
        self.assertEqual(rejected.payload["error"], "method_not_allowed")

    def test_a_protected_route_requires_a_bearer_before_it_runs(self) -> None:
        missing = self._dispatch("GET", "/v1/workspaces", authorization=None)
        self.assertEqual(missing.status, 401)
        spaced = self._dispatch("GET", "/v1/workspaces", authorization="Bearer one two")
        self.assertEqual(spaced.status, 401)
        unknown = self._dispatch("GET", "/v1/missing")
        self.assertEqual(unknown.status, 404)
        self.assertEqual(unknown.payload["error"], "workspace_not_found")
        self.assertEqual(unknown.payload["message"], "Not found.")
        hidden = self._dispatch("GET", "/v1/missing", authorization=None)
        self.assertEqual(hidden.status, 401)
        wrong = self._dispatch("PUT", "/v1/workspaces")
        self.assertEqual(wrong.status, 405)
        self.assertEqual(
            self._dispatch("PUT", "/v1/workspaces", authorization=None).status, 401
        )

    def test_register_lists_fetches_and_removes_a_workspace(self) -> None:
        workspace = make_workspace(self.root, "demo")
        created = self._post(str(workspace))
        self.assertEqual(created.status, 201)
        again = self._post(str(workspace))
        self.assertEqual(again.status, 200)
        self.assertEqual(again.payload["id"], created.payload["id"])
        listing = self._dispatch("GET", "/v1/workspaces")
        self.assertEqual(listing.status, 200)
        self.assertTrue(listing.payload["workspaces"][0]["available"])
        fetched = self._dispatch("GET", f"/v1/workspaces/{created.payload['id']}")
        self.assertEqual(fetched.status, 200)
        self.assertTrue(fetched.payload["available"])
        removed = self._dispatch("DELETE", f"/v1/workspaces/{created.payload['id']}")
        self.assertEqual(removed.status, 200)
        missing = self._dispatch("GET", f"/v1/workspaces/{created.payload['id']}")
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.payload["error"], "workspace_not_found")

    def test_register_rejects_a_bad_body_before_the_store(self) -> None:
        entered: list[str] = []
        workspace = make_workspace(self.root, "demo")
        cases = [
            (b"not-json", "Request body is not JSON."),
            (
                json.dumps({"path": str(workspace), "tool": TOOL, "extra": 1}).encode(),
                "Request fields are invalid.",
            ),
            (json.dumps({"path": str(workspace)}).encode(), "Tool name is invalid."),
            (json.dumps({"path": 1, "tool": TOOL}).encode(), "Workspace path is not absolute."),
            (
                json.dumps({"path": str(workspace), "name": "x" * 201, "tool": TOOL}).encode(),
                "Workspace name is invalid.",
            ),
        ]
        for body, message in cases:
            entered.clear()
            result = self._dispatch(
                "POST",
                "/v1/workspaces",
                body=body,
                before_store=lambda: entered.append("entered"),
            )
            self.assertEqual(result.status, 400, message)
            self.assertEqual(result.payload["message"], message)
            self.assertEqual(entered, [])
            self.assertFalse((self.home / "registry.json").exists())

    def test_relative_path_is_rejected_even_when_cwd_contains_the_workspace(self) -> None:
        workspace = make_workspace(self.root, "demo")
        os.chdir(self.root)
        rejected = self._post(workspace.name)
        self.assertEqual(rejected.status, 400)
        self.assertFalse((self.home / "registry.json").exists())
        created = self._post(str(workspace))
        self.assertEqual(created.status, 201)
        self.assertEqual(created.payload["path"], str(workspace.resolve()))
        for relative in ("~/somewhere", "../escape"):
            self.assertEqual(self._post(relative).status, 400)
        listing = self.store.list()
        self.assertEqual([row["path"] for row in listing], [str(workspace.resolve())])

    def test_a_corrupt_registry_stays_unchanged(self) -> None:
        workspace = make_workspace(self.root, "demo")
        registry = self.home / "registry.json"
        registry.write_bytes(b"{")
        result = self._post(str(workspace))
        self.assertEqual(result.status, 409)
        self.assertEqual(result.payload["error"], "registry_corrupt")
        self.assertEqual(registry.read_bytes(), b"{")

    def test_a_busy_registry_is_reported_without_the_store_exception(self) -> None:
        def raise_busy() -> list[dict[str, object]]:
            raise RegistryBusy("held")

        self.store.list = raise_busy  # type: ignore[method-assign]
        result = self._dispatch("GET", "/v1/workspaces")
        self.assertEqual(result.status, 409)
        self.assertEqual(result.payload["error"], "registry_busy")
        self.assertEqual(result.payload["message"], "The registry is busy.")

    def test_store_access_runs_the_deadline_hook_first(self) -> None:
        seen: list[str] = []

        def before_store() -> None:
            seen.append("entered")

        self._dispatch("GET", "/v1/workspaces", before_store=before_store)
        self.assertEqual(seen, ["entered"])

    def test_an_added_route_receives_its_path_segment(self) -> None:
        captured: list[str] = []

        @self.router.route("GET", "/v1/echo/{workspace_id}")
        def echo(call: Call) -> Result:
            captured.append(call.params["workspace_id"])
            return Result(200, {"id": call.params["workspace_id"]})

        result = self._dispatch("GET", "/v1/echo/ws_abc")
        self.assertEqual(result.payload, {"id": "ws_abc"})
        self.assertEqual(captured, ["ws_abc"])
        self.assertEqual(self._dispatch("GET", "/v1/echo").status, 404)
        self.assertEqual(self._dispatch("GET", "/v1/echo/ws_abc/more").status, 404)
        with self.assertRaises(ValueError):
            self.router.route("GET", "/v1/{workspace_id}/child")(echo)

    def test_a_nested_workspace_path_is_not_found(self) -> None:
        result = self._dispatch("GET", "/v1/workspaces/ws_one/child")
        self.assertEqual(result.status, 404)

    def _post(self, path: str) -> Result:
        body = json.dumps({"path": path, "tool": TOOL}).encode("utf-8")
        return self._dispatch("POST", "/v1/workspaces", body=body)

    def _dispatch(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        authorization: str | None = f"Bearer {TOKEN}",
        before_store: Callable[[], None] | None = None,
    ) -> Result:
        call = Call(
            method=method,
            path=path,
            store=self.store,
            token=TOKEN,
            instance_id=INSTANCE,
            authorization=authorization,
            body=body,
        )
        if before_store is not None:
            call.before_store = before_store
        return self.router.dispatch(call)


if __name__ == "__main__":
    unittest.main()
