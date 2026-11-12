# drums-sdk

Python SDK for [Drums](https://drums.run): isolated desktop environments for computer-use agents and RL.
Create a desktop, act in it, take screenshots, fork it into copies of the same state, and score it with a task's
checker. Standard library only; Python 3.9 or later.

Full documentation: **[console.drums.run/docs](https://console.drums.run/docs)**

## Five-minute start

### 1. Install

```bash
python3 -m venv drums-venv
drums-venv/bin/pip install drums-sdk
```

The package on PyPI is `drums-sdk`; the import name is `drums`.

```python
import drums
```

### 2. Point it at your Drums

The SDK reads two environment variables. Your API key comes from **API Keys** in the
[console](https://console.drums.run); it starts with `drums_sk_` and is shown once.

```bash
export DRUMS_URL=https://beta.drums.run/gw     # Drums Cloud gateway; or http://127.0.0.1:7070 for a local drumsd
export DRUMS_TOKEN=drums_sk_...                # your API key
```

The key is sent over HTTPS, or over plain HTTP to loopback only (`localhost`, `127.0.0.0/8`, `::1`). Any other
`http://` URL raises `drums.InsecureTransport` before a request is made; `drums.connect(url, token,
allow_insecure_http=True)` or `DRUMS_ALLOW_INSECURE_HTTP=1` opts in.

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
  desktop; `drums mcp` serves the whole workflow to an agent over MCP (stdio), see [MCP server](#mcp-server).
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

## MCP server

`drums mcp` (the same as `python -m drums.mcp`) is an MCP server on stdio that gives an agent the whole workflow:
start a computer, act in it, verify against a task checker, fork and reset it, and read run results. It talks to
the Drums gateway (or a plain drumsd for the desktop tools) at `DRUMS_URL` with `DRUMS_TOKEN`.

```
drums mcp                        # all tools; desktop tools take computer_id
drums mcp --env <computer_id>    # desktop tools default to this computer
drums mcp --create [--task FILE] # create one computer, bind it, close it on exit
drums mcp --print-config claude|claude-local|cursor|codex
```

The same tool list is served remotely by the gateway at `<GATEWAY_URL>/mcp` (MCP Streamable HTTP, authenticated
with a workspace API key; Drums MCP uses API keys, not OAuth). Replace `<GATEWAY_URL>` with your workspace's gateway
URL and `drums_sk_...` with a key from the console.

Claude Code, remote:

```
claude mcp add --transport http drums <GATEWAY_URL>/mcp --header "Authorization: Bearer drums_sk_..."
```

Claude Code, local (this package):

```
claude mcp add drums -e DRUMS_URL=<GATEWAY_URL> -e DRUMS_TOKEN=drums_sk_... -- drums mcp
```

Cursor (`~/.cursor/mcp.json` or `.cursor/mcp.json`):

```json
{"mcpServers": {"drums": {"url": "<GATEWAY_URL>/mcp", "headers": {"Authorization": "Bearer drums_sk_..."}}}}
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.drums]
command = "drums"
args = ["mcp"]
env = { DRUMS_URL = "<GATEWAY_URL>", DRUMS_TOKEN = "drums_sk_..." }
```

This repository is also a Claude Code plugin (`.claude-plugin/plugin.json`, `.mcp.json`, and the
`drums-workflow` skill under `skills/`), which installs the server and a skill that teaches the loop below.

### Tools

Workspace tools (the gateway's HTTP API; `*` needs the Drums gateway and answers "not available on this server"
against a plain drumsd):

| tool | what it does |
|---|---|
| `list_templates` | base desktop images a computer can start from |
| `list_computers`, `get_computer` | the workspace's computers: state, validity, task |
| `create_computer` | a new computer from a template or snapshot, optionally with a task |
| `destroy_computer` | close a computer for good (destructive) |
| `list_snapshots`, `create_snapshot`, `delete_snapshot` | saved desktop states (delete is destructive) |
| `list_tasks`, `get_task` | the verified computer-use tasks on the host |
| `list_runs`, `create_run`, `get_run` * | runs: submit a RunSpec, read status, counts and events |
| `list_evaluations`, `create_evaluation`, `get_evaluation` * | runs with kind `evaluation` |
| `list_api_keys`, `create_api_key`, `revoke_api_key` * | API keys (metadata only; revoke is destructive) |
| `read_audit_log` * | who did what, newest first; MCP calls appear as `mcp.<tool>` |

Desktop tools (each takes `computer_id`):

| tool | what it does |
|---|---|
| `gui` | pyautogui code in the desktop (1920x1080) |
| `screenshot` | a PNG of the desktop (image content) |
| `shell` | a bash command as the desktop user (no root, no internet) |
| `read_file`, `write_file`, `list_dir` | the desktop's files (`~` is `/home/user`) |
| `office_convert`, `spreadsheet_read` | LibreOffice conversions and sheet reads |
| `browser_tabs`, `browser_open` | Chrome tabs through its DevTools port (1337) |
| `load_task` | load a task into a running computer (setup runs, a new episode starts) |
| `verify` | score the desktop with the task's checker: score, valid, trusted, audit flags |
| `fork` | n children that continue from this exact state; the parent keeps running |
| `reset` | back to the clean template state; the task's setup runs again |

The canonical list with every input schema is `drums/mcp_tools.json`; `tools/list` returns exactly it, in order.

### Safety

- Gated tools need `confirm: true` from the caller. `destroy_computer`, `delete_snapshot` and `revoke_api_key`
  (the tools marked `destructive` in `mcp_tools.json`) are refused before any request is sent unless the tool
  arguments carry `confirm: true`. The caller (the agent) supplies that argument; Drums does not prompt anyone.
  Whether a person approves the call is the MCP client's job (Claude Code, Cursor and Codex each have their own
  tool-approval settings): there is no confirmation step inside Drums, and `confirm: true` from the caller is the
  only thing the server checks. `reset` also discards a computer's desktop state, but it is not gated.
- The token only travels over HTTPS, or over plain HTTP to loopback (`localhost`, `127.0.0.0/8`, `::1`, where a
  local drumsd listens). With a token set, any other `http://` URL in `--url` or `DRUMS_URL` is refused before a
  request is sent, with a message naming the host and the opt-ins (never the token). To send the token over plain
  HTTP anyway, opt in: `drums mcp --allow-insecure-http`, `DRUMS_ALLOW_INSECURE_HTTP=1`, or
  `drums.connect(url, token, allow_insecure_http=True)` / `drums.Client(..., allow_insecure_http=True)` in the SDK
  (the `Client` applies the same rule, so SDK scripts are covered too; the error is `drums.InsecureTransport`).
- Redirects are followed only to the same origin (scheme, host, port), at most 5 hops, with the same transport
  check on each hop; any other redirect raises `drums.RedirectRefused` before it is followed, so the token is
  never re-sent to another host or over plain HTTP because a server said `Location`.
- Arguments are checked against the tool's schema before anything is sent; a violation is a tool error naming the
  field. A tool failure is always a tool result with `isError`, never a protocol error.
- `--print-config` prints placeholders (`<GATEWAY_URL>` when no URL is known, always `drums_sk_...`), never a key.
- The token comes from `DRUMS_TOKEN`, else the file `DRUMS_TOKEN_FILE`, else `/etc/drums/token`; it is never
  printed or written by the server, and never appears in an error message.
- Keys are workspace-scoped on the gateway: a key never sees another workspace's computers.
- `create_api_key` returns the plaintext key once; nothing stores it.

## Development

```bash
git clone https://github.com/Drums-Labs/drums-python-sdk.git
cd drums-python-sdk
python3 -m unittest discover -s tests -v     # runs against a fake daemon; no network
```

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version and [RELEASE.md](RELEASE.md) for how a release is cut.
