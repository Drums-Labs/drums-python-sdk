"""Desktop tokens: `drums.connect(url, key)` against a gateway that advertises token mode exchanges the API key once at
the console, sends only the token to the gateway, renews it before it expires and once after a 401, falls back to the
key in `both` mode when the console is away, and never shows the key or the token in a repr or an error.
A fake gateway and a fake console on loopback; no network."""
import io
import json
import os
import ssl
import sys
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import drums  # noqa: E402

KEY = "drums_sk_abcdefghijkl_" + "K" * 43


class State:
    mode = "token"              # what the gateway advertises
    console_up = True
    console_status = 200
    renew_after_s = 3300
    expires_in = 3600
    issued = []                 # tokens the console handed out
    exchanges = 0
    revoked = set()             # tokens the gateway refuses from now on
    gateway_auth = []           # Authorization headers the gateway saw on /v1
    console_auth = []
    console_url = ""
    console_script = []         # (status, Retry-After or None) answered before the console issues a token

    @classmethod
    def reset(cls):
        cls.mode, cls.console_up, cls.console_status = "token", True, 200
        cls.renew_after_s, cls.expires_in = 3300, 3600
        cls.issued, cls.exchanges, cls.revoked = [], 0, set()
        cls.gateway_auth, cls.console_auth = [], []
        cls.console_script = []


def _send(h, code, obj):
    b = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)


class Console(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        State.console_auth.append(self.headers.get("Authorization"))
        if self.path != "/api/v1/tokens":
            return _send(self, 404, {"code": "not_found"})
        if not State.console_up:
            return _send(self, 503, {"code": "retryable_infrastructure_failure"})
        if State.console_script:
            status, ra = State.console_script.pop(0)
            b = json.dumps({"code": "rate_limited" if status == 429 else "retryable_infrastructure_failure"}).encode()
            self.send_response(status)
            if ra is not None:
                self.send_header("Retry-After", str(ra))
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if self.headers.get("Authorization") != f"Bearer {KEY}" or State.console_status == 401:
            return _send(self, 401, {"error": "missing, invalid, revoked or expired API key", "code": "unauthenticated"})
        State.exchanges += 1
        tok = f"eyJhbGciOiJFZERTQSJ9.eyJuIjo{State.exchanges}fQ.{'s' * 86}"
        State.issued.append(tok)
        _send(self, 200, {"token": tok, "token_type": "Bearer", "expires_in": State.expires_in, "renew_after_s": State.renew_after_s,
                          "expires_at": "2026-11-17T20:00:00Z", "workspace_id": "w", "hosts": []})


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth_ok(self):
        a = self.headers.get("Authorization") or ""
        State.gateway_auth.append(a)
        tok = a[len("Bearer "):]
        if State.mode == "token":
            return tok in State.issued and tok not in State.revoked
        if State.mode == "both":
            return tok == KEY or (tok in State.issued and tok not in State.revoked)
        return tok == KEY

    def do_GET(self):
        if self.path == "/gw/auth":
            if State.mode == "drumsd":
                return _send(self, 404, {"error": "no such route"})
            url = f"{State.console_url}/api/v1/tokens" if State.mode != "api_key" else None
            return _send(self, 200, {"auth_mode": State.mode if State.mode != "drumsd" else "api_key", "token_url": url, "keys_url": None, "host_id": "h"})
        if not self._auth_ok():
            return _send(self, 401, {"error": "missing, invalid, revoked or expired desktop token", "code": "unauthenticated"})
        if self.path == "/gw/v1/health":
            return _send(self, 200, {"ok": True, "version": "0.4.2"})
        return _send(self, 404, {"error": "no such route"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if not self._auth_ok():
            return _send(self, 401, {"error": "missing, invalid, revoked or expired desktop token", "code": "unauthenticated"})
        return _send(self, 200, {"reset": True})


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class TokenExchange(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = serve(Console)
        cls.gateway = serve(Gateway)
        State.console_url = f"http://127.0.0.1:{cls.console.server_address[1]}"
        cls.url = f"http://127.0.0.1:{cls.gateway.server_address[1]}/gw"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.console, cls.gateway):
            s.shutdown()
            s.server_close()

    def setUp(self):
        State.reset()

    def connect(self, **kw):
        return drums.connect(self.url, KEY, close_on_exit=False, handle_signals=False, retry_s=0.0, **kw)

    def test_signature_unchanged_and_the_key_goes_to_the_console_only_once(self):
        c = self.connect()
        for _ in range(5):
            self.assertTrue(c.health()["ok"])
        self.assertEqual(State.exchanges, 1)
        self.assertEqual(State.console_auth, [f"Bearer {KEY}"])
        self.assertTrue(State.gateway_auth)
        for a in State.gateway_auth:
            self.assertNotIn(KEY, a)
            self.assertEqual(a, f"Bearer {State.issued[0]}")

    def test_renews_before_expiry(self):
        State.renew_after_s = 1
        c = self.connect()
        c.health()
        time.sleep(1.2)
        c.health()
        self.assertEqual(State.exchanges, 2)
        self.assertEqual(State.gateway_auth[-1], f"Bearer {State.issued[1]}")

    def test_a_refused_token_is_renewed_once_and_the_call_retried(self):
        c = self.connect()
        c.health()
        State.revoked.add(State.issued[0])                     # e.g. the signing key rotated
        self.assertEqual(c.call("POST", "/v1/slots/x/reset", {}), {"reset": True})
        self.assertEqual(State.exchanges, 2)

    def test_a_revoked_key_raises_auth_error_without_the_key_or_a_token(self):
        c = self.connect()
        c.health()
        State.revoked.add(State.issued[0])
        State.console_status = 401
        with self.assertRaises(drums.AuthError) as cm:
            c.health()
        msg = str(cm.exception)
        self.assertNotIn(KEY, msg)
        self.assertNotIn("K" * 8, msg)
        for t in State.issued:
            self.assertNotIn(t, msg)
        self.assertIn("refused the API key", msg)

    def test_token_mode_with_the_console_down_is_a_clear_error(self):
        State.console_up = False
        with self.assertRaises(drums.DaemonUnavailable) as cm:
            self.connect(retry=drums.RetryPolicy(max_wait_s=0))
        self.assertIn("could not exchange the API key for a desktop token", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))

    def test_both_mode_falls_back_to_the_key_when_the_console_is_down(self):
        State.mode, State.console_up = "both", False
        c = self.connect()
        self.assertTrue(c.health()["ok"])
        self.assertEqual(State.gateway_auth[-1], f"Bearer {KEY}")
        tries = len(State.console_auth)
        for _ in range(3):
            c.health()
        self.assertEqual(len(State.console_auth), tries, "no exchange attempt per call while the console is away")

    def test_the_exchange_waits_for_a_429_retry_after(self):
        State.console_script = [(429, 1)]
        t0 = time.monotonic()
        c = self.connect()
        self.assertTrue(c.health()["ok"])
        self.assertGreaterEqual(time.monotonic() - t0, 1.0)
        self.assertEqual(State.exchanges, 1)
        self.assertEqual(len(State.console_auth), 2)
        self.assertEqual(State.gateway_auth[-1], f"Bearer {State.issued[0]}")

    def test_token_mode_retries_a_5xx_from_the_console_within_the_budget(self):
        State.console_script = [(503, None), (502, None)]
        c = self.connect(retry=drums.RetryPolicy(max_wait_s=5, base_s=0.05))
        self.assertTrue(c.health()["ok"])
        self.assertEqual(len(State.console_auth), 3)

    def test_the_exchange_honours_the_retry_off_switch(self):
        State.console_script = [(429, 1)]
        t0 = time.monotonic()
        with self.assertRaises(drums.DaemonUnavailable) as cm:
            self.connect(retry=drums.RetryPolicy(max_wait_s=0))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn("HTTP 429", str(cm.exception))
        self.assertEqual(cm.exception.retry_after, 1.0)

    def test_both_mode_does_not_wait_on_a_failing_console(self):
        State.mode, State.console_script = "both", [(503, 5)]
        t0 = time.monotonic()
        c = self.connect()
        self.assertTrue(c.health()["ok"])
        self.assertLess(time.monotonic() - t0, 2.0, "the key still works in both mode, so no 503 backoff")
        self.assertEqual(State.gateway_auth[-1], f"Bearer {KEY}")
        self.assertIn("HTTP 503", c._cred.last_error)

    def test_a_renewal_after_a_401_also_retries_a_429(self):
        c = self.connect()
        c.health()
        State.revoked.add(State.issued[0])
        State.console_script = [(429, 0)]
        self.assertTrue(c.health()["ok"])
        self.assertEqual(State.exchanges, 2)
        self.assertEqual(State.gateway_auth[-1], f"Bearer {State.issued[1]}")

    def test_a_key_sent_before_discovery_answered_is_retried_after_the_401(self):
        c = drums.Client(self.url, KEY, retry_s=0.0)
        real = c._cred._discover
        calls = []

        def flaky(client):
            calls.append(1)
            if len(calls) == 1:
                return None                      # no answer: the mode stays unknown and the key goes out as is
            return real(client)
        c._cred._discover = flaky
        self.assertTrue(c.health()["ok"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(State.gateway_auth[-1], f"Bearer {State.issued[0]}")

    def test_api_key_mode_and_plain_drumsd_send_the_key_and_never_exchange(self):
        for mode in ("api_key", "drumsd"):
            State.reset()
            State.mode = mode
            c = self.connect()
            c.health()
            self.assertEqual(State.exchanges, 0, mode)
            self.assertEqual(State.gateway_auth[-1], f"Bearer {KEY}")

    def test_a_drumsd_token_is_sent_as_is_with_no_discovery(self):
        tok = "t" * 43
        c = drums.Client(self.url, tok)
        self.assertEqual(c._cred.bearer(c), tok)
        self.assertEqual(State.gateway_auth, [])

    def test_reprs_never_show_the_key_or_the_token(self):
        c = self.connect()
        c.health()
        for text in (repr(c), str(c), repr(c._cred), str(c._cred)):
            self.assertNotIn(KEY, text)
            self.assertNotIn("K" * 8, text)
            self.assertNotIn(State.issued[0], text)
        self.assertIn("desktop token", repr(c))

    def test_the_key_never_goes_to_a_plain_http_token_url_off_loopback(self):
        State.console_url_saved = State.console_url
        try:
            State.console_url = "http://console.example.com"
            with self.assertRaises(drums.InsecureTransport) as cm:
                self.connect()
            self.assertNotIn(KEY, str(cm.exception))
        finally:
            State.console_url = State.console_url_saved



class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _Opener:
    def __init__(self, outcome):
        self.outcome = outcome

    def open(self, req, timeout=None):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return _Resp(self.outcome)


def _cert_failure():
    return urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))


class ExchangeCertificates(unittest.TestCase):
    """The token exchange builds its own connection to the console: it gets the same certifi fallback as the gateway."""

    def client(self, outcomes):
        from unittest import mock
        c = drums.Client("https://gw.example/gw", KEY)
        c._cred._mode, c._cred._token_url = "token", "https://console.example/api/v1/tokens"
        built = []

        def build(token):
            built.append((token is not None, c._certifi_ctx is not None))
            return _Opener(outcomes.pop(0))
        return c, built, mock.patch.object(c, "_build_opener", side_effect=build)

    def test_a_failed_system_store_on_the_console_switches_to_certifi(self):
        from unittest import mock
        tok = b'{"token": "tok-1", "expires_in": 3600}'
        c, built, patch = self.client([_cert_failure(), b"gateway opener", tok])   # _use_certifi rebuilds the gateway's opener too
        err = io.StringIO()
        with patch, mock.patch.object(drums, "_certifi_context", return_value=ssl.create_default_context()), redirect_stderr(err):
            self.assertEqual(c._cred.bearer(c), "tok-1")
        self.assertTrue(c._certifi)
        self.assertEqual(built, [(True, False), (True, True), (True, True)])     # the retry used certifi's bundle
        line = err.getvalue().strip()
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("https://console.example:443", line)
        self.assertNotIn(KEY, line)
        self.assertNotIn("tok-1", line)

    def test_without_certifi_the_console_error_names_the_fix(self):
        from unittest import mock
        c, _, patch = self.client([_cert_failure()])
        with patch, mock.patch.object(drums, "_certifi_context", return_value=None):
            with self.assertRaises(drums.CertificateError) as cm:
                c._cred.bearer(c)
        msg = str(cm.exception)
        self.assertIn("console.example", msg)
        self.assertIn("pip install certifi", msg)
        self.assertNotIn(KEY, msg)

    def test_a_client_already_on_certifi_uses_it_for_the_exchange(self):
        from unittest import mock
        c, built, patch = self.client([b'{"token": "tok-2", "expires_in": 3600}'])
        c._certifi, c._certifi_ctx = True, ssl.create_default_context()
        with patch:
            self.assertEqual(c._cred.bearer(c), "tok-2")
        self.assertEqual(built, [(True, True)])


if __name__ == "__main__":
    unittest.main()
