"""Typed errors. Every error carries what the server said; errors about an environment carry its record and gate report."""
from __future__ import annotations


class DrumsError(Exception):
    """Base class. `status` is the HTTP status (0 when no reply), `message` the server's error text."""

    def __init__(self, status: int, message: str, body=None):
        super().__init__(f"drumsd HTTP {status}: {message}" if status else message)
        self.status, self.message, self.body = status, message, body


class AuthError(DrumsError):
    """401: missing or wrong token."""


class BadRequest(DrumsError):
    """400: the request is wrong (bad task JSON, bad id, n out of range)."""


class NotFound(DrumsError):
    """404: no such env, image, state or task."""


class Conflict(DrumsError):
    """409: the env's state does not allow this operation (e.g. step on a quarantined env)."""


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


class DaemonUnavailable(DrumsError):
    """drumsd did not answer (down, restarting, network). For a POST the outcome is unknown: call env.refresh()."""


class GuestError(DrumsError):
    """502: the desktop's guest server did not answer or refused the action."""


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
    """Some fork children did not come up. `ok` are the ready children (usable), `failed` the EnvFailed errors."""

    def __init__(self, ok: list, failed: list, checkpoint: str | None):
        self.ok, self.failed, self.checkpoint = ok, failed, checkpoint
        super().__init__(0, f"fork: {len(ok)} children ready, {len(failed)} failed: " + "; ".join(str(e) for e in failed[:3]))


class BatchError(DrumsError):
    """Some of a batch failed. `ok` are the results that worked, `failed` a list of (index, exception)."""

    def __init__(self, ok: list, failed: list):
        self.ok, self.failed = ok, failed
        super().__init__(0, f"batch: {len(ok)} ok, {len(failed)} failed: " + "; ".join(f"[{i}] {e}" for i, e in failed[:3]))


class InvalidEpisode(DrumsError):
    """A reward from an episode whose gates did not all pass. `reward` holds the score, flagged `valid=False`.
    Do not train on it. (Also raised by the legacy client when an env is not ready.)"""

    def __init__(self, status_or_reward=0, message: str | None = None):
        if isinstance(status_or_reward, int):
            self.reward = None
            super().__init__(status_or_reward, message or "invalid episode")
        else:
            self.reward = status_or_reward
            r = status_or_reward
            super().__init__(0, message or f"env {r.env_id} episode {r.episode} is {r.verdict} (failed/pending gates: {r.bad_gates}); score {r.score} is not a usable reward")


class CheckerError(DrumsError):
    """The task's checker raised or could not run: there is no score. `reward` has the details."""

    def __init__(self, reward):
        self.reward = reward
        super().__init__(0, f"checker error on env {reward.env_id}: {reward.checker_error}")
