"""Desktop tokens: `drums.connect(url, key)` against a gateway that advertises token mode exchanges the API key once at
the console, sends only the token to the gateway, renews it before it expires and once after a 401, falls back to the
key in `both` mode when the console is away, and never shows the key or the token in a repr or an error.
A fake gateway and a fake console on loopback; no network."""
import json
import os
import sys
import threading
import time
import unittest
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

    @classmethod
    def reset(cls):
        cls.mode, cls.console_up, cls.console_status = "token", True, 200
        cls.renew_after_s, cls.expires_in = 3300, 3600
        cls.issued, cls.exchanges, cls.revoked = [], 0, set()
        cls.gateway_auth, cls.console_auth = [], []


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
            self.connect()
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


if __name__ == "__main__":
    unittest.main()
