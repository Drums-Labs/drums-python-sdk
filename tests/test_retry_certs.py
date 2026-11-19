"""Retry-After on every call (429 from the gateway's rate limit or no capacity, for GET and POST alike), the retry
budget and its off switch, and the TLS fallback to certifi's CA bundle. Localhost fakes only."""
import email.utils
import io
import json
import os
import ssl
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import drums  # noqa: E402

TOKEN = "t" * 43
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (8).to_bytes(4, "big") + (8).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"


class Limited(BaseHTTPRequestHandler):
    """Every path answers 429 `rate_limited` (with `retry_after`, when set) for its first `refuse` requests, then 200."""
    refuse = 0
    retry_after = "0"
    seen = []
    lock = threading.Lock()
    down_503 = 0

    def log_message(self, *a):
        pass

    def _reply(self, code, obj=None, raw=None, headers=None):
        b = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        with Limited.lock:
            Limited.seen.append((self.command, self.path, time.monotonic()))
            hits = sum(1 for m, p, _ in Limited.seen if p == self.path and m == self.command)
            down = Limited.down_503
            if down:
                Limited.down_503 -= 1
        if down:
            return self._reply(503, {"error": "starting", "code": "retryable_infrastructure_failure"}, headers={"Retry-After": "0.2"})
        if hits <= Limited.refuse:
            h = {"Retry-After": Limited.retry_after} if Limited.retry_after is not None else {}
            return self._reply(429, {"error": "too many requests; slow down", "code": "rate_limited"}, headers=h)
        if self.path.endswith("/screenshot"):
            return self._reply(200, raw=PNG)
        if self.path.endswith("/step"):
            return self._reply(200, {"result": {"returncode": 0}, "episode": 1, "validity": {"verdict": "valid"}})
        if self.path.endswith("/verify"):
            return self._reply(200, {"slot": "s1", "score": 1.0, "valid": True, "validity": {"verdict": "valid"}})
        return self._reply(200, {"id": "s1", "state": "ready", "episode": 1, "validity": {"verdict": "valid"}})

    do_GET = do_POST = do_DELETE = _handle


class RetryAfter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Limited)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Limited.seen.clear()
        Limited.refuse, Limited.retry_after, Limited.down_503 = 0, "0", 0

    def client(self, **kw):
        return drums.Client(self.url, TOKEN, retry_s=1, retry=drums.RetryPolicy(**{"base_s": 0.02, "jitter": 0, **kw}))

    def env(self, c):
        return drums.Env(c, {"id": "s1", "state": "ready"})

    def test_step_screenshot_verify_create_are_all_retried_after_a_429(self):
        Limited.refuse = 2
        c = self.client()
        e = self.env(c)
        self.assertEqual(e.step("pyautogui.click(1, 1)").result["returncode"], 0)
        self.assertEqual(e.screenshot(), PNG)
        self.assertEqual(e.verify().score, 1.0)
        self.assertEqual(c.create().id, "s1")
        for method, path in (("POST", "/v1/slots/s1/step"), ("GET", "/v1/slots/s1/screenshot"), ("POST", "/v1/slots/s1/verify"), ("POST", "/v1/slots")):
            self.assertEqual(sum(1 for m, p, _ in Limited.seen if (m, p) == (method, path)), 3, path)

    def test_the_wait_honours_retry_after(self):
        Limited.refuse, Limited.retry_after = 1, "0.4"
        c = self.client()
        t0 = time.monotonic()
        self.env(c).step("pyautogui.click(1, 1)")
        self.assertGreaterEqual(time.monotonic() - t0, 0.4)
        t = [ts for m, p, ts in Limited.seen if p.endswith("/step")]
        self.assertGreaterEqual(t[1] - t[0], 0.38)

    def test_retry_after_as_an_http_date(self):
        when = email.utils.formatdate(time.time() + 1.2, usegmt=True)
        self.assertGreater(drums._retry_after({"Retry-After": when}), 0.0)
        self.assertLessEqual(drums._retry_after({"Retry-After": when}), 1.2)
        self.assertEqual(drums._retry_after({"Retry-After": "3"}), 3.0)
        self.assertIsNone(drums._retry_after({"Retry-After": "soon"}))
        self.assertIsNone(drums._retry_after({}))

    def test_the_budget_runs_out_and_the_error_says_how_long_to_wait(self):
        Limited.refuse, Limited.retry_after = 100, "0.1"
        c = self.client(max_wait_s=0.35)
        t0 = time.monotonic()
        with self.assertRaises(drums.Overloaded) as cm:
            self.env(c).step("pyautogui.click(1, 1)")
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(cm.exception.retry_after, 0.1)
        self.assertGreaterEqual(cm.exception.retries, 2)
        self.assertEqual(sum(1 for _, p, _ in Limited.seen if p.endswith("/step")), cm.exception.retries + 1)

    def test_max_attempts_bounds_the_retries(self):
        Limited.refuse = 100
        with self.assertRaises(drums.Overloaded) as cm:
            self.env(self.client(max_attempts=3)).screenshot()
        self.assertEqual(cm.exception.retries, 3)

    def test_off_switch_kwarg_and_environment(self):
        Limited.refuse = 1
        with self.assertRaises(drums.Overloaded):
            self.env(self.client(max_wait_s=0)).step("x")
        Limited.seen.clear()
        with mock.patch.dict(os.environ, {"DRUMS_RETRY_MAX_WAIT_S": "0"}):
            c = drums.Client(self.url, TOKEN)
            self.assertEqual(c.retry.max_wait_s, 0.0)
            with self.assertRaises(drums.Overloaded):
                self.env(c).step("x")
        with mock.patch.dict(os.environ, {"DRUMS_RETRY_MAX_WAIT_S": "12.5"}):
            self.assertEqual(drums.Client(self.url, TOKEN).retry.max_wait_s, 12.5)
        with mock.patch.dict(os.environ, {"DRUMS_RETRY_MAX_WAIT_S": "nonsense"}):
            self.assertEqual(drums.Client(self.url, TOKEN).retry.max_wait_s, 60.0)
        self.assertEqual(drums.connect(self.url, TOKEN, check=False, close_on_exit=False, handle_signals=False,
                                       retry=drums.RetryPolicy(max_wait_s=5)).retry.max_wait_s, 5)

    def test_jitter_spreads_the_waits(self):
        p = drums.RetryPolicy(base_s=0.5, jitter=0.25)
        waits = {round(p.wait(0, 1.0), 6) for _ in range(50)}
        self.assertGreater(len(waits), 10)
        self.assertTrue(all(1.0 <= w <= 1.0 + 0.25 * 1.5 + 1e-9 for w in waits), waits)
        q = drums.RetryPolicy(base_s=0.5, cap_s=4, jitter=0)
        self.assertEqual([q.wait(i, None) for i in range(5)], [0.5, 1.0, 2.0, 4.0, 4.0])

    def test_a_get_503_waits_for_its_retry_after(self):
        Limited.down_503 = 1
        c = self.client()
        t0 = time.monotonic()
        self.assertEqual(c.call("GET", "/v1/health")["id"], "s1")
        self.assertGreaterEqual(time.monotonic() - t0, 0.2)

    def test_a_post_503_is_still_not_retried(self):
        Limited.down_503 = 1
        with self.assertRaises(drums.DaemonUnavailable):
            self.client().call("POST", "/v1/slots", {"checkpoint": "x"})
        self.assertEqual(len(Limited.seen), 1)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _Opener:
    def __init__(self, outcomes):
        self.outcomes, self.urls = list(outcomes), []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return _Resp(o)


def cert_failure():
    return urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"))


class CertifiFallback(unittest.TestCase):
    def test_a_failed_system_store_switches_to_certifi_once_with_a_hint(self):
        c = drums.Client("https://gw.example/gw", TOKEN)
        c._opener = _Opener([cert_failure()])
        good = _Opener([b'{"ok": true}', b'{"ok": true}'])
        err = io.StringIO()
        with mock.patch.object(drums, "_certifi_context", return_value=ssl.create_default_context()), \
                mock.patch.object(drums.urllib.request, "build_opener", return_value=good) as build, redirect_stderr(err):
            self.assertEqual(c.call("POST", "/v1/slots/s1/step", {"action": {}}), {"ok": True})     # safe: nothing was sent
            self.assertEqual(c.health(), {"ok": True})
        self.assertEqual(build.call_count, 1)
        self.assertTrue(c._certifi)
        lines = err.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("certifi", lines[0])
        self.assertIn("https://gw.example:443", lines[0])
        self.assertIn("Install Certificates.command", lines[0])
        self.assertNotIn(TOKEN, err.getvalue())

    def test_without_certifi_the_error_names_the_fix_and_is_not_retried(self):
        c = drums.Client("https://gw.example/gw", TOKEN, retry_s=30)
        c._opener = _Opener([cert_failure()])
        t0 = time.monotonic()
        with mock.patch.object(drums, "_certifi_context", return_value=None):
            with self.assertRaises(drums.CertificateError) as cm:
                c.health()
        self.assertLess(time.monotonic() - t0, 1.0)              # a GET is not retried for retry_s on a cert failure
        self.assertIsInstance(cm.exception, drums.DaemonUnavailable)
        msg = str(cm.exception)
        self.assertIn("Install Certificates.command", msg)
        self.assertIn("pip install certifi", msg)
        self.assertIn("gw.example", msg)
        self.assertNotIn(TOKEN, msg)

    def test_when_certifi_fails_too_the_error_says_so(self):
        c = drums.Client("https://gw.example/gw", TOKEN)
        c._opener = _Opener([cert_failure()])
        with mock.patch.object(drums, "_certifi_context", return_value=ssl.create_default_context()), \
                mock.patch.object(drums.urllib.request, "build_opener", return_value=_Opener([cert_failure()])), redirect_stderr(io.StringIO()):
            with self.assertRaises(drums.CertificateError) as cm:
                c.health()
        self.assertIn("certifi's bundle could not verify it either", str(cm.exception))

    def test_the_certifi_opener_keeps_the_redirect_policy(self):
        c = drums.Client("https://gw.example/gw", TOKEN)
        built = []
        real = urllib.request.build_opener

        def spy(*handlers):
            built.append(handlers)
            return real(*handlers)
        with mock.patch.object(drums, "_certifi_context", return_value=ssl.create_default_context()), \
                mock.patch.object(drums.urllib.request, "build_opener", side_effect=spy), redirect_stderr(io.StringIO()):
            self.assertTrue(c._use_certifi())
            self.assertFalse(c._use_certifi())                  # once
        self.assertTrue(any(isinstance(h, drums._RedirectPolicy) for h in built[0]))
        self.assertTrue(any(isinstance(h, urllib.request.HTTPSHandler) for h in built[0]))


if __name__ == "__main__":
    unittest.main()
