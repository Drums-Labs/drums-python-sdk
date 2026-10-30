"""Drums SDK: isolated desktop environments for computer-use RL (standard library only).

    import drums
    client = drums.connect("http://127.0.0.1:7070", token)
    with client.create(image="osworld-ubuntu", task="035f41ba") as env:      # OSWorld task id, path or JSON dict
        env.step("pyautogui.click(100, 200)")                                 # pyautogui code, or an action dict
        obs = env.observe()                                                   # PNG + metadata
        kids = env.fork(8)                                                    # 8 independent copies of this state
        state = env.save()                                                    # a checkpoint; client.create(from_state=state)
        r = env.verify()                                                      # task checker -> Reward (raises if the episode is invalid)
        print(r.score, r.valid, env.metrics()["usage"])

Validity: every create/reset/fork child starts an episode with a gate report (isolation, mount table, network,
cgroup limits, state hash, processes alive after resume, ...). An episode is valid only when every gate passed.
`verify()` always carries the verdict; by default it raises InvalidEpisode instead of returning a reward from an
invalid episode. Retries: drumsd itself retries only failures that happened before any restored process ran; the
SDK retries only GET requests while drumsd is unreachable. Everything else is raised with the gate report.
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import (AuthError, BadRequest, BatchError, CheckerError, Conflict, DaemonUnavailable, DrumsError, EnvFailed,  # noqa: F401
                     ForkError, GateFailed, GuestError, InvalidEpisode, NotFound, RestoreFailed, TaskSetupFailed)

__version__ = "0.2.0"
DEFAULT_URL = os.environ.get("DRUMS_URL") or os.environ.get("DRUMSD_URL") or "http://127.0.0.1:7070"
DEFAULT_IMAGE = "osworld-ubuntu"

__all__ = ["connect", "Client", "Env", "Reward", "Observation", "StepResult", "State", "env", "__version__",
           "DrumsError", "AuthError", "BadRequest", "NotFound", "Conflict", "DaemonUnavailable", "GuestError", "EnvFailed",
           "RestoreFailed", "GateFailed", "TaskSetupFailed", "ForkError", "BatchError", "InvalidEpisode", "CheckerError"]

_STATUS = {400: BadRequest, 401: AuthError, 404: NotFound, 409: Conflict, 422: BadRequest, 502: GuestError, 503: DaemonUnavailable}


def connect(url: str | None = None, token: str | None = None, *, timeout: float = 900.0, retry_s: float = 60.0, check: bool = True) -> "Client":
    """A client for one drumsd. `url`/`token` default to $DRUMS_URL / $DRUMS_TOKEN. With check=True the token is tried
    at once (AuthError / DaemonUnavailable here rather than on the first create)."""
    c = Client(url or DEFAULT_URL, token if token is not None else os.environ.get("DRUMS_TOKEN"), timeout=timeout, retry_s=retry_s)
    if check:
        c.health()
    return c


class Client:
    def __init__(self, url: str = DEFAULT_URL, token: str | None = None, timeout: float = 900.0, retry_s: float = 60.0):
        self.url, self.token, self.timeout, self.retry_s = url.rstrip("/"), token, timeout, retry_s

    # transport --------------------------------------------------------------------------------------------------------
    def call(self, method: str, path: str, body=None, raw: bool = False, timeout: float | None = None):
        """One request. GETs are retried while drumsd is unreachable (up to retry_s: a restarting drumsd comes back and
        reconciles); other methods are never retried, since their outcome is unknown after a dropped connection."""
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        deadline = time.monotonic() + (self.retry_s if method == "GET" else 0.0)
        delay = 0.25
        while True:
            req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    b = r.read()
                    return b if raw else json.loads(b or b"null")
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                try:
                    body_j = json.loads(text)
                    msg = body_j.get("error", text) if isinstance(body_j, dict) else text
                except ValueError:
                    body_j, msg = None, text
                if e.code == 503 and time.monotonic() < deadline:
                    pass
                else:
                    raise _STATUS.get(e.code, DrumsError)(e.code, msg, body_j) from None
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                if time.monotonic() >= deadline:
                    what = "the outcome is unknown; call env.refresh()" if method != "GET" else f"retried for {self.retry_s:.0f} s"
                    raise DaemonUnavailable(0, f"drumsd at {self.url} unreachable on {method} {path} ({what}): {getattr(e, 'reason', e)}") from None
            time.sleep(delay)
            delay = min(delay * 2, 2.0)

    # daemon -----------------------------------------------------------------------------------------------------------
    def health(self) -> dict:
        return self.call("GET", "/v1/health")

    def metrics(self) -> dict:
        """Aggregate metrics of the current window: op latency (create/reset/fork/step/verify/...), episodes by verdict,
        failures and retries by cause, rewards, and cost (host price x wall time, per valid episode / verified reward)."""
        return self.call("GET", "/v1/metrics")

    def reset_metrics(self) -> dict:
        """Close the current metrics window (its ledger goes to drumsd's event log) and start a new one."""
        return self.call("POST", "/v1/metrics/reset", {})

    def images(self) -> list:
        """Checkpoints envs can be created from: installed images and saved states."""
        return self.call("GET", "/v1/checkpoints")

    def tasks(self, domain: str | None = None) -> list:
        q = "?" + urllib.parse.urlencode({"domain": domain}) if domain else ""
        return self.call("GET", "/v1/tasks" + q)

    def task(self, task_id: str) -> dict:
        """An OSWorld task JSON from the host's pinned checkout, by id or unique prefix (>= 8 characters)."""
        return self.call("GET", f"/v1/tasks/{urllib.parse.quote(task_id)}")

    def envs(self) -> list:
        return [Env(self, r) for r in self.call("GET", "/v1/slots") if r.get("state") != "destroyed"]

    def _task(self, task):
        if task is None or isinstance(task, dict):
            return task
        if isinstance(task, str) and os.path.exists(task):
            with open(task) as f:
                return json.load(f)
        if isinstance(task, str):
            return self.task(task)
        raise BadRequest(400, f"task must be an OSWorld JSON dict, a path, or a task id; got {type(task).__name__}")

    # envs -------------------------------------------------------------------------------------------------------------
    def create(self, image: str = DEFAULT_IMAGE, task=None, *, from_state=None, setup: bool | None = None) -> "Env":
        """A new env from an image (or from a saved state), with an optional OSWorld task whose setup runs now and after
        every reset. Raises RestoreFailed / GateFailed / TaskSetupFailed if it did not come up ready."""
        ck = str(from_state) if from_state is not None else image
        body = {"checkpoint": ck, "task": self._task(task)}
        if setup is not None:
            body["setup"] = setup
        e = Env(self, self.call("POST", "/v1/slots", body))
        e._check_up()
        return e

    def create_many(self, n: int, image: str = DEFAULT_IMAGE, task=None, *, from_state=None, workers: int = 8) -> list:
        """n envs created concurrently. All ready -> the list; otherwise BatchError(ok=[ready envs], failed=[(i, error)])."""
        t = self._task(task)
        res = self.map(lambda i: self.create(image, t, from_state=from_state), range(n), workers=workers)
        ok = [r for r in res if isinstance(r, Env)]
        failed = [(i, r) for i, r in enumerate(res) if not isinstance(r, Env)]
        if failed:
            raise BatchError(ok, failed)
        return ok

    @staticmethod
    def map(fn, items, workers: int = 8) -> list:
        """fn over items in threads; each result is the return value or the exception it raised (never dropped)."""
        items = list(items)

        def run(x):
            try:
                return fn(x)
            except Exception as e:  # noqa: BLE001
                return e
        with cf.ThreadPoolExecutor(max(1, min(workers, len(items) or 1))) as ex:
            return list(ex.map(run, items))


def _action(a):
    if isinstance(a, str):
        return {"type": "pyautogui", "code": a}
    return a


def _png_size(png: bytes):
    if len(png) >= 24 and png[12:16] == b"IHDR":
        return struct.unpack(">II", png[16:24])
    return None, None


class State(str):
    """A saved state (checkpoint id). Pass to client.create(from_state=...)."""


class StepResult(dict):
    """The guest's reply to an action ('result'), the episode's validity at that moment, and the step's seconds.
    `observation` is set when the step asked for a screenshot."""

    @property
    def result(self):
        return self.get("result")

    @property
    def seconds(self):
        return self.get("seconds")

    @property
    def observation(self):
        return self.get("observation")


class Observation:
    def __init__(self, png: bytes, meta: dict):
        self.png, self.meta = png, meta
        self.width, self.height = _png_size(png)

    def __repr__(self):
        return f"<drums.Observation {self.width}x{self.height} env={self.meta.get('env')} episode={self.meta.get('episode')} {self.meta.get('verdict')}>"


class Reward:
    """A checker result with the episode's gate verdict. Use `score` for training only when `valid` is True."""

    def __init__(self, d: dict):
        self.raw = d
        self.env_id = d.get("slot")
        self.episode = d.get("episode")
        self.task_id = d.get("task_id")
        self.score = d.get("score")
        self.valid = bool(d.get("valid"))
        self.validity = d.get("validity") or {}
        self.verdict = self.validity.get("verdict")
        self.bad_gates = self.validity.get("gates", [])
        self.checker_error = d.get("checker_error")
        self.gates = d.get("gates")
        self.seconds = d.get("seconds")

    def __repr__(self):
        return f"<drums.Reward env={self.env_id} ep={self.episode} score={self.score} valid={self.valid}{' ERR' if self.checker_error else ''}>"


class Env:
    """One isolated desktop environment (a drumsd slot)."""

    def __init__(self, client: Client, record: dict):
        self.client, self.record = client, record
        self.steps = 0

    # identity ---------------------------------------------------------------------------------------------------------
    id = property(lambda self: self.record["id"])
    episode = property(lambda self: self.record.get("episode"))
    state = property(lambda self: self.record.get("state"))
    validity = property(lambda self: self.record.get("validity") or {})
    task = property(lambda self: self.record.get("task"))

    def refresh(self) -> dict:
        self.record = self.client.call("GET", f"/v1/slots/{self.id}")
        return self.record

    def _check_up(self):
        if self.record.get("state") == "ready":
            return
        st, err = self.record.get("state"), (self.record.get("last_error") or self.record.get("reason") or "")
        if st == "quarantined":
            raise GateFailed(self.record)
        if "task setup" in err:
            raise TaskSetupFailed(self.record)
        raise RestoreFailed(self.record)

    # episode ----------------------------------------------------------------------------------------------------------
    def reset(self) -> "Env":
        """New episode from this env's checkpoint (and the task's setup, for an env created with a task)."""
        self.record = self.client.call("POST", f"/v1/slots/{self.id}/reset")
        self.steps = 0
        self._check_up()
        return self

    def step(self, action, screenshot: bool = False) -> StepResult:
        """pyautogui code (str) or an action dict: {"type": "pyautogui", "code"}, {"type": "execute", "command", "shell"},
        {"type": "launch", "command", "shell"}, {"type": "sleep", "seconds"}."""
        r = self.client.call("POST", f"/v1/slots/{self.id}/step", {"action": _action(action), "screenshot": screenshot})
        self.steps += 1
        if r.get("screenshot_png_b64"):
            png = base64.b64decode(r.pop("screenshot_png_b64"))
            r["screenshot_png"] = png
            r["observation"] = Observation(png, {"env": self.id, "episode": r.get("episode"), "verdict": (r.get("validity") or {}).get("verdict")})
        return StepResult(r)

    def observe(self) -> Observation:
        png = self.screenshot()
        rec = self.refresh()
        t = rec.get("task") or {}
        return Observation(png, {"env": self.id, "episode": rec.get("episode"), "state": rec.get("state"),
                                 "verdict": (rec.get("validity") or {}).get("verdict"), "task_id": t.get("id"),
                                 "instruction": t.get("instruction"), "steps": self.steps, "t": time.time()})

    def screenshot(self) -> bytes:
        return self.client.call("GET", f"/v1/slots/{self.id}/screenshot", raw=True)

    def gates(self) -> dict:
        return self.client.call("GET", f"/v1/slots/{self.id}/gates")

    def valid(self, timeout: float = 30.0) -> bool:
        """Wait until the episode's verdict is no longer pending; True only when every gate passed."""
        t0 = time.monotonic()
        while True:
            v = self.gates()["validity"]["verdict"]
            if v != "pending":
                return v == "valid"
            if time.monotonic() - t0 > timeout:
                return False
            time.sleep(0.25)

    def wait_valid(self, timeout: float = 60.0) -> "Env":
        """Block until this episode's last gate (processes alive 5 s after resume) has resolved. Raises GateFailed if the
        episode is invalid. Act after this when your first actions may end processes: a process that exits in the first
        seconds after a restore cannot be told apart from a bad restore, so the episode would be flagged invalid."""
        t0 = time.monotonic()
        while True:
            g = self.gates()
            v = g["validity"]["verdict"]
            if v == "valid":
                return self
            if v == "invalid":
                raise GateFailed(self.refresh())
            if time.monotonic() - t0 > timeout:
                raise DrumsError(0, f"env {self.id}: verdict still pending after {timeout} s")
            time.sleep(0.25)

    def verify(self, strict: bool = True) -> Reward:
        """Run the task's checker on the current state. The server waits for the episode's last gate first.
        strict=True: raises InvalidEpisode (reward attached, flagged) if the episode is not valid, CheckerError if the
        checker failed. strict=False: returns the Reward; check `.valid` before using `.score`.
        Note: OSWorld checkers may run a postconfig (save, close a window) that changes the desktop."""
        r = Reward(self.client.call("POST", f"/v1/slots/{self.id}/verify", {}))
        if strict and r.checker_error is not None:
            raise CheckerError(r)
        if strict and not r.valid:
            raise InvalidEpisode(r)
        return r

    def load_task(self, task) -> "Env":
        """Load an OSWorld task into this ready env (setup runs now and after every reset)."""
        self.record = self.client.call("POST", f"/v1/slots/{self.id}/task", {"task": self.client._task(task)})
        return self

    def metrics(self) -> dict:
        """This env's metrics: op latency (reset/step/fork/verify/...), episodes by verdict, failures and retries by
        cause, rewards, lifetime, and its share of the host cost by slot-seconds."""
        return self.client.call("GET", f"/v1/slots/{self.id}/metrics")

    # branching --------------------------------------------------------------------------------------------------------
    def save(self, name: str | None = None) -> State:
        """Checkpoint the current state (this env keeps running). client.create(from_state=...) starts from it."""
        return State(self.client.call("POST", f"/v1/slots/{self.id}/capture", {"id": name} if name else {})["id"])

    def fork(self, n: int, strict: bool = True) -> list:
        """Checkpoint this state and start n independent children from it (concurrently); this env keeps running.
        strict=True: if any child failed, raises ForkError(ok=[ready children], failed=[EnvFailed...])."""
        r = self.client.call("POST", f"/v1/slots/{self.id}/branch", {"n": n})
        ok, failed = [], []
        for c in r["children"]:
            if "error" in c:
                failed.append(DrumsError(0, c["error"]))
                continue
            e = Env(self.client, c)
            try:
                e._check_up()
                ok.append(e)
            except EnvFailed as x:
                failed.append(x)
                if not strict:
                    ok.append(e)
        if failed and strict:
            raise ForkError(ok, failed, (r.get("checkpoint") or {}).get("id"))
        return ok

    branch = fork      # 0.1 name

    # lifecycle --------------------------------------------------------------------------------------------------------
    def quarantine(self, reason: str = "client") -> dict:
        self.record = self.client.call("POST", f"/v1/slots/{self.id}/quarantine", {"reason": reason})
        return self.record

    def release(self) -> dict:
        """Kill a quarantined env's processes; it becomes down and a reset restores it."""
        self.record = self.client.call("POST", f"/v1/slots/{self.id}/release", {})
        return self.record

    def close(self) -> dict:
        if self.record.get("state") in ("destroyed",):
            return self.record
        self.record = self.client.call("DELETE", f"/v1/slots/{self.id}")
        return self.record

    def guest(self) -> "GuestClient":
        """An OSWorld guest-server-like transport (execute, launch, screenshot, screen_size, platform) over the API."""
        return GuestClient(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.close()
        except DrumsError:
            pass

    def __repr__(self):
        return f"<drums.Env {self.id} {self.state} episode={self.episode} {self.validity.get('verdict')}>"


class GuestClient:
    """The GuestClient protocol of drums-lab rlbackends (execute/launch/screenshot/screen_size/platform), through drumsd."""

    def __init__(self, env: Env):
        self.env, self.base, self.timeout = env, f"{env.client.url}/v1/slots/{env.id}", env.client.timeout

    def execute(self, command, shell: bool = False, allow_rc=(0,), timeout=None) -> dict:
        r = self.env.step({"type": "execute", "command": command, "shell": shell})["result"]
        if allow_rc is not None and r.get("returncode") not in allow_rc:
            raise GuestError(0, f"guest command rc={r.get('returncode')} err={str(r.get('error'))[:300]!r}")
        return r

    def launch(self, command, shell: bool = False) -> None:
        self.env.step({"type": "launch", "command": command, "shell": shell})

    def screenshot(self) -> bytes:
        return self.env.screenshot()

    def screen_size(self):
        w, h = _png_size(self.screenshot())
        return w, h

    def platform(self, timeout=10) -> str:
        return "Linux" if self.env.refresh().get("state") == "ready" else "unavailable"


# ---- 0.1 compatibility ----------------------------------------------------------------------------------------------
def env(task: dict | None = None, *, from_state: str | None = None, checkpoint: str = "clean", url: str = DEFAULT_URL,
        token: str | None = None) -> Env:
    """0.1 entry point: a new env (`from_state` wins over `checkpoint`). Prefer drums.connect(...).create(...)."""
    c = Client(url, token if token is not None else os.environ.get("DRUMS_TOKEN"))
    rec = c.call("POST", "/v1/slots", {"checkpoint": from_state or checkpoint, "task": task})
    e = Env(c, rec)
    e._check_up()
    return e
