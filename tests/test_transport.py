"""Token transport: a token goes over https, or over plain http to loopback only (localhost, 127.0.0.0/8, ::1).
Any other http URL is refused before a request, in the Client and in `drums mcp`, unless the caller opts in with
allow_insecure_http=True / $DRUMS_ALLOW_INSECURE_HTTP=1 / --allow-insecure-http. No message ever carries the token."""
import contextlib
import io
import json
import os
import socket
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import drums  # noqa: E402
from drums import cli  # noqa: E402

SECRET = "drums_sk_abcdefghijkl_" + "Q" * 43
ENV_KEYS = ("DRUMS_TOKEN", "DRUMS_URL", "DRUMS_TOKEN_FILE", "DRUMS_ALLOW_INSECURE_HTTP", "DRUMS_API_KEY")
LIST_TOOLS = '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n'


@contextlib.contextmanager
def environment(env):
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_cli(argv, env=None, stdin=""):
    """`drums <argv>` with a controlled environment and stdin; (rc, stdout, stderr). An allowed `drums mcp` serves the
    lines on stdin and returns at EOF, so an empty stdin starts and stops the server without any network."""
    out, err = io.StringIO(), io.StringIO()
    saved_stdin, sys.stdin = sys.stdin, io.StringIO(stdin)
    try:
        with environment(env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as e:
                rc = e.code
    finally:
        sys.stdin = saved_stdin
    return rc, out.getvalue(), err.getvalue()


class ClientTransport(unittest.TestCase):
    def test_plain_http_off_loopback_is_refused_with_a_token(self):
        for url in ("http://example.com", "http://example.com:7070/gw", "http://10.0.0.5:7070", "http://gw.internal/"):
            with environment({}):
                with self.assertRaises(drums.InsecureTransport) as cm:
                    drums.Client(url, SECRET)
                with self.assertRaises(drums.InsecureTransport):
                    drums.connect(url, SECRET, check=False)
            msg = str(cm.exception)
            self.assertNotIn(SECRET, msg, url)
            self.assertNotIn("Q" * 8, msg, url)
            self.assertNotIn(SECRET, repr(cm.exception))
            self.assertIn("--allow-insecure-http", msg)
            self.assertIn("DRUMS_ALLOW_INSECURE_HTTP=1", msg)
            self.assertIn("allow_insecure_http=True", msg)
            self.assertIn("https://", msg)
            self.assertIsInstance(cm.exception, drums.DrumsError)

    def test_token_from_the_environment_is_checked_too(self):
        with environment({"DRUMS_TOKEN": SECRET}):
            with self.assertRaises(drums.InsecureTransport) as cm:
                drums.Client("http://example.com")
            self.assertNotIn(SECRET, str(cm.exception))

    def test_loopback_http_and_https_are_allowed(self):
        with environment({}):
            for url in ("http://localhost:7070", "http://localhost", "http://127.0.0.1", "http://127.0.0.1:7070",
                        "http://127.255.0.9:7070", "http://[::1]:7070", "https://beta.drums.run/gw", "https://example.com:8443/gw"):
                c = drums.Client(url, SECRET)
                self.assertEqual(c.token, SECRET, url)
                self.assertEqual(drums.connect(url, SECRET, check=False).url, url.rstrip("/"), url)

    def test_no_token_means_no_rule(self):
        with environment({}):
            self.assertEqual(drums.Client("http://example.com", None).url, "http://example.com")
            self.assertEqual(drums.Client("http://example.com", "").url, "http://example.com")

    def test_opt_in_kwarg_and_env(self):
        with environment({}):
            c = drums.Client("http://example.com", SECRET, allow_insecure_http=True)
            self.assertEqual(c.url, "http://example.com")
            self.assertEqual(drums.connect("http://example.com", SECRET, check=False, allow_insecure_http=True).url, "http://example.com")
            with self.assertRaises(drums.InsecureTransport):
                drums.Client("http://example.com", SECRET, allow_insecure_http=False)
        for value in ("1", "true", "yes"):
            with environment({"DRUMS_ALLOW_INSECURE_HTTP": value}):
                self.assertEqual(drums.Client("http://example.com", SECRET).url, "http://example.com", value)
        for value in ("0", "", "no"):
            with environment({"DRUMS_ALLOW_INSECURE_HTTP": value}):
                with self.assertRaises(drums.InsecureTransport):
                    drums.Client("http://example.com", SECRET)
        with environment({"DRUMS_ALLOW_INSECURE_HTTP": "1"}):      # an explicit False wins over the environment
            with self.assertRaises(drums.InsecureTransport):
                drums.Client("http://example.com", SECRET, allow_insecure_http=False)

    def test_check_transport_is_exported(self):
        with environment({}):
            self.assertIsNone(drums.check_transport("https://x.example", SECRET))
            self.assertIsNone(drums.check_transport("http://x.example", None))
            with self.assertRaises(drums.InsecureTransport):
                drums.check_transport("http://x.example", SECRET)


class CliTransport(unittest.TestCase):
    def test_url_flag_off_loopback_is_refused_and_names_the_flag(self):
        rc, out, err = run_cli(["mcp", "--url", "http://example.com"], {"DRUMS_TOKEN": SECRET})
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("drums mcp: refusing to send the Drums token over plain http to example.com", err)
        self.assertIn("--allow-insecure-http", err)
        self.assertNotIn(SECRET, err)
        self.assertNotIn("Q" * 8, err)

    def test_drums_url_env_off_loopback_is_refused(self):
        rc, out, err = run_cli(["mcp"], {"DRUMS_TOKEN": SECRET, "DRUMS_URL": "http://example.com:7070"})
        self.assertEqual(rc, 2)
        self.assertIn("example.com", err)
        self.assertNotIn(SECRET, err + out)

    def test_token_file_fallback_is_refused_too(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as f:
            f.write(SECRET + "\n")
        try:
            rc, out, err = run_cli(["mcp", "--url", "http://example.com"], {"DRUMS_TOKEN_FILE": f.name})
        finally:
            os.unlink(f.name)
        self.assertEqual(rc, 2)
        self.assertNotIn(SECRET, err + out)
        self.assertIn("refusing", err)

    def test_loopback_http_and_https_start_the_server(self):
        for url in ("http://localhost:7070", "http://127.0.0.1:7070", "https://beta.drums.run/gw"):
            rc, out, err = run_cli(["mcp", "--url", url], {"DRUMS_TOKEN": SECRET}, stdin=LIST_TOOLS)
            self.assertEqual(rc, 0, (url, err))
            self.assertEqual(err, "", url)
            reply = json.loads(out)
            self.assertEqual(len(reply["result"]["tools"]), 34, url)      # tools/list needs no gateway
            self.assertNotIn(SECRET, out)

    def test_opt_in_flag_and_env_allow_plain_http(self):
        rc, out, err = run_cli(["mcp", "--url", "http://example.com", "--allow-insecure-http"], {"DRUMS_TOKEN": SECRET}, stdin=LIST_TOOLS)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(json.loads(out)["result"]["tools"]), 34)
        rc, out, err = run_cli(["mcp", "--url", "http://example.com"], {"DRUMS_TOKEN": SECRET, "DRUMS_ALLOW_INSECURE_HTTP": "1"}, stdin=LIST_TOOLS)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(json.loads(out)["result"]["tools"]), 34)
        self.assertNotIn(SECRET, out + err)

    def test_no_token_plain_http_is_not_refused(self):
        rc, out, err = run_cli(["mcp", "--url", "http://example.com"], {}, stdin="")
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "")

    def test_print_config_never_prints_a_token_from_any_variable(self):
        for var in ("DRUMS_TOKEN", "DRUMS_API_KEY"):
            for url in ("https://beta.drums.run/gw", "http://example.com:7070", "http://127.0.0.1:7070"):
                for client in cli.CLIENTS:
                    rc, out, err = run_cli(["mcp", "--url", url, "--print-config", client], {var: SECRET})
                    self.assertEqual(rc, 0, (var, url, client))
                    self.assertNotIn(SECRET, out + err, (var, url, client))
                    self.assertNotIn("Q" * 8, out + err, (var, url, client))
                    self.assertIn("drums_sk_...", out, (var, url, client))
                    self.assertIn(url, out, (var, url, client))
                    self.assertEqual(err, "", (var, url, client))   # printing a snippet sends nothing: no transport refusal


class Redirecting(BaseHTTPRequestHandler):
    """A loopback server that redirects: every request is recorded as (path, headers). Location targets are built
    from the query-free path; `remote_port` is a second loopback listener that must never see a connection."""
    requests = []
    remote_port = 0

    def log_message(self, *a):
        pass

    def _redirect(self, code, location):
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        Redirecting.requests.append((self.path, dict(self.headers)))
        host = f"127.0.0.1:{self.server.server_address[1]}"
        p = self.path
        if p == "/to-remote-host":
            self._redirect(302, "http://example.com/v1/health")
        elif p == "/to-remote-https":
            self._redirect(301, "https://example.com/v1/health")
        elif p == "/to-other-loopback-port":
            self._redirect(307, f"http://127.0.0.1:{Redirecting.remote_port}/v1/health")
        elif p == "/to-remote-with-secrets":
            self._redirect(302, "http://user:hunter2@example.com/v1/health?token=" + SECRET)
        elif p == "/same-relative":
            self._redirect(302, "/landed")
        elif p == "/same-absolute":
            self._redirect(308, f"http://{host}/landed")
        elif p == "/same-twice":
            self._redirect(302, "/same-relative")
        elif p == "/loop":
            self._redirect(302, "/loop")
        elif p.startswith("/chain/"):
            self._redirect(302, f"/chain/{int(p.rsplit('/', 1)[1]) + 1}")
        elif p == "/landed":
            self._json({"landed": True, "authorization": self.headers.get("Authorization")})
        else:
            self._json({"path": p})

    do_POST = do_GET


class Redirects(unittest.TestCase):
    """Redirect policy: same origin (scheme, host, port) only, at most drums.MAX_REDIRECTS hops, the token never sent
    elsewhere. The origin here is a plain-http loopback server (allowed without opt-in); an https origin cannot be
    served by a stdlib test fixture, so the https->http case calls the handler's redirect_request directly with an
    https request URL, which is exactly what urllib calls for a 30x and the only input the policy looks at."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Redirecting)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.remote = socket.socket()
        cls.remote.bind(("127.0.0.1", 0))
        cls.remote.listen(4)
        cls.remote.settimeout(0.2)
        Redirecting.remote_port = cls.remote.getsockname()[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.remote.close()

    def setUp(self):
        Redirecting.requests = []
        with environment({}):
            self.client = drums.Client(self.url, SECRET, retry_s=0.0)

    def remote_saw_a_connection(self):
        try:
            c, _ = self.remote.accept()
        except socket.timeout:
            return False
        c.close()
        return True

    def assert_refused(self, path, *expect_in_message):
        Redirecting.requests = []
        with self.assertRaises(drums.RedirectRefused) as cm:
            self.client.call("GET", path)
        msg = str(cm.exception)
        self.assertNotIn(SECRET, msg)
        self.assertNotIn("Q" * 8, msg)
        self.assertNotIn("hunter2", msg)
        self.assertNotIn("?", msg)
        self.assertNotIn("/v1/", msg)
        for e in expect_in_message:
            self.assertIn(e, msg)
        self.assertIsInstance(cm.exception, drums.DrumsError)
        self.assertEqual(len(Redirecting.requests), 1, "only the first request was made")
        return cm.exception

    def test_cross_host_redirect_is_refused_unfollowed(self):
        self.assert_refused("/to-remote-host", "HTTP 302", "http://example.com:80", "127.0.0.1")
        self.assert_refused("/to-remote-https", "HTTP 301", "https://example.com:443")

    def test_loopback_to_remote_port_is_refused_and_nothing_reaches_it(self):
        self.assert_refused("/to-other-loopback-port", "HTTP 307", f"http://127.0.0.1:{Redirecting.remote_port}")
        self.assertFalse(self.remote_saw_a_connection())

    def test_refusal_text_has_no_token_userinfo_or_query(self):
        e = self.assert_refused("/to-remote-with-secrets", "http://example.com:80")
        self.assertNotIn("user", str(e))
        self.assertNotIn("token=", str(e))
        self.assertEqual(e.target, "http://example.com:80")

    def test_same_origin_redirect_is_followed_with_the_token(self):
        for path, hops in (("/same-relative", 1), ("/same-absolute", 1), ("/same-twice", 2)):
            Redirecting.requests = []
            out = self.client.call("GET", path)
            self.assertEqual(out["landed"], True, path)
            self.assertEqual(out["authorization"], f"Bearer {SECRET}", path)
            self.assertEqual([p for p, _ in Redirecting.requests][-1], "/landed", path)
            self.assertEqual(len(Redirecting.requests), hops + 1, path)
            for _, h in Redirecting.requests:
                self.assertEqual(h.get("Authorization"), f"Bearer {SECRET}", path)

    def test_redirect_loop_and_long_chain_are_capped(self):
        for path in ("/loop", "/chain/0"):
            Redirecting.requests = []
            with self.assertRaises(drums.RedirectRefused) as cm:
                self.client.call("GET", path)
            self.assertIn(f"more than {drums.MAX_REDIRECTS} redirects", str(cm.exception))
            self.assertNotIn(SECRET, str(cm.exception))
            self.assertEqual(len(Redirecting.requests), drums.MAX_REDIRECTS + 1, path)   # the first request + 5 hops
        self.assertEqual(drums.MAX_REDIRECTS, 5)

    def test_https_origin_to_plain_http_is_refused_by_the_handler(self):
        """The policy, called as urllib's http_error_30x calls it, with an https request URL."""
        handler = drums._RedirectPolicy(SECRET, allow_insecure_http=False)
        req = urllib.request.Request("https://gw.example/gw/v1/health", headers={"Authorization": f"Bearer {SECRET}"})
        fp = io.BytesIO(b"")
        for target in ("http://gw.example/gw/v1/health", "http://gw.example:443/gw/v1/health", "https://gw.example:8443/gw/v1/health",
                       "https://other.example/gw/v1/health", "http://127.0.0.1:7070/v1/health"):
            with self.assertRaises(drums.RedirectRefused) as cm:
                handler.redirect_request(req, fp, 302, "Found", {}, target)
            self.assertNotIn(SECRET, str(cm.exception), target)
            self.assertIn("https://gw.example:443", str(cm.exception), target)
        # the same origin, spelled with or without the default port, is followed with the token
        for target in ("https://gw.example/gw/v1/other", "https://gw.example:443/gw/v1/other", "https://GW.example/gw/v1/other"):
            new = handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, target)
            self.assertEqual(new.get_header("Authorization"), f"Bearer {SECRET}", target)
            self.assertEqual(new.full_url, target)
        # a sixth hop is refused even to the same origin
        req.redirect_dict = {"https://gw.example/gw/a": 5}
        with self.assertRaises(drums.RedirectRefused):
            handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "https://gw.example/gw/v1/other")

    def test_opted_in_http_origin_follows_same_origin_only(self):
        # a client opted into plain http off loopback still never follows a redirect to another origin
        handler = drums._RedirectPolicy(SECRET, allow_insecure_http=True)
        req = urllib.request.Request("http://gw.internal:7070/v1/health", headers={"Authorization": f"Bearer {SECRET}"})
        new = handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "http://gw.internal:7070/v1/other")
        self.assertEqual(new.get_header("Authorization"), f"Bearer {SECRET}")
        with self.assertRaises(drums.RedirectRefused):
            handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "http://gw2.internal:7070/v1/other")


if __name__ == "__main__":
    unittest.main()
