# Changelog

All notable changes to the `drums` Python SDK. The SDK talks to drumsd over its HTTP API; the drumsd version each
SDK version was built against is noted where it matters.

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

The first version published on PyPI, as `drums-sdk` (`pip install drums-sdk`; the import name stays `drums`). Versions 0.2.0 to 0.2.4 shipped as wheels sent with invites.
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
