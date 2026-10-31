"""An OOM kill inside an env makes its episode invalid (gate no_oom_kill), wherever in the episode it happens. Run on the
host as root against a real installed drumsd:

  A. during the task setup: the setup launches a process that exceeds the env's memory (the baseline is taken before setup)
  B. after the last step, before verify: a background allocation that is OOM-killed while the agent is idle
     (this is how GIMP a746add2 died in runtime-v05: valid=True with score 0.0 before the fix)
  C. control: the same env with no allocation is valid.

    sudo python3 oom_host.py OUT.json
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drums  # noqa: E402

c = drums.connect("http://127.0.0.1:7070", open("/etc/drums/token").read().strip())
TASK = "2ad9387a-65d8-4e33-ad5b-7580065a27ca"   # Chrome bookmarks (has an evaluator; the test only needs a reward)
# allocate and touch memory well beyond memory.max + swap.max of a slot (4 GiB + up to 3220 MiB): the OOM killer takes it
HOG = "import time; time.sleep(%s); b = bytearray(); [b.extend(b'x' * (64 << 20)) for _ in range(160)]"
R = {}


def reward(e):
    r = e.verify(strict=False)
    return {"score": r.score, "valid": r.valid, "verdict": r.verdict, "bad_gates": r.bad_gates}


def oom_in(r):
    return (not r["valid"]) and "no_oom_kill" in json.dumps(r["bad_gates"])


# C. control
e = c.create(task=c.task(TASK)).wait_valid()
e.step({"type": "sleep", "seconds": 1})
R["C_control"] = reward(e)
e.close()
# B. hog started by the last step, killed while idle, before verify
e = c.create(task=c.task(TASK)).wait_valid()
e.step({"type": "launch", "command": ["python3", "-c", HOG % 2], "shell": False})
time.sleep(25)
R["B_after_last_step"] = reward(e)
e.close()
# A. during setup: a task whose config launches the hog
t = dict(c.task(TASK))
t["config"] = list(t.get("config") or []) + [{"type": "launch", "parameters": {"command": ["python3", "-c", HOG % 0]}}]
e = c.create(task=t)
try:
    e.wait_valid()
    time.sleep(25)
    R["A_during_setup"] = reward(e)
except drums.GateFailed:
    # since no_app_crash (daily-use): a kill during setup can already make the episode invalid before it is handed out
    # (Chrome dies of SIGTRAP under the same memory pressure); the env is never valid, and no_oom_kill is named
    r = e.refresh()
    bad = [g["gate"] for g in (r.get("gates") or {}).get("gates", []) if g.get("status") == "fail"]
    R["A_during_setup"] = {"score": None, "valid": False, "verdict": (r.get("validity") or {}).get("verdict"), "bad_gates": bad,
                           "invalid_at": "wait_valid"}
e.close()
R["pass"] = R["C_control"]["valid"] and oom_in(R["B_after_last_step"]) and oom_in(R["A_during_setup"])
json.dump(R, open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/oom_host.json", "w"), indent=1)
print(json.dumps(R, indent=1))
sys.exit(0 if R["pass"] else 1)
