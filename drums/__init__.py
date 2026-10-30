"""Drums SDK: isolated desktop environments for computer-use RL (standard library only).

    import drums
    with drums.connect("http://127.0.0.1:7070", token) as client:              # closes the envs it made on exit
        with client.create(image="osworld-ubuntu", task="035f41ba") as env:  # OSWorld task id, path or JSON dict
            env.step("pyautogui.click(100, 200)")                             # pyautogui code, or an action dict
            obs = env.observe()                                               # PNG + metadata
            with env.fork(8, keep_state=False) as kids:                       # 8 independent copies of this state
                print(kids.state)                                             # the saved state the fork made
                r = kids[0].verify()                                          # task checker -> Reward (raises if invalid)
            state = env.save()                                                # a saved state; client.create(from_state=state)
            print(r.score, r.valid, env.metrics()["usage"])

Names: the SDK says env, saved state, fork and save; the HTTP API and raw records say slot (`/v1/slots`,
`"slot": "s104"`), checkpoint (`/v1/checkpoints`), branch (`/branch`) and capture (`/capture`). They are the same things.

Validity: every create/reset/fork child starts an episode with a gate report (isolation, mount table, network,
cgroup limits, state hash, processes alive after resume, ...). An episode is valid only when every gate passed and
the env was never quarantined (`Reward.valid`; the reasons are in `Reward.invalid_reasons`). `verify()` always
carries the verdict; by default it raises InvalidEpisode instead of returning a reward from an invalid episode.
Retries: drumsd itself retries only failures that happened before any restored process ran; the SDK retries only GET
requests while drumsd is unreachable. Everything else is raised with the gate report.

Cleanup: envs and saved states have leases on the server (default: reaped after 30 min unused; `idle_timeout=` and
`ttl=` on create/fork/save change that, 0 = never), so a crashed trainer does not hold the host forever. The client
also closes the envs it created when the process exits (atexit, and SIGTERM/SIGHUP when their handlers were the
default); `connect(..., close_on_exit=False)` turns that off.
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

import atexit
import signal
import threading
import warnings

from .errors import (ActionFailed, AuthError, BadRequest, BatchError, CheckerError, CheckpointStale, Conflict, DaemonUnavailable, DrumsError, EnvFailed,  # noqa: F401
                     EnvGone, ForkError, GateFailed, GuestError, InvalidEpisode, NotFound, NotQuiescent, Overloaded, RestoreFailed, TaskSetupFailed)

__version__ = "0.2.4"
DEFAULT_URL = os.environ.get("DRUMS_URL") or os.environ.get("DRUMSD_URL") or "http://127.0.0.1:7070"
DEFAULT_IMAGE = "osworld-ubuntu"

__all__ = ["ActionFailed", "connect", "Client", "Env", "Reward", "Observation", "StepResult", "State", "ForkResult", "env", "__version__",
           "DrumsError", "AuthError", "BadRequest", "NotFound", "EnvGone", "Conflict", "DaemonUnavailable", "GuestError", "EnvFailed",
           "RestoreFailed", "GateFailed", "TaskSetupFailed", "ForkError", "BatchError", "InvalidEpisode", "CheckerError", "NotQuiescent", "Overloaded",
           "CheckpointStale"]

_STATUS = {400: BadRequest, 401: AuthError, 404: NotFound, 409: Conflict, 410: EnvGone, 422: BadRequest, 429: Overloaded, 502: GuestError,
           503: DaemonUnavailable}


def connect(url: str | None = None, token: str | None = None, *, timeout: float = 900.0, retry_s: float = 60.0, check: bool = True,
            close_on_exit: bool = True, handle_signals: bool = True) -> "Client":
    """A client for one drumsd. `url`/`token` default to $DRUMS_URL / $DRUMS_TOKEN. With check=True the token is tried
    at once (AuthError / DaemonUnavailable here rather than on the first create).
    close_on_exit=True: the envs this client created and did not close are closed when the process exits (atexit) or
    when the client is used as a context manager and the block ends. handle_signals=True: SIGTERM and SIGHUP exit
    through that cleanup too, when their handler is still the default one (never replaces a handler you installed;
    main thread only). A `kill -9` cannot be caught: the server-side leases reap those envs."""
    c = Client(url or DEFAULT_URL, token if token is not None else os.environ.get("DRUMS_TOKEN"), timeout=timeout, retry_s=retry_s,
               close_on_exit=close_on_exit, handle_signals=handle_signals)
    if check:
        c.health()
    return c


def _exit_on_signal(signum, frame):  # noqa: ARG001
    raise SystemExit(128 + signum)


def _install_signal_exit():
    """SIGTERM/SIGHUP -> SystemExit, so `finally`, context managers and atexit run (only over default handlers)."""
    if threading.current_thread() is not threading.main_thread():
        return
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None and signal.getsignal(sig) == signal.SIG_DFL:
            try:
                signal.signal(sig, _exit_on_signal)
            except (ValueError, OSError):
                pass


class Client:
    """A connection to one drumsd (use drums.connect). Creates envs, lists them, and reads health and metrics."""

    def __init__(self, url: str = DEFAULT_URL, token: str | None = None, timeout: float = 900.0, retry_s: float = 60.0,
                 close_on_exit: bool = False, handle_signals: bool = False):
        self.url, self.timeout, self.retry_s = url.rstrip("/"), timeout, retry_s
        self.token = token if token is not None else os.environ.get("DRUMS_TOKEN")
        self._owned: dict[str, None] = {}          # ids of envs this client created and has not closed (insertion order)
        self._owned_lock = threading.Lock()
        self.close_on_exit = close_on_exit
        if close_on_exit:
            atexit.register(self._close_at_exit)
        if handle_signals:
            _install_signal_exit()

    # cleanup ----------------------------------------------------------------------------------------------------------
    def _own(self, env_id):
        if env_id:
            with self._owned_lock:
                self._owned[env_id] = None

    def _disown(self, env_id):
        with self._owned_lock:
            self._owned.pop(env_id, None)

    def owned_envs(self) -> list:
        """Ids of the envs this client created and has not closed."""
        with self._owned_lock:
            return list(self._owned)

    def close(self, timeout: float = 30.0) -> list:
        """Close every env this client created and has not closed (in parallel). Envs that are already gone are
        skipped. Returns [(env_id, error or None)]. Saved states are not deleted here (their leases, or
        fork(keep_state=False), or client.delete_state, take care of them)."""
        ids = self.owned_envs()
        if not ids:
            return []

        def one(i):
            try:
                self.call("DELETE", f"/v1/slots/{urllib.parse.quote(i)}", timeout=timeout)
            except (EnvGone, NotFound):
                pass
            except Exception as e:  # noqa: BLE001
                return i, e
            finally:
                self._disown(i)
            return i, None
        try:
            with cf.ThreadPoolExecutor(min(16, len(ids))) as ex:
                return list(ex.map(one, ids))
        except RuntimeError:          # at interpreter shutdown no new thread can start: one by one, within a budget
            deadline = time.monotonic() + max(timeout, 1.0)
            out = []
            for i in self.owned_envs():
                if time.monotonic() > deadline:
                    out.append((i, DrumsError(0, "not closed: the close budget ran out (its lease will reap it)")))
                    continue
                out.append(one(i))
            return out

    def _close_at_exit(self):
        # bounded: an unreachable drumsd must not hang the exit (the envs' leases reap whatever is left)
        if self.close_on_exit:
            try:
                self.close(timeout=10.0)
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

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
                    cls = _STATUS.get(e.code, DrumsError)
                    if e.code == 409 and isinstance(body_j, dict) and body_j.get("code") == "not_quiescent":
                        cls = NotQuiescent
                    if e.code == 409 and isinstance(body_j, dict) and body_j.get("code") == "checkpoint_stale":
                        cls = CheckpointStale
                    if e.code == 409 and "slot is destroyed" in str(msg):      # drumsd < 0.4.2
                        cls = EnvGone
                    raise cls(e.code, msg, body_j) from None
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
        failures and retries by cause, rewards, and cost (host price x wall time, per valid episode / verified reward).
        The cost counts the whole window's wall time, idle time included: the window starts when drumsd started or at
        the last reset_metrics(), so call reset_metrics() at the start of a run to measure that run."""
        return self.call("GET", "/v1/metrics")

    def reset_metrics(self) -> dict:
        """Close the current metrics window (its ledger goes to drumsd's event log) and start a new one."""
        return self.call("POST", "/v1/metrics/reset", {})

    def images(self) -> list:
        """What envs can be created from: installed images and saved states (API: GET /v1/checkpoints)."""
        return self.call("GET", "/v1/checkpoints")

    def delete_state(self, state) -> dict:
        """Delete a saved state (from save() or fork(); API: checkpoint): its template server stops and its images are
        freed. Refused (Conflict) while an env made from it still exists. drumsd also deletes a state by itself when
        its lease runs out (default: 30 min with no env made from it) or, for fork(keep_state=False), when its last
        env closes."""
        return self.call("DELETE", f"/v1/checkpoints/{urllib.parse.quote(str(state))}")

    def tasks(self, domain: str | None = None) -> list:
        q = "?" + urllib.parse.urlencode({"domain": domain}) if domain else ""
        return self.call("GET", "/v1/tasks" + q)

    def task(self, task_id: str) -> dict:
        """An OSWorld task JSON from the host's pinned checkout, by id or unique prefix (>= 8 characters)."""
        return self.call("GET", f"/v1/tasks/{urllib.parse.quote(task_id)}")

    def envs(self) -> list:
        """The live envs on the host (drumsd >= 0.4 lists only those; older ones list every env, so destroyed ones are
        dropped here). These Env objects are not owned by this client: close_on_exit does not close them."""
        return [Env(self, r) for r in self.call("GET", "/v1/slots") if r.get("state") != "destroyed"]

    def env_records(self, include_destroyed: bool = False, page_size: int = 500):
        """Every env record, live ones only or with the destroyed ones (their records are kept), as a generator over
        pages of `page_size` (GET /v1/slots?limit=&after=&include_destroyed=). An older drumsd answers with one list."""
        after = None
        while True:
            q = {"include_destroyed": "true" if include_destroyed else "false", "limit": str(page_size)}
            if after:
                q["after"] = after
            r = self.call("GET", "/v1/slots?" + urllib.parse.urlencode(q))
            if isinstance(r, list):              # drumsd < 0.4: no paging, every env
                yield from (x for x in r if include_destroyed or x.get("state") != "destroyed")
                return
            yield from r.get("items") or []
            after = r.get("next_after")
            if not after:
                return

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
    def create(self, image: str = DEFAULT_IMAGE, task=None, *, from_state=None, setup: bool | None = None,
               idle_timeout: float | None = None, ttl: float | None = None) -> "Env":
        """A new env from an image (or from a saved state), with an optional OSWorld task whose setup runs now and after
        every reset. Raises RestoreFailed / GateFailed / TaskSetupFailed if it did not come up ready (the env still
        exists on the server, down or quarantined: close it, or let its lease reap it).
        idle_timeout / ttl (seconds): the env's lease. drumsd destroys it after `idle_timeout` s with no request naming
        it, or `ttl` s after it was created. None = the server's default (30 min idle, no TTL); 0 = no limit."""
        ck = str(from_state) if from_state is not None else image
        body = {"checkpoint": ck, "task": self._task(task)}
        if setup is not None:
            body["setup"] = setup
        if idle_timeout is not None:
            body["idle_timeout_s"] = idle_timeout
        if ttl is not None:
            body["ttl_s"] = ttl
        e = Env(self, self.call("POST", "/v1/slots", body))
        self._own(e.record.get("id"))
        e._check_up()
        return e

    def create_many(self, n: int, image: str = DEFAULT_IMAGE, task=None, *, from_state=None, workers: int = 8,
                    idle_timeout: float | None = None, ttl: float | None = None) -> list:
        """n envs created concurrently. All ready -> the list; otherwise BatchError(ok=[ready envs], failed=[(i, error)])."""
        t = self._task(task)
        res = self.map(lambda i: self.create(image, t, from_state=from_state, idle_timeout=idle_timeout, ttl=ttl), range(n), workers=workers)
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
    """A saved state (API: checkpoint id). Pass to client.create(from_state=...); free it with client.delete_state()."""


class ForkResult(list):
    """What fork() returns: the ready children, as the plain list fork() always returned, plus the fork's saved state.

    `state` is the saved state the fork made (a State). With fork(..., keep_state=False) drumsd deletes it once its
    last env is closed; otherwise it stays until client.delete_state(state) or its lease (default 30 min with no env
    made from it). `checkpoint` is the server's full record of it, `failed` the children that did not come up
    (strict=False only), `seconds` the fork's time. As a context manager it closes the children at the end."""

    def __init__(self, children, state=None, checkpoint=None, failed=None, seconds=None, client=None):
        super().__init__(children)
        self.state = State(state) if state else None
        self.checkpoint = checkpoint or {}
        self.failed = failed or []
        self.seconds = seconds
        self._client = client

    def close(self) -> None:
        """Close every child (already closed ones are skipped)."""
        for k in self:
            try:
                getattr(k, "sync", k).close()      # an aio AsyncEnv is closed through its sync Env
            except DrumsError:
                pass

    def delete_state(self) -> dict | None:
        """Close the children, then delete the saved state now (NotFound/EnvGone-safe)."""
        self.close()
        if self.state and self._client is not None:
            try:
                return self._client.delete_state(self.state)
            except NotFound:
                return None
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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
        self.checker_skipped = d.get("checker_skipped")     # e.g. "quarantined: client": the checker did not run
        self.gates = d.get("gates")
        self.seconds = d.get("seconds")
        # why the episode is not valid, in words (empty when valid). drumsd >= 0.4.2 sends them; for an older one
        # they are derived, and a quarantined env's "valid" verdict (the old inconsistency) is reported as invalid.
        self.invalid_reasons = list(d.get("invalid_reasons") or [])
        if not self.valid and not self.invalid_reasons:
            if d.get("state") not in (None, "ready"):
                self.invalid_reasons.append(f"env is {d.get('state')}")
            self.invalid_reasons += [f"gate {g} {'pending' if self.verdict == 'pending' else 'failed'}" for g in self.bad_gates]
        if not self.valid and self.verdict == "valid":
            self.verdict = "invalid"

    def __repr__(self):
        return f"<drums.Reward env={self.env_id} ep={self.episode} score={self.score} valid={self.valid}{' ERR' if self.checker_error else ''}>"


class Env:
    """One isolated desktop environment (API: a slot, `/v1/slots/{id}`)."""

    def __init__(self, client: Client, record: dict):
        self.client, self.record = client, record
        self.steps = 0
        self._last_fork = None
        self.last_state = None     # the saved state the last fork() of this env made (a State), or None

    @property
    def last_fork(self):
        """Deprecated (0.2.4): the last fork()'s raw server reply. Use the ForkResult fork() returns (`.state`,
        `.checkpoint`) or `env.last_state`."""
        warnings.warn("Env.last_fork is deprecated; use the ForkResult fork() returns (.state, .checkpoint) or env.last_state",
                      DeprecationWarning, stacklevel=2)
        return self._last_fork

    @last_fork.setter
    def last_fork(self, v):
        self._last_fork = v

    # identity ---------------------------------------------------------------------------------------------------------
    id = property(lambda self: self.record["id"])
    episode = property(lambda self: self.record.get("episode"))
    state = property(lambda self: self.record.get("state"))
    validity = property(lambda self: self.record.get("validity") or {})
    task = property(lambda self: self.record.get("task"))
    from_state = property(lambda self: State(self.record.get("checkpoint")))     # the checkpoint this env restores to

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
        """New episode from the image or saved state this env was made from (and the task's setup, for an env created
        with a task)."""
        self.record = self.client.call("POST", f"/v1/slots/{self.id}/reset")
        self.steps = 0
        self._last_fork = None
        self._check_up()
        return self

    def step(self, action, screenshot: bool = False, check: bool = True) -> StepResult:
        """pyautogui code (str) or an action dict: {"type": "pyautogui", "code"}, {"type": "execute", "command", "shell"},
        {"type": "launch", "command", "shell"}, {"type": "sleep", "seconds"}. Screen 1920x1080, origin top left, pixels.
        pyautogui and execute run inside the env as its desktop user `user` (not root), with OSWorld's prefix
        (`import pyautogui, time`). See docs/actions-and-observations.md.
        check=True (default): an action that ran and exited non-zero raises ActionFailed with its exit status, output
        and error; check=False returns the result as is. A guest that does not answer or times out (120 s) raises
        GuestError either way."""
        a = _action(action)
        r = self.client.call("POST", f"/v1/slots/{self.id}/step", {"action": a, "screenshot": screenshot})
        self.steps += 1
        res = r.get("result")
        if check and isinstance(res, dict) and res.get("returncode") not in (None, 0):
            raise ActionFailed(res, a)
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
        strict=True: raises InvalidEpisode (reward attached, flagged, with `.invalid_reasons`) if the episode is not
        valid, CheckerError if the checker failed. strict=False: returns the Reward; use `.score` only when `.valid`.
        On a quarantined env the checker is not run: score None, valid False, reason "quarantined: <why>".
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
        cause, rewards, lifetime, and its share of the host cost by env-seconds (API field: slot_seconds)."""
        return self.client.call("GET", f"/v1/slots/{self.id}/metrics")

    # branching --------------------------------------------------------------------------------------------------------
    def wait_quiescent(self, timeout: float | None = None, window: float | None = None) -> dict:
        """Block until this env's process tree is safe to capture: no process or thread starting or exiting for
        `window` seconds (server default 1 s), no task exiting, no unreaped zombie. fork() and save() already do this
        themselves, so calling it is optional (useful to time it, or to wait without capturing). Returns the wait
        record; raises NotQuiescent after `timeout` seconds (server default 30 s)."""
        body = {k: v for k, v in (("timeout_s", timeout), ("window_s", window)) if v is not None}
        return self.client.call("POST", f"/v1/slots/{self.id}/quiescent", body)

    def save(self, name: str | None = None, quiesce_timeout: float | None = None, *, idle_timeout: float | None = None,
             ttl: float | None = None) -> State:
        """Save the current state (API: capture; this env keeps running). client.create(from_state=...) starts from it.
        Safe right after an action: drumsd waits until the tree is quiescent and checks that the dump is that tree
        (NotQuiescent if it cannot within `quiesce_timeout`, server default 30 s; nothing is captured then).
        idle_timeout / ttl (seconds): the state's lease. drumsd deletes it after `idle_timeout` s with no env made from
        it (counted from its creation or its last env's close), or `ttl` s after it was made once no env uses it.
        None = the server's default (30 min idle, no TTL); 0 = no limit."""
        body = {"id": name} if name else {}
        if quiesce_timeout is not None:
            body["quiesce_timeout_s"] = quiesce_timeout
        if idle_timeout is not None:
            body["idle_timeout_s"] = idle_timeout
        if ttl is not None:
            body["ttl_s"] = ttl
        return State(self.client.call("POST", f"/v1/slots/{self.id}/capture", body)["id"])

    def fork(self, n: int, strict: bool = True, quiesce_timeout: float | None = None, *, keep_state: bool = True,
             idle_timeout: float | None = None, ttl: float | None = None, state_idle_timeout: float | None = None,
             state_ttl: float | None = None) -> ForkResult:
        """Save this state and start n independent children from it, concurrently (API: branch); this env keeps
        running. Safe right after an action (no sleep needed): the capture waits for a quiescent tree, see save().
        Returns a ForkResult: the ready children (a list, as before) with `.state`, the saved state the fork made
        (also `env.last_state`). keep_state=False: drumsd deletes that state as soon as its last env is closed, so a
        loop does not leak one state (~600 MB of RAM) per fork; keep_state=True (default): it stays until
        client.delete_state(state) or its lease. idle_timeout/ttl: the children's leases (see create);
        state_idle_timeout/state_ttl: the state's (see save).
        strict=True: if any child failed, raises ForkError(ok=[ready children], failed=[EnvFailed...], state)."""
        body = {"n": n}
        if quiesce_timeout is not None:
            body["quiesce_timeout_s"] = quiesce_timeout
        if not keep_state:
            body["delete_state_with_children"] = True
        for k, v in (("idle_timeout_s", idle_timeout), ("ttl_s", ttl), ("state_idle_timeout_s", state_idle_timeout), ("state_ttl_s", state_ttl)):
            if v is not None:
                body[k] = v
        r = self.client.call("POST", f"/v1/slots/{self.id}/branch", body)
        self._last_fork = r
        ck = r.get("checkpoint") or {}
        self.last_state = State(ck["id"]) if ck.get("id") else None
        ok, failed = [], []
        for c in r["children"]:
            if "error" in c:
                failed.append(DrumsError(0, c["error"]))
                continue
            self.client._own(c.get("id"))
            e = Env(self.client, c)
            try:
                e._check_up()
                ok.append(e)
            except EnvFailed as x:
                failed.append(x)
                if not strict:
                    ok.append(e)
        if failed and strict:
            raise ForkError(ok, failed, ck.get("id"))
        return ForkResult(ok, ck.get("id"), ck, failed, r.get("seconds"), self.client)

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
        """Destroy this env (its records are kept on the host). A no-op on an env that is already gone (closed here or
        elsewhere, or reaped by its lease)."""
        if self.record.get("state") in ("destroyed",):
            self.client._disown(self.id)
            return self.record
        try:
            self.record = self.client.call("DELETE", f"/v1/slots/{self.id}")
        except (EnvGone, NotFound) as e:
            self.record = {**self.record, "state": "destroyed", "closed_reason": getattr(e, "closed_reason", None) or str(e)}
        self.client._disown(self.id)
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
        r = self.env.step({"type": "execute", "command": command, "shell": shell}, check=False)["result"]
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
def env(task: dict | None = None, *, from_state: str | None = None, checkpoint: str = DEFAULT_IMAGE, url: str = DEFAULT_URL,
        token: str | None = None) -> Env:
    """0.1 entry point: a new env (`from_state` wins over `checkpoint`, default the installed image "osworld-ubuntu";
    before 0.2.4 the default was "clean", which no install has). Prefer drums.connect(...).create(...)."""
    c = Client(url, token if token is not None else os.environ.get("DRUMS_TOKEN"))
    rec = c.call("POST", "/v1/slots", {"checkpoint": from_state or checkpoint, "task": task})
    e = Env(c, rec)
    e._check_up()
    return e
