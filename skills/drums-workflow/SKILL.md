---
name: drums-workflow
description: Use when the user wants to run, test or train a computer-use agent on an isolated Drums desktop (start a computer, act in it, verify against a task, fork or reset it, read run results) through the Drums MCP tools.
---

# Drums workflow

Drums gives you isolated Linux desktops ("computers") with a task checker. The MCP server exposes two layers of tools:

- Workspace tools: `list_templates`, `list_computers`, `create_computer`, `get_computer`, `destroy_computer`,
  `list_snapshots`, `create_snapshot`, `delete_snapshot`, `list_tasks`, `get_task`, `list_runs`, `create_run`,
  `get_run`, `list_evaluations`, `create_evaluation`, `get_evaluation`, `list_api_keys`, `create_api_key`,
  `revoke_api_key`, `read_audit_log`.
- Desktop tools (each takes `computer_id`): `gui`, `screenshot`, `shell`, `read_file`, `write_file`, `list_dir`,
  `office_convert`, `spreadsheet_read`, `browser_tabs`, `browser_open`, `verify`, `fork`, `reset`, `load_task`.

Runs, evaluations, API keys and the audit log need the Drums gateway. Against a plain drumsd those tools answer
"not available on this server (needs the Drums gateway)"; the computer and desktop tools work on both.

## The loop

1. Pick a template: `list_templates`. Use a template's `checkpoint` (for example `osworld-ubuntu`) as `template`.
2. Pick a task (optional): `list_tasks` (filter with `domain`, e.g. `chrome`, `os`, `libreoffice_calc`), then
   `get_task` to read the instruction, setup and checker.
3. Start a computer: `create_computer` with `template` and `task_id` (or a full `task` document). Keep the returned
   `id`: it is the `computer_id` for every desktop tool. Set `ttl_s` when the work has a known length.
4. Wait until it is ready: call `get_computer` until `state` is `ready` and the validity verdict is `valid`. Do not
   act on a computer whose verdict is `invalid` or whose state is `quarantined`; tell the user and start another.
5. Act in the desktop:
   - `gui` runs pyautogui code (screen 1920x1080, origin top left). Take a `screenshot` after every GUI action and
     look at it before the next one.
   - `shell` runs a bash command as the desktop user (no root, no internet). `read_file`, `write_file`, `list_dir`
     work on the same filesystem; `~` is `/home/user`.
   - `office_convert` and `spreadsheet_read` use LibreOffice; `browser_tabs` and `browser_open` need Chrome running
     with `--remote-debugging-port=1337`.
6. Score it: `verify` runs the loaded task's checker and returns `score` (0..1), `valid` (every gate of the episode
   passed), `trusted` and `audit_flags`. Use the score only when `valid` is true.
7. Branch at a decision point: `fork` with `n` makes n children that continue from this exact state (apps open, files
   as they are). The parent keeps running. Each child's id is a new `computer_id`; record which child tried what.
8. Try again from clean: `reset` returns the computer to its template state and runs the task's setup again.
   `load_task` puts a different task into a running computer.
9. Save a state worth keeping: `create_snapshot`; it appears in `list_snapshots` and can be the `template` of a
   later `create_computer`.
10. Clean up: `destroy_computer` with `confirm: true` for every computer you started (children too) once the user is
    done with them. Destroyed computers cannot be recovered.
11. Read results: `get_run` / `get_evaluation` for a run's status, attempt counts and last events; `read_audit_log`
    (filter `action: "mcp"`) to see what the MCP session did, newest first.

## Runs and evaluations

`create_run` submits a RunSpec (name, kind, task_set_id, task_ids, template_version_id, agent, execution, budget);
the host's runner picks it up. `create_evaluation` is the same with `kind` set to `evaluation`. Both return `run_id`;
poll `get_run` / `get_evaluation`. `list_runs` and `list_evaluations` filter by `status`.

## Rules

- Never call `destroy_computer`, `delete_snapshot` or `revoke_api_key` without telling the user first. They are
  refused unless the tool arguments carry `confirm: true`; you, the caller, supply it, and passing it is your
  statement that the user agreed. Drums does not ask anyone: approval of the call belongs to the MCP client
  (Claude Code, Cursor, Codex) and its tool-approval settings, not to the server.
- The token is sent over HTTPS, or over plain HTTP to loopback only; `drums mcp` refuses any other `http://`
  URL unless started with `--allow-insecure-http` (or `DRUMS_ALLOW_INSECURE_HTTP=1`). Do not add that opt-in on
  the user's behalf.
- Prefer `shell`, `read_file` and `list_dir` over GUI actions when you only need to check something; use the GUI
  for what the task actually asks a user to do.
- Keep every computer id you create (and every fork child id) and report them; a lost id is a running desktop
  nobody can clean up until its lease expires.
- Take a screenshot after each GUI action; do not chain blind clicks.
- `create_api_key` returns the plaintext key once. Show it to the user and never write it into files or logs.
- A computer's validity verdict decides whether a `verify` score counts. Report `valid: false` as "no usable
  result", never as a failure of the task.
- Every desktop tool call needs `computer_id` unless the server was started with `--env`; pass it explicitly when
  more than one computer is in play.
