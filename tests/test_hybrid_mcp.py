"""drums.hybrid and drums.mcp against a stand-in env (no drumsd): tool dispatch, chunked writes, MCP JSON-RPC."""
import base64
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from drums import hybrid, mcp  # noqa: E402


class R:
    def __init__(self, result):
        self.result = result


class FakeEnv:
    """Runs execute actions on this machine's python3 for the stdlib snippets; records everything."""

    def __init__(self, root):
        self.steps, self.root, self.files = [], root, {}

    def step(self, action, check=True):
        self.steps.append(action)
        if action["type"] == "pyautogui":
            return R({"returncode": 0})
        cmd = list(action["command"])
        import subprocess
        cmd = [a.replace("/home/user", self.root) for a in cmd]
        if cmd[0] == "timeout":                  # not on every test machine; the desktop has it
            cmd = cmd[2:]
        p = subprocess.run(cmd, capture_output=True, text=True)
        return R({"returncode": p.returncode, "output": p.stdout, "error": p.stderr})

    def screenshot(self):
        return b"\x89PNG fake"

    def verify(self, strict=True):
        class Rew:
            score, valid, trusted, audit_flags, checker_error = 1.0, True, True, [], None
        return Rew()


class Hybrid(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.env = FakeEnv(self.tmp.name)
        self.t = hybrid.HybridTools(self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def test_files_round_trip_through_the_desktop_and_shell_sees_them(self):
        big = "x" * (hybrid.CHUNK * 2 + 17)                       # more than two argv chunks
        self.assertTrue(self.t.write_file("~/Downloads/a.txt", big)["ok"])
        self.assertEqual(self.t.read_file("~/Downloads/a.txt")["text"], big)
        self.assertEqual(self.t.shell("wc -c < ~/Downloads/a.txt".replace("~", self.tmp.name))["stdout"].strip(), str(len(big)))
        self.assertEqual([e["name"] for e in self.t.list_dir("~/Downloads")["entries"]], ["a.txt"])
        self.assertEqual(self.t.read_file("~/nope")["exists"], False)
        self.t.write_file("~/b.bin", b"\x00\xff")
        self.assertEqual(base64.b64decode(self.t.read_file("~/b.bin")["base64"]), b"\x00\xff")

    def test_dispatch_and_schema(self):
        names = {t["name"] for t in hybrid.TOOLS}
        self.assertTrue({"gui", "shell", "read_file", "write_file", "office_convert", "browser_tabs"} <= names)
        self.assertEqual(self.t.call("gui", {"code": "pyautogui.click(1, 2)"})["returncode"], 0)
        with self.assertRaises(ValueError):
            self.t.call("rm_rf", {})


class Mcp(unittest.TestCase):
    def test_json_rpc_session(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            srv = mcp.Server(FakeEnv(d))
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "write_file", "arguments": {"path": "~/x.txt", "content": "hi"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "screenshot", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "verify", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 7, "method": "bogus"}]
            out = io.StringIO()
            srv.serve(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), out)
            rep = [json.loads(x) for x in out.getvalue().splitlines()]
            self.assertEqual([r["id"] for r in rep], [1, 2, 3, 4, 5, 6, 7])       # no reply to the notification
            self.assertIn("verify", [t["name"] for t in rep[1]["result"]["tools"]])
            self.assertTrue(json.loads(rep[2]["result"]["content"][0]["text"])["ok"])
            self.assertEqual(open(os.path.join(d, "x.txt")).read(), "hi")
            self.assertEqual(rep[3]["result"]["content"][0]["type"], "image")
            self.assertEqual(json.loads(rep[4]["result"]["content"][0]["text"])["score"], 1.0)
            self.assertTrue(rep[5]["result"]["isError"])
            self.assertEqual(rep[6]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
