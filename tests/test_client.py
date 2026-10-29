"""drums client against a fake drumsd (no network beyond localhost, no root)."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drums  # noqa: E402


def slot(i, state="ready", episode=1, verdict="pending"):
    return {"id": f"s{i}", "state": state, "episode": episode, "validity": {"verdict": verdict}, "port": 25000 + i}


class Fake(BaseHTTPRequestHandler):
    calls = []
    gates_seq = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj, raw=None):
        b = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null")

    def do_POST(self):
        b = self._body()
        Fake.calls.append(("POST", self.path, b))
        if self.path == "/v1/slots":
            if b["checkpoint"] == "broken":
                return self._send(201, slot(9, state="quarantined"))
            return self._send(201, slot(100))
        if self.path.endswith("/reset"):
            return self._send(200, slot(100, episode=2))
        if self.path.endswith("/step"):
            return self._send(200, {"result": {"status": "success"}, "screenshot_png_b64": "iVBORw0KGgo=", "episode": 2, "validity": {"verdict": "pending"}})
        if self.path.endswith("/capture"):
            return self._send(200, {"id": "s100-c101"})
        if self.path.endswith("/branch"):
            return self._send(200, {"checkpoint": {"id": "s100-c102"}, "children": [slot(103), slot(104)]})
        self._send(404, {"error": "no"})

    def do_GET(self):
        Fake.calls.append(("GET", self.path, None))
        if self.path.endswith("/gates"):
            v = Fake.gates_seq.pop(0) if Fake.gates_seq else "valid"
            return self._send(200, {"validity": {"verdict": v}})
        if self.path.endswith("/screenshot"):
            return self._send(200, None, raw=b"\x89PNG....")
        self._send(404, {"error": "no slot"})

    def do_DELETE(self):
        Fake.calls.append(("DELETE", self.path, None))
        self._send(200, slot(100, state="destroyed"))


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    def setUp(self):
        Fake.calls.clear()

    def test_env_reset_step_branch_save_close(self):
        e = drums.env({"id": "t1", "setup": []}, url=self.url)
        self.assertEqual(e.id, "s100")
        self.assertEqual(Fake.calls[0][2], {"checkpoint": "clean", "task": {"id": "t1", "setup": []}})
        e.reset()
        self.assertEqual(e.episode, 2)
        r = e.step("pyautogui.click(1, 2)", screenshot=True)
        self.assertEqual(Fake.calls[-1][2]["action"], {"type": "pyautogui", "code": "pyautogui.click(1, 2)"})
        self.assertTrue(r["screenshot_png"].startswith(b"\x89PNG"))
        kids = e.branch(2)
        self.assertEqual([k.id for k in kids], ["s103", "s104"])
        self.assertEqual(e.save(), "s100-c101")
        self.assertEqual(e.screenshot()[:4], b"\x89PNG")
        e.close()
        self.assertEqual(e.state, "destroyed")

    def test_from_state_wins(self):
        drums.env(from_state="s100-c101", url=self.url)
        self.assertEqual(Fake.calls[0][2]["checkpoint"], "s100-c101")

    def test_valid_waits_for_pending(self):
        e = drums.env(url=self.url)
        Fake.gates_seq[:] = ["pending", "pending", "valid"]
        self.assertTrue(e.valid(timeout=5))
        Fake.gates_seq[:] = ["invalid"]
        self.assertFalse(e.valid(timeout=5))

    def test_a_slot_that_did_not_come_up_raises(self):
        with self.assertRaises(drums.InvalidEpisode):
            drums.env(checkpoint="broken", url=self.url)

    def test_http_errors_carry_the_message(self):
        c = drums.Client(self.url)
        with self.assertRaises(drums.DrumsError) as cm:
            c.call("GET", "/v1/slots/nope")
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("no slot", cm.exception.message)


if __name__ == "__main__":
    unittest.main()
