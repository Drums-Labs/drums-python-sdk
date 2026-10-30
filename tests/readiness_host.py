"""Reproduce 'a process already exiting at capture time fails the alive gate in every child'.
Each trial: create a Chrome env with the OSWorld task, wait for its gates, do the trigger action, then fork(N)
IMMEDIATELY (no sleep), and record every child's verdict and the alive-gate detail.

    sudo python3 python/tests/readiness_host.py --mode MODE --trials 20 --children 2 --out out.json

Modes: slow_exit (a helper ends one thread every 250 ms after SIGTERM, then exits: the exiting-process case),
close_window / pkill_chrome / tabclose / kill_renderer (Chrome shutting down, or a tab closed, just before the fork),
silent_exit_2s / silent_exit_8s (a helper that does nothing visible for 2 or 8 s after SIGTERM, then exits: beyond what
can be seen in advance), post_resume_kill (nothing before the fork; the helper is SIGKILLed in child 0 right after the
fork returns: that child must fail the alive gate, the other must stay valid).
Without capture readiness, slow_exit fails the alive gate in every child; with it, every child is valid.
Runs on the drums host (token from /etc/drums/token) with the SDK at --sdk."""
import argparse, json, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--mode", required=True)
ap.add_argument("--trials", type=int, default=20)
ap.add_argument("--children", type=int, default=2)
ap.add_argument("--out", required=True)
ap.add_argument("--sdk", default="/opt/drums/src/drumsd/python")
ap.add_argument("--task", default="2ad9387a-65d8-4e33-ad5b-7580065a27ca")
a = ap.parse_args()
sys.path.insert(0, a.sdk)
import drums  # noqa: E402

HELPER = r"""
import signal, sys, threading, time
mode = sys.argv[1]
stop = threading.Event()
evs = [threading.Event() for _ in range(8)]
ts = [threading.Thread(target=e.wait, daemon=False) for e in evs]
for t in ts: t.start()
def term(*_):
    stop.set()
signal.signal(signal.SIGTERM, term)
while not stop.is_set():
    time.sleep(0.05)
if mode == "slow":          # visible teardown: one thread ends every 250 ms, then the process exits
    for e, t in zip(evs, ts):
        e.set(); t.join(); time.sleep(0.25)
else:                       # silent: nothing changes for DELAY s, then it exits
    time.sleep(float(mode))
    for e in evs: e.set()
sys.exit(0)
"""
WRITE_HELPER = {"type": "execute", "command": ["python3", "-c", f"open('/tmp/exiter.py','w').write({HELPER!r})"], "shell": False}

def start_helper(arg):
    return [WRITE_HELPER, {"type": "launch", "command": ["python3", "/tmp/exiter.py", arg], "shell": False},
            {"type": "sleep", "seconds": 2}]

SETUP = {"post_resume_kill": start_helper("slow"), "slow_exit": start_helper("slow"), "silent_exit_2s": start_helper("2.0"), "silent_exit_8s": start_helper("8.0")}.get(a.mode, [])
TERM_HELPER = [{"type": "execute", "command": ["pkill", "-TERM", "-f", "/tmp/exiter.py"], "shell": False}]

KILL_HELPER = {"type": "execute", "command": ["pkill", "-KILL", "-f", "/tmp/exiter.py"], "shell": False}
TRIGGER = {
    # nothing before the fork; after it, the helper is SIGKILLed in child 0 only (a real death after resume)
    "post_resume_kill": [],
    "slow_exit": TERM_HELPER, "silent_exit_2s": TERM_HELPER, "silent_exit_8s": TERM_HELPER,
    # close the browser window: Chrome's whole tree shuts down gracefully
    "close_window": ["pyautogui.hotkey('ctrl', 'shift', 'w')"],
    # Chrome's whole process tree shuts down (browser, GPU, utility, renderers exit over the next ~0.5-2 s)
    "pkill_chrome": [{"type": "execute", "command": ["pkill", "-x", "chrome"], "shell": False}],
    # the original report's prefix, with no sleep after the close
    "tabclose": ["pyautogui.hotkey('ctrl', 't'); time.sleep(1)", "pyautogui.hotkey('ctrl', 'w')"],
    # SIGTERM to the newest chrome process (a renderer or helper), then fork at once
    "kill_renderer": [{"type": "execute", "command": ["pkill", "-TERM", "-n", "-x", "chrome"], "shell": False}],
}[a.mode]

c = drums.connect("http://127.0.0.1:7070", open("/etc/drums/token").read().strip())
h = c.health()
out = {"mode": a.mode, "trials": [], "health": {"version": h.get("version"), "criu": h.get("criu")}, "sdk": a.sdk, "sdk_version": drums.__version__,
       "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
for i in range(a.trials):
    rec = {"i": i}
    env = None
    try:
        env = c.create(image="osworld-ubuntu", task=a.task)
        for act in SETUP:
            env.step(act)
        env.wait_valid()
        rec["parent"] = env.id
        for act in TRIGGER:
            env.step(act)
        t0 = time.monotonic()
        try:
            r = c.call("POST", f"/v1/slots/{env.id}/branch", {"n": a.children})
        except drums.DrumsError as e:
            rec["fork_error"] = {"type": type(e).__name__, "status": e.status, "message": e.message[:800], "body": e.body}
            rec["fork_s"] = round(time.monotonic() - t0, 3)
            raise
        rec["fork_s"] = round(time.monotonic() - t0, 3)
        rec["capture_s"] = r.get("capture_s")
        ck = r.get("checkpoint") or {}
        rec["checkpoint"] = ck.get("id")
        rec["quiescence"] = ck.get("quiescence")
        kids = []
        t_forked = time.monotonic()
        envs = [drums.Env(c, ch) if "error" not in ch else ch for ch in r["children"]]
        if a.mode == "post_resume_kill" and isinstance(envs[0], drums.Env):
            envs[0].step(KILL_HELPER)
            rec["killed_in"] = envs[0].id
            rec["kill_s_after_fork"] = round(time.monotonic() - t_forked, 3)
        for k in envs:
            if not isinstance(k, drums.Env):
                kids.append({"error": k["error"]})
                continue
            ok = k.valid(timeout=40)
            g = k.gates()
            alive = [x for x in g["gates"]["gates"] if x["gate"] == "alive_after_resume"] if isinstance(g.get("gates"), dict) else []
            kids.append({"id": k.id, "valid": ok, "verdict": g["validity"]["verdict"], "alive": alive[-1] if alive else None})
            k.close()
        rec["children"] = kids
        rec["all_valid"] = all(k.get("valid") for k in kids)
        try:
            c.delete_state(ck.get("id"))
        except drums.DrumsError as e:
            rec["delete_state_error"] = str(e)
    except drums.DrumsError as e:
        rec.setdefault("error", f"{type(e).__name__}: {e}"[:800])
    finally:
        if env is not None:
            try:
                env.close()
            except drums.DrumsError:
                pass
    out["trials"].append(rec)
    q = rec.get("quiescence") or {}
    print(i, rec.get("all_valid"), rec.get("fork_s"), rec.get("capture_s"), len(q.get("attempts", [])) if q else "-",
          [ (k.get("verdict"), (k.get("alive") or {}).get("detail", {}).get("counted")) for k in rec.get("children", [])], rec.get("error", ""), flush=True)
    json.dump(out, open(a.out, "w"), indent=1, default=str)
tr = out["trials"]
out["summary"] = {"trials": len(tr), "all_children_valid": sum(1 for t in tr if t.get("all_valid")),
                  "children": sum(len(t.get("children", [])) for t in tr),
                  "children_valid": sum(1 for t in tr for k in t.get("children", []) if k.get("valid")),
                  "fork_errors": sum(1 for t in tr if "fork_error" in t), "other_errors": sum(1 for t in tr if "error" in t and "fork_error" not in t)}
out["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
json.dump(out, open(a.out, "w"), indent=1, default=str)
print(json.dumps(out["summary"]))
