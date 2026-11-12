"""drums.mcp, the two-layer server: tools/list == mcp_tools.json, every tool once against a fake gateway or a
stand-in desktop, confirm refusals before any request, --env binding, 404 -> "not available", schema violations."""
import base64
import io
import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import drums  # noqa: E402
from drums import mcp  # noqa: E402

TOKEN = "k" * 43
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_JSON = os.path.join(HERE, "..", "drums", "mcp_tools.json")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
CHECKPOINTS = [{"id": "osworld-ubuntu", "installed": True, "captured_from": None, "created_epoch": 1.0},
               {"id": "snp-1111", "installed": False, "captured_from": "c1", "created_epoch": 2.0},
               {"id": "fork-22", "captured_from": "c2", "created_epoch": 3.0}]


def computer(i, state="ready", **kw):
    return {"id": f"c{i}", "state": state, "episode": 1, "validity": {"verdict": "valid", "gates": []}, "gates": {"big": "report"}, **kw}


class FakeGateway(BaseHTTPRequestHandler):
    """Emulates the /v1 routes the tools use and records every request as (method, path, body, headers)."""
    calls = []
    no_runs = False        # a plain drumsd: /v1/runs, /v1/api-keys, /v1/audit are not routes (404, empty body)

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None):
        b = raw if raw is not None else (b"" if obj is None else json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _record(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null")
        FakeGateway.calls.append((method, self.path, body, dict(self.headers)))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"error": "missing or wrong bearer token", "code": "unauthorized"})
            return None
        return body

    def _route(self, method, body):
        u = urllib.parse.urlsplit(self.path)
        path, q = u.path, dict(urllib.parse.parse_qsl(u.query))
        parts = path.strip("/").split("/")
        gateway_only = len(parts) >= 2 and parts[1] in ("runs", "api-keys", "audit")
        if gateway_only and FakeGateway.no_runs:
            return self._send(404)                                 # axum's default: no body at all
        # ---- slots
        if path == "/v1/slots" and method == "GET":
            rows = [computer(1), computer(2, state="destroyed")]
            if q.get("include_destroyed") != "true":
                rows = [r for r in rows if r["state"] != "destroyed"]
            return self._send(200, {"items": rows[: int(q.get("limit") or 100)], "next_after": None})
        if path == "/v1/slots" and method == "POST":
            return self._send(201, computer(7, checkpoint=body.get("checkpoint"), task=body.get("task")))
        if path.startswith("/v1/slots/"):
            cid = parts[2]
            if cid == "missing":
                return self._send(404, {"error": f"no env {cid} on this drumsd", "code": "not_found"})
            if len(parts) == 3 and method == "GET":
                return self._send(200, computer(1))
            if len(parts) == 3 and method == "DELETE":
                return self._send(200, computer(1, state="destroyed"))
            op = parts[3]
            if op == "step":
                return self._send(200, {"result": {"returncode": 0, "output": "", "error": ""}, "seconds": 0.1})
            if op == "screenshot":
                return self._send(200, raw=PNG)
            if op == "reset":
                return self._send(200, computer(1, episode=2))
            if op == "task":
                return self._send(200, computer(1, task=body.get("task")))
            if op == "verify":
                return self._send(200, {"slot": cid, "score": 1.0, "valid": True, "validity": {"verdict": "valid", "gates": []},
                                        "audit": {"trusted": True, "flags": []}, "checker_error": None})
            if op == "capture":
                return self._send(200, {"id": "snp-new", "captured_from": cid, "name": body.get("id")})
            if op == "branch":
                n = int(body.get("n") or 1)
                return self._send(200, {"children": [computer(10 + i) for i in range(n)], "checkpoint": {"id": "fork-99"}, "seconds": 0.42})
        # ---- checkpoints
        if path == "/v1/checkpoints" and method == "GET":
            return self._send(200, CHECKPOINTS)
        if path.startswith("/v1/checkpoints/") and method == "DELETE":
            return self._send(200, {"id": parts[2], "deleted": True})
        # ---- tasks
        if path == "/v1/tasks" and method == "GET":
            rows = [{"id": "t1", "domain": "chrome", "instruction": "open a tab"}, {"id": "t2", "domain": "os", "instruction": "make a folder"}]
            if q.get("domain"):
                rows = [r for r in rows if r["domain"] == q["domain"]]
            return self._send(200, rows)
        if path.startswith("/v1/tasks/"):
            return self._send(200, {"id": parts[2], "domain": "chrome", "instruction": "open a tab", "config": [], "evaluator": {"func": "x"}})
        # ---- runs
        if path == "/v1/runs" and method == "GET":
            return self._send(200, {"items": [{"id": "r1", "kind": q.get("kind") or "scripted_test", "status": q.get("status") or "queued"}], "next_cursor": None})
        if path == "/v1/runs" and method == "POST":
            return self._send(202, {"run_id": "r2", "operation_id": "op2", "status": "queued", "created": True})
        if path.startswith("/v1/runs/"):
            if parts[2] == "nope":
                return self._send(404, {"error": "no such run", "code": "not_found"})
            return self._send(200, {"id": parts[2], "status": "completed", "attempts": {"done": 3}, "events": []})
        # ---- api keys
        if path == "/v1/api-keys" and method == "GET":
            return self._send(200, {"items": [{"id": "k1", "name": "ci", "prefix": "drums_sk_abc", "scopes": ["runs:read"]}]})
        if path == "/v1/api-keys" and method == "POST":
            return self._send(201, {"id": "k2", "prefix": "drums_sk_new", "key": "drums_sk_new_plaintext", "expires_at": None})
        if path.startswith("/v1/api-keys/") and method == "DELETE":
            return self._send(200, {"id": parts[2], "revoked_at": "2026-11-11T00:00:00Z"})
        # ---- audit
        if path == "/v1/audit" and method == "GET":
            return self._send(200, {"items": [{"action": "mcp.list_computers", "actor": {"kind": "mcp"}, "outcome": "success"}], "next_cursor": "n1"})
        self._send(404, {"error": "no such route", "code": "not_found"})

    def do_GET(self):
        b = self._record("GET")
        if b is not None or self.headers.get("Authorization") == f"Bearer {TOKEN}":
            self._route("GET", b)

    def do_POST(self):
        b = self._record("POST")
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            self._route("POST", b)

    def do_DELETE(self):
        b = self._record("DELETE")
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            self._route("DELETE", b)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        FakeGateway.calls = []
        FakeGateway.no_runs = False
        self.client = drums.Client(self.url, TOKEN, retry_s=0.5)
        self.srv = mcp.Server(self.client)

    # helpers ------------------------------------------------------------------------------------------------------------
    def rpc(self, srv, method, params=None, mid=1):
        return srv.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def tool(self, name, args=None, srv=None):
        """tools/call -> (result dict, parsed text or image bytes)."""
        r = self.rpc(srv or self.srv, "tools/call", {"name": name, "arguments": args or {}})
        res = r["result"]
        c = res["content"][0]
        if c["type"] == "image":
            return res, base64.b64decode(c["data"])
        if res["isError"]:
            return res, c["text"]
        return res, json.loads(c["text"])

    def last(self):
        return FakeGateway.calls[-1]

    # protocol -----------------------------------------------------------------------------------------------------------
    def test_tools_list_equals_mcp_tools_json_in_order_with_annotations(self):
        with open(TOOLS_JSON) as f:
            want = json.load(f)["tools"]
        got = self.rpc(self.srv, "tools/list")["result"]["tools"]
        self.assertEqual(len(got), 34)
        self.assertEqual([t["name"] for t in got], [t["name"] for t in want])
        for g, w in zip(got, want):
            self.assertEqual(g["description"], w["description"])
            self.assertEqual(g["inputSchema"], w["inputSchema"])
            self.assertEqual(g["inputSchema"]["type"], "object")
            ann = g["annotations"]
            self.assertEqual(set(ann), {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"})
            self.assertEqual(ann["destructiveHint"], bool(w.get("destructive")))
            self.assertFalse(ann["openWorldHint"])
        byname = {t["name"]: t["annotations"] for t in got}
        for n in ("list_templates", "get_run", "read_audit_log", "screenshot", "browser_tabs", "verify"):
            self.assertTrue(byname[n]["readOnlyHint"], n)
        for n in ("create_computer", "gui", "shell", "fork", "destroy_computer"):
            self.assertFalse(byname[n]["readOnlyHint"], n)
        self.assertTrue(byname["list_runs"]["idempotentHint"])
        self.assertFalse(byname["screenshot"]["idempotentHint"])
        for n in ("destroy_computer", "delete_snapshot", "revoke_api_key"):
            self.assertTrue(byname[n]["destructiveHint"], n)

    def test_initialize_ping_unknown_method_notifications_and_parse_errors(self):
        r = self.rpc(self.srv, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})["result"]
        self.assertEqual(r["protocolVersion"], "2024-11-05")
        self.assertEqual(r["capabilities"], {"tools": {}})
        self.assertEqual(r["serverInfo"], {"name": "drums", "version": drums.__version__})
        self.assertEqual(self.rpc(self.srv, "initialize")["result"]["protocolVersion"], mcp.PROTOCOL)
        self.assertEqual(self.rpc(self.srv, "ping")["result"], {})
        self.assertEqual(self.rpc(self.srv, "resources/list")["error"]["code"], -32601)
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "id": 9, "result": {}}))      # a client's response: ignored
        out = io.StringIO()
        self.srv.serve(io.StringIO('{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n{not json\n'
                                   '[{"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/x"}]\n'), out)
        rep = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(rep[0]["id"], 1)
        self.assertEqual(rep[1]["error"]["code"], -32700)
        self.assertEqual([r["id"] for r in rep[2]], [2])                                     # batch: one array, no notification reply
        self.assertEqual(self.rpc(self.srv, "tools/call", {"name": "no_such_tool"})["result"]["isError"], True)

    # workspace tools --------------------------------------------------------------------------------------------------
    def test_templates_and_snapshots_split_the_checkpoint_list(self):
        res, out = self.tool("list_templates")
        self.assertEqual(self.last()[:2], ("GET", "/v1/checkpoints"))
        self.assertEqual([t["checkpoint"] for t in out["templates"]], ["osworld-ubuntu"])
        res, out = self.tool("list_snapshots")
        self.assertEqual([s["id"] for s in out["snapshots"]], ["snp-1111", "fork-22"])

    def test_list_get_create_destroy_computer(self):
        res, out = self.tool("list_computers")
        self.assertEqual(self.last()[:2], ("GET", "/v1/slots"))
        self.assertEqual([c["id"] for c in out["computers"]], ["c1"])
        self.assertNotIn("gates", out["computers"][0])
        res, out = self.tool("list_computers", {"include_destroyed": True, "limit": 5})
        self.assertEqual(self.last()[1], "/v1/slots?include_destroyed=true&limit=5")
        self.assertEqual([c["id"] for c in out["computers"]], ["c1", "c2"])

        res, out = self.tool("create_computer", {"template": "osworld-ubuntu", "task_id": "t1", "ttl_s": 600, "setup": True})
        self.assertEqual(FakeGateway.calls[-2][:2], ("GET", "/v1/tasks/t1"))               # task_id resolved first
        m, p, body, _ = self.last()
        self.assertEqual((m, p), ("POST", "/v1/slots"))
        self.assertEqual(body, {"checkpoint": "osworld-ubuntu", "task": {"id": "t1", "domain": "chrome", "instruction": "open a tab", "config": [], "evaluator": {"func": "x"}},
                                "ttl_s": 600, "setup": True})
        self.assertEqual(out["id"], "c7")
        res, out = self.tool("create_computer", {"template": "snp-1111", "task": {"id": "inline", "config": []}})
        self.assertEqual(self.last()[2], {"checkpoint": "snp-1111", "task": {"id": "inline", "config": []}})
        res, out = self.tool("create_computer", {"template": "osworld-ubuntu"})
        self.assertEqual(self.last()[2], {"checkpoint": "osworld-ubuntu", "task": None})

        res, out = self.tool("get_computer", {"computer_id": "c1"})
        self.assertEqual(self.last()[:2], ("GET", "/v1/slots/c1"))
        self.assertEqual(out["state"], "ready")
        res, out = self.tool("get_computer", {"computer_id": "missing"})
        self.assertTrue(res["isError"])
        self.assertIn("no env missing", out)

        res, out = self.tool("destroy_computer", {"computer_id": "c1", "confirm": True})
        self.assertEqual(self.last()[:2], ("DELETE", "/v1/slots/c1"))
        self.assertEqual(out["state"], "destroyed")

    def test_snapshots_create_and_delete(self):
        res, out = self.tool("create_snapshot", {"computer_id": "c1", "name": "after-login"})
        self.assertEqual(self.last()[:3], ("POST", "/v1/slots/c1/capture", {"id": "after-login"}))
        self.assertEqual(out["id"], "snp-new")
        res, out = self.tool("create_snapshot", {"computer_id": "c1"})
        self.assertEqual(self.last()[2], {})
        res, out = self.tool("delete_snapshot", {"snapshot_id": "snp-1111", "confirm": True})
        self.assertEqual(self.last()[:2], ("DELETE", "/v1/checkpoints/snp-1111"))
        self.assertTrue(out["deleted"])

    def test_tasks(self):
        res, out = self.tool("list_tasks")
        self.assertEqual(self.last()[:2], ("GET", "/v1/tasks"))
        self.assertEqual([t["id"] for t in out["tasks"]], ["t1", "t2"])
        res, out = self.tool("list_tasks", {"domain": "os", "limit": 1})
        self.assertEqual(self.last()[1], "/v1/tasks?domain=os")
        self.assertEqual([t["id"] for t in out["tasks"]], ["t2"])
        res, out = self.tool("get_task", {"task_id": "t2"})
        self.assertEqual(self.last()[:2], ("GET", "/v1/tasks/t2"))
        self.assertEqual(out["evaluator"], {"func": "x"})

    def test_runs_and_evaluations(self):
        res, out = self.tool("list_runs")
        self.assertEqual(self.last()[1], "/v1/runs?limit=50")
        self.assertEqual(out["items"][0]["id"], "r1")
        res, out = self.tool("list_runs", {"status": "running", "limit": 5})
        self.assertEqual(self.last()[1], "/v1/runs?status=running&limit=5")
        res, out = self.tool("list_evaluations", {"limit": 3})
        self.assertEqual(self.last()[1], "/v1/runs?kind=evaluation&limit=3")
        self.assertEqual(out["items"][0]["kind"], "evaluation")

        spec = {"name": "n", "kind": "scripted_test", "task_ids": ["t1"]}
        res, out = self.tool("create_run", {"spec": spec, "idempotency_key": "abcdefgh-1"})
        m, p, body, h = self.last()
        self.assertEqual((m, p, body), ("POST", "/v1/runs", spec))
        self.assertEqual(h.get("Idempotency-Key"), "abcdefgh-1")
        self.assertEqual(out["run_id"], "r2")
        res, out = self.tool("create_run", {"spec": spec})
        self.assertIsNone(self.last()[3].get("Idempotency-Key"))
        res, out = self.tool("create_evaluation", {"spec": spec})
        self.assertEqual(self.last()[2]["kind"], "evaluation")
        self.assertEqual(spec["kind"], "scripted_test")                                     # the caller's spec is not changed

        res, out = self.tool("get_run", {"run_id": "r1"})
        self.assertEqual(self.last()[:2], ("GET", "/v1/runs/r1"))
        self.assertEqual(out["attempts"], {"done": 3})
        res, out = self.tool("get_evaluation", {"run_id": "r1"})
        self.assertEqual(self.last()[:2], ("GET", "/v1/runs/r1"))
        res, out = self.tool("get_run", {"run_id": "nope"})                                 # a gateway's own 404 is passed on
        self.assertTrue(res["isError"])
        self.assertIn("no such run", out)
        self.assertNotIn("not available", out)

    def test_api_keys_and_audit(self):
        res, out = self.tool("list_api_keys")
        self.assertEqual(self.last()[:2], ("GET", "/v1/api-keys"))
        self.assertEqual(out["items"][0]["prefix"], "drums_sk_abc")
        res, out = self.tool("create_api_key", {"name": "ci", "scopes": ["runs:read", "computers:read"], "expires_in_days": 30})
        self.assertEqual(self.last()[:3], ("POST", "/v1/api-keys", {"name": "ci", "scopes": ["runs:read", "computers:read"], "expires_in_days": 30}))
        self.assertEqual(out["key"], "drums_sk_new_plaintext")
        res, out = self.tool("create_api_key", {"name": "ci", "scopes": ["runs:read"]})
        self.assertEqual(self.last()[2], {"name": "ci", "scopes": ["runs:read"]})
        res, out = self.tool("revoke_api_key", {"key_id": "k1", "confirm": True})
        self.assertEqual(self.last()[:2], ("DELETE", "/v1/api-keys/k1"))
        self.assertEqual(out["id"], "k1")
        res, out = self.tool("read_audit_log")
        self.assertEqual(self.last()[1], "/v1/audit?limit=50")
        self.assertEqual(out["items"][0]["action"], "mcp.list_computers")
        res, out = self.tool("read_audit_log", {"action": "mcp", "limit": 10, "cursor": "n1"})
        self.assertEqual(self.last()[1], "/v1/audit?action=mcp&limit=10&cursor=n1")

    def test_gateway_only_tools_against_a_plain_drumsd(self):
        FakeGateway.no_runs = True
        for name, args in (("list_runs", {}), ("create_run", {"spec": {}}), ("get_run", {"run_id": "r1"}), ("list_evaluations", {}),
                           ("create_evaluation", {"spec": {}}), ("get_evaluation", {"run_id": "r1"}), ("list_api_keys", {}),
                           ("create_api_key", {"name": "a", "scopes": ["runs:read"]}), ("revoke_api_key", {"key_id": "k", "confirm": True}),
                           ("read_audit_log", {})):
            res, out = self.tool(name, args)
            self.assertTrue(res["isError"], name)
            self.assertEqual(out, "not available on this server (needs the Drums gateway)", name)

    # desktop tools through the real Env over the fake gateway -------------------------------------------------------
    def test_desktop_tools_over_the_api(self):
        res, out = self.tool("screenshot", {"computer_id": "c1"})
        self.assertEqual(self.last()[:2], ("GET", "/v1/slots/c1/screenshot"))
        self.assertEqual(res["content"][0]["type"], "image")
        self.assertEqual(res["content"][0]["mimeType"], "image/png")
        self.assertEqual(out, PNG)

        res, out = self.tool("gui", {"computer_id": "c1", "code": "pyautogui.click(1, 2)"})
        self.assertEqual(self.last()[:3], ("POST", "/v1/slots/c1/step", {"action": {"type": "pyautogui", "code": "pyautogui.click(1, 2)"}, "screenshot": False}))
        self.assertEqual(out["returncode"], 0)
        res, out = self.tool("shell", {"computer_id": "c1", "command": "ls ~", "timeout_s": 5})
        self.assertEqual(self.last()[2]["action"], {"type": "execute", "command": ["timeout", "5", "bash", "-c", "ls ~"], "shell": False})
        self.assertEqual(out["returncode"], 0)

        res, out = self.tool("verify", {"computer_id": "c1"})
        self.assertEqual(self.last()[:3], ("POST", "/v1/slots/c1/verify", {}))
        self.assertEqual(out, {"score": 1.0, "valid": True, "trusted": True, "audit_flags": [], "checker_error": None})

        res, out = self.tool("fork", {"computer_id": "c1", "n": 3})
        self.assertEqual(self.last()[:3], ("POST", "/v1/slots/c1/branch", {"n": 3}))
        self.assertEqual(out["children"], ["c10", "c11", "c12"])
        self.assertEqual(out["seconds"], 0.42)
        self.assertEqual(out["state"], "fork-99")
        res, out = self.tool("fork", {"computer_id": "c1"})
        self.assertEqual(self.last()[2], {"n": 1})

        res, out = self.tool("reset", {"computer_id": "c1"})
        self.assertEqual(self.last()[:2], ("POST", "/v1/slots/c1/reset"))
        self.assertEqual(out["episode"], 2)
        self.assertNotIn("gates", out)

        res, out = self.tool("load_task", {"computer_id": "c1", "task_id": "t1"})
        self.assertEqual(FakeGateway.calls[-2][:2], ("GET", "/v1/tasks/t1"))
        self.assertEqual(self.last()[:2], ("POST", "/v1/slots/c1/task"))
        self.assertEqual(self.last()[2]["task"]["id"], "t1")
        res, out = self.tool("load_task", {"computer_id": "c1", "task": {"id": "inline"}})
        self.assertEqual(self.last()[2], {"task": {"id": "inline"}})
        res, out = self.tool("load_task", {"computer_id": "c1"})
        self.assertTrue(res["isError"])
        self.assertIn("task_id or task", out)

    # safety -----------------------------------------------------------------------------------------------------------
    def test_destructive_tools_refuse_without_confirm_before_any_request(self):
        for name, args in (("destroy_computer", {"computer_id": "c1"}), ("destroy_computer", {"computer_id": "c1", "confirm": False}),
                           ("destroy_computer", {"computer_id": "c1", "confirm": "yes"}), ("delete_snapshot", {"snapshot_id": "snp-1111"}),
                           ("revoke_api_key", {"key_id": "k1"}), ("revoke_api_key", {"key_id": "k1", "confirm": 1})):
            res, out = self.tool(name, args)
            self.assertTrue(res["isError"], name)
            self.assertEqual(out, f"refused: pass confirm: true to {name}")
        self.assertEqual(FakeGateway.calls, [])

    def test_schema_violations_name_the_field_and_send_nothing(self):
        cases = [("get_computer", {}, "computer_id"), ("fork", {"computer_id": "c1", "n": 0}, "'n'"), ("fork", {"computer_id": "c1", "n": 65}, "'n'"),
                 ("fork", {"computer_id": "c1", "n": "2"}, "'n'"), ("shell", {"computer_id": "c1", "command": ""}, "'command'"),
                 ("shell", {"computer_id": "c1", "command": "ls", "timeout_s": 500}, "'timeout_s'"), ("gui", {"computer_id": "c1", "code": 5}, "'code'"),
                 ("list_computers", {"limit": 501}, "'limit'"), ("list_computers", {"bogus": 1}, "'bogus'"),
                 ("create_computer", {"template": ""}, "'template'"), ("create_computer", {"template": "x", "task": "t1"}, "'task'"),
                 ("create_api_key", {"name": "a", "scopes": []}, "'scopes'"), ("create_api_key", {"name": "a", "scopes": ["root"]}, "'scopes[0]'"),
                 ("create_api_key", {"name": "a", "scopes": ["runs:read", "runs:read"]}, "'scopes'"),
                 ("create_api_key", {"name": "a", "scopes": ["runs:read"], "expires_in_days": 400}, "'expires_in_days'"),
                 ("read_audit_log", {"action": "Not Valid"}, "'action'"), ("create_run", {}, "'spec'"), ("create_run", {"spec": []}, "'spec'"),
                 ("get_computer", {"computer_id": "x" * 65}, "'computer_id'"), ("list_tasks", {"domain": 3}, "'domain'")]
        for name, args, field in cases:
            res, out = self.tool(name, args)
            self.assertTrue(res["isError"], (name, args))
            self.assertIn(field, out, (name, args, out))
        self.assertEqual(FakeGateway.calls, [])

    def test_env_binding_fills_computer_id_for_desktop_tools_only(self):
        bound = mcp.Server(self.client, default_computer_id="c1")
        res, out = self.tool("screenshot", {}, srv=bound)
        self.assertEqual(self.last()[1], "/v1/slots/c1/screenshot")
        res, out = self.tool("verify", {}, srv=bound)
        self.assertEqual(self.last()[1], "/v1/slots/c1/verify")
        res, out = self.tool("get_computer", {}, srv=bound)                   # a workspace tool that takes computer_id is filled too
        self.assertEqual(self.last()[1], "/v1/slots/c1")
        res, out = self.tool("screenshot", {"computer_id": "c2"}, srv=bound)  # an explicit id wins
        self.assertEqual(self.last()[1], "/v1/slots/c2/screenshot")
        res, out = self.tool("list_tasks", {}, srv=bound)                     # no computer_id in the schema: nothing added
        self.assertEqual(self.last()[1], "/v1/tasks")
        res, out = self.tool("screenshot", {})                                # unbound: the schema's required field is missing
        self.assertTrue(res["isError"])
        self.assertIn("'computer_id'", out)

    def test_wrong_token_is_a_tool_error_not_a_protocol_error(self):
        srv = mcp.Server(drums.Client(self.url, "wrong", retry_s=0.5))
        r = self.rpc(srv, "tools/call", {"name": "list_computers", "arguments": {}})
        self.assertIn("result", r)
        self.assertTrue(r["result"]["isError"])
        self.assertIn("401", r["result"]["content"][0]["text"])

    def test_no_client_configured(self):
        res, out = self.tool("list_computers", {}, srv=mcp.Server(None))
        self.assertTrue(res["isError"])
        self.assertIn("DRUMS_URL", out)


class FakeDesktopTests(unittest.TestCase):
    """Every desktop tool once through the FakeEnv of test_hybrid_mcp (the stdlib snippets run on this machine)."""

    def setUp(self):
        import tempfile
        from test_hybrid_mcp import FakeEnv
        self.tmp = tempfile.TemporaryDirectory()

        class Env(FakeEnv):
            def __init__(s, root):
                super().__init__(root)
                s.record = {"id": "c1", "state": "ready", "episode": 1, "gates": {"x": 1}}

            def fork(s, n, strict=True):
                return drums.ForkResult([drums.Env(None, {"id": f"c{10 + i}"}) for i in range(n)], "fork-1", {"id": "fork-1"}, [], 0.2)

            def reset(s):
                s.record = dict(s.record, episode=s.record["episode"] + 1)
                return s

            def load_task(s, task):
                s.record = dict(s.record, task=task)
                return s
        self.env = Env(self.tmp.name)
        self.ids = []

        def factory(cid):
            self.ids.append(cid)
            return self.env
        self.srv = mcp.Server(client=None, default_computer_id="c1", env_factory=factory)

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, name, args=None):
        res = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})["result"]
        c = res["content"][0]
        return res, (base64.b64decode(c["data"]) if c["type"] == "image" else c["text"] if res["isError"] else json.loads(c["text"]))

    def test_every_desktop_tool_once(self):
        res, out = self.tool("write_file", {"path": "~/Desktop/notes.txt", "content": "hello\n"})
        self.assertTrue(out["ok"])
        self.assertEqual(self.ids, ["c1"])                                                  # --env binding
        res, out = self.tool("read_file", {"computer_id": "c1", "path": "~/Desktop/notes.txt"})
        self.assertEqual(out["text"], "hello\n")
        res, out = self.tool("list_dir", {"path": "~/Desktop"})
        self.assertEqual([e["name"] for e in out["entries"]], ["notes.txt"])
        res, out = self.tool("shell", {"command": "echo hi"})
        self.assertEqual(out["stdout"].strip(), "hi")
        res, out = self.tool("gui", {"code": "pyautogui.click(1, 2)"})
        self.assertEqual(out["returncode"], 0)
        self.assertEqual(self.env.steps[-1], {"type": "pyautogui", "code": "pyautogui.click(1, 2)"})
        res, out = self.tool("screenshot", {})
        self.assertEqual(res["content"][0]["type"], "image")
        self.assertEqual(out, b"\x89PNG fake")
        res, out = self.tool("browser_tabs", {})                                           # no Chrome here: an error record, not a crash
        self.assertIn("error", out)
        res, out = self.tool("browser_open", {"url": "http://127.0.0.1:8000/"})
        self.assertIn("error", out)
        res, out = self.tool("office_convert", {"path": "~/Desktop/notes.txt", "format": "pdf"})
        self.assertFalse(res["isError"])                                                   # soffice is not here: ok False, with the log
        self.assertIn("ok", out)
        res, out = self.tool("spreadsheet_read", {"path": "~/Desktop/notes.txt"})
        self.assertFalse(res["isError"])
        res, out = self.tool("verify", {})
        self.assertEqual(out["score"], 1.0)
        res, out = self.tool("fork", {"n": 2})
        self.assertEqual(out, {"children": ["c10", "c11"], "seconds": 0.2, "state": "fork-1"})
        res, out = self.tool("reset", {})
        self.assertEqual(out["episode"], 2)
        self.assertNotIn("gates", out)
        res, out = self.tool("load_task", {"task": {"id": "inline", "config": []}})
        self.assertEqual(out["task"]["id"], "inline")
        res, out = self.tool("load_task", {"task_id": "t1"})                               # needs the API to resolve the id: no client here
        self.assertTrue(res["isError"])
        self.assertIn("DRUMS_URL", out)


if __name__ == "__main__":
    unittest.main()
