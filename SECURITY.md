# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.3.x | yes |
| 0.2.x | security fixes only, until 0.4.0 is released |
| earlier | no |

The SDK uses only the Python standard library and has no runtime dependencies. Upgrade with `pip install -U drums-sdk`.

## Reporting a vulnerability

Report it privately, in this order of preference:

1. GitHub's private reporting form:
   [github.com/Drums-Labs/drums-python-sdk/security/advisories/new](https://github.com/Drums-Labs/drums-python-sdk/security/advisories/new).
   It opens a private advisory that only the maintainers can see.
2. Email: security@drums.run.

Do not open a public issue, and do not include a live API key in the report (revoke it instead; see below).

What to expect:

- an acknowledgement within 3 business days;
- an assessment and, where the report is confirmed, a fix or a mitigation within 30 days for the SDK itself. Problems
  in the hosted service or the runtime the SDK talks to are passed to the people who run them the same day;
- credit in the changelog if you want it.

Please give us that time before publishing. If you do not hear back within the acknowledgement window, open an issue
that says only "security report pending" and a maintainer will follow up privately.

## If a key leaks

An API key (`drums_sk_...`) is a bearer credential: whoever holds it acts as its owner, within the key's scopes.
Revoke it at once under **API Keys** in [console.drums.run](https://console.drums.run) (revocation takes effect on
the next request; valid keys are never cached), check the audit log for calls made with it, close any computers you
do not recognise, and create a replacement. Rewriting git history does not undo a leak.

## What this SDK guarantees

- **Transport.** A token is sent over HTTPS, or over plain HTTP to loopback only (`localhost`, `127.0.0.0/8`,
  `::1`). Any other `http://` URL raises `drums.InsecureTransport` before a request is made, unless the caller opts in
  (`allow_insecure_http=True`, `DRUMS_ALLOW_INSECURE_HTTP=1`, `drums mcp --allow-insecure-http`).
- **Redirects.** Followed only to the same origin (scheme, host, port), at most 5 hops, with the transport check on
  every hop; anything else raises `drums.RedirectRefused` before it is followed. The token is never re-sent to another
  host because a server said `Location`.
- **No token in output.** The token is never printed, written or included in an error message by the SDK, the
  `drums` command or the MCP server. `drums mcp --print-config` prints a placeholder.
- **Destructive MCP tools need `confirm: true`.** `destroy_computer`, `delete_snapshot` and `revoke_api_key` are
  refused before any request unless the arguments carry `confirm: true`. The caller supplies it; whether a person
  approves the call is the MCP client's job. `reset` also discards desktop state and is not gated.
- **Schema checks.** MCP tool arguments are checked against `drums/mcp_tools.json` before anything is sent.

## Known limitations of the runtime the SDK talks to

These describe Drums Cloud and `drumsd` as published at
[Isolation and worker pools](https://docs.drums.run/concepts/isolation-and-worker-pools); they are not
properties of this package, but anyone using it should know them.

- **The isolation boundary between customers is the host.** A worker host is bound to one workspace; desktops of
  different workspaces never share a host. Inside a host, desktops are separated by Linux namespaces (pid, mount,
  network, ipc, uts, user), cgroups and a seccomp filter. That is process isolation within one trusted customer
  boundary, not a virtual machine boundary: do not run desktops of parties that do not trust each other on one host.
- **Desktops restored or forked from the same template share the template's uid range.** This is accepted only
  inside a single trusted customer boundary. Per-desktop uid remapping is tracked as hardening required before any
  untrusted multi-tenant use.
- **Agents never escalate.** The desktop user is in no administrator group, every process it drives runs with
  `no_new_privs`, and `sudo`, `su` and `pkexec` refuse. Task setup steps that need root are run by the runtime as
  trusted provisioning; a task that needs the agent itself to act as root is refused with `drums.TaskUnsupported`.
- **The hardened execution profile is the default on Drums Cloud.** A self-hosted `drumsd` runs with whatever
  profile its operator installed; ask them which.
- **Open item: a crashed application after a fork.** In daily-use tests of runtime 0.4.2, a forked desktop whose
  browser had crashed could still return a reward marked valid. The reward audit (`Reward.trusted`, `audit_flags`)
  and the alive-after-resume gate are the mitigations; until the fix is confirmed on the release you run against,
  prefer `trusted` over `valid` for training signal and treat a score from a desktop whose application was not
  running as suspect.
- **No outbound network from a desktop.** Loopback only, no DNS. Anything a task needs from the network must be
  provisioned by the operator.

Rate limits, quotas and what the gateway checks on every call are in the
[authentication reference](https://docs.drums.run/reference/authentication).
