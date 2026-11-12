"""Token transport: a token goes over https, or over plain http to loopback only (localhost, 127.0.0.0/8, ::1).
Any other http URL is refused before a request, in the Client and in `drums mcp`, unless the caller opts in with
allow_insecure_http=True / $DRUMS_ALLOW_INSECURE_HTTP=1 / --allow-insecure-http. No message ever carries the token."""
import contextlib
import io
import json
import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
