"""Typed errors. Every error carries what the server said; errors about an environment carry its record and gate report."""
from __future__ import annotations


class DrumsError(Exception):
    """Base class. `status` is the HTTP status (0 when no reply), `message` the server's error text."""

    def __init__(self, status: int, message: str, body=None):
        super().__init__(f"drumsd HTTP {status}: {message}" if status else message)
        self.status, self.message, self.body = status, message, body


class AuthError(DrumsError):
    """401: missing or wrong token."""


class InsecureTransport(DrumsError):
    """Refused before any request: a token would travel over plain HTTP to a host that is not loopback. The message
    names the host and the opt-ins (`--allow-insecure-http`, `DRUMS_ALLOW_INSECURE_HTTP=1`, `allow_insecure_http=True`);
    it never carries the token."""

    def __init__(self, host: str, scheme: str = "http"):
        super().__init__(0, f"refusing to send the Drums token over plain {scheme or 'http'} to {host}: use an https:// URL, "
                            "or opt in with --allow-insecure-http (drums mcp), DRUMS_ALLOW_INSECURE_HTTP=1, "
                            "or Client(..., allow_insecure_http=True)")
        self.host, self.scheme = host, scheme


class BadRequest(DrumsError):
    """400: the request is wrong (bad task JSON, bad id, n out of range)."""


class TaskUnsupported(BadRequest):
    """422 `task_unsupported`: the task needs the agent itself to act as root, and agents get no privilege escalation
    in this execution profile (https://console.drums.run/docs/concepts/tasks-and-verifiers). Nothing was started. `task_id` and `reason` say which and why.
    A task whose setup alone needs root is supported: drumsd runs that step as trusted provisioning."""

    def __init__(self, status: int, message: str, body=None):
        super().__init__(status, message, body)
        b = body if isinstance(body, dict) else {}
        self.task_id = b.get("task_id")
        self.reason = b.get("reason")


class NotFound(DrumsError):
    """404: no such env, image, state or task."""


class EnvGone(NotFound):
    """410 `env_gone`: the env was destroyed (closed by a client, or reaped by its lease). Every operation on it
    raises this; `env.close()` on it is a no-op. Create a new env to go on. `closed_reason` says why when it was not a
    client's close (e.g. "lease: idle 1800 s"). A subclass of NotFound, so `except NotFound` still catches it.
    (Against drumsd < 0.4.2, the 409 "slot is destroyed" reply is raised as this too.)"""

    def __init__(self, status: int, message: str, body=None):
        super().__init__(status, message, body)
        b = body if isinstance(body, dict) else {}
        self.env_id = b.get("env")
        self.closed_reason = b.get("closed_reason")


class Conflict(DrumsError):
    """409: the env's state does not allow this operation (e.g. step on a quarantined env; code `wrong_state`).
    A destroyed env raises EnvGone instead."""


class Overloaded(DrumsError):
    """429 `overloaded`: drumsd refused a create, save or fork because the host is near its memory limit (or under
    memory pressure, or the state tmpfs is nearly full). Nothing was started; try again later. `reason` is memory,
    memory_pressure or state_space; `admission` has the numbers (available, in flight, needed, floor)."""

    def __init__(self, status: int, message: str, body=None):
        super().__init__(status, message, body)
        b = body if isinstance(body, dict) else {}
        self.reason = b.get("reason")
        self.admission = b.get("admission") or {}


class NotQuiescent(Conflict):
    """409 `not_quiescent`: fork/save/wait_quiescent found no moment when the env's process tree was safe to capture
    (a process or thread kept starting or exiting, a task was exiting, or a zombie was not reaped) within the timeout.
    Nothing was captured and the env is still ready. `unstable` lists what was unstable
    ({kind, pid, tid, comm}, host pids); `quiescence` is the full record of the wait and of every dump attempt."""

    def __init__(self, status: int, message: str, body=None):
        super().__init__(status, message, body)
        b = body if isinstance(body, dict) else {}
        self.unstable = b.get("unstable") or []
        self.quiescence = b.get("quiescence") or {}


class CheckpointStale(Conflict):
    """409 `checkpoint_stale`: the installed image's checkpoint was made by other runtime sources or another image
    inspection than this drumsd runs with (e.g. an upgrade that changed how the desktop logs in). drumsd refuses envs
    from it; run the installer again, which provisions the origin anew. `provenance` is the checkpoint's, `current` drumsd's."""

    def __init__(self, status: int, message: str, body=None):
        super().__init__(status, message, body)
        b = body if isinstance(body, dict) else {}
        self.provenance = b.get("provenance")
        self.current = b.get("current")


class DaemonUnavailable(DrumsError):
    """drumsd did not answer (down, restarting, network). For a POST the outcome is unknown: call env.refresh()."""


class GuestError(DrumsError):
    """502: the desktop's guest server did not answer or refused the action."""


class ActionFailed(GuestError):
    """step(): the action ran in the desktop and exited non-zero (a pyautogui action that raised, a command that
    failed). Before 0.2.2 step() returned such a result without raising, so a failed action looked like a done one.
    `returncode`, `output` and `error` are the guest's; `result` is the whole guest reply. step(..., check=False)
    returns it instead (for commands whose exit status is the answer, e.g. `test -f`)."""

    def __init__(self, result: dict, action):
        self.result = result or {}
        self.returncode = self.result.get("returncode")
        self.output = self.result.get("output")
        self.error = self.result.get("error")
        self.action = action
        super().__init__(0, f"action exited {self.returncode}: {str(self.error or '')[-400:]!r}", self.result)


class EnvFailed(DrumsError):
    """A create/reset/fork child did not come up ready. `record` is the env record, `gates` its gate report,
    `cause` the failed gate or error. The env is down or quarantined on the server; nothing was retried beyond
    what drumsd retries itself (failures before any restored process ran), and those attempts are in record['cost']."""

    def __init__(self, record: dict, message: str | None = None):
        self.record = record
        self.gates = record.get("gates", {})
        self.state = record.get("state")
        self.env_id = record.get("id")
        self.cause = record.get("reason") or record.get("last_error") or record.get("state")
        super().__init__(0, message or f"env {self.env_id} is {self.state}: {self.cause}", record)


class RestoreFailed(EnvFailed):
    """The restore failed before the desktop ran (the env is down; a reset tries again from scratch)."""


class GateFailed(EnvFailed):
    """A gate failed: the env is quarantined; its state is not trusted. Close it (or release + reset)."""


class TaskSetupFailed(EnvFailed):
    """The task's OSWorld setup failed after a good restore."""


class ForkError(DrumsError):
    """Some fork children did not come up. `ok` are the ready children (usable), `failed` the EnvFailed errors,
    `state` the saved state the fork made (also `checkpoint`, its 0.2 name): delete it with client.delete_state(state)
    once its envs are closed, unless the fork had keep_state=False (then drumsd deletes it)."""

    def __init__(self, ok: list, failed: list, checkpoint: str | None):
        self.ok, self.failed, self.checkpoint = ok, failed, checkpoint
        self.state = checkpoint
        super().__init__(0, f"fork: {len(ok)} children ready, {len(failed)} failed: " + "; ".join(str(e) for e in failed[:3]))


class BatchError(DrumsError):
    """Some of a batch failed. `ok` are the results that worked, `failed` a list of (index, exception)."""

    def __init__(self, ok: list, failed: list):
        self.ok, self.failed = ok, failed
        super().__init__(0, f"batch: {len(ok)} ok, {len(failed)} failed: " + "; ".join(f"[{i}] {e}" for i, e in failed[:3]))


class InvalidEpisode(DrumsError):
    """A reward from an episode that is not valid (a gate failed or is still pending, or the env was quarantined).
    `reward` holds it, flagged `valid=False`, with `reward.invalid_reasons`. Do not train on it.
    (Also raised by the legacy client when an env is not ready.)"""

    def __init__(self, status_or_reward=0, message: str | None = None):
        if isinstance(status_or_reward, int):
            self.reward = None
            super().__init__(status_or_reward, message or "invalid episode")
        else:
            self.reward = status_or_reward
            r = status_or_reward
            why = "; ".join(getattr(r, "invalid_reasons", None) or []) or f"failed/pending gates: {r.bad_gates}"
            super().__init__(0, message or f"env {r.env_id} episode {r.episode} is invalid ({why}); score {r.score} is not a usable reward")


class CheckerError(DrumsError):
    """The task's checker raised or could not run: there is no score. `reward` has the details."""

    def __init__(self, reward):
        self.reward = reward
        super().__init__(0, f"checker error on env {reward.env_id}: {reward.checker_error}")
