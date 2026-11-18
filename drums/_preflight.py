"""`drums preflight` / `drums.preflight(...)`: before any GPU is paid for, check that the gateway, the key and the
workspace can carry N desktops for H hours. Every block of the D1 run (rate limit, spend cap, live-desktop quota,
host capacity, control-plane health, a task set nobody had validated) would have shown up here.

    drums preflight --desktops 128 --hours 3 --task-set 0b5c2a52-... [--model-gpus 2:6] [--json]

    report = drums.preflight(desktops=128, hours=3, task_set="0b5c2a52-...")
    if not report.ok:
        print(report.table())

What is checked, and where the numbers come from (only reads; nothing is created):

    gateway        GET /v1/health answers, how fast (control-plane health; the gateway reads the database to answer)
    console        the console URL answers (sign-in page)
    auth           the key is accepted, not revoked, and does not expire during the run (GET /v1/api-keys, by prefix);
                   on a gateway in desktop-token mode (GET /auth says token or both), the key is exchanged at the
                   console first, so a console that cannot issue a token shows up here, not as a gateway failure
    scopes         computers:read + computers:write (required; computers:write also opens runs); usage:read wanted
    host capacity  free desktops on the bound host >= N (GET /v1/health `capacity`), and the host takes creates
                   (drumsd ok, no stale image checkpoint)
    rate limit     the per-key limit vs N x REQUESTS_PER_DESKTOP_S, and its burst vs N x BURST_PER_DESKTOP
    quota          the workspace's live-desktop limit, minus what is live now, >= N
    spend cap      spent this period + N x H x the desktop-hour price <= the cap
    task set       (with --task-set) registered for this workspace, and validated (oracle accepted, none rejected)

The rate limit, quota, spend cap and price come from `GET /v1/limits` (scope usage:read; Drums-Labs/drums#49, shape
in `_limits_view`). On a gateway that does not serve it yet, those rows say "not checked" and why, and
`--desktop-hour-usd` supplies the price for the projection. A row that could not be checked never fails the
preflight; `--strict` does.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import AuthError, DrumsError, NotFound

#: D1 rule of thumb (2026-11-16, 128 desktops on Qwen3.5-27B): each ACTIVE desktop costs about one action and one
#: screenshot per turn every few seconds through the gateway, about 0.5 requests per second (128 desktops needed about
#: 60 req/s). A run of N desktops needs a per-key rate of at least N x this.
REQUESTS_PER_DESKTOP_S = 0.5
#: A run's start: every desktop is created and takes its first screenshot within a few seconds, so the burst
#: (bucket size) should hold about one request per desktop.
BURST_PER_DESKTOP = 1.0
#: The gateway's default per-key limit (RATE_LIMIT_PER_S / RATE_LIMIT_BURST unset): a pilot setting.
DEFAULT_RATE_PER_S, DEFAULT_RATE_BURST = 20, 60
REQUIRED_SCOPES = ("computers:read", "computers:write")
#: runs:write is not wanted: the gateway opens trainer runs with computers:write too (Drums-Labs/drums#41)
WANTED_SCOPES = {"usage:read": "usage and cost reads (and GET /v1/limits, so the quota and spend rows) are refused"}
#: /v1/health slower than this is a warning, ten times slower a failure (the 2026-11-16 outage answered in 10 to 90 s)
HEALTH_SLOW_S = 2.0
DEFAULT_CONSOLE_URL = "https://console.drums.run"

PASS, FAIL, WARN, SKIP, INFO = "pass", "fail", "warn", "not checked", "info"
_KEY = re.compile(r"^drums_sk_([a-z2-7]{12})_")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class Check:
    """One row: `name`, `status` (pass, fail, warn, not checked, info), `detail`, and for anything but a pass the
    `fix` and a console `link`."""

    def __init__(self, name: str, status: str, detail: str, fix: str | None = None, link: str | None = None):
        self.name, self.status, self.detail, self.fix, self.link = name, status, detail, fix, link

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix, "link": self.link}

    def __repr__(self):
        return f"<Check {self.name} {self.status}: {self.detail}>"


class PreflightReport:
    """The checks, in order. `ok` is False when any check failed; `exit_code(strict)` is what `drums preflight`
    returns (1 on a failure; with strict also on a warning or a row that could not be checked)."""

    def __init__(self, desktops: int, hours: float, checks: list, need: dict):
        self.desktops, self.hours, self.checks, self.need = desktops, hours, checks, need

    @property
    def ok(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)

    def exit_code(self, strict: bool = False) -> int:
        if not self.ok:
            return 1
        if strict and any(c.status in (WARN, SKIP) for c in self.checks):
            return 1
        return 0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "desktops": self.desktops, "hours": self.hours, "need": self.need,
                "checks": [c.to_dict() for c in self.checks]}

    def table(self) -> str:
        w = max(len(c.name) for c in self.checks) if self.checks else 5
        lines = [f"drums preflight: {self.desktops} desktops for {self.hours:g} h", "",
                 f"{'CHECK'.ljust(w)}  {'RESULT'.ljust(11)}  DETAIL"]
        for c in self.checks:
            lines.append(f"{c.name.ljust(w)}  {c.status.upper().ljust(11)}  {c.detail}")
        todo = [c for c in self.checks if c.status in (FAIL, WARN, SKIP) and (c.fix or c.link)]
        if todo:
            lines += ["", "To do:"]
            for c in todo:
                lines.append(f"  [{c.status}] {c.name}: {c.fix or ''}{(' ' + c.link) if c.link else ''}")
        n_fail = sum(c.status == FAIL for c in self.checks)
        n_skip = sum(c.status == SKIP for c in self.checks)
        lines += ["", ("PASS" if self.ok else f"FAIL ({n_fail} check{'s' if n_fail != 1 else ''} failed)")
                  + (f"; {n_skip} could not be checked" if n_skip else "")]
        return "\n".join(lines) + "\n"


def parse_model_gpus(v) -> dict | None:
    """'8' (total), '2:6' or '2,6' (inference:trainer) -> {"inference", "trainer", "total"}; None for None."""
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return {"inference": None, "trainer": None, "total": v}
    parts = [p for p in re.split(r"[:,]", str(v).strip()) if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"--model-gpus takes a GPU count (8) or inference:trainer (2:6); got {v!r}") from None
    if not nums or any(n < 0 for n in nums) or len(nums) > 2:
        raise ValueError(f"--model-gpus takes a GPU count (8) or inference:trainer (2:6); got {v!r}")
    if len(nums) == 1:
        return {"inference": None, "trainer": None, "total": nums[0]}
    return {"inference": nums[0], "trainer": nums[1], "total": nums[0] + nums[1]}


def _num(d, *keys):
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                pass
    return None


def _limits_view(doc) -> dict:
    """The fields preflight reads from `GET /v1/limits` (served by Drums-Labs/drums#49; any of them may be missing,
    and the gateway's extra fields are ignored):

        {"workspace": {"max_live_computers", "live_computers", "max_spend_usd", "spent_usd", "period_end",
                       "rate_limit_profile"},
         "rate_limit": {"per_s", "burst"},
         "price": {"desktop_hour_usd"},
         "host": {"id", "status", "max_desktops", "live", "free", "admission"}}

    `host.admission` is open, busy (the create gate is full right now), full, down (drumsd did not answer), or the
    host status when it is not active. `host.live` / `free` may lag a burst of creates by up to 30 s.
    """
    d = doc if isinstance(doc, dict) else {}
    ws = d.get("workspace") if isinstance(d.get("workspace"), dict) else d
    rl = d.get("rate_limit") if isinstance(d.get("rate_limit"), dict) else {}
    price = d.get("price") if isinstance(d.get("price"), dict) else {}
    host = d.get("host") if isinstance(d.get("host"), dict) else {}
    return {
        "max_live": _num(ws, "max_live_computers", "max_live"),
        "live": _num(ws, "live_computers", "live"),
        "max_spend": _num(ws, "max_spend_usd"),
        "cap_set": "max_spend_usd" in ws,
        "spent": _num(ws, "spent_usd", "period_spend_usd"),
        "period_end": ws.get("period_end"),
        "rate_per_s": _num(rl, "per_s", "rate_per_s"),
        "rate_burst": _num(rl, "burst"),
        "desktop_hour_usd": _num(price, "desktop_hour_usd", "usd_per_desktop_hour"),
        "host_id": host.get("id"),
        "host_status": host.get("status"),
        "host_admission": host.get("admission"),
        "host_max": _num(host, "max_desktops"),
        "host_live": _num(host, "live"),
        "host_free": _num(host, "free"),
        "rate_profile": ws.get("rate_limit_profile") if isinstance(ws.get("rate_limit_profile"), str) else None,
    }


class _Links:
    def __init__(self, console: str, workspace: str | None):
        self.console, self.ws = console.rstrip("/"), workspace

    def page(self, path: str, what: str) -> str:
        if self.ws:
            return f"{self.console}/w/{urllib.parse.quote(self.ws)}/{path}"
        return f"{self.console} ({what})"


def _console_up(url: str, timeout: float) -> tuple:
    """(ok, detail, fix) for a GET of the console's sign-in page; no token is sent. A failed certificate check is tried
    again with certifi's bundle when certifi is installed (python.org's macOS Python has no root certificates)."""
    import ssl  # noqa: PLC0415
    req = urllib.request.Request(url.rstrip("/") + "/sign-in", method="GET", headers={"User-Agent": "drums-preflight"})
    t0 = time.monotonic()
    ctx = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:      # noqa: S310 (the user's or the default https URL)
                return r.status < 500, f"HTTP {r.status} in {time.monotonic() - t0:.1f} s", None
        except urllib.error.HTTPError as e:
            return e.code < 500, f"HTTP {e.code} in {time.monotonic() - t0:.1f} s", "the console is failing: wait before launching"
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLCertVerificationError):
                if ctx is None:
                    try:
                        import certifi  # noqa: PLC0415
                        ctx = ssl.create_default_context(cafile=certifi.where())
                        continue
                    except ImportError:
                        pass
                return False, "TLS certificate not verified by this Python", ("on macOS run \"Install Certificates.command\" "
                                                                             "(in /Applications/Python 3.x/) or pip install certifi")
            return False, f"no answer ({reason})", "the console is down: wait before launching"
    return False, "no answer", "the console is down: wait before launching"


def preflight(desktops: int, hours: float, *, model_gpus=None, task_set: str | None = None, url: str | None = None,
              token: str | None = None, console_url: str | None = None, workspace: str | None = None,
              desktop_hour_usd: float | None = None, gpu_hour_usd: float | None = None, client=None,
              check_console: bool = True, timeout: float = 20.0) -> PreflightReport:
    """Check that the gateway, the key and the workspace can carry `desktops` desktops for `hours` hours.

    url/token default to $DRUMS_URL / $DRUMS_TOKEN (or pass a `client`); console_url to $DRUMS_CONSOLE_URL or
    https://console.drums.run; workspace (the console slug, for links) to $DRUMS_WORKSPACE. desktop_hour_usd
    overrides the gateway's desktop-hour price; gpu_hour_usd with model_gpus adds the GPU cost to the projection
    (GPUs are not billed by Drums, so they are not checked against the spend cap). Reads only; never raises for a
    failed check (see the report), only for bad arguments."""
    from . import Client  # noqa: PLC0415 (import cycle)

    if not isinstance(desktops, int) or desktops < 1:
        raise ValueError("desktops must be a positive integer")
    if not isinstance(hours, (int, float)) or hours <= 0:
        raise ValueError("hours must be a positive number")
    gpus = parse_model_gpus(model_gpus)
    if client is None:
        client = Client(url or os.environ.get("DRUMS_URL") or "", token if token is not None else os.environ.get("DRUMS_TOKEN"),
                        timeout=timeout, retry_s=3.0)
    tok = client.token or ""
    # desktop-token mode: GET <url>/auth, then the key goes to the console's token URL. Do it first, so a console that
    # cannot issue a token is reported as such and not as "the gateway is down".
    cred = client._cred
    token_err = None
    try:
        cred.bearer(client)
    except AuthError as e:
        token_err = e
    except DrumsError as e:
        if cred.mode in ("token", "both"):
            token_err = e
    token_mode = cred.mode if cred.mode in ("token", "both") else None
    token_origin = _origin(cred.token_url) if cred.token_url else None
    console = (console_url or os.environ.get("DRUMS_CONSOLE_URL") or token_origin or DEFAULT_CONSOLE_URL).rstrip("/")
    links = _Links(console, workspace or os.environ.get("DRUMS_WORKSPACE"))
    need_rate = desktops * REQUESTS_PER_DESKTOP_S
    need_burst = desktops * BURST_PER_DESKTOP
    need = {"requests_per_s": need_rate, "burst": need_burst, "requests_per_desktop_s": REQUESTS_PER_DESKTOP_S,
            "desktop_hours": desktops * hours}
    checks: list = []
    add = checks.append

    # ---- token exchange (gateways in desktop-token mode) ------------------------------------------------------------
    if token_err is not None:
        add(Check("gateway", PASS, f"{client.url} answers (auth mode {token_mode or 'token'})"))
        if isinstance(token_err, AuthError):
            add(Check("auth", FAIL, f"the console refused the key when asked for a desktop token: {token_err.message}",
                      "create a key (or check DRUMS_TOKEN has no stray space or newline)", links.page("api-keys", "API keys")))
        else:
            add(Check("auth", FAIL, f"no desktop token: {token_err.message}",
                      "this gateway takes desktop tokens only; the console must answer to issue one: wait, or report it",
                      token_origin or console))
        return _finish(desktops, hours, checks, need, gpus, gpu_hour_usd, check_console, console, timeout)

    # ---- gateway (control plane) + auth ------------------------------------------------------------------------------
    health, t0 = None, time.monotonic()
    try:
        health = client.call("GET", "/v1/health", timeout=timeout)
        took = time.monotonic() - t0
        if took > HEALTH_SLOW_S * 10:
            add(Check("gateway", FAIL, f"/v1/health answered in {took:.1f} s: the control plane is overloaded",
                      "wait for it to recover before launching; a run would see 503s", links.page("overview", "Overview")))
        elif took > HEALTH_SLOW_S:
            add(Check("gateway", WARN, f"/v1/health answered in {took:.1f} s (slow; normal is well under {HEALTH_SLOW_S:g} s)",
                      "launch with fewer desktops first, or wait", links.page("overview", "Overview")))
        else:
            add(Check("gateway", PASS, f"{client.url} answered in {took:.2f} s (drumsd {health.get('version', '?') if isinstance(health, dict) else '?'})"))
    except AuthError as e:
        add(Check("gateway", PASS, f"{client.url} answers"))
        add(Check("auth", FAIL, f"the key was refused: {e.message}",
                  "create a key (or check DRUMS_TOKEN has no stray space or newline)", links.page("api-keys", "API keys")))
        return _finish(desktops, hours, checks, need, gpus, gpu_hour_usd, check_console, console, timeout)
    except DrumsError as e:
        if e.status == 403:
            add(Check("gateway", PASS, f"{client.url} answers"))
            add(Check("auth", FAIL, f"the key was refused here: {e.message}",
                      "use the gateway URL of the key's own workspace (shown with the key in the console)", links.page("api-keys", "API keys")))
            return _finish(desktops, hours, checks, need, gpus, gpu_hour_usd, check_console, console, timeout)
        add(Check("gateway", FAIL, f"{client.url or '(no DRUMS_URL)'}: {e.message if e.status else e}",
                  "check DRUMS_URL (it ends in /gw for Drums Cloud) and that the gateway is up", links.page("overview", "Overview")))
        return _finish(desktops, hours, checks, need, gpus, gpu_hour_usd, check_console, console, timeout, auth_unknown=True)
    health = health if isinstance(health, dict) else {}

    # the key's own row: scopes and expiry
    m = _KEY.match(tok)
    key_row = None
    try:
        rows = (client.call("GET", "/v1/api-keys", timeout=timeout) or {}).get("items") or []
        key_row = next((r for r in rows if m and r.get("prefix") == m.group(1)), None)
    except DrumsError as e:
        add(Check("auth", PASS, f"the key is accepted (its metadata could not be read: {e.message})"))
    if key_row is not None:
        exp = key_row.get("expires_at")
        exp_s = _epoch(exp) if exp else None
        if key_row.get("revoked_at"):
            add(Check("auth", FAIL, "the key is revoked", "create a new key", links.page("api-keys", "API keys")))
        elif exp_s is not None and exp_s < time.time() + hours * 3600:
            left = max(0.0, (exp_s - time.time()) / 3600)
            add(Check("auth", FAIL, f"the key expires in {left:.1f} h, before the {hours:g} h run ends ({exp})",
                      "create a key that outlives the run", links.page("api-keys", "API keys")))
        else:
            add(_auth_pass(f"key {key_row.get('name') or m.group(1)!s} accepted" + (f", expires {exp}" if exp else ", no expiry"),
                           cred, token_mode, token_origin, links))
        scopes = set(key_row.get("scopes") or [])
        missing = [s for s in REQUIRED_SCOPES if s not in scopes]
        wanted = [s for s in WANTED_SCOPES if s not in scopes]
        if missing:
            add(Check("scopes", FAIL, f"missing {', '.join(missing)} (has {', '.join(sorted(scopes)) or 'none'})",
                      "create a key with computers:read and computers:write", links.page("api-keys", "API keys")))
        elif wanted:
            add(Check("scopes", WARN, f"no {', '.join(wanted)}: " + "; ".join(WANTED_SCOPES[s] for s in wanted),
                      f"add {', '.join(wanted)} to the key", links.page("api-keys", "API keys")))
        else:
            add(Check("scopes", PASS, ", ".join(sorted(scopes))))
    else:
        if not any(c.name == "auth" for c in checks):
            add(_auth_pass("the key is accepted", cred, token_mode, token_origin, links))
        add(Check("scopes", SKIP, "the key's row was not found by its prefix, so its scopes and expiry are unknown",
                  "check the key's scopes on the API keys page", links.page("api-keys", "API keys")))

    # ---- the limits summary (rate limit, quota, spend, price) -------------------------------------------------------
    lim, lim_why = None, None
    try:
        lim = _limits_view(client.call("GET", "/v1/limits", timeout=timeout))
    except NotFound:
        lim_why = "this gateway does not publish GET /v1/limits yet"
    except DrumsError as e:
        if e.status == 403:
            lim_why = "the key has no usage:read, which GET /v1/limits needs"
        else:
            lim_why = f"GET /v1/limits failed: {e.message if e.status else e}"

    # host capacity + admission
    cap = health.get("capacity") if isinstance(health.get("capacity"), dict) else {}
    live, mx = _num(cap, "live"), _num(cap, "max")
    if (mx is None or live is None) and lim and lim["host_max"] is not None:
        mx = lim["host_max"]
        live = lim["host_live"] if lim["host_live"] is not None else (mx - lim["host_free"] if lim["host_free"] is not None else None)
    if mx is not None and live is not None:
        free = int(mx - live)
        if free >= desktops:
            add(Check("host capacity", PASS, f"{free} of {int(mx)} desktops free on the bound host"))
        else:
            add(Check("host capacity", FAIL, f"{free} of {int(mx)} desktops free on the bound host; {desktops} needed",
                      f"close idle desktops, or ask for a bigger host (or run {max(free, 0)} desktops)", links.page("computers", "Computers")))
    else:
        add(Check("host capacity", SKIP, "the health reply carries no capacity"))
    stale = [c.get("id") for c in health.get("checkpoints") or [] if isinstance(c, dict) and c.get("stale_reason")]
    host_status = lim.get("host_status") if lim else None
    admission = lim.get("host_admission") if lim else None
    if health.get("ok") is False or admission == "down":
        add(Check("admission", FAIL, "drumsd on the bound host reports not ok: creates will be refused",
                  "the host needs an operator; report it with the request id", links.page("overview", "Overview")))
    elif host_status and host_status != "active":
        add(Check("admission", FAIL, f"the bound host is {host_status}: new desktops are refused", "wait, or ask for another host",
                  links.page("overview", "Overview")))
    elif stale:
        add(Check("admission", WARN, f"stale image checkpoints (creates from them are refused): {', '.join(map(str, stale))}",
                  "use another image or ask an operator to reinstall it", links.page("templates", "Desktop images")))
    else:
        add(Check("admission", PASS, "drumsd ok, no stale checkpoint" + (f", host {host_status}" if host_status else "")
                  + (f", creates {admission}" if admission and admission != "open" else "")))

    # rate limit
    if lim and lim["rate_per_s"] is not None:
        per_s, burst = lim["rate_per_s"], lim["rate_burst"]
        if per_s < need_rate or (burst is not None and burst < need_burst):
            add(Check("rate limit", FAIL, f"{per_s:g} req/s, burst {burst if burst is not None else '?'}: {desktops} desktops need about "
                      f"{need_rate:g} req/s and a burst of {need_burst:g} ({REQUESTS_PER_DESKTOP_S:g} req/s per active desktop)",
                      "ask for a training rate profile", links.page("usage", "Usage")))
        else:
            add(Check("rate limit", PASS, f"{per_s:g} req/s, burst {burst if burst is not None else '?'}; need about {need_rate:g} req/s"
                      + (f" (profile {lim['rate_profile']})" if lim.get("rate_profile") else "")))
    else:
        verdict = WARN if need_rate > DEFAULT_RATE_PER_S or need_burst > DEFAULT_RATE_BURST else SKIP
        add(Check("rate limit", verdict, f"not checked ({lim_why or 'GET /v1/limits has no rate_limit'}); {desktops} desktops need about "
                  f"{need_rate:g} req/s, burst {need_burst:g}. The gateway's default is {DEFAULT_RATE_PER_S} req/s, burst {DEFAULT_RATE_BURST}",
                  "confirm the per-key limit of your host with support before a large run" if verdict == WARN else None))

    # quota
    if lim and lim["max_live"] is not None:
        in_use = lim["live"] or 0.0
        if lim["max_live"] - in_use >= desktops:
            add(Check("quota", PASS, f"{int(lim['max_live'] - in_use)} more live desktops allowed (limit {int(lim['max_live'])}, {int(in_use)} live)"))
        else:
            add(Check("quota", FAIL, f"the workspace may run {int(lim['max_live'])} live desktops ({int(in_use)} live now); {desktops} needed",
                      "ask for a higher live-desktop quota", links.page("usage", "Usage")))
    else:
        add(Check("quota", SKIP, f"not checked ({lim_why or 'GET /v1/limits has no max_live_computers'})",
                  "see the workspace's live-desktop limit", links.page("usage", "Usage")))

    # spend
    price = desktop_hour_usd if desktop_hour_usd is not None else (lim["desktop_hour_usd"] if lim else None)
    if price is not None:
        projected = desktops * hours * float(price)
        need["desktop_hour_usd"] = float(price)
        need["projected_desktop_usd"] = round(projected, 2)
        if lim and lim["cap_set"]:
            cap_usd, spent = lim["max_spend"], lim["spent"] or 0.0
            if cap_usd is None:
                add(Check("spend cap", PASS, f"no cap; projected {projected:,.2f} USD ({desktops} x {hours:g} h x {price:g} USD)"))
            elif spent + projected > cap_usd:
                add(Check("spend cap", FAIL, f"{spent:,.2f} spent + {projected:,.2f} projected > cap {cap_usd:,.2f} USD "
                          f"({desktops} x {hours:g} h x {price:g} USD per desktop-hour)",
                          f"raise the cap to at least {spent + projected:,.0f} USD or run fewer desktop-hours", links.page("usage", "Usage")))
            else:
                add(Check("spend cap", PASS, f"{spent:,.2f} spent + {projected:,.2f} projected <= cap {cap_usd:,.2f} USD"))
        else:
            add(Check("spend cap", SKIP, f"projected {projected:,.2f} USD ({desktops} x {hours:g} h x {price:g} USD); the cap was not checked "
                      f"({lim_why or 'GET /v1/limits has no max_spend_usd'})",
                      f"make sure the spend cap leaves at least {projected:,.0f} USD", links.page("usage", "Usage")))
    else:
        add(Check("spend cap", SKIP, f"not checked: no desktop-hour price ({lim_why or 'GET /v1/limits has no price'}); "
                  f"{desktops * hours:g} desktop-hours", "pass --desktop-hour-usd to project the cost", links.page("usage", "Usage")))

    # task set
    if task_set:
        add(_task_set_check(client, task_set, lim, links, timeout))

    return _finish(desktops, hours, checks, need, gpus, gpu_hour_usd, check_console, console, timeout)


def _origin(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def _auth_pass(detail: str, cred, token_mode, token_origin, links) -> Check:
    """The auth row once the gateway accepted the request: say how (desktop token or API key)."""
    if token_mode and cred.using_token:
        return Check("auth", PASS, f"{detail}; desktop token from {token_origin} (gateway auth mode {token_mode})")
    if token_mode == "both":
        return Check("auth", WARN, f"{detail}, but as the API key: the gateway takes desktop tokens and the exchange "
                     f"failed ({cred.last_error or 'no token'})",
                     "the run works on the key; check the console answers so the data path does not depend on it",
                     token_origin or links.console)
    return Check("auth", PASS, detail)


def _task_set_check(client, task_set: str, lim, links, timeout) -> Check:
    try:
        if _UUID.match(task_set):
            ts = client.call("GET", f"/v1/task-sets/{urllib.parse.quote(task_set)}", timeout=timeout)
        else:
            items = (client.call("GET", "/v1/task-sets?limit=200", timeout=timeout) or {}).get("items") or []
            hit = [t for t in items if t.get("name") == task_set]
            if not hit:
                return Check("task set", FAIL, f"no task set named {task_set!r} in this workspace",
                             "register it (drums_tasks register, or Tasks > New in the console)", links.page("tasks", "Tasks"))
            ts = hit[0]
    except NotFound:
        return Check("task set", FAIL, f"task set {task_set} is not registered for this workspace",
                     "register it (drums_tasks register, or Tasks > New in the console)", links.page("tasks", "Tasks"))
    except DrumsError as e:
        return Check("task set", SKIP, f"could not read task set {task_set}: {e.message if e.status else e}")
    tid = ts.get("id") or task_set
    link = links.page(f"tasks/{tid}", "Tasks")
    v = ts.get("validation") if isinstance(ts.get("validation"), dict) else None
    name = f"{ts.get('name') or tid} ({ts.get('task_count', '?')} tasks)"
    if not v and ts.get("platform"):
        return Check("task set", WARN, f"{name} is a platform set with no validation record for this host",
                     "validate the tasks you train on (oracle 1.0, near misses 0.0) before a long run", link)
    if not v:
        return Check("task set", FAIL, f"{name} is registered but was never validated on a host",
                     "run the validator on the host (oracle 1.0 and every near miss 0.0) and register the result", link)
    acc, rej = int(v.get("accepted") or 0), int(v.get("rejected") or 0)
    host = v.get("host_id")
    where = f"on {host}" if host else "on a host"
    if acc == 0 or rej > 0:
        return Check("task set", FAIL, f"{name}: validation {where} accepted {acc}, rejected {rej}",
                     "fix or drop the rejected tasks and validate again", link)
    bound = lim.get("host_id") if lim else None
    if bound and host and bound != host:
        return Check("task set", WARN, f"{name}: validated on {host}, but this workspace is bound to {bound}",
                     "validate it on the bound host", link)
    return Check("task set", PASS, f"{name}: validated {where} at {v.get('at', '?')}, accepted {acc}, rejected 0"
                 + ("" if bound else " (the bound host could not be compared)"))


def _finish(desktops, hours, checks, need, gpus, gpu_hour_usd, check_console, console, timeout, auth_unknown=False) -> PreflightReport:
    if check_console:
        ok, detail, fix = _console_up(console, min(timeout, 10.0))
        checks.insert(1 if checks else 0, Check("console", PASS if ok else FAIL, f"{console}: {detail}", None if ok else fix,
                                                None if ok else console))
    if auth_unknown:
        checks.append(Check("auth", SKIP, "not checked: the gateway did not answer"))
    if gpus:
        split = f"{gpus['inference']} inference + {gpus['trainer']} trainer" if gpus["inference"] is not None else f"{gpus['total']}"
        detail = f"{split} GPUs for the model (not billed by Drums)"
        if gpu_hour_usd is not None:
            gpu_usd = gpus["total"] * hours * float(gpu_hour_usd)
            need["projected_gpu_usd"] = round(gpu_usd, 2)
            detail += f"; projected {gpu_usd:,.2f} USD at {gpu_hour_usd:g} USD per GPU-hour"
        detail += (". In D1 (Qwen3.5-27B, 2 inference + 6 trainer GPUs) the trainer kept 36 to 47 of 128 desktops in "
                   "flight; size N to what your trainer keeps in flight")
        checks.append(Check("model GPUs", INFO, detail))
    return PreflightReport(desktops, hours, checks, need)


def _epoch(iso: str) -> float | None:
    import datetime as dt  # noqa: PLC0415
    try:
        s = iso.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except (ValueError, AttributeError):
        return None


def add_arguments(ap):
    ap.add_argument("--desktops", type=int, required=True, metavar="N", help="desktops the run keeps live")
    ap.add_argument("--hours", type=float, required=True, metavar="H", help="how long the run lasts")
    ap.add_argument("--model-gpus", metavar="G", help="GPUs for the model: a count (8) or inference:trainer (2:6)")
    ap.add_argument("--task-set", metavar="ID", help="a registered task set (id or name) the run uses")
    ap.add_argument("--desktop-hour-usd", type=float, metavar="USD", help="desktop-hour price for the projection (default: the gateway's)")
    ap.add_argument("--gpu-hour-usd", type=float, metavar="USD", help="with --model-gpus: GPU-hour price, for the total projection")
    ap.add_argument("--url", metavar="URL", help="the gateway URL (default $DRUMS_URL)")
    ap.add_argument("--console-url", metavar="URL", help=f"the console (default $DRUMS_CONSOLE_URL or {DEFAULT_CONSOLE_URL})")
    ap.add_argument("--workspace", metavar="SLUG", help="the console workspace slug, for links (default $DRUMS_WORKSPACE)")
    ap.add_argument("--no-console", action="store_true", help="do not check that the console answers")
    ap.add_argument("--strict", action="store_true", help="exit 1 also on a warning or a check that could not run")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--allow-insecure-http", action="store_true", help="allow the token over plain http to a non-loopback host")
    return ap


def run(a, out, err) -> int:
    from . import Client  # noqa: PLC0415
    from .errors import InsecureTransport  # noqa: PLC0415
    from .mcp import read_token  # noqa: PLC0415

    url = (a.url or os.environ.get("DRUMS_URL") or "").strip()
    token = read_token()
    if not url:
        err.write("drums preflight: no gateway URL: pass --url or set DRUMS_URL (Drums Cloud: https://<host>/gw)\n")
        return 2
    if not token:
        err.write("drums preflight: no key: set DRUMS_TOKEN (or DRUMS_TOKEN_FILE)\n")
        return 2
    if a.desktops < 1 or a.hours <= 0:
        err.write("drums preflight: --desktops must be at least 1 and --hours above 0\n")
        return 2
    try:
        parse_model_gpus(a.model_gpus)
        client = Client(url, token, timeout=20.0, retry_s=3.0, allow_insecure_http=True if a.allow_insecure_http else None)
    except ValueError as e:
        err.write(f"drums preflight: {e}\n")
        return 2
    except InsecureTransport as e:
        err.write(f"drums preflight: {e}\n")
        return 2
    rep = preflight(a.desktops, a.hours, model_gpus=a.model_gpus, task_set=a.task_set, client=client, console_url=a.console_url,
                    workspace=a.workspace, desktop_hour_usd=a.desktop_hour_usd, gpu_hour_usd=a.gpu_hour_usd,
                    check_console=not a.no_console)
    out.write(json.dumps(rep.to_dict(), indent=2) + "\n" if a.json else rep.table())
    return rep.exit_code(a.strict)
