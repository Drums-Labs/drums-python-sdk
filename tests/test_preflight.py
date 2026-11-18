"""`drums preflight` / drums.preflight against a localhost fake gateway: today's gateway (no GET /v1/limits: those rows
say "not checked" and never fail), a gateway with the limits summary (every row decided), each failure with its fix
and link, the exit codes, --json, and no token in any output."""
import contextlib
import datetime as dt
import io
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import drums  # noqa: E402
from drums import _preflight as pf, cli  # noqa: E402

PREFIX = "abcdefghijkl"
TOKEN = f"drums_sk_{PREFIX}_" + "Q" * 43
TS = "0b5c2a52-77a0-4c0e-9a39-0a7a0f3c1d10"


def iso(hours_from_now):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours_from_now)).isoformat()


class Gw(BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        st = Gw.state
        if self.path == "/sign-in":
            return self._reply(st.get("console", 200), {})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._reply(401, {"error": "missing, invalid, revoked or expired API key", "code": "unauthenticated"})
        if st.get("binding"):
            return self._reply(403, {"error": "this endpoint serves another compute pool", "code": "forbidden"})
        if self.path == "/v1/health":
            time.sleep(st.get("health_delay", 0))
            return self._reply(200, {"ok": st.get("ok", True), "version": "0.4.3", "checkpoints": st.get("checkpoints", [{"id": "osworld-ubuntu", "stale_reason": None}]),
                                     "capacity": {"live": st.get("live", 10), "max": st.get("max", 200)}})
        if self.path == "/v1/api-keys":
            row = {"id": "k1", "name": "trainer", "prefix": PREFIX, "scopes": st.get("scopes", ["computers:read", "computers:write", "runs:write", "usage:read"]),
                   "expires_at": st.get("expires_at"), "revoked_at": None}
            return self._reply(200, {"items": [{"id": "k0", "prefix": "zzzzzzzzzzzz", "scopes": []}, row]})
        if self.path == "/v1/limits":
            if "limits" not in st:
                return self._reply(404, {"error": "no such route", "code": "not_found"})
            return self._reply(200, st["limits"])
        if self.path.startswith("/v1/task-sets/"):
            if self.path.endswith(TS) and "task_set" in st:
                return self._reply(200, st["task_set"])
            return self._reply(404, {"error": "no such task set", "code": "not_found"})
        if self.path.startswith("/v1/task-sets"):
            return self._reply(200, {"items": [st["task_set"]] if "task_set" in st else [], "next_cursor": None})
        return self._reply(404, {"error": "no such route", "code": "not_found"})


LIMITS = {"workspace": {"max_live_computers": 256, "live_computers": 10, "max_spend_usd": 500, "spent_usd": 40},
          "rate_limit": {"per_s": 600, "burst": 1800}, "price": {"desktop_hour_usd": 0.1}, "host": {"id": "drums-prod-2", "status": "active"}}
VALID_SET = {"id": TS, "name": "factory-v1", "task_count": 48, "validation": {"host_id": "drums-prod-2", "accepted": 48, "rejected": 0, "at": "2026-11-16T05:00:00Z"}}


class Preflight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Gw)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Gw.state = {}

    def pre(self, n=128, h=3.0, **kw):
        kw.setdefault("check_console", False)
        return drums.preflight(n, h, url=self.url, token=kw.pop("token", TOKEN), **kw)

    def row(self, rep, name):
        hits = [c for c in rep.checks if c.name == name]
        self.assertEqual(len(hits), 1, [c.name for c in rep.checks])
        return hits[0]

    def test_todays_gateway_passes_what_it_can_and_says_what_it_could_not_check(self):
        rep = self.pre(16, 2.0)
        self.assertTrue(rep.ok, rep.table())
        for name in ("gateway", "auth", "scopes", "host capacity", "admission"):
            self.assertEqual(self.row(rep, name).status, "pass", name)
        for name in ("rate limit", "quota", "spend cap"):
            c = self.row(rep, name)
            self.assertEqual(c.status, "not checked", name)
            self.assertIn("does not publish GET /v1/limits", c.detail)
        self.assertEqual(rep.exit_code(), 0)
        self.assertEqual(rep.exit_code(strict=True), 1)
        self.assertIn("3 could not be checked", rep.table())

    def test_the_rate_rule_is_explicit(self):
        self.assertEqual(pf.REQUESTS_PER_DESKTOP_S, 0.5)
        rep = self.pre(128, 1.0)
        self.assertEqual(rep.need["requests_per_s"], 64.0)
        c = self.row(rep, "rate limit")
        self.assertEqual(c.status, "warn")                      # 64 req/s > the 20/s default and nobody can tell us the real one
        self.assertIn("64 req/s", c.detail)
        self.assertIn("20 req/s", c.detail)

    def test_with_the_limits_summary_every_row_is_decided(self):
        Gw.state = {"limits": LIMITS, "task_set": VALID_SET}
        rep = self.pre(128, 3.0, task_set=TS)
        self.assertTrue(rep.ok, rep.table())
        self.assertEqual({c.status for c in rep.checks}, {"pass"}, rep.table())
        self.assertEqual(rep.need["projected_desktop_usd"], 38.4)
        self.assertEqual(rep.exit_code(strict=True), 0)

    def test_each_limit_fails_with_its_fix_and_link(self):
        Gw.state = {"limits": {**LIMITS, "rate_limit": {"per_s": 20, "burst": 60}, "workspace": {"max_live_computers": 10, "live_computers": 0,
                                                                                                 "max_spend_usd": 100, "spent_usd": 90}}}
        rep = self.pre(128, 3.0, workspace="acme", console_url="https://console.example")
        self.assertFalse(rep.ok)
        for name, words in (("rate limit", "64 req/s"), ("quota", "10 live desktops"), ("spend cap", "cap 100.00")):
            c = self.row(rep, name)
            self.assertEqual(c.status, "fail", name)
            self.assertIn(words, c.detail)
            self.assertTrue(c.fix)
            self.assertEqual(c.link, "https://console.example/w/acme/usage")
        self.assertEqual(rep.exit_code(), 1)
        self.assertIn("To do:", rep.table())

    def test_the_price_flag_projects_the_cost(self):
        rep = self.pre(128, 3.0, desktop_hour_usd=0.25)
        c = self.row(rep, "spend cap")
        self.assertEqual(c.status, "not checked")
        self.assertIn("96.00 USD", c.detail)
        self.assertEqual(rep.need["projected_desktop_usd"], 96.0)

    def test_host_capacity_and_admission(self):
        Gw.state = {"live": 150, "max": 200}
        self.assertEqual(self.row(self.pre(128), "host capacity").status, "fail")
        Gw.state = {"ok": False}
        self.assertEqual(self.row(self.pre(8), "admission").status, "fail")
        Gw.state = {"checkpoints": [{"id": "osworld-ubuntu", "stale_reason": "runtime changed"}]}
        self.assertEqual(self.row(self.pre(8), "admission").status, "warn")
        Gw.state = {"limits": {**LIMITS, "host": {"id": "drums-prod-2", "status": "draining"}}}
        self.assertEqual(self.row(self.pre(8), "admission").status, "fail")

    def test_key_expiring_during_the_run_or_missing_scopes(self):
        Gw.state = {"expires_at": iso(1)}
        c = self.row(self.pre(8, 3.0), "auth")
        self.assertEqual(c.status, "fail")
        self.assertIn("expires in", c.detail)
        Gw.state = {"expires_at": iso(100)}
        self.assertEqual(self.row(self.pre(8, 3.0), "auth").status, "pass")
        Gw.state = {"scopes": ["computers:read"]}
        self.assertEqual(self.row(self.pre(8), "scopes").status, "fail")
        Gw.state = {"scopes": ["computers:read", "computers:write"]}
        c = self.row(self.pre(8), "scopes")
        self.assertEqual(c.status, "warn")
        self.assertIn("runs:write", c.detail)

    def test_a_wrong_key_or_another_workspace(self):
        rep = self.pre(8, token=f"drums_sk_{PREFIX}_" + "X" * 43)
        self.assertEqual(self.row(rep, "auth").status, "fail")
        self.assertFalse(rep.ok)
        Gw.state = {"binding": True}
        c = self.row(self.pre(8), "auth")
        self.assertEqual(c.status, "fail")
        self.assertIn("another compute pool", c.detail)

    def test_unreachable_gateway(self):
        rep = drums.preflight(8, 1.0, url="http://127.0.0.1:9", token=TOKEN, check_console=False)
        self.assertEqual(self.row(rep, "gateway").status, "fail")
        self.assertEqual(self.row(rep, "auth").status, "not checked")

    def test_slow_control_plane_warns(self):
        Gw.state = {"health_delay": 0.3}
        old = pf.HEALTH_SLOW_S
        pf.HEALTH_SLOW_S = 0.1
        try:
            self.assertEqual(self.row(self.pre(8), "gateway").status, "warn")
        finally:
            pf.HEALTH_SLOW_S = old

    def test_task_set(self):
        self.assertEqual(self.row(self.pre(8, task_set=TS), "task set").status, "fail")             # not registered
        Gw.state = {"task_set": {**VALID_SET, "validation": None}}
        c = self.row(self.pre(8, task_set=TS), "task set")
        self.assertEqual(c.status, "fail")
        self.assertIn("never validated", c.detail)
        Gw.state = {"task_set": {**VALID_SET, "validation": None, "platform": True}}
        self.assertEqual(self.row(self.pre(8, task_set=TS), "task set").status, "warn")             # e.g. the OSWorld set
        Gw.state = {"task_set": {**VALID_SET, "validation": {**VALID_SET["validation"], "rejected": 2}}}
        self.assertEqual(self.row(self.pre(8, task_set=TS), "task set").status, "fail")
        Gw.state = {"task_set": VALID_SET}
        c = self.row(self.pre(8, task_set="factory-v1"), "task set")                               # by name
        self.assertEqual(c.status, "pass")
        self.assertIn("could not be compared", c.detail)
        Gw.state = {"task_set": VALID_SET, "limits": {**LIMITS, "host": {"id": "drums-rc1", "status": "active"}}}
        self.assertEqual(self.row(self.pre(8, task_set=TS), "task set").status, "warn")

    def test_console_row(self):
        rep = self.pre(8, check_console=True, console_url=self.url)
        self.assertEqual(self.row(rep, "console").status, "pass")
        Gw.state = {"console": 502}
        self.assertEqual(self.row(self.pre(8, check_console=True, console_url=self.url), "console").status, "fail")

    def test_model_gpus(self):
        self.assertEqual(pf.parse_model_gpus("2:6"), {"inference": 2, "trainer": 6, "total": 8})
        self.assertEqual(pf.parse_model_gpus("8")["total"], 8)
        with self.assertRaises(ValueError):
            pf.parse_model_gpus("lots")
        rep = self.pre(128, 2.0, model_gpus="2:6", gpu_hour_usd=4.0)
        c = self.row(rep, "model GPUs")
        self.assertEqual(c.status, "info")
        self.assertEqual(rep.need["projected_gpu_usd"], 64.0)

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            drums.preflight(0, 1.0, url=self.url, token=TOKEN)
        with self.assertRaises(ValueError):
            drums.preflight(8, 0, url=self.url, token=TOKEN)


def cli_run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    keys = ("DRUMS_TOKEN", "DRUMS_URL", "DRUMS_TOKEN_FILE", "DRUMS_CONSOLE_URL", "DRUMS_WORKSPACE")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc, out.getvalue(), err.getvalue()


class PreflightCli(Preflight.__base__):
    @classmethod
    def setUpClass(cls):
        Preflight.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Gw.state = {}

    def test_table_and_exit_codes(self):
        env = {"DRUMS_URL": self.url, "DRUMS_TOKEN": TOKEN}
        rc, out, err = cli_run(["preflight", "--desktops", "16", "--hours", "2", "--no-console"], env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("CHECK", out)
        self.assertIn("NOT CHECKED", out)
        rc, _, _ = cli_run(["preflight", "--desktops", "16", "--hours", "2", "--no-console", "--strict"], env)
        self.assertEqual(rc, 1)
        Gw.state = {"live": 199}
        rc, out, _ = cli_run(["preflight", "--desktops", "16", "--hours", "2", "--no-console"], env)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("Q" * 8, out)

    def test_json(self):
        Gw.state = {"limits": LIMITS}
        rc, out, _ = cli_run(["preflight", "--desktops", "128", "--hours", "3", "--no-console", "--json", "--model-gpus", "2:6"],
                             {"DRUMS_URL": self.url, "DRUMS_TOKEN": TOKEN})
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertTrue(d["ok"])
        self.assertEqual(d["need"]["requests_per_s"], 64.0)
        self.assertEqual([c["name"] for c in d["checks"]][:2], ["gateway", "auth"])
        self.assertNotIn(TOKEN, out)

    def test_usage_errors_exit_2(self):
        rc, _, err = cli_run(["preflight", "--desktops", "8", "--hours", "1"], {"DRUMS_TOKEN": TOKEN})
        self.assertEqual(rc, 2)
        self.assertIn("DRUMS_URL", err)
        rc, _, err = cli_run(["preflight", "--desktops", "8", "--hours", "1"], {"DRUMS_URL": self.url})
        self.assertEqual(rc, 2)
        self.assertIn("DRUMS_TOKEN", err)
        rc, _, err = cli_run(["preflight", "--desktops", "8", "--hours", "1", "--model-gpus", "x"], {"DRUMS_URL": self.url, "DRUMS_TOKEN": TOKEN})
        self.assertEqual(rc, 2)
        rc, _, err = cli_run(["preflight", "--desktops", "8", "--hours", "1"], {"DRUMS_URL": "http://gw.example/gw", "DRUMS_TOKEN": TOKEN})
        self.assertEqual(rc, 2)
        self.assertNotIn(TOKEN, err)


if __name__ == "__main__":
    unittest.main()
