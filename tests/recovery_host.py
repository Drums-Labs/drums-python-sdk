"""Failure-recovery tests against a real installed drumsd (run on the host as root):

  A. drumsd SIGKILLed while idle: systemd restarts it; the SDK's GETs ride over the restart; running envs are adopted
     (same init pid, still ready) and take steps. Their current episode ends invalid (`no_app_crash`: it started before
     the new daemon's crash watch, which cannot vouch for the time drumsd was down); the next reset is valid.
  B. drumsd SIGKILLed in the middle of a reset: the SDK's POST surfaces DaemonUnavailable (outcome unknown); after the
     restart the env is `down` (reconciled, its tree killed); the next reset is valid.
  C. a restore that crashes (every process of the slot's cgroups killed while CRIU restores): the reset surfaces as
     RestoreFailed or GateFailed with the record; the env is down or quarantined, never ready; release+reset or a new env
     from the same image is valid; the failure is in the metrics by cause.
  D. template server SIGKILLed: the next reset finds it gone, starts a new one before restoring (or retries if it died
     mid-request), and the episode is valid.

    sudo python3 recovery_host.py OUT.json
"""
import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drums  # noqa: E402

URL = "http://127.0.0.1:7070"
TOKEN = open("/etc/drums/token").read().strip()
R = {"tests": {}}


def drumsd_pid():
    out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "drumsd"], capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def wait_new_pid(old, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        p = drumsd_pid()
        if p and p != old:
            return p
        time.sleep(0.1)
    raise RuntimeError("drumsd did not restart")


def check(name, cond, **ev):
    R["tests"].setdefault(name, []).append({"ok": bool(cond), **ev})
    print(f"{'PASS' if cond else 'FAIL'} {name}: {json.dumps(ev, default=str)[:400]}", flush=True)


def slot_cgroups(env):
    pid = env.refresh()["init_pid"]
    cg = [l[3:] for l in open(f"/proc/{pid}/cgroup").read().splitlines() if l.startswith("0::")][0]
    slot_cg = os.path.dirname("/sys/fs/cgroup" + cg)
    return [d for d in glob.glob(slot_cg + "/*.scope")]


def main(out):
    c = drums.connect(URL, TOKEN, retry_s=60)
    img = "osworld-ubuntu"

    # ---- A
    e1, e2 = c.create(img), c.create(img)
    e1.wait_valid(); e2.wait_valid()
    pids = {e.id: e.refresh()["init_pid"] for e in (e1, e2)}
    old = drumsd_pid()
    t0 = time.monotonic()
    os.kill(old, signal.SIGKILL)
    h = c.health()                                  # retried GET across the restart
    new = wait_new_pid(old)
    check("A_restart_seen_by_sdk", h["ok"] and new != old, seconds=round(time.monotonic() - t0, 2), old=old, new=new)
    for e in (e1, e2):
        r = e.refresh()
        check("A_env_adopted", r["state"] == "ready" and r["init_pid"] == pids[e.id],
              env=e.id, state=r["state"], pid=r["init_pid"], verdict=r["validity"]["verdict"])
        s = e.step({"type": "execute", "command": "echo alive", "shell": True})
        check("A_env_steps_after_restart", s.result.get("output", "").strip() == "alive", env=e.id)
        g = e.gates()
        crash = next((x for x in g["gates"] if x["gate"] == "no_app_crash"), None)
        check("A_adopted_episode_is_not_vouched_for", g["validity"]["verdict"] == "invalid" and crash is not None
              and any("before the crash watch" in s for s in crash["detail"]["incomplete"]), env=e.id, validity=g["validity"])
    e2.reset()
    check("A_next_reset_valid", e2.valid(timeout=30), env=e2.id, episode=e2.episode)

    # ---- B
    old = drumsd_pid()
    err = {}

    def do_reset():
        try:
            e1.reset()
        except Exception as x:  # noqa: BLE001
            err["e"] = x
    th = threading.Thread(target=do_reset)
    th.start()
    # kill as soon as the journal shows the reset in progress (a reset takes well under a second on this host)
    rec = f"/var/lib/drums/journal/slots/{e1.id}.json"
    t0 = time.monotonic()
    seen = None
    while time.monotonic() - t0 < 10:
        try:
            st = json.load(open(rec)).get("state")
        except (OSError, ValueError):
            st = None
        if st == "resetting":
            seen = round(time.monotonic() - t0, 3)
            break
        time.sleep(0.002)
    os.kill(old, signal.SIGKILL)
    R["B_killed_after_s"] = seen
    th.join()
    wait_new_pid(old)
    check("B_post_surfaces_unavailable", isinstance(err.get("e"), drums.DaemonUnavailable), error=repr(err.get("e"))[:300])
    r = e1.refresh()
    check("B_env_reconciled_down", r["state"] == "down", state=r["state"], reason=r.get("reason"))
    e1.reset()
    check("B_next_reset_valid", e1.valid(timeout=30), episode=e1.episode)
    check("B_other_env_untouched", e2.refresh()["state"] == "ready" and e2.record["init_pid"] == pids[e2.id])

    # ---- C
    cgs = slot_cgroups(e2)
    stop = threading.Event()
    kills = [0]

    def killer():
        while not stop.is_set():
            for d in cgs:
                try:
                    if open(d + "/cgroup.procs").read().strip():
                        open(d + "/cgroup.kill", "w").write("1")
                        kills[0] += 1
                except OSError:
                    pass
            time.sleep(0.02)
    th = threading.Thread(target=killer)
    m0 = c.metrics()["usage"]["failures_by_cause"]
    th.start()
    time.sleep(0.3)
    exc = None
    try:
        e2.reset()
    except drums.EnvFailed as x:
        exc = x
    finally:
        stop.set(); th.join()
    check("C_crashed_restore_is_an_error", isinstance(exc, (drums.RestoreFailed, drums.GateFailed)), error=type(exc).__name__ if exc else None,
          state=exc.state if exc else e2.state, cause=str(exc.cause)[:300] if exc else None, kills=kills[0],
          attempts=(exc.record.get("cost") if exc else None))
    r = e2.refresh()
    check("C_env_not_ready", r["state"] in ("down", "quarantined"), state=r["state"])
    m1 = c.metrics()["usage"]["failures_by_cause"]
    check("C_failure_in_metrics", sum(m1.values()) > sum(m0.values()), before=m0, after=m1)
    if r["state"] == "quarantined":
        e2.release()
    e2.reset()
    check("C_reset_after_crash_valid", e2.valid(timeout=30), episode=e2.episode)
    e3 = c.create(img)
    check("C_new_env_from_same_image_valid", e3.valid(timeout=30), env=e3.id)

    # ---- D
    srv = [int(open(p).read()) for p in glob.glob("/var/lib/drums/state/ps-0.pid")]
    if srv:
        os.kill(srv[0], signal.SIGKILL)
        time.sleep(0.5)
        r0 = c.metrics()["usage"]["retries_by_cause"]
        e3.reset()
        r1 = c.metrics()["usage"]["retries_by_cause"]
        check("D_template_server_restarted_and_valid", e3.valid(timeout=30), attempts=e3.record["cost"]["attempts"], retries_before=r0, retries_after=r1)
    else:
        check("D_template_server_restarted_and_valid", False, error="no ps-0.pid")

    for e in (e1, e2, e3):
        e.close()
    R["metrics"] = c.metrics()
    json.dump(R, open(out, "w"), indent=1, default=str)
    n = sum(len(v) for v in R["tests"].values())
    p = sum(1 for v in R["tests"].values() for x in v if x["ok"])
    print(f"{p}/{n} checks passed")
    return 0 if p == n else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "recovery.json"))
