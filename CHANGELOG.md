# Changelog

All notable changes to the `drums` Python SDK. The SDK talks to drumsd over its HTTP API; the drumsd version each
SDK version was built against is noted where it matters.

## 0.4.0 - 2026-11-18

Fixes for training clients, from our first 128-desktop training run, integrated in one release. Minor version: new API and one
behaviour change (429 replies are retried, with an off switch); nothing is removed and no dependency is added. Every
0.3 call keeps its signature and meaning. Built against drumsd 0.4.2 and the current Drums Cloud gateway; against an older gateway each
feature falls back as described below.

### Changed

- **429 replies are retried on every call** (#11). A 429 (the gateway's per-key rate limit, or no capacity
  right now: nothing was started) is retried for every method, step, screenshot, verify, create and fork included,
  waiting at least the reply's `Retry-After` (seconds or an HTTP date), with jittered exponential backoff, for at
  most 60 s of waiting per call (or 20 retries). `drums.RetryPolicy(max_wait_s, max_attempts, base_s, cap_s, jitter)`
  via `connect(..., retry=...)`; `DRUMS_RETRY_MAX_WAIT_S` sets the budget and `0` restores the 0.3.0 behaviour
  (raise at once). `drums mcp` waits at most 10 s. `Overloaded` carries `retry_after` and `retries`. A GET that gets
  a 503 honours `Retry-After`; a POST that gets a 503 is still never retried.

### Added

- **Desktop tokens** (#14). `drums.connect(url, key)`
  keeps its signature. With a Drums API key (`drums_sk_...`) the client asks the gateway once how it authenticates
  (`GET <url>/auth`, no credential). When the gateway says `token` or `both`, the key is sent once to the console's
  token URL (https, or loopback http; the same transport and redirect rules as every other request) and the gateway
  only ever receives the short-lived desktop token. The token is renewed before it expires (the console's
  `renew_after_s`, else 5 min before expiry) and once after a 401, then the call is sent again. The exchange and
  every renewal use the client's `RetryPolicy` (a 429 waits for its `Retry-After`; in `token` mode, with no fallback,
  a 5xx or a dropped connection is retried within the same budget) and the certifi fallback below. In `both` mode an
  unreachable console falls back to the API key at once (and tries the exchange again after a minute); in `token`
  mode it raises `DaemonUnavailable` naming the console, and a refused key raises `AuthError`. A key sent before the
  gateway's mode was known (the discovery request got no answer) is retried once after a 401. A drumsd token, or a
  gateway in `api_key` mode, works exactly as before. Neither the key nor the token appears in a repr
  (`repr(client)` shows the auth kind only) or an error message.
- **macOS certificates** (#11). When the system store cannot verify the gateway or the console and `certifi`
  is installed, the client switches to certifi's CA bundle once (for both) and prints one line naming the server;
  without certifi it raises the new `CertificateError` (a `DaemonUnavailable`) with the fix
  (`Install Certificates.command` or `pip install certifi`). No new dependency.
- **Run object** (#12, matching the gateway's trainer runs). `with client.run(name, kind="training",
  task_set=..., config=..., desktops_provisioned=N) as run:` opens a run (`POST /v1/runs`),
  `run.step(step, reward=, zero_advantage_share=, rollouts=, desktops_busy=, desktops_provisioned=)` buffers points
  sent at most once per 30 s (`PATCH /v1/runs/{id}`), and the block's end closes it (`POST /v1/runs/{id}/close`:
  completed, failed with the exception, cancelled on Ctrl-C). Kinds: `training`, `rl`, `evaluation`, `eval`.
  Desktops created inside carry `X-Drums-Run-Label` (the label the open reply names, which differs from the name
  when another running run holds it) and `X-Drums-Run-Id`. A 409 on a heartbeat or close means the run has ended on
  the gateway (closed, or lost after 30 min without a heartbeat): the SDK stops sending for it with one warning
  (`run.ended`). A `config` that is not a JSON object or is over 16 KiB as JSON is sent as its `config_hash` and a
  `config_summary` (scalar top-level values), with one warning. A 400 on open warns with the gateway's message and
  leaves `run.id` None; only a 404 or 405 means the routes are missing. On a gateway without these routes, or with a key that has neither `runs:write` nor
  `computers:write`, it is a no-op with one warning (`strict=True` raises `RunNotSupported`).
  `client.run(<run uuid>)` alone still returns the run's record; `get_run` and `open_run` are the explicit names.
- **`drums preflight`** (#13). `drums preflight --desktops N --hours H [--model-gpus 2:6] [--task-set ID] [--json]`
  and `drums.preflight(...)`: gateway and console health, key validity, expiry and scopes, host capacity and
  admission, rate limit (N x 0.5 requests/s, a rule of thumb from our training runs, `REQUESTS_PER_DESKTOP_S`), live-desktop quota, spend cap at
  the projected desktop-hour cost, and the task set's registration and validation. A pass/fail table with a fix and a
  console link per row; exit 1 on a failure (`--strict`: also on a warning or a row it could not check). Works in
  desktop-token mode: the exchange runs first, so a console that cannot issue a token is an auth failure (a `both`
  gateway on the key alone is a warning), and the console row defaults to the token URL's origin. Rate limit, quota,
  spend cap, price and host admission come from `GET /v1/limits` (scope `usage:read`); a
  gateway without it gets "not checked" on those rows.
- **MCP custom tasks** (#15). `create_computer` and `load_task` take `task_set_id` + `task_id` (a task of
  a registered set) besides `task` and `task_id`; a task document sent as a JSON string is accepted; new tools
  `list_task_sets` and `get_task_set` (36 tools, the gateway's tools.json); `verify` on a computer with no task says
  so. Needs the gateway route `GET /v1/task-sets/{id}/tasks/{task_id}`.

### CI

- A `pip-audit` job on what CI installs, with an allowlist whose entries expire, and a weekly audit that opens an
  issue on failure (#10).
- The test matrix (3.9 to 3.13) runs on `ubuntu-24.04`: `ubuntu-latest` is 26.04, which has no Python 3.9 build.

## 0.3.0 - 2026-11-12

The MCP server and the `drums` command. The hosted MCP endpoint on the gateway (`<gateway URL>/mcp`) is live already;
this release adds the local server and the client methods for the new gateway routes.

- `drums mcp` (the same as `python -m drums.mcp`): a stdio MCP server with the 34 workspace and desktop tools of
  `drums/mcp_tools.json`, the list the gateway serves. `--env <computer id>` binds the desktop tools to one computer;
  `--print-config claude|claude-local|cursor|codex` prints the matching client snippet with a placeholder key.
  Destructive tools (`destroy_computer`, `delete_snapshot`, `revoke_api_key`) are refused unless the caller passes
  `confirm: true` in the tool arguments, before any request is sent; the MCP client, not Drums, owns the user's
  approval of such a call.
- Token transport rule, in the `Client` and the CLI: a token is sent over https, or over plain http to loopback
  only (`localhost`, `127.0.0.0/8`, `::1`). Any other `http://` URL (`--url`, `DRUMS_URL`) raises the new
  `drums.InsecureTransport` before any request (exit 2 with a one-line message from `drums mcp`); the message names
  the host and the opt-ins, never the token. Opt in with `drums mcp --allow-insecure-http`,
  `DRUMS_ALLOW_INSECURE_HTTP=1`, or `connect(..., allow_insecure_http=True)` / `Client(..., allow_insecure_http=True)`.
  `drums.check_transport(url, token, allow_insecure_http=None)` is the check itself.
- Redirect rule: the `Client` follows a redirect only to the same origin (scheme, host, port) as the request, at
  most `drums.MAX_REDIRECTS` (5) hops, and runs the transport check on every hop. A redirect to another origin
  (another host or port, https to http) or a sixth hop raises the new `drums.RedirectRefused` before it is
  followed, so the token is never re-sent elsewhere by way of a `Location` header (urllib's default follows and
  re-sends `Authorization`). The message names the target's scheme, host and port only. `308` is followed like
  `307` on every supported Python.
- `drums` console script (`drums mcp`, `drums version`); `mcp_tools.json` ships in the wheel.
- `Client.runs`, `create_run`, `run`, `api_keys`, `create_api_key`, `revoke_api_key`, `audit` for the gateway's
  `/v1/runs`, `/v1/api-keys` and `/v1/audit` routes; `call()` takes extra headers.
- The repository is a Claude Code plugin (`.claude-plugin/plugin.json`, `.mcp.json`, the `drums-workflow` skill).
- `drums.hybrid` `write_file` sends chunks of 8000 base64 characters (the gateway caps a step argument at 8192);
  `gui` code is at most 16384 characters.

## 0.2.5 - first release on PyPI

The first version published on PyPI, as `drums-sdk` (`pip install drums-sdk`; the import name stays `drums`). Versions 0.2.0 to 0.2.4 were not published.
Everything below in the 0.2.x entries is included.

Packaging and release:

- Package metadata for PyPI: readme, classifiers, project URLs, `requires-python >= 3.9`, no runtime dependencies.
- `LICENSE`, `CHANGELOG.md`, `RELEASE.md`, and a GitHub Actions release workflow (PyPI Trusted Publishing, TestPyPI
  on demand).
- Docstrings no longer point at documents that live in other repositories.

Since 0.2.4 (drumsd 0.4.2 and later):

- `Env.prefork()`: prepare a fork right after an action, so a later `fork()` from that point is fast.
- `Reward.audit`, `Reward.audit_flags`, `Reward.trusted`: the per-rollout reward audit drumsd returns (recheck,
  redundant checker, scope, checker timeout or crash). `trusted` is `True` only for a valid episode with a score and
  no audit flag; an older drumsd sends no audit, so `trusted` is `None`, never `True`.
- `drums.hybrid`: GUI, shell, file and app-API tools over one desktop (`HybridTools`, and `TOOLS` as JSON schema).
- `drums.mcp`: an MCP stdio server exposing the hybrid tools plus `verify`; `python -m drums.mcp --env ID` or
  `--create [--task task.json]`. Token from `DRUMS_TOKEN`, else the file in `DRUMS_TOKEN_FILE`, else
  `/etc/drums/token`. A bound env is never closed on exit; a created one is.
- `TaskUnsupported` (422 `task_unsupported`): the task needs the agent itself to act as root, which this execution
  profile does not allow. Setup steps that need root are run by drumsd as provisioning and are supported.

## 0.2.4

- `EnvGone` (410, and the older 409 "slot is destroyed"); `close()` is a no-op on a gone env.
- `fork()` returns `ForkResult`: the list of children plus `.state`, the saved state the fork made.
  `keep_state=False` deletes it when the children close. `last_state` and `last_fork` are deprecated.
- Leases on `create`, `fork` and `save` (`idle_timeout=`, `ttl=`; default: reaped after 30 minutes unused).
- The client closes the envs it created at process exit, on SIGTERM/SIGHUP (when their handlers were the default)
  and when used as a context manager; `connect(..., close_on_exit=False)` turns that off.
- `Reward.invalid_reasons`.
- The legacy `env()` entry point defaults to the installed image.

## 0.2.3

- `client.env_records()` pages through env records (destroyed ones opt-in).
- `CheckpointStale` for a saved state made from an installed image that has since changed.

## 0.2.2

- `step()` raises `ActionFailed` when the action ran and exited non-zero; `check=False` returns the result as is.
- `Overloaded` (429): create, save and fork are refused near the host's memory limit.
- `drums.aio` runs calls on its own thread pool (`max_threads`, default 1024), not the event loop's default
  executor, so N envs driven with `asyncio.gather` really run N calls at once.

## 0.2.1

- `NotQuiescent` (a `Conflict`), `env.wait_quiescent(timeout, window)`, and `quiesce_timeout=` on `fork` and `save`.
- `client.delete_state()`, `env.from_state`.
- `Client()` takes the token from `DRUMS_TOKEN` when none is given.
- `env.wait_valid()` blocks until the episode's alive gate resolves and raises `GateFailed` if it is invalid.

## 0.2.0

- New client: `connect(url, token)`, `client.create(image, task, from_state)`, and on an env `step`, `observe`,
  `fork`, `save`, `verify`, `reset`, `close`.
- Typed errors carrying the server's status, message and body; errors about an env carry its record and gate report.
- GET-only retries while the daemon is unreachable; POST and DELETE are never retried.
- Batch and asyncio variants; `client.metrics()`.
- Unit tests against a fake daemon (no network).

## 0.1.0

- Thin client: `env`, `reset`, `step`, `branch`, `save`, `from_state`, `valid`.
