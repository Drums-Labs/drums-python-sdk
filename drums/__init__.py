"""Thin client for drumsd (standard library only).

    import drums
    env = drums.env(task)                 # a new slot from the clean checkpoint (task setup runs after every reset)
    env.reset()                           # new episode: restore + every gate
    env.step({"type": "pyautogui", "code": "pyautogui.click(100, 200)"})
    kids = env.branch(8)                  # capture this state, 8 children from it
    state = env.save()                    # checkpoint id of the current state
    env2 = drums.env(from_state=state)    # a new slot from that checkpoint
    env.valid()                           # waits for the alive gate; True only if every gate passed
    env.close()

A rollout is usable only when its episode is valid. `step()` returns the episode's current validity; `valid()` waits
for the gate that resolves after the slot answered (alive_after_resume, default 5 s).
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("DRUMSD_URL", "http://127.0.0.1:7070")

__all__ = ["env", "Env", "Client", "DrumsError", "InvalidEpisode"]


class DrumsError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"drumsd HTTP {status}: {message}")
        self.status, self.message = status, message


class InvalidEpisode(DrumsError):
    pass


class Client:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 600.0):
        self.url, self.timeout = url.rstrip("/"), timeout

    def call(self, method: str, path: str, body=None, raw: bool = False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                b = r.read()
                return b if raw else json.loads(b or b"null")
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            try:
                msg = json.loads(msg).get("error", msg)
            except ValueError:
                pass
            raise DrumsError(e.code, msg) from None

    def checkpoints(self):
        return self.call("GET", "/v1/checkpoints")

    def slots(self):
        return self.call("GET", "/v1/slots")


def _action(a):
    if isinstance(a, str):
        return {"type": "pyautogui", "code": a}
    return a


class Env:
    """One drumsd slot."""

    def __init__(self, client: Client, record: dict):
        self.client, self.record = client, record

    # identity ---------------------------------------------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.record["id"]

    @property
    def episode(self) -> int:
        return self.record["episode"]

    @property
    def state(self) -> str:
        return self.record["state"]

    @property
    def validity(self) -> dict:
        return self.record["validity"]

    def refresh(self) -> dict:
        self.record = self.client.call("GET", f"/v1/slots/{self.id}")
        return self.record

    def _check_up(self):
        if self.record.get("state") != "ready":
            raise InvalidEpisode(409, f"slot {self.id} is {self.record.get('state')}: {self.record.get('reason') or self.record.get('last_error')}")

    # lifecycle --------------------------------------------------------------------------------------------------
    def reset(self) -> dict:
        """Start a new episode. Raises InvalidEpisode if the restore or a synchronous gate failed."""
        self.record = self.client.call("POST", f"/v1/slots/{self.id}/reset")
        self._check_up()
        return self.record

    def step(self, action, screenshot: bool = False) -> dict:
        r = self.client.call("POST", f"/v1/slots/{self.id}/step", {"action": _action(action), "screenshot": screenshot})
        if r.get("screenshot_png_b64"):
            r["screenshot_png"] = base64.b64decode(r.pop("screenshot_png_b64"))
        return r

    def screenshot(self) -> bytes:
        return self.client.call("GET", f"/v1/slots/{self.id}/screenshot", raw=True)

    def gates(self) -> dict:
        return self.client.call("GET", f"/v1/slots/{self.id}/gates")

    def valid(self, timeout: float = 30.0) -> bool:
        """Wait until the episode's verdict is no longer pending; True only when every gate passed."""
        t0 = time.monotonic()
        while True:
            g = self.gates()
            v = g["validity"]["verdict"]
            if v != "pending":
                return v == "valid"
            if time.monotonic() - t0 > timeout:
                return False
            time.sleep(0.25)

    def save(self, name: str | None = None) -> str:
        """Checkpoint the current state; returns the checkpoint id for drums.env(from_state=...)."""
        return self.client.call("POST", f"/v1/slots/{self.id}/capture", {"id": name} if name else {})["id"]

    def branch(self, n: int) -> list:
        """Capture the current state and start n children from it (concurrently). Children that failed to come up
        are returned as Env objects in state down/quarantined, never silently dropped."""
        r = self.client.call("POST", f"/v1/slots/{self.id}/branch", {"n": n})
        out = []
        for c in r["children"]:
            if "error" in c:
                raise DrumsError(500, c["error"])
            out.append(Env(self.client, c))
        return out

    def quarantine(self, reason: str = "client") -> dict:
        self.record = self.client.call("POST", f"/v1/slots/{self.id}/quarantine", {"reason": reason})
        return self.record

    def close(self) -> dict:
        self.record = self.client.call("DELETE", f"/v1/slots/{self.id}")
        return self.record

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return f"<drums.Env {self.id} {self.state} episode={self.episode} {self.validity.get('verdict')}>"


def env(task: dict | None = None, *, from_state: str | None = None, checkpoint: str = "clean", url: str = DEFAULT_URL) -> Env:
    """A new slot. `from_state` (a checkpoint id from save()/branch) wins over `checkpoint`."""
    c = Client(url)
    rec = c.call("POST", "/v1/slots", {"checkpoint": from_state or checkpoint, "task": task})
    e = Env(c, rec)
    e._check_up()
    return e
