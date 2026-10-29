"""Integration test against a real drumsd on a Linux host (root daemon, adopted checkpoint `clean`).

    python3 integration_host.py OUT_DIR

Each check prints one JSON line and is appended to OUT_DIR/integration.jsonl; the exit code is the number of failed
checks. Needs: drumsd running (restart script /opt/drumsd/restart.sh CONFIG), checkpoint `clean` adopted."""
import json
import os
import subprocess
import sys
import time
import secrets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drums  # noqa: E402

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
CONFIG = os.environ.get("DRUMSD_CONFIG", "/opt/drumsd/drumsd.json")
failed = 0


def check(name, ok, **detail):
    global failed
    rec = {"t": time.time(), "check": name, "ok": bool(ok), **detail}
    print(json.dumps(rec, default=str), flush=True)
    with open(os.path.join(OUT, "integration.jsonl"), "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    if not ok:
        failed += 1


def sh(e, cmd):
    r = e.step({"type": "execute", "command": cmd, "shell": True})
    return (r["result"].get("output") or "").strip(), r


def gate_status(rec, gate):
    return next((g["status"] for g in rec["gates"]["gates"] if g["gate"] == gate), None)


c = drums.Client()
made = []
try:
    # 1. create from the clean checkpoint; every gate, then the alive gate
    t0 = time.monotonic()
    e = drums.env()
    made.append(e)
    check("create_ready", e.state == "ready", slot=e.id, seconds=round(time.monotonic() - t0, 3), timings=e.record["timings"])
    check("create_valid_after_alive_gate", e.valid(), gates=[(g["gate"], g["status"]) for g in e.gates()["gates"]["gates"]])

    # 2. step: write a file in the desktop
    tok = secrets.token_hex(6)
    out, r = sh(e, f"echo {tok} > ~/Desktop/drumsd-it-{tok}.txt && ls ~/Desktop")
    check("step_writes_desktop", f"drumsd-it-{tok}.txt" in out, episode=r["episode"], validity=r["validity"])
    png = e.screenshot()
    check("screenshot_png", png[:8] == b"\x89PNG\r\n\x1a\n", bytes=len(png))

    # 3. reset puts the checkpoint's state back (state hash gate) and the file is gone
    e.reset()
    out, _ = sh(e, "ls ~/Desktop")
    check("reset_restores_clean_state", f"drumsd-it-{tok}.txt" not in out and gate_status(e.record, "state_hash") == "pass", episode=e.episode, timings=e.record["timings"])
    check("reset_valid", e.valid())

    # 4. save a reached state, start a slot from it
    sh(e, f"echo {tok} > ~/Desktop/reached-{tok}.txt")
    t0 = time.monotonic()
    state = e.save()
    check("save_returns_checkpoint", bool(state), checkpoint=state, seconds=round(time.monotonic() - t0, 3))
    e2 = drums.env(from_state=state)
    made.append(e2)
    out, _ = sh(e2, "ls ~/Desktop")
    check("from_state_has_reached_file", f"reached-{tok}.txt" in out, slot=e2.id)
    check("from_state_valid", e2.valid())
    out, _ = sh(e, "ls ~/Desktop")
    check("parent_continues_after_save", f"reached-{tok}.txt" in out and e.refresh()["state"] == "ready")

    # 5. branch(2): both children have the reached state; a token written in one is visible in no other slot
    t0 = time.monotonic()
    kids = e.branch(2)
    made.extend(kids)
    check("branch_children_ready", len(kids) == 2 and all(k.state == "ready" for k in kids), seconds=round(time.monotonic() - t0, 3), ids=[k.id for k in kids])
    check("branch_children_valid", all(k.valid() for k in kids))
    canary = secrets.token_hex(6)
    sh(kids[0], f"echo {canary} > /tmp/canary-{canary}; echo {canary} > ~/Desktop/canary-{canary}.txt")
    seen = {x.id: sh(x, f"ls /tmp ~/Desktop")[0].count(canary) for x in [e, e2, kids[1]]}
    check("branch_isolation_canary", all(v == 0 for v in seen.values()), seen=seen)
    iso = [gate_status(k.refresh(), "isolation") for k in kids]
    check("branch_isolation_gate", iso == ["pass", "pass"], iso=iso)
    # resetting a child returns it to the branch point, not to the clean state
    kids[0].reset()
    out, _ = sh(kids[0], "ls ~/Desktop /tmp")
    check("child_reset_returns_to_branch_point", f"reached-{tok}.txt" in out and canary not in out and gate_status(kids[0].record, "state_hash") == "pass")

    # 6. a gate failure quarantines and is never valid: a checkpoint adopted with a wrong expected hash
    try:
        c.call("POST", "/v1/checkpoints", {"id": "it-wronghash", "slot": 0, "expected_state_hash": "0" * 64})
    except drums.DrumsError as ex:
        if ex.status != 409:
            raise
    rec = c.call("POST", "/v1/slots", {"checkpoint": "it-wronghash"})
    bad = drums.Env(c, rec)
    made.append(bad)
    check("wrong_hash_quarantined", rec["state"] == "quarantined" and rec["validity"]["verdict"] == "invalid" and gate_status(rec, "state_hash") == "fail",
          state=rec["state"], reason=rec.get("reason"), validity=rec["validity"])
    try:
        bad.reset()
        check("quarantine_refuses_reset", False)
    except drums.DrumsError as ex:
        check("quarantine_refuses_reset", ex.status == 409, msg=ex.message)
    rel = c.call("POST", f"/v1/slots/{bad.id}/release")
    cg_procs = subprocess.run(["sh", "-c", f"cat /sys/fs/cgroup/system.slice/drumsbench-pool.scope/*-{rec['num']}-pool.scope/*/cgroup.procs 2>/dev/null | wc -l"], capture_output=True, text=True).stdout.strip()
    check("release_kills_tree", rel["state"] == "down" and cg_procs == "0", state=rel["state"], cgroup_procs=cg_procs)

    # 7. the template server dies: the next reset restarts it and succeeds
    ck = c.call("GET", "/v1/checkpoints/clean")
    subprocess.run(["kill", "-9", str(ck["server_pid"])], check=False)
    time.sleep(0.3)
    e.reset()
    ck2 = c.call("GET", "/v1/checkpoints/clean")
    check("template_server_restarted", e.state == "ready" and ck2["server_pid"] != ck["server_pid"] and e.valid(), old=ck["server_pid"], new=ck2["server_pid"],
          attempts=e.record["cost"]["attempts"])

    # 8. the daemon dies mid-reset: restart reconciles (slot down, cgroup empty), the next reset is valid
    p = subprocess.Popen(["curl", "-s", "-XPOST", f"http://127.0.0.1:7070/v1/slots/{e2.id}/reset"], stdout=subprocess.DEVNULL)
    time.sleep(0.45)
    subprocess.run(["pkill", "-9", "-x", "drumsd"])
    p.wait()
    time.sleep(0.5)
    subprocess.run(["/opt/drumsd/restart.sh", CONFIG], check=True)
    r = c.call("GET", f"/v1/slots/{e2.id}")
    check("crash_mid_reset_reconciled", r["state"] in ("down", "ready"), state=r["state"], reason=r.get("reason"))
    e2.record = r
    e2.reset()
    check("reset_after_crash_valid", e2.state == "ready" and e2.valid(), timings=e2.record["timings"])
    others = [x.refresh()["state"] for x in [e] + kids]
    check("other_slots_survive_daemon_crash", all(s == "ready" for s in others), states=others)
    m = c.call("GET", "/metrics", raw=True).decode()
    check("metrics_exposed", "drumsd_reset_seconds_bucket" in m and "drumsd_gate_results_total" in m, lines=len(m.splitlines()))
finally:
    for x in made:
        try:
            r = x.close()
            check(f"destroy_{x.id}", r["state"] == "destroyed", state=r["state"])
        except drums.DrumsError as ex:
            check(f"destroy_{x.id}", False, error=ex.message)
print(json.dumps({"failed": failed}))
sys.exit(failed)
