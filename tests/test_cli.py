"""The `drums` command: version, and `drums mcp --print-config` for every client (placeholders only, never a token)."""
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import drums  # noqa: E402
from drums import cli  # noqa: E402

SECRET = "drums_sk_abcdefghijkl_" + "Z" * 43


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    saved = {k: os.environ.get(k) for k in ("DRUMS_TOKEN", "DRUMS_URL", "DRUMS_TOKEN_FILE")}
    for k in saved:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as e:
                rc = e.code
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    def test_version(self):
        rc, out, _ = run(["version"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"drums {drums.__version__}\n")

    def test_print_config_never_prints_a_token(self):
        env = {"DRUMS_TOKEN": SECRET, "DRUMS_URL": "https://beta.drums.run/gw"}
        for client in cli.CLIENTS:
            rc, out, _ = run(["mcp", "--print-config", client], env)
            self.assertEqual(rc, 0, client)
            self.assertIn("drums_sk_...", out, client)
            self.assertNotIn(SECRET, out, client)
            self.assertNotIn("Z" * 10, out, client)
            self.assertIn("https://beta.drums.run/gw", out, client)
            self.assertNotIn("<GATEWAY_URL>", out, client)

    def test_print_config_exact_snippets(self):
        env = {"DRUMS_TOKEN": SECRET}
        self.assertEqual(run(["mcp", "--print-config", "claude"], env)[1],
                         'claude mcp add --transport http drums <GATEWAY_URL>/mcp --header "Authorization: Bearer drums_sk_..."\n')
        self.assertEqual(run(["mcp", "--print-config", "claude-local"], env)[1],
                         "claude mcp add drums -e DRUMS_URL=<GATEWAY_URL> -e DRUMS_TOKEN=drums_sk_... -- drums mcp\n")
        self.assertEqual(run(["mcp", "--print-config", "cursor"], env)[1],
                         '{"mcpServers": {"drums": {"url": "<GATEWAY_URL>/mcp", "headers": {"Authorization": "Bearer drums_sk_..."}}}}\n')
        self.assertEqual(run(["mcp", "--print-config", "codex"], env)[1],
                         '[mcp_servers.drums]\ncommand = "drums"\nargs = ["mcp"]\nenv = { DRUMS_URL = "<GATEWAY_URL>", DRUMS_TOKEN = "drums_sk_..." }\n')
        # --url wins over $DRUMS_URL; a trailing slash is dropped
        rc, out, _ = run(["mcp", "--url", "https://gw.example/gw/", "--print-config", "cursor"], {"DRUMS_URL": "https://other.example"})
        self.assertIn('"url": "https://gw.example/gw/mcp"', out)

    def test_bad_client_and_bad_subcommand(self):
        rc, out, err = run(["mcp", "--print-config", "emacs"])
        self.assertNotEqual(rc, 0)
        rc, out, err = run([])
        self.assertEqual(rc, 2)
        self.assertIn("mcp", out)
        rc, out, err = run(["mcp", "--env", "c1", "--create"])
        self.assertEqual(rc, 2)
        self.assertIn("exclude", err)

    def test_module_entry_point_matches(self):
        from drums import mcp
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mcp.main(["--print-config", "codex"])
        self.assertEqual(rc, 0)
        self.assertIn('command = "drums"', out.getvalue())


if __name__ == "__main__":
    unittest.main()
