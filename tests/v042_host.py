"""0.4.2 acceptance on an installed host (run as root, against the real drumsd): every fix the outside user needed.

    sudo python3 python/tests/v042_host.py OUT.json [--only validity,gone,lease,fork_state,state_lease,kill9,template_logs]

  validity       a quarantined env is invalid everywhere: verify(strict=False) valid=False with verdict "invalid" and the
                 failed gate not_quarantined; strict verify raises InvalidEpisode naming the quarantine; the checker
                 does not run; /v1/metrics moves the episode from valid to invalid (and a quarantine while the verdict
                 is still pending counts it invalid once, never valid)
  gone           any operation on a destroyed env is 410 env_gone (SDK EnvGone, a NotFound); close() on it is a no-op;
                 an id that never existed is 404
  lease          idle timeout and TTL per create; a used env outlives its idle timeout; the reaper's metric and health
  fork_state     fork() returns the saved state; keep_state=False deletes it when the last child closes
  state_lease    a saved state's idle timeout deletes it
  kill9          a trainer killed with SIGKILL leaves envs and states that its leases reap
  template_logs  no template-server log is left in the state dir after forks and deletes
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import drums  # noqa: E402

URL = "http://127.0.0.1:7070"
TOKEN = open("/etc/drums/token").read().strip()
TASK = "e0df059f"           # OSWorld os task (rename a file on the Desktop): offline setup, cheap checker
STATE_DIR = "/var/lib/drums/state"
c = drums.connect(URL, TOKEN, close_on_exit=False)
R = {}


def raw(method, path):
    req = urllib.request.Request(URL + path, method=method, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def episodes():
    m = c.metrics()["usage"]
    return dict(m["episodes"]), dict(m["verify"])


def live_ids():
    return {e.id for e in c.envs()}          # GET /v1/slots names no env: it does not count as a use


def state_ids():
    return {x["id"] for x in c.images()}


def wait_until(pred, limit, step=1.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        if pred():
            return round(time.time() - t0, 1)
        time.sleep(step)
    return None


def check(name, ok, **kw):
    R.setdefault(name, {"checks": []})["checks"].append({"ok": bool(ok), **kw})
    print(("PASS " if ok else "FAIL ") + name + " " + json.dumps(kw, default=str)[:400], flush=True)


def t_validity():
    task = c.task(TASK)
    # A. valid episode (counted valid), then quarantined by the client
    e = c.create(task=task).wait_valid()
    ep0, v0 = episodes()
    e.quarantine("client")
    r = e.verify(strict=False)
    check("validity", not r.valid and r.verdict == "invalid" and r.bad_gates == ["not_quarantined"], case="A reward",
          valid=r.valid, verdict=r.verdict, bad_gates=r.bad_gates, reasons=r.invalid_reasons)
    check("validity", r.score is None and r.checker_error is None and r.checker_skipped == "quarantined: client", case="A checker not run",
          score=r.score, checker_skipped=r.checker_skipped, checker_error=r.checker_error)
    check("validity", r.invalid_reasons == ["quarantined: client"], case="A reasons", reasons=r.invalid_reasons)
    try:
        e.verify()
        check("validity", False, case="A strict did not raise")
    except drums.InvalidEpisode as x:
        msg = str(x)
        check("validity", "quarantined: client" in msg and "is valid" not in msg, case="A strict message", message=msg)
    g = e.gates()
    check("validity", g["validity"] == {"verdict": "invalid", "gates": ["not_quarantined"]}, case="A gates endpoint", validity=g["validity"])
    ep1, v1 = episodes()
    check("validity", ep1["valid"] - ep0["valid"] == -1 and ep1["invalid"] - ep0["invalid"] == 1, case="A metrics move valid -> invalid",
          before=ep0, after=ep1)
    check("validity", v1["valid"] == v0["valid"] and v1.get("skipped_quarantined", 0) - v0.get("skipped_quarantined", 0) == 2,
          case="A metrics: no verified reward, 2 skipped", before=v0, after=v1)
    e.close()
    # B. quarantined while the verdict is still pending: counted invalid once, never valid
    ep0, _ = episodes()
    e = c.create(task=task)
    pending = e.validity.get("verdict")
    e.quarantine("client")
    time.sleep(8)          # the alive gate resolves 5 s after ready: it must not count the episode valid
    ep1, _ = episodes()
    r = e.verify(strict=False)
    check("validity", pending == "pending" and ep1["valid"] == ep0["valid"] and ep1["invalid"] - ep0["invalid"] == 1 and not r.valid,
          case="B quarantine while pending", verdict_at_create=pending, before=ep0, after=ep1, reward_valid=r.valid, verdict=r.verdict)
    e.close()
    # C. control: a valid episode is valid and its checker runs
    e = c.create(task=task).wait_valid()
    r = e.verify(strict=False)
    check("validity", r.valid and r.verdict == "valid" and r.score is not None and r.invalid_reasons == [], case="C control",
          valid=r.valid, score=r.score, reasons=r.invalid_reasons)
    e.close()


def t_gone():
    e = c.create()
    eid = e.id
    e.close()
    stale = drums.Env(c, {"id": eid, "state": "ready"})
    for what, fn in (("step", lambda: stale.step("pyautogui.press('shift')")), ("reset", stale.reset), ("verify", stale.verify),
                     ("fork", lambda: stale.fork(1)), ("screenshot", stale.screenshot)):
        try:
            fn()
            check("gone", False, op=what, error="no error")
        except drums.EnvGone as x:
            check("gone", x.status == 410 and isinstance(x, drums.NotFound) and (x.body or {}).get("code") == "env_gone", op=what, status=x.status)
        except drums.DrumsError as x:
            check("gone", False, op=what, error=f"{type(x).__name__}: {x}")
    rec = stale.close()
    check("gone", rec.get("state") == "destroyed", op="close() on a destroyed env is a no-op", state=rec.get("state"))
    st, body = raw("DELETE", f"/v1/slots/{eid}")
    check("gone", st == 200 and body["state"] == "destroyed", op="DELETE is idempotent", status=st)
    try:
        drums.Env(c, {"id": "s99999", "state": "ready"}).step("x")
        check("gone", False, op="unknown id")
    except drums.EnvGone:
        check("gone", False, op="unknown id raised EnvGone")
    except drums.NotFound as x:
        check("gone", x.status == 404, op="unknown id is 404 NotFound", status=x.status)


def t_lease():
    h = c.health()
    reap_every = h["leases"]["defaults"]["reap_interval_s"]
    check("lease", h["leases"]["defaults"]["env_idle_timeout_s"] == 1800.0, case="default idle timeout", defaults=h["leases"]["defaults"])
    reaped0 = dict(h["leases"]["reaped"])
    idle = c.create(idle_timeout=20)
    ttl = c.create(ttl=25, idle_timeout=0)
    busy = c.create(idle_timeout=20)
    check("lease", idle.record["lease"] == {"idle_timeout_s": 20.0, "ttl_s": None} and idle.record.get("lease_expires_epoch"), case="record shows the lease",
          lease=idle.record.get("lease"), expires=idle.record.get("lease_expires_epoch"))
    t0 = time.time()
    gone_idle = gone_ttl = None
    while time.time() - t0 < 20 + 25 + reap_every + 15:
        busy.refresh()                          # a use every ~2 s: never idle
        live = live_ids()
        if gone_idle is None and idle.id not in live:
            gone_idle = round(time.time() - t0, 1)
        if gone_ttl is None and ttl.id not in live:
            gone_ttl = round(time.time() - t0, 1)
        if gone_idle and gone_ttl:
            break
        time.sleep(2)
    check("lease", gone_idle is not None and 20 <= gone_idle <= 20 + reap_every + 10, case="idle env reaped", seconds=gone_idle)
    check("lease", gone_ttl is not None and 25 <= gone_ttl <= 25 + reap_every + 10, case="ttl env reaped", seconds=gone_ttl)
    check("lease", busy.id in live_ids(), case="a used env outlives its idle timeout", alive_after_s=round(time.time() - t0, 1))
    _, rec = raw("GET", f"/v1/slots/{idle.id}")
    check("lease", rec.get("state") == "destroyed" and rec.get("closed_reason") == "lease: idle 20 s", case="closed_reason", closed_reason=rec.get("closed_reason"))
    try:
        idle.step("pyautogui.press('shift')")
        check("lease", False, case="step on reaped env did not raise")
    except drums.EnvGone as x:
        check("lease", "lease: idle 20 s" in str(x), case="step on a reaped env: EnvGone with the reason", message=str(x))
    busy.close()
    h = c.health()
    d = {k: v - reaped0.get(k, 0) for k, v in h["leases"]["reaped"].items()}
    check("lease", d.get("env:idle", 0) >= 1 and d.get("env:ttl", 0) >= 1, case="health counts", delta=d)
    prom = urllib.request.urlopen(urllib.request.Request(URL + "/metrics", headers={"Authorization": f"Bearer {TOKEN}"})).read().decode()
    lines = [x for x in prom.splitlines() if x.startswith("drumsd_lease_reaped_total")]
    check("lease", any('kind="env"' in x and 'reason="idle"' in x for x in lines), case="prometheus metric", lines=lines)


def t_fork_state():
    p = c.create()
    kids = p.fork(2, keep_state=False)
    st = kids.state
    check("fork_state", isinstance(kids, list) and len(kids) == 2 and st and st == p.last_state and st in state_ids(),
          case="fork returns the state", state=st)
    kids[0].close()
    check("fork_state", st in state_ids(), case="kept while a child lives")
    kids[1].close()
    s = wait_until(lambda: st not in state_ids(), 20, 0.5)
    check("fork_state", s is not None, case="keep_state=False: deleted after the last child closed", seconds=s)
    kids = p.fork(1)                    # default keep_state=True: stays
    st2 = kids.state
    kids.close()
    time.sleep(3)
    check("fork_state", st2 in state_ids(), case="keep_state=True: kept after the children closed")
    c.delete_state(st2)
    p.close()


def t_state_lease():
    p = c.create()
    reap_every = c.health()["leases"]["defaults"]["reap_interval_s"]
    st = p.save(idle_timeout=20)
    p.close()
    s = wait_until(lambda: st not in state_ids(), 20 + reap_every + 15, 1)
    check("state_lease", s is not None and s >= 15, case="idle saved state deleted", seconds=s)


def t_kill9():
    code = textwrap.dedent(f"""
        import json, sys, time
        sys.path.insert(0, {os.path.join(HERE, '..')!r})
        import drums
        c = drums.connect({URL!r}, {TOKEN!r})
        p = c.create(idle_timeout=30)
        kids = p.fork(2, keep_state=False, idle_timeout=30)
        k2 = p.fork(1, idle_timeout=30, state_idle_timeout=30)
        print(json.dumps({{"envs": [p.id] + [k.id for k in kids] + [k.id for k in k2], "states": [kids.state, k2.state]}}), flush=True)
        while True:
            for e in [p] + list(kids) + list(k2):
                e.step({{"type": "sleep", "seconds": 0.5}})
    """)
    pr = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    ids = json.loads(pr.stdout.readline())
    time.sleep(10)
    pr.send_signal(signal.SIGKILL)
    pr.wait()
    t_kill = time.time()
    envs, states = set(ids["envs"]), set(ids["states"])
    check("kill9", envs <= live_ids() and states <= state_ids(), case="trainer killed with SIGKILL; its envs and states are live", **ids)
    reap_every = c.health()["leases"]["defaults"]["reap_interval_s"]
    se = wait_until(lambda: not (envs & live_ids()), 30 + reap_every + 30, 1)
    ss = wait_until(lambda: not (states & state_ids()), 30 + reap_every + 30, 1)
    check("kill9", se is not None, case="envs reaped", seconds_after_kill=round(time.time() - t_kill, 1) if se is not None else None)
    check("kill9", ss is not None, case="states deleted (children_closed and idle)", seconds_after_kill=round(time.time() - t_kill, 1) if ss is not None else None)


def t_template_logs():
    loose = [n for n in os.listdir(STATE_DIR) if n.startswith("template-server-")]
    check("template_logs", loose == [], case="no template-server log in the state dir", loose=loose[:10], n=len(loose))
    held = []
    for ck in c.images():
        d = os.path.join(STATE_DIR, f"slot{ck['slot']}")
        held.append({"checkpoint": ck["id"], "logs_in_its_dir": sorted(n for n in os.listdir(d) if n.startswith("template-server-")) if os.path.isdir(d) else None})
    check("template_logs", all(h["logs_in_its_dir"] for h in held), case="each live checkpoint's server logs are in its own dir", held=held)


TESTS = {"validity": t_validity, "gone": t_gone, "lease": t_lease, "fork_state": t_fork_state, "state_lease": t_state_lease,
         "kill9": t_kill9, "template_logs": t_template_logs}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--only", default=",".join(TESTS))
    a = ap.parse_args()
    t0 = time.time()
    for n in a.only.split(","):
        ts = time.time()
        try:
            TESTS[n]()
        except Exception as x:  # noqa: BLE001
            import traceback
            check(n, False, error=f"{type(x).__name__}: {x}", tb=traceback.format_exc()[-1500:])
        R[n]["seconds"] = round(time.time() - ts, 1)
        R[n]["pass"] = all(x["ok"] for x in R[n]["checks"])
    R["pass"] = all(R[n]["pass"] for n in a.only.split(","))
    R["seconds"] = round(time.time() - t0, 1)
    json.dump(R, open(a.out, "w"), indent=1, default=str)
    print(json.dumps({k: (v["pass"] if isinstance(v, dict) else v) for k, v in R.items()}))
    sys.exit(0 if R["pass"] else 1)
