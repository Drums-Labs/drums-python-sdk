"""What a newcomer sees with no server configured and with no key. Localhost only, no network."""
import os
import socket
import time
import unittest
from unittest import mock

import drums
from drums.errors import AuthError, DaemonUnavailable


def closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class OnboardingTests(unittest.TestCase):
    def test_no_server_configured_fails_fast_and_says_what_to_set(self):
        url = closed_port_url()
        env = {k: v for k, v in os.environ.items() if k not in ("DRUMS_URL", "DRUMSD_URL")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(drums, "LOCAL_URL", url):
            c = drums.Client(url, None, retry_s=30)
            t0 = time.monotonic()
            with self.assertRaises(DaemonUnavailable) as cm:
                c.health()
            self.assertLess(time.monotonic() - t0, 5, "must not retry for retry_s when nobody chose a server")
            self.assertIn("DRUMS_URL is not set", str(cm.exception))
            self.assertIn("request-access", str(cm.exception))

    def test_a_server_named_in_env_is_still_retried(self):
        url = closed_port_url()
        with mock.patch.dict(os.environ, {"DRUMS_URL": url}), mock.patch.object(drums, "LOCAL_URL", url):
            c = drums.Client(url, None, retry_s=0.6)
            t0 = time.monotonic()
            with self.assertRaises(DaemonUnavailable) as cm:
                c.health()
            self.assertGreaterEqual(time.monotonic() - t0, 0.5)
            self.assertIn("unreachable", str(cm.exception))

    def test_a_401_says_how_to_get_a_key(self):
        e = AuthError(401, "missing, invalid, revoked or expired API key")
        self.assertIn("server HTTP 401", str(e))
        self.assertIn("console.drums.run/request-access", str(e))
        self.assertEqual(e.message, "missing, invalid, revoked or expired API key")


if __name__ == "__main__":
    unittest.main()
