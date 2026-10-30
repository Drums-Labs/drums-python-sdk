"""Destroy many envs and show the state tmpfs does not grow: kept records go to forgotten_dir on disk.

    sudo python3 python/tests/forgotten_host.py --n 200 --batch 8 --out forgotten.json

Each round creates `batch` envs from the image, waits for their gates, closes them, then reads the state dir's free
bytes (statvfs, host side) and drumsd's health. Pass: state-dir usage after the last round is within --slack-mib of
the first round, and forgotten_dir grew by one archive per closed env."""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import drums  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=200)
ap.add_argument("--batch", type=int, default=8)
ap.add_argument("--slack-mib", type=float, default=256)
ap.add_argument("--state-dir", default="/var/lib/drums/state")
ap.add_argument("--out", required=True)
a = ap.parse_args()
c = drums.connect("http://127.0.0.1:7070", open("/etc/drums/token").read().strip())


def used():
    s = os.statvfs(a.state_dir)
    return (s.f_blocks - s.f_bfree) * s.f_frsize


h0 = c.health()
fd = h0.get("forgotten", {}).get("dir")
n_arch0 = len([x for x in os.listdir(fd) if x.endswith(".tar.zst")]) if fd and os.path.isdir(fd) else 0
rows, done, failed = [], 0, 0
t0 = time.time()
while done < a.n:
    k = min(a.batch, a.n - done)
    try:
        envs = c.create_many(k, image="osworld-ubuntu")
    except drums.BatchError as be:
        envs = be.ok
        failed += len(be.failed)
        for _, err in be.failed:
            if getattr(err, "env_id", None):
                drums.Env(c, {"id": err.env_id}).close()
    for e in envs:
        try:
            e.wait_valid()
        except drums.DrumsError:
            failed += 1
        e.close()
    done += k
    h = c.health()
    rows.append({"closed": done, "state_used_bytes": used(), "forgotten_bytes": h.get("forgotten", {}).get("bytes"), "warnings": h.get("warnings"), "t": round(time.time() - t0, 1)})
    print(json.dumps(rows[-1]), flush=True)
n_arch1 = len([x for x in os.listdir(fd) if x.endswith(".tar.zst")]) if fd and os.path.isdir(fd) else 0
growth = (rows[-1]["state_used_bytes"] - rows[0]["state_used_bytes"]) / 2**20
res = {"n": a.n, "invalid_or_failed": failed, "rows": rows, "state_growth_mib_first_to_last_round": round(growth, 1),
       "archives_added": n_arch1 - n_arch0, "forgotten_dir": fd,
       "pass": growth <= a.slack_mib and (n_arch1 - n_arch0) >= a.n}
json.dump(res, open(a.out, "w"), indent=1)
print(json.dumps({k: v for k, v in res.items() if k != "rows"}))
sys.exit(0 if res["pass"] else 1)
