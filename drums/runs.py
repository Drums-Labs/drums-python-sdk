"""The run object: a training (or evaluation) run the SDK opens, feeds and closes, so the console's Runs page can show
it (task set, config hash, step counter, reward, zero-advantage share, rollouts, desktops busy, cost) instead of a
list of anonymous computers (audit A-36).

    with client.run("qwen27b-d2", kind="training", task_set=task_set_id, config=cfg) as run:
        for step in range(steps):
            ...
            run.step(step, reward=mean_reward, zero_advantage_share=zero_adv, rollouts=n, desktops_busy=busy)

Wire (gateway routes; the SDK feature-detects them and is a no-op with one warning on a gateway without them):

    POST /v1/runs                 {name, kind, task_set_id | task_set, config, config_hash, label, desktops_provisioned?}
                                  -> {run_id | id, label, ...} (Idempotency-Key header: a retried open returns the same
                                  run). The reply's `label` is the one in use: when a running run already holds the
                                  label the gateway gives this one `<label>-<4 of its id>`, and desktops get that.
    PATCH /v1/runs/{id}           {steps: [{step, reward, zero_advantage_share, rollouts, desktops_busy,
                                            desktops_provisioned, at, extra?}]}
    POST /v1/runs/{id}/close      {status: completed | failed | cancelled, error, steps_total}

A 409 on PATCH or close means the gateway has ended the run (closed, or marked lost after 30 min without a
heartbeat): the SDK stops sending for it, with one warning, instead of retrying the same points forever.

Buffering: `step()` only records the point; the buffer is sent at most once per `flush_s` seconds (default 30), from
inside the next `step()` call that finds the interval passed, and once more on close. A failed send keeps the
points for the next one (at most `MAX_BUFFER` points are kept; the oldest go first and are counted in `dropped`).
Nothing here raises into the training loop unless `strict=True`: a run the gateway cannot record is a warning, not
a crashed job. An AuthError (the key itself is wrong) is always raised, since every other call would fail too.

Desktops: while a run is open, every `client.create()` / `create_many()` sends `X-Drums-Run-Label: <name>` (the
gateway stores it as the computer's label, shown and filterable in the console; fork children inherit it) and,
once the gateway gave the run an id, `X-Drums-Run-Id: <id>`.
"""
from __future__ import annotations

import atexit
import datetime as _dt
import hashlib
import json
import re
import threading
import time
import urllib.parse
import uuid
import warnings

from .errors import AuthError, BadRequest, Conflict, DrumsError, NotFound

DEFAULT_FLUSH_S = 30.0
MAX_BUFFER = 10_000
LABEL_MAX = 120          # the gateway keeps at most 120 characters of a run label
RUN_KINDS = ("training", "evaluation", "eval", "rl")   # the gateway stores evaluation/eval as eval, rl as training
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def is_uuid(v) -> bool:
    return isinstance(v, str) and bool(_UUID.match(v))


def config_hash(config) -> str | None:
    """sha256 of the canonical JSON of `config` (sorted keys, no spaces); None when there is no config."""
    if config is None:
        return None
    text = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunNotSupported(DrumsError):
    """The gateway has no run routes yet (or the key has neither runs:write nor computers:write); raised only with
    strict=True."""


class Run:
    """One run on the gateway. Make it with `client.run(name, kind=..., ...)` (or `client.open_run`); use it as a
    context manager, or call `open()` and `close()` yourself.

    `id` is the gateway's run id (None until opened, or when the gateway cannot record runs), `supported` False
    when the gateway has no run routes (then every method is a no-op), `steps_total` the number of step() calls,
    `last` the last point, `dropped` points lost to a full buffer, `ended` the reason when the gateway answered 409
    (the run ended there) and the SDK stopped sending. `label` is the gateway's label once the run is open.
    `desktops_provisioned` (optional) is how many desktops the trainer provisioned for the run, sent on open; a
    step() can send its own count too."""

    def __init__(self, client, name: str, kind: str = "training", *, task_set=None, config=None, flush_s: float = DEFAULT_FLUSH_S,
                 strict: bool = False, label: str | None = None, desktops_provisioned: int | None = None, clock=time.monotonic):
        if not isinstance(name, str) or not name.strip():
            raise BadRequest(400, "a run needs a name")
        if kind not in RUN_KINDS:
            raise BadRequest(400, f"kind must be one of {', '.join(RUN_KINDS)}; got {kind!r}")
        self.client, self.name, self.kind = client, name.strip(), kind
        self.task_set = task_set
        self.config = config
        self.config_hash = config_hash(config)
        self.flush_s = float(flush_s)
        self.strict = strict
        self.label = (label or self.name)[:LABEL_MAX]
        self.desktops_provisioned = desktops_provisioned
        self.ended: str | None = None            # set when the gateway answered 409: the run ended there
        self.id: str | None = None
        self.record: dict = {}
        self.supported: bool | None = None       # None: not tried yet
        self.status = "new"                      # new -> open -> closed
        self.steps_total = 0
        self.last: dict | None = None
        self.dropped = 0
        self.idempotency_key = str(uuid.uuid4())
        self._buf: list = []
        self._lock = threading.Lock()
        self._clock = clock
        self._last_flush: float | None = None
        self._warned: set = set()
        self._labelled = False
        self._timeout = None                     # per-call timeout; short at interpreter exit

    # ---- helpers ------------------------------------------------------------------------------------------------------
    def _warn(self, key: str, msg: str):
        if key in self._warned:
            return
        self._warned.add(key)
        warnings.warn(f"drums run {self.name!r}: {msg}", RuntimeWarning, stacklevel=3)

    def _unsupported(self, why: str, e: Exception | None = None):
        self.supported = False
        if self.strict:
            raise RunNotSupported(getattr(e, "status", 0) or 0, f"the gateway cannot record runs: {why}") from e
        self._warn("unsupported", f"not recorded on the gateway ({why}); training goes on, desktops still get the run label")

    def headers(self) -> dict:
        """The headers a create sends while this run is open."""
        h = {"X-Drums-Run-Label": self.label}
        if self.id:
            h["X-Drums-Run-Id"] = self.id
        return h

    # ---- lifecycle ----------------------------------------------------------------------------------------------------
    def open(self) -> "Run":
        """POST /v1/runs. Idempotent. On a gateway without run routes the run is a no-op (one warning)."""
        if self.status == "new":
            self.status = "open"
            self.client._push_run(self)
            self._labelled = True
            atexit.register(self._at_exit)
        if self.id or self.supported is False:
            return self
        body = {"name": self.name, "kind": self.kind, "config": self.config, "config_hash": self.config_hash, "label": self.label}
        if self.desktops_provisioned is not None:
            body["desktops_provisioned"] = int(self.desktops_provisioned)
        if self.task_set is not None:
            body["task_set_id" if is_uuid(self.task_set) else "task_set"] = self.task_set
        try:
            r = self.client.call("POST", "/v1/runs", body, headers={"Idempotency-Key": self.idempotency_key},
                                 timeout=self._timeout)
        except AuthError:
            self.status = "closed"
            self._release()
            raise
        except (BadRequest, NotFound) as e:          # 400/404/405/422: today's gateway (RunSpec only) or a plain drumsd
            self._unsupported(f"POST /v1/runs answered {e.status}: {e.message}", e)
            return self
        except DrumsError as e:
            if e.status == 403:
                self._unsupported(f"forbidden: {e.message} (the key needs runs:write; computers:write also suffices)", e)
                return self
            if self.strict:
                raise
            self._warn("open", f"could not open the run yet ({e}); retrying at the next flush")
            return self
        rid = (r or {}).get("run_id") or (r or {}).get("id") if isinstance(r, dict) else None
        if not rid:
            self._unsupported("POST /v1/runs gave no run id")
            return self
        self.id, self.record, self.supported = str(rid), r, True
        got = r.get("label")
        if isinstance(got, str) and got.strip():
            self.label = got.strip()[:LABEL_MAX]         # the label in use (another running run may have held ours)
        return self

    def step(self, step: int, reward: float | None = None, zero_advantage_share: float | None = None, rollouts: int | None = None,
             desktops_busy: int | None = None, desktops_provisioned: int | None = None, **extra) -> None:
        """Record one trainer step. Sent with the others at most once per `flush_s` seconds (and on close)."""
        if self.status == "closed":
            self._warn("closed", "step() after close() is ignored")
            return
        if self.status == "new":
            self.open()
        point = {"step": int(step), "at": _now_iso()}
        for k, v in (("reward", reward), ("zero_advantage_share", zero_advantage_share), ("rollouts", rollouts), ("desktops_busy", desktops_busy),
                     ("desktops_provisioned", desktops_provisioned)):
            if v is not None:
                point[k] = v
        if extra:
            point["extra"] = extra
        with self._lock:
            self.steps_total += 1
            self.last = point
            if self.supported is False or self.ended:
                return
            self._buf.append(point)
            if len(self._buf) > MAX_BUFFER:
                over = len(self._buf) - MAX_BUFFER
                del self._buf[:over]
                self.dropped += over
            due = self._last_flush is None or self._clock() - self._last_flush >= self.flush_s
        if due:
            self.flush()

    def flush(self) -> bool:
        """Send the buffered points now (PATCH /v1/runs/{id}). True when nothing is left to send."""
        if self.supported is False or self.ended:
            return True
        if not self.id:
            self.open()
            if not self.id:
                self._last_flush = self._clock()
                return self.supported is False
        with self._lock:
            pts, self._buf = self._buf, []
            self._last_flush = self._clock()
        if not pts:
            return True
        try:
            self.client.call("PATCH", f"/v1/runs/{urllib.parse.quote(self.id)}", {"steps": pts}, timeout=self._timeout)
            return True
        except AuthError:
            raise
        except Conflict as e:
            self._end_remote(e, len(pts))
            return True
        except (BadRequest, NotFound) as e:
            if e.status in (404, 405) and "method not allowed" in str(e.message):
                self._unsupported(f"PATCH /v1/runs/{{id}} answered {e.status}: {e.message}", e)
                return True
            with self._lock:
                self._buf[:0] = pts
            if self.strict:
                raise
            self._warn("flush", f"could not send steps ({e}); keeping them for the next flush")
            return False
        except DrumsError as e:
            with self._lock:
                self._buf[:0] = pts
            if self.strict:
                raise
            self._warn("flush", f"could not send steps ({e}); keeping them for the next flush")
            return False

    def close(self, status: str = "completed", error: str | None = None) -> None:
        """Flush, then POST /v1/runs/{id}/close. Idempotent. `status`: completed, failed or cancelled."""
        if status not in ("completed", "failed", "cancelled"):
            raise BadRequest(400, f"status must be completed, failed or cancelled; got {status!r}")
        if self.status == "closed":
            return
        try:
            if self.status == "new":
                return
            self.flush()
            if self.id and self.supported and not self.ended:
                body = {"status": status, "error": (error or None) and str(error)[:1000], "steps_total": self.steps_total}
                try:
                    self.client.call("POST", f"/v1/runs/{urllib.parse.quote(self.id)}/close", body, timeout=self._timeout)
                except AuthError:
                    raise
                except Conflict as e:              # already ended there (closed elsewhere, or marked lost)
                    self.ended = self.ended or f"{e.message}"
                except DrumsError as e:
                    if self.strict:
                        raise
                    self._warn("close", f"could not close the run on the gateway ({e}); it stays open there until it times out")
        finally:
            self.status = "closed"
            self._release()

    def _end_remote(self, e, unsent: int = 0):
        """A 409 on PATCH: the gateway ended the run (closed elsewhere, or lost after 30 min without a heartbeat).
        Drop what is buffered and stop sending; retrying would get the same answer."""
        with self._lock:
            lost, self._buf = unsent + len(self._buf), []
        self.ended = str(e.message)
        self._warn("ended", f"the gateway says the run has ended ({e.message}); its steps are no longer sent"
                   + (f" ({lost} buffered points dropped)" if lost else ""))

    def _release(self):
        if self._labelled:
            self.client._pop_run(self)
            self._labelled = False
        try:
            atexit.unregister(self._at_exit)
        except Exception:  # noqa: BLE001
            pass

    def _at_exit(self):
        # bounded: a gateway that does not answer must not hang the exit
        self._timeout = 10.0
        try:
            self.close("failed", "the process exited without closing the run")
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "Run":
        return self.open()

    def __exit__(self, et, ev, tb):
        if et is None:
            self.close("completed")
        elif issubclass(et, KeyboardInterrupt):
            self.close("cancelled", "KeyboardInterrupt")
        else:
            self.close("failed", f"{et.__name__}: {ev}")
        return False

    def __repr__(self):
        return f"<drums.Run {self.name!r} id={self.id} {self.status} steps={self.steps_total}{'' if self.supported is not False else ' (not recorded)'}{' (ended on the gateway)' if self.ended else ''}>"
