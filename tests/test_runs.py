"""The run object (A-36): open on POST /v1/runs, buffered steps flushed at most once per flush_s by PATCH
/v1/runs/{id}, close on POST /v1/runs/{id}/close, the run label on creates while it is open, and a no-op with one
warning against a gateway that does not have these routes yet (today's gateway answers 400 to a training body and
404 "method not allowed" to PATCH). Localhost fakes only."""
import json
import os
import sys
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import drums  # noqa: E402
from drums import runs  # noqa: E402

TOKEN = "t" * 43
RUN_ID = "7d4f0c1e-3b9a-4c55-9a1e-2f6b8c0d1e2f"


class Gw(BaseHTTPRequestHandler):
    mode = "w3"            # "w3": the run routes exist; "today": the 0.3 gateway; "down": 503 on runs
    calls = []

    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        Gw.calls.append({"method": self.command, "path": self.path, "body": body, "headers": dict(self.headers)})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._reply(401, {"error": "missing, invalid, revoked or expired API key", "code": "unauthenticated"})
        if self.path == "/v1/slots" and self.command == "POST":
            return self._reply(201, {"id": f"c{len(Gw.calls)}", "state": "ready", "episode": 1, "validity": {"verdict": "pending"}})
        if self.path.startswith("/v1/runs"):
            if Gw.mode == "down":
                return self._reply(503, {"error": "the service is busy", "code": "retryable_infrastructure_failure"})
            if Gw.mode == "noscope":
                return self._reply(403, {"error": "this key does not have the scope for this call", "code": "forbidden"})
            if Gw.mode == "today":
                if self.command == "POST" and self.path == "/v1/runs":
                    return self._reply(400, {"error": "invalid run spec: task_ids Required", "code": "invalid_request"})
                return self._reply(404, {"error": "method not allowed on this path", "code": "not_found"})
            if self.command == "POST" and self.path == "/v1/runs":
                return self._reply(201, {"run_id": RUN_ID, "status": "running", "created": True})
            if self.command == "PATCH" and self.path == f"/v1/runs/{RUN_ID}":
                return self._reply(200, {"run_id": RUN_ID, "steps_recorded": len(body["steps"])})
            if self.command == "POST" and self.path == f"/v1/runs/{RUN_ID}/close":
                return self._reply(200, {"run_id": RUN_ID, "status": body["status"]})
            if self.command == "GET" and self.path == f"/v1/runs/{RUN_ID}":
                return self._reply(200, {"run": {"id": RUN_ID}})
        return self._reply(404, {"error": "no such route", "code": "not_found"})

    do_GET = do_POST = do_PATCH = do_DELETE = _handle


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class RunObject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Gw)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Gw.calls.clear()
        Gw.mode = "w3"
        self.c = drums.Client(self.url, TOKEN, retry_s=0.2)

    def calls(self, method, path_part):
        return [c for c in Gw.calls if c["method"] == method and path_part in c["path"]]

    def run_obj(self, **kw):
        clock = Clock()
        r = runs.Run(self.c, "qwen27b-d2", "training", task_set="0b5c2a52-77a0-4c0e-9a39-0a7a0f3c1d10", config={"lr": 1e-5, "k": 6},
                     clock=clock, **kw)
        return r, clock

    def test_open_step_flush_close(self):
        r, clock = self.run_obj()
        with r:
            self.assertEqual(r.id, RUN_ID)
            r.step(0, reward=0.25, zero_advantage_share=0.5, rollouts=64, desktops_busy=40)   # first point: sent at once
            for i in range(1, 6):
                clock.t += 5
                r.step(i, reward=0.3)                                                      # inside 30 s: buffered
            self.assertEqual(len(self.calls("PATCH", "/v1/runs/")), 1)
            clock.t += 30
            r.step(6, reward=0.4)                                                          # 30 s later: one flush
            self.assertEqual(len(self.calls("PATCH", "/v1/runs/")), 2)
            r.step(7, reward=0.5)
        patches = self.calls("PATCH", "/v1/runs/")
        self.assertEqual(len(patches), 3)                                                 # the last one on close
        sent = [p["step"] for c in patches for p in c["body"]["steps"]]
        self.assertEqual(sent, list(range(8)))
        first = patches[0]["body"]["steps"][0]
        self.assertEqual({k: first[k] for k in ("step", "reward", "zero_advantage_share", "rollouts", "desktops_busy")},
                         {"step": 0, "reward": 0.25, "zero_advantage_share": 0.5, "rollouts": 64, "desktops_busy": 40})
        self.assertTrue(first["at"].endswith("Z"))
        opened = self.calls("POST", "/v1/runs")[0]
        self.assertEqual(opened["path"], "/v1/runs")
        self.assertEqual(opened["body"]["kind"], "training")
        self.assertEqual(opened["body"]["task_set_id"], "0b5c2a52-77a0-4c0e-9a39-0a7a0f3c1d10")
        self.assertEqual(opened["body"]["config_hash"], runs.config_hash({"k": 6, "lr": 1e-5}))       # key order does not matter
        self.assertTrue(opened["headers"].get("Idempotency-Key"))
        closed = self.calls("POST", "/close")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["body"], {"status": "completed", "error": None, "steps_total": 8})
        self.assertEqual(r.status, "closed")

    def test_never_more_than_one_flush_per_interval(self):
        r, clock = self.run_obj()
        r.open()
        for i in range(1000):
            clock.t += 0.1                                  # 100 s of steps, 10 per second
            r.step(i, reward=0.1)
        self.assertLessEqual(len(self.calls("PATCH", "/v1/runs/")), 1 + 100 // 30)
        r.close()
        self.assertEqual(sum(len(c["body"]["steps"]) for c in self.calls("PATCH", "/v1/runs/")), 1000)

    def test_an_exception_closes_the_run_failed_and_propagates(self):
        r, _ = self.run_obj()
        with self.assertRaises(ValueError):
            with r:
                r.step(0, reward=0.0)
                raise ValueError("trainer blew up")
        body = self.calls("POST", "/close")[0]["body"]
        self.assertEqual(body["status"], "failed")
        self.assertIn("ValueError: trainer blew up", body["error"])

    def test_keyboard_interrupt_is_cancelled(self):
        r, _ = self.run_obj()
        with self.assertRaises(KeyboardInterrupt):
            with r:
                raise KeyboardInterrupt
        self.assertEqual(self.calls("POST", "/close")[0]["body"]["status"], "cancelled")

    def test_creates_inside_the_run_carry_its_label_and_id(self):
        self.c.create()
        with self.c.run("label-me", kind="training", config={}) as r:
            self.c.create()
            with self.c.run("inner", kind="evaluation"):
                self.c.create()
            self.c.create()
        self.c.create()
        creates = [c["headers"] for c in self.calls("POST", "/v1/slots")]
        self.assertEqual([h.get("X-Drums-Run-Label") for h in creates], [None, "label-me", "inner", "label-me", None])
        self.assertEqual(creates[1].get("X-Drums-Run-Id"), RUN_ID)
        self.assertIsInstance(r, drums.Run)

    def test_on_todays_gateway_it_is_a_noop_with_one_warning_and_labels_still_apply(self):
        Gw.mode = "today"
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with self.c.run("d2", kind="training", task_set="factory-v1") as r:
                for i in range(50):
                    r.step(i, reward=0.5)
                self.c.create()
        msgs = [str(x.message) for x in w if issubclass(x.category, RuntimeWarning)]
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("not recorded on the gateway", msgs[0])
        self.assertFalse(r.supported)
        self.assertIsNone(r.id)
        self.assertEqual(r.steps_total, 50)
        self.assertEqual(len(self.calls("POST", "/v1/runs")), 1)                     # tried once, never again
        self.assertEqual(self.calls("PATCH", "/v1/runs"), [])
        self.assertEqual(self.calls("POST", "/v1/runs")[0]["body"]["task_set"], "factory-v1")   # a name, not an id
        self.assertEqual(self.calls("POST", "/v1/slots")[0]["headers"].get("X-Drums-Run-Label"), "d2")

    def test_strict_raises_on_todays_gateway(self):
        Gw.mode = "today"
        with self.assertRaises(drums.RunNotSupported):
            with self.c.run("d2", kind="training", strict=True):
                pass

    def test_a_key_without_runs_write_is_a_warning(self):
        Gw.mode = "noscope"
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with self.c.run("d2", kind="training") as r:
                r.step(0)
        self.assertFalse(r.supported)
        self.assertIn("runs:write", str(w[0].message))

    def test_a_gateway_that_is_down_keeps_the_points_and_opens_later(self):
        Gw.mode = "down"
        r, clock = self.run_obj()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            r.open()
            r.step(0, reward=0.1)
            r.step(1, reward=0.2)
            self.assertIsNone(r.id)
            self.assertIsNone(r.supported)
            Gw.mode = "w3"
            clock.t += 31
            r.step(2, reward=0.3)
        self.assertEqual(r.id, RUN_ID)
        self.assertEqual([p["step"] for p in self.calls("PATCH", "/v1/runs/")[0]["body"]["steps"]], [0, 1, 2])
        r.close()

    def test_a_failed_flush_keeps_the_points(self):
        r, clock = self.run_obj()
        r.open()
        Gw.mode = "down"
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            r.step(0, reward=0.1)
        Gw.mode = "w3"
        clock.t += 31
        r.step(1, reward=0.2)
        ok = [c for c in self.calls("PATCH", "/v1/runs/")][-1]
        self.assertEqual([p["step"] for p in ok["body"]["steps"]], [0, 1])
        r.close()

    def test_the_buffer_is_bounded(self):
        Gw.mode = "down"
        r, _ = self.run_obj()
        old = runs.MAX_BUFFER
        runs.MAX_BUFFER = 5
        try:
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                for i in range(12):
                    r.step(i)
        finally:
            runs.MAX_BUFFER = old
        self.assertEqual(len(r._buf), 5)
        self.assertEqual(r.dropped, 7)
        self.assertEqual(r.last["step"], 11)

    def test_client_run_with_a_uuid_alone_is_the_03_read(self):
        self.assertEqual(self.c.run(RUN_ID), {"run": {"id": RUN_ID}})
        self.assertEqual(self.c.get_run(RUN_ID), {"run": {"id": RUN_ID}})
        self.assertIsInstance(self.c.run("my-run"), drums.Run)                       # a name: a Run (not opened yet)
        self.assertIsInstance(self.c.run(RUN_ID, kind="training"), drums.Run)
        self.assertEqual(Gw.calls[-1]["method"], "GET")                              # building a Run sends nothing

    def test_bad_arguments(self):
        with self.assertRaises(drums.BadRequest):
            self.c.open_run("", kind="training")
        with self.assertRaises(drums.BadRequest):
            self.c.open_run("x", kind="pretraining")
        r = self.c.open_run("x")
        with self.assertRaises(drums.BadRequest):
            r.close("exploded")

    def test_close_is_idempotent_and_step_after_close_is_ignored(self):
        r, _ = self.run_obj()
        with r:
            r.step(0)
        r.close()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            r.step(1)
        self.assertEqual(len(self.calls("POST", "/close")), 1)
        self.assertIn("after close", str(w[0].message))

    def test_process_exit_closes_an_open_run_failed_with_a_short_timeout(self):
        r, _ = self.run_obj()
        r.open()
        r.step(0)
        r._at_exit()
        self.assertEqual(r._timeout, 10.0)
        body = self.calls("POST", "/close")[0]["body"]
        self.assertEqual(body["status"], "failed")
        self.assertIn("without closing", body["error"])

    def test_a_wrong_key_raises(self):
        c = drums.Client(self.url, "wrong", retry_s=0.2)
        with self.assertRaises(drums.AuthError):
            with c.run("x", kind="training"):
                pass


if __name__ == "__main__":
    unittest.main()
