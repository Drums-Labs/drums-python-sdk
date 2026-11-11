"""drums SDK against a fake drumsd (localhost only, no root)."""
import asyncio
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drums  # noqa: E402
import drums.aio  # noqa: E402

TOKEN = "t" * 43
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"


def slot(i, state="ready", episode=1, verdict="pending", **kw):
    return {"id": f"s{i}", "state": state, "episode": episode, "validity": {"verdict": verdict}, "port": 25000 + i, **kw}


class Fake(BaseHTTPRequestHandler):
    calls = []
    gates_seq = []
    verify_reply = {}
    down = False
    step_delay = 0.0

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

    def _authed(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"error": "missing or wrong bearer token"})
            return False
        return True

    def _gone(self):
        """/v1/slots/gone/...: drumsd >= 0.4.2's 410; /v1/slots/old/...: the 409 of older ones."""
        if self.path.startswith("/v1/slots/gone"):
            self._send(410, {"error": "env gone is destroyed (lease: idle 1800 s): step is not possible; create a new env",
                             "code": "env_gone", "env": "gone", "closed_reason": "lease: idle 1800 s"})
            return True
        if self.path.startswith("/v1/slots/old"):
            self._send(409, {"error": "slot is destroyed: step is not allowed (allowed from: ready)"})
            return True
        return False

    def do_POST(self):
        b = self._body()
        Fake.calls.append(("POST", self.path, b))
        if not self._authed():
            return
        if self._gone():
            return
        if self.path == "/v1/slots":
            ck = b["checkpoint"]
            if ck == "full":
                return self._send(429, {"error": "overloaded: create refused: 41000 MiB available ...", "code": "overloaded",
                                        "reason": "memory", "admission": {"need_mib": 1024, "floor_mib": 40000}})
            if ck == "broken":
                return self._send(201, slot(9, state="quarantined", reason="gate state_hash failed: {...}"))
            if ck == "crash":
                return self._send(201, slot(9, state="down", reason="criu restore failed", last_error="criu restore failed (exit 1)"))
            if ck == "stale":
                return self._send(409, {"error": "checkpoint stale is stale: made before provenance was recorded", "code": "checkpoint_stale",
                                        "provenance": None, "current": {"source_sha256": "a", "inspection_sha256": "i"}})
            if ck == "agentroot":
                return self._send(422, {"error": "task 5812b315 is unsupported in this execution profile: agent root", "code": "task_unsupported",
                                        "task_id": "5812b315-e7bd-4265-b51f-863c02174c28", "reason": "agent root is not available"})
            if ck == "setupfail":
                return self._send(201, slot(9, state="down", last_error="task setup failed: download"))
            if b.get("task") and not isinstance(b["task"], dict):
                return self._send(400, {"error": "task must be an object"})
            return self._send(201, slot(100, task=b.get("task")))
        if self.path.endswith("/reset"):
            return self._send(200, slot(100, episode=2))
        if self.path.endswith("/step"):
            time.sleep(Fake.step_delay)
            if "raise" in json.dumps(b.get("action")):
                return self._send(200, {"result": {"status": "success", "returncode": 1, "output": "", "error": "Traceback ... ValueError"},
                                        "episode": 2, "validity": {"verdict": "pending"}, "seconds": 0.1})
            return self._send(200, {"result": {"status": "success", "returncode": 0}, "screenshot_png_b64": __import__("base64").b64encode(PNG).decode(),
                                    "episode": 2, "validity": {"verdict": "pending"}, "seconds": 0.1})
        if self.path.endswith("/prefork"):
            return self._send(202, {"state": "preparing", "n": b.get("n")})
        if self.path.endswith("/capture"):
            return self._send(200, {"id": "s100-c101"})
        if self.path.endswith("/quiescent") or (self.path.endswith("/branch") and b.get("quiesce_timeout_s") == 0.1):
            if (b or {}).get("timeout_s") == 0.1 or (b or {}).get("quiesce_timeout_s") == 0.1:
                return self._send(409, {"error": "env s100 is not quiescent, nothing was captured", "code": "not_quiescent",
                                        "unstable": [{"kind": "thread_exited", "pid": 552, "tid": 553, "comm": "chrome"}],
                                        "quiescence": {"attempts": [{"n": 1}]}})
            return self._send(200, {"quiescent": True, "waited_s": 1.05, "window_s": 1.0})
        if self.path.endswith("/branch"):
            kids = [slot(103), slot(104)] if b["n"] == 2 else [slot(103), slot(105, state="down", last_error="criu restore failed")]
            return self._send(200, {"checkpoint": {"id": "s100-c102"}, "state": "s100-c102", "children": kids, "seconds": 2.9})
        if self.path.endswith("/verify"):
            return self._send(200, Fake.verify_reply)
        if self.path == "/v1/metrics/reset":
            return self._send(200, {"closed": {}})
        self._send(404, {"error": "no"})

    def do_GET(self):
        Fake.calls.append(("GET", self.path, None))
        if Fake.down:
            return self._send(503, {"error": "starting"})
        if not self._authed():
            return
        if self.path == "/v1/health":
            return self._send(200, {"ok": True, "version": "0.2.0"})
        if self.path == "/v1/metrics":
            return self._send(200, {"cost": {"usd_per_valid_episode": 0.01}})
        if self.path.startswith("/v1/tasks/"):
            tid = self.path.rsplit("/", 1)[1]
            return self._send(200, {"id": tid + "-full", "config": [], "evaluator": {"func": "x"}})
        if self.path.endswith("/gates"):
            v = Fake.gates_seq.pop(0) if Fake.gates_seq else "valid"
            return self._send(200, {"validity": {"verdict": v}})
        if self.path.endswith("/screenshot"):
            return self._send(200, None, raw=PNG)
        if self.path.endswith("/metrics"):
            return self._send(200, {"slot": "s100", "usage": {"ops": {}}})
        if self.path.startswith("/v1/slots/"):
            return self._send(200, slot(100, task={"id": "abc", "instruction": "do it"}))
        if self.path.startswith("/v1/slots"):
            # drumsd 0.4: 7 envs s100..s106, s100/s103/s106 destroyed; the plain call lists live ones, any query the paged form
            import urllib.parse as up
            allr = [slot(100 + i, state="destroyed" if i % 3 == 0 else "ready") for i in range(7)]
            if "?" not in self.path:
                return self._send(200, [r for r in allr if r["state"] != "destroyed"])
            q = dict(up.parse_qsl(self.path.split("?", 1)[1]))
            rows = [r for r in allr if q.get("include_destroyed") == "true" or r["state"] != "destroyed"]
            rows = [r for r in rows if not q.get("after") or r["id"] > q["after"]]
            n = int(q.get("limit", 500))
            return self._send(200, {"items": rows[:n], "next_after": rows[n - 1]["id"] if len(rows) > n else None})
        self._send(404, {"error": "no slot"})

    def do_DELETE(self):
        Fake.calls.append(("DELETE", self.path, None))
        if not self._authed():
            return
        if self._gone():
            return
        if self.path.startswith("/v1/checkpoints/"):
            if self.path.endswith("busy"):
                return self._send(409, {"error": "checkpoint busy is used by envs [\"s1\"]"})
            return self._send(200, {"id": self.path.rsplit("/", 1)[1], "deleted": True})
        self._send(200, slot(100, state="destroyed"))


def reward(valid=True, score=1.0, err=None):
    return {"slot": "s100", "episode": 1, "task_id": "abc", "score": score, "valid": valid, "checker_error": err,
            "validity": {"verdict": "valid"} if valid else {"verdict": "invalid", "gates": ["alive_after_resume"]}, "gates": {"gates": []}}


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ThreadingHTTPServer.request_queue_size = 512     # many concurrent clients in test_async_concurrency
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.calls.clear()
        Fake.down = False
        self.c = drums.connect(self.url, TOKEN, retry_s=1, close_on_exit=False)

    def test_wrong_token_is_an_auth_error_at_connect(self):
        with self.assertRaises(drums.AuthError):
            drums.connect(self.url, "nope")

    def test_create_step_observe_save_close(self):
        e = self.c.create(image="osworld-ubuntu", task={"id": "abc", "config": [], "evaluator": {}})
        self.assertEqual(e.id, "s100")
        self.assertEqual(Fake.calls[-1][2]["checkpoint"], "osworld-ubuntu")
        r = e.step("pyautogui.click(1, 2)", screenshot=True)
        self.assertEqual(Fake.calls[-1][2]["action"], {"type": "pyautogui", "code": "pyautogui.click(1, 2)"})
        self.assertEqual((r.observation.width, r.observation.height), (1920, 1080))
        o = e.observe()
        self.assertEqual(o.meta["instruction"], "do it")
        self.assertEqual(o.width, 1920)
        st = e.save()
        self.assertIsInstance(st, drums.State)
        e2 = self.c.create(from_state=st)
        self.assertEqual(Fake.calls[-1][2]["checkpoint"], "s100-c101")
        with e2:
            pass
        self.assertEqual(Fake.calls[-1][0], "DELETE")

    def test_task_by_id_is_fetched_from_the_host(self):
        self.c.create(task="035f41ba")
        self.assertEqual(Fake.calls[-2][1], "/v1/tasks/035f41ba")
        self.assertEqual(Fake.calls[-1][2]["task"]["id"], "035f41ba-full")

    def test_failures_are_typed_with_the_record(self):
        with self.assertRaises(drums.GateFailed) as g:
            self.c.create(image="broken")
        self.assertEqual(g.exception.state, "quarantined")
        self.assertIn("state_hash", g.exception.cause)
        with self.assertRaises(drums.RestoreFailed):
            self.c.create(image="crash")
        with self.assertRaises(drums.TaskSetupFailed):
            self.c.create(image="setupfail")

    def test_verify_never_hands_out_an_invalid_reward_silently(self):
        e = self.c.create()
        Fake.verify_reply = reward(valid=True, score=1.0)
        r = e.verify()
        self.assertTrue(r.valid)
        self.assertEqual(r.score, 1.0)
        Fake.verify_reply = reward(valid=False, score=1.0)
        with self.assertRaises(drums.InvalidEpisode) as x:
            e.verify()
        self.assertFalse(x.exception.reward.valid)
        self.assertEqual(x.exception.reward.bad_gates, ["alive_after_resume"])
        r = e.verify(strict=False)
        self.assertFalse(r.valid)
        Fake.verify_reply = reward(valid=True, score=None, err="KeyError: x")
        with self.assertRaises(drums.CheckerError):
            e.verify()

    def test_the_reward_audit_reaches_the_caller(self):
        e = self.c.create()
        Fake.verify_reply = reward(valid=True, score=1.0)
        r = e.verify()
        self.assertEqual((r.audit, r.audit_flags, r.trusted), ({}, [], None))      # an older drumsd: unknown, not trusted
        Fake.verify_reply = dict(reward(valid=True, score=1.0), audit={"version": 1, "flags": ["scope_violation"], "trusted": False,
                                                                       "scope": {"ran": True, "violations": 1}})
        r = e.verify()
        self.assertEqual((r.score, r.trusted, r.audit_flags), (1.0, False, ["scope_violation"]))
        self.assertEqual(r.audit["scope"]["violations"], 1)

    def test_fork_reports_failed_children(self):
        e = self.c.create()
        kids = e.fork(2)
        self.assertEqual([k.id for k in kids], ["s103", "s104"])
        with self.assertRaises(drums.ForkError) as f:
            e.fork(3)
        self.assertEqual([k.id for k in f.exception.ok], ["s103"])
        self.assertIsInstance(f.exception.failed[0], drums.RestoreFailed)
        self.assertEqual(len(e.fork(3, strict=False)), 2)

    def test_quiescence_is_typed_and_optional(self):
        e = self.c.create(image="img")
        self.assertTrue(e.wait_quiescent()["quiescent"])
        with self.assertRaises(drums.NotQuiescent) as cm:
            e.wait_quiescent(timeout=0.1)
        self.assertIsInstance(cm.exception, drums.Conflict, "NotQuiescent is a Conflict (old except clauses still catch it)")
        self.assertEqual(cm.exception.unstable[0]["comm"], "chrome")
        self.assertEqual(cm.exception.quiescence["attempts"][0]["n"], 1)
        with self.assertRaises(drums.NotQuiescent):
            e.fork(2, quiesce_timeout=0.1)
        # fork/save without options send exactly the 0.2 bodies (backward compatible)
        Fake.calls.clear()
        e.fork(2)
        e.save()
        bodies = [c[2] for c in Fake.calls if c[0] == "POST"]
        self.assertEqual(bodies, [{"n": 2}, {}])
        e.save("x", quiesce_timeout=5)
        self.assertEqual(Fake.calls[-1][2], {"id": "x", "quiesce_timeout_s": 5})

    def test_valid_waits_for_pending(self):
        e = self.c.create()
        Fake.gates_seq = ["pending", "pending", "valid"]
        self.assertTrue(e.valid(timeout=5))

    def test_get_retries_while_down_post_does_not(self):
        Fake.down = True
        t0 = time.monotonic()
        with self.assertRaises(drums.DaemonUnavailable):
            self.c.health()
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)      # retried for retry_s
        n = len([c for c in Fake.calls if c[1] == "/v1/health"])
        self.assertGreater(n, 1)
        Fake.down = False
        dead = drums.Client("http://127.0.0.1:9", TOKEN, retry_s=5)
        t0 = time.monotonic()
        with self.assertRaises(drums.DaemonUnavailable) as x:
            dead.call("POST", "/v1/slots", {"checkpoint": "x"})
        self.assertLess(time.monotonic() - t0, 2.0)               # never retried
        self.assertIn("outcome is unknown", str(x.exception))

    def test_batch_and_map(self):
        envs = self.c.create_many(3, workers=3)
        self.assertEqual(len(envs), 3)
        with self.assertRaises(drums.BatchError) as b:
            self.c.create_many(2, image="crash")
        self.assertEqual(len(b.exception.failed), 2)
        res = drums.Client.map(lambda x: 1 / x, [1, 0])
        self.assertEqual(res[0], 1.0)
        self.assertIsInstance(res[1], ZeroDivisionError)

    def test_delete_state(self):
        self.assertTrue(self.c.delete_state(drums.State("s100-c101"))["deleted"])
        with self.assertRaises(drums.Conflict):
            self.c.delete_state("busy")

    def test_metrics(self):
        self.assertIn("cost", self.c.metrics())
        self.assertIn("usage", self.c.create().metrics())

    def test_async(self):
        async def main():
            c = await drums.aio.connect(self.url, TOKEN)
            envs = await asyncio.gather(*[c.create() for _ in range(4)])
            Fake.verify_reply = reward()
            rs = await asyncio.gather(*[e.verify() for e in envs])
            return rs
        rs = asyncio.run(main())
        self.assertEqual([r.score for r in rs], [1.0] * 4)

    def test_async_concurrency_is_not_capped_by_the_default_executor(self):
        # asyncio.to_thread runs on the loop's default executor: min(32, cpu_count + 4) threads. 128 envs stepping at
        # once then ran in waves of <= 32, so a 128-env asyncio driver was silently a 32-env one.
        n, delay = 128, 0.5

        async def main():
            c = await drums.aio.connect(self.url, TOKEN)
            envs = await asyncio.gather(*[c.create() for _ in range(n)])
            Fake.step_delay = delay
            try:
                t0 = time.monotonic()
                await asyncio.gather(*[e.step("pyautogui.moveTo(1, 1)") for e in envs])
                return time.monotonic() - t0
            finally:
                Fake.step_delay = 0.0
        took = asyncio.run(main())
        self.assertLess(took, 3 * delay, f"{n} concurrent steps of {delay} s took {took:.2f} s")

    def test_overloaded_is_typed_and_not_retried(self):
        n = len([c for c in Fake.calls if c[1] == "/v1/slots"])
        with self.assertRaises(drums.Overloaded) as cm:
            self.c.create(image="full")
        self.assertEqual(cm.exception.reason, "memory")
        self.assertEqual(cm.exception.admission["floor_mib"], 40000)
        self.assertEqual(len([c for c in Fake.calls if c[1] == "/v1/slots"]), n + 1)     # a POST is never retried

    def test_a_failed_action_raises_with_its_exit_status(self):
        e = self.c.create()
        with self.assertRaises(drums.ActionFailed) as cm:
            e.step("raise ValueError()")
        self.assertEqual(cm.exception.returncode, 1)
        self.assertIn("ValueError", cm.exception.error)
        r = e.step("raise ValueError()", check=False)              # opt out: the exit status is the answer
        self.assertEqual(r.result["returncode"], 1)
        self.assertEqual(e.step("pyautogui.moveTo(1, 1)").result["returncode"], 0)
        with self.assertRaises(drums.GuestError):                   # GuestClient keeps its allow_rc contract
            e.guest().execute("raise", allow_rc=(0,))

    def test_env_listing_live_by_default_and_paged_history(self):
        self.assertEqual([e.id for e in self.c.envs()], ["s101", "s102", "s104", "s105"])
        self.assertEqual([r["id"] for r in self.c.env_records(page_size=3)], ["s101", "s102", "s104", "s105"])
        n = len([c for c in Fake.calls if c[1].startswith("/v1/slots?")])
        allr = [r["id"] for r in self.c.env_records(include_destroyed=True, page_size=2)]
        self.assertEqual(allr, [f"s{100 + i}" for i in range(7)])
        self.assertEqual(len([c for c in Fake.calls if c[1].startswith("/v1/slots?")]) - n, 4)      # 2+2+2+1

    def test_stale_checkpoint_is_typed(self):
        with self.assertRaises(drums.CheckpointStale) as cm:
            self.c.create(image="stale")
        self.assertIsInstance(cm.exception, drums.Conflict)
        self.assertEqual(cm.exception.current["inspection_sha256"], "i")

    def test_a_task_that_needs_agent_root_is_typed(self):
        with self.assertRaises(drums.TaskUnsupported) as cm:
            self.c.create(image="agentroot")
        self.assertIsInstance(cm.exception, drums.BadRequest)
        self.assertEqual(cm.exception.task_id, "5812b315-e7bd-4265-b51f-863c02174c28")
        self.assertIn("agent root", cm.exception.reason)

    def test_guest_adapter(self):
        g = self.c.create().guest()
        self.assertEqual(g.execute("ls", shell=True)["returncode"], 0)
        self.assertEqual(g.screen_size(), (1920, 1080))
        self.assertEqual(g.platform(), "Linux")

    def test_legacy_entry_point(self):
        e = drums.env(checkpoint="clean", url=self.url, token=TOKEN)
        self.assertEqual(e.id, "s100")
        self.assertIs(drums.Env.branch, drums.Env.fork)
        # 0.2.4: the default is the installed image (it was "clean", which no install has: NotFound on a default install)
        drums.env(url=self.url, token=TOKEN)
        self.assertEqual(Fake.calls[-1][2]["checkpoint"], "osworld-ubuntu")

    def test_a_destroyed_env_raises_env_gone_and_close_is_a_no_op(self):
        for eid in ("gone", "old"):                   # 0.4.2's 410, and the 409 of older drumsd
            e = drums.Env(self.c, {"id": eid, "state": "ready"})
            with self.assertRaises(drums.EnvGone) as cm:
                e.step("pyautogui.press('shift')")
            self.assertIsInstance(cm.exception, drums.NotFound, "EnvGone is a NotFound")
            with self.assertRaises(drums.EnvGone):
                e.reset()
            r = e.close()                                # no-op, no raise
            self.assertEqual(r["state"], "destroyed")
            self.assertEqual(e.close()["state"], "destroyed")
        self.assertEqual(cm.exception.status, 409)
        e = drums.Env(self.c, {"id": "gone", "state": "ready"})
        with self.assertRaises(drums.EnvGone) as cm:
            e.step("x")
        self.assertEqual((cm.exception.status, cm.exception.closed_reason), (410, "lease: idle 1800 s"))

    def test_fork_returns_its_saved_state_and_the_old_shape(self):
        e = self.c.create()
        Fake.calls.clear()
        kids = e.fork(2)
        self.assertIsInstance(kids, list)                # the 0.2 shape: a list of the ready children
        self.assertIsInstance(kids, drums.ForkResult)
        self.assertEqual([k.id for k in kids], ["s103", "s104"])
        self.assertEqual(kids.state, "s100-c102")
        self.assertIsInstance(kids.state, drums.State)
        self.assertEqual(e.last_state, "s100-c102")
        self.assertEqual(kids.seconds, 2.9)
        self.assertEqual(Fake.calls[-1][2], {"n": 2}, "no option: the 0.2 body")
        with self.assertWarns(DeprecationWarning):
            self.assertEqual(e.last_fork["checkpoint"]["id"], "s100-c102")
        e.fork(2, keep_state=False, idle_timeout=120, state_idle_timeout=0)
        self.assertEqual(Fake.calls[-1][2], {"n": 2, "delete_state_with_children": True, "idle_timeout_s": 120, "state_idle_timeout_s": 0})
        with self.assertRaises(drums.ForkError) as f:
            e.fork(3)
        self.assertEqual(f.exception.state, "s100-c102")
        with e.fork(2) as k2:
            pass
        self.assertEqual([c[1] for c in Fake.calls[-2:]], ["/v1/slots/s103", "/v1/slots/s104"])
        self.assertTrue(all(c[0] == "DELETE" for c in Fake.calls[-2:]), "the with-block closes the children")

    def test_prefork_sends_the_fork_body_and_returns_the_reply(self):
        e = self.c.create()
        Fake.calls.clear()
        r = e.prefork(4)
        self.assertEqual(Fake.calls[-1][:2], ("POST", "/v1/slots/s100/prefork"))
        self.assertEqual(Fake.calls[-1][2], {"n": 4}, "no option: just n")
        self.assertEqual(r["n"], 4)
        e.prefork(2, quiesce_timeout=0.5, keep_state=False, idle_timeout=120, state_ttl=60)
        self.assertEqual(Fake.calls[-1][2], {"n": 2, "quiesce_timeout_s": 0.5, "delete_state_with_children": True, "idle_timeout_s": 120, "state_ttl_s": 60})

    def test_leases_are_sent_only_when_set(self):
        Fake.calls.clear()
        self.c.create()
        self.assertNotIn("idle_timeout_s", Fake.calls[-1][2])
        self.c.create(idle_timeout=60, ttl=600)
        self.assertEqual((Fake.calls[-1][2]["idle_timeout_s"], Fake.calls[-1][2]["ttl_s"]), (60, 600))
        e = self.c.create()
        e.save(idle_timeout=0, ttl=3600)
        self.assertEqual(Fake.calls[-1][2], {"idle_timeout_s": 0, "ttl_s": 3600})

    def test_a_quarantined_reward_is_invalid_with_a_reason_that_agrees(self):
        e = self.c.create()
        # drumsd 0.4.2: the checker is skipped, the verdict and gates name the quarantine
        Fake.verify_reply = {"slot": "s147", "episode": 1, "score": None, "valid": False, "checker_error": None, "state": "quarantined",
                             "checker_skipped": "quarantined: client", "invalid_reasons": ["quarantined: client"],
                             "validity": {"verdict": "invalid", "gates": ["not_quarantined"]}}
        r = e.verify(strict=False)
        self.assertEqual((r.valid, r.verdict, r.bad_gates, r.invalid_reasons), (False, "invalid", ["not_quarantined"], ["quarantined: client"]))
        self.assertEqual(r.checker_skipped, "quarantined: client")
        with self.assertRaises(drums.InvalidEpisode) as cm:
            e.verify()
        self.assertIn("is invalid (quarantined: client)", str(cm.exception))
        # drumsd < 0.4.2 said verdict "valid" with valid False on a quarantined env: the SDK no longer echoes that
        Fake.verify_reply = {"slot": "s147", "episode": 1, "score": 0.0, "valid": False, "checker_error": None, "state": "quarantined",
                             "validity": {"verdict": "valid"}}
        r = e.verify(strict=False)
        self.assertEqual((r.verdict, r.invalid_reasons), ("invalid", ["env is quarantined"]))
        with self.assertRaises(drums.InvalidEpisode) as cm:
            e.verify()
        self.assertNotIn("is valid", str(cm.exception))
        self.assertIn("env is quarantined", str(cm.exception))

    def test_the_client_closes_the_envs_it_created(self):
        c = drums.connect(self.url, TOKEN, retry_s=1, close_on_exit=False)
        a, b = c.create(), c.create()
        self.assertEqual(c.owned_envs(), ["s100"], "the fake gives every env the id s100")
        a.close()
        self.assertEqual(c.owned_envs(), [])
        b.close()
        Fake.calls.clear()
        with drums.connect(self.url, TOKEN, retry_s=1, close_on_exit=False) as c2:
            c2.create()
            c2.envs()                                   # listed envs are not the client's: never closed by it
        self.assertEqual([x[:2] for x in Fake.calls if x[0] == "DELETE"], [("DELETE", "/v1/slots/s100")])
        self.assertEqual(c2.owned_envs(), [])

    def test_a_killed_trainer_closes_its_envs_on_sigterm_and_exit(self):
        import subprocess
        import textwrap
        code = textwrap.dedent(f"""
            import os, signal, sys, time
            sys.path.insert(0, {os.path.join(os.path.dirname(__file__), "..")!r})
            import drums
            c = drums.connect({self.url!r}, {TOKEN!r}, retry_s=1)
            c.create()
            print("created", flush=True)
            if sys.argv[1] == "term":
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(30)
            """)
        for how, rc in (("term", 128 + 15), ("exit", 0)):
            Fake.calls.clear()
            p = subprocess.run([sys.executable, "-c", code, how], capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, rc, p.stderr)
            self.assertIn(("DELETE", "/v1/slots/s100"), [x[:2] for x in Fake.calls], f"{how}: the env was not closed")


if __name__ == "__main__":
    unittest.main()
