# drums

Python SDK for [Drums](https://drums.run): isolated desktop environments for computer-use agents and RL.
Create a desktop, act in it, take screenshots, fork it into copies of the same state, and score it with a task's
checker. Standard library only; Python 3.9 or later.

Full documentation: **[console.drums.run/docs](https://console.drums.run/docs)**

## Five-minute start

### 1. Install

```bash
python3 -m venv drums-venv
drums-venv/bin/pip install drums
```

### 2. Point it at your Drums

The SDK reads two environment variables. Your API key comes from **API Keys** in the
[console](https://console.drums.run); it starts with `drums_sk_` and is shown once.

```bash
export DRUMS_URL=https://beta.drums.run/gw     # Drums Cloud gateway; or http://127.0.0.1:7070 for a local drumsd
export DRUMS_TOKEN=drums_sk_...                # your API key
```

Check the connection:

```python
import drums

client = drums.connect()                 # reads DRUMS_URL and DRUMS_TOKEN
print(client.health()["version"])
```

A `401` raises `drums.AuthError`: the key is mistyped, revoked or expired.

### 3. Create a computer, screenshot, act, verify, destroy

Save as `quickstart.py` and run `drums-venv/bin/python quickstart.py`. It starts a desktop with OSWorld task
`e0df059f` (rename a folder on the Desktop), waits until every gate of the episode has passed, saves a screenshot,
performs the task with one action, runs the task's checker and closes the desktop.

```python
import drums

with drums.connect() as client:                            # closes every desktop it created when the block ends
    with client.create(task="e0df059f", ttl=900) as env:  # OSWorld task id; ttl: lifetime in seconds
        env.wait_valid()                                   # every gate of this episode passed
        obs = env.observe()                                # PNG screenshot plus metadata
        open("screen.png", "wb").write(obs.png)
        print("task:", obs.meta["instruction"])
        env.step({"type": "execute", "shell": True,
                  "command": "mv ~/Desktop/todo_list_Jan_1 ~/Desktop/todo_list_Jan_2"})
        reward = env.verify(strict=False)                  # the task's checker, with the episode's validity
        print("score", reward.score, "valid", reward.valid, reward.invalid_reasons)
    print("computers left:", len(client.envs()))           # 0: the with-block destroyed it
```

Expected output (ids and sizes differ):

```text
task: I have a directory named "todo_list_Jan_1". Can you help me change its name into "todo_list_Jan_2"?
score 1 valid True []
computers left: 0
```

A GUI agent acts with pyautogui instead of a shell command: `env.step("pyautogui.click(960, 540)")`.
Use a score only when `valid` is `True`; with the default `strict=True`, `verify()` raises `InvalidEpisode`
for an invalid episode instead of returning a reward.

### 4. Go further

- Fork one desktop into N copies of the same state for group rollouts: `with env.fork(8) as kids: ...`
  ([guide](https://console.drums.run/docs/guides/group-rollouts-with-fork))
- Run a policy over many tasks: [Evaluate on OSWorld](https://console.drums.run/docs/guides/evaluate-on-osworld)
- `drums.aio` is the asyncio variant of the same calls; `drums.hybrid` adds shell, file and app-API tools over one
  desktop; `python -m drums.mcp` serves them to an agent over MCP (stdio).
- Every method: [Python SDK reference](https://console.drums.run/docs/reference/python-sdk).
  Every endpoint: [HTTP API](https://console.drums.run/docs/reference/http-api).
  If something fails: [Errors](https://console.drums.run/docs/reference/errors).

## How it behaves

- **Cleanup.** `with drums.connect() as client:` closes every desktop the client created when the block ends, and so
  do process exit and SIGTERM/SIGHUP. Desktops and saved states also have server-side leases (default: reaped after
  30 minutes unused), so a crashed trainer does not hold a host.
- **Validity.** Every episode starts with a gate report (isolation, mounts, network, limits, state hash, processes
  alive after resume). `Reward.valid` is `True` only when every gate passed and the desktop was never quarantined.
- **Retries.** GET requests are retried while the server is unreachable (up to `retry_s`, default 60 s). POST and
  DELETE are never retried: after a lost reply the outcome is unknown, so check `client.envs()` before trying again.
- **Errors.** Every failure is a typed exception under `drums.DrumsError` carrying the server's status, message and
  body; errors about a desktop carry its record and gate report.

## Development

```bash
git clone https://github.com/Drums-Labs/drums-python-sdk.git
cd drums-python-sdk
python3 -m unittest discover -s tests -v     # runs against a fake daemon; no network
```

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version and [RELEASE.md](RELEASE.md) for how a release is cut.
