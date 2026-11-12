"""The Drums MCP server (stdio, JSON-RPC 2.0): the workspace and desktop tools of drums/mcp_tools.json.

    drums mcp                        # == python -m drums.mcp; talks to $DRUMS_URL with $DRUMS_TOKEN
    drums mcp --env <computer_id>    # desktop tools default to this computer when computer_id is omitted
    drums mcp --create [--task FILE] # create one computer, bind it, close it when the server exits

Two layers, one tool list (drums/mcp_tools.json, the same list the gateway's /gw/mcp serves):
- workspace tools (list_templates, create_computer, list_runs, create_api_key, read_audit_log, ...) call the HTTP API
  through `Client.call`; runs, evaluations, API keys and the audit log exist only on the Drums gateway, so against a
  plain drumsd those tools answer "not available on this server (needs the Drums gateway)";
- desktop tools (gui, screenshot, shell, read_file, ..., verify, fork, reset, load_task) act on one computer through
  drums.hybrid.HybridTools and drums.Env.

Framing: one JSON-RPC message per line on stdin, one reply per line on stdout (what MCP stdio clients send); a JSON
array on a line is a batch. Notifications get no reply, unknown methods -32601, unreadable lines -32700. A tool
failure is a tool result with isError true (never a JSON-RPC error). Screenshots are image content (PNG), everything
else one text item holding JSON. Destructive tools (destroy_computer, delete_snapshot, revoke_api_key) are refused
before any request unless `confirm` is true. Token: $DRUMS_TOKEN, else the file $DRUMS_TOKEN_FILE, else
/etc/drums/token. Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse

from . import Client, Env, __version__, connect
from .errors import DrumsError, NotFound
from .hybrid import HybridTools

PROTOCOL = "2025-06-18"
TOOLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_tools.json")
NOT_AVAILABLE = "not available on this server (needs the Drums gateway)"
READ_ONLY = {"screenshot", "browser_tabs", "verify"}
_TOOLS = None


class ToolError(Exception):
    """A tool failure: becomes a tool result with isError true and this one-line text."""


# ---- the tool list -------------------------------------------------------------------------------------------------
def load_tools() -> list:
    """The canonical tool list (drums/mcp_tools.json), as loaded: name, layer, scope, destructive, description, inputSchema."""
    global _TOOLS
    if _TOOLS is None:
        with open(TOOLS_PATH, encoding="utf-8") as f:
            _TOOLS = json.load(f)["tools"]
    return _TOOLS


def tool_by_name() -> dict:
    return {t["name"]: t for t in load_tools()}


def annotations(tool: dict) -> dict:
    name = tool["name"]
    listing = name.startswith(("list_", "get_", "read_"))
    return {"readOnlyHint": listing or name in READ_ONLY, "destructiveHint": bool(tool.get("destructive")),
            "idempotentHint": listing, "openWorldHint": False}


def tool_list() -> list:
    """What tools/list answers: the tools of mcp_tools.json in order, with name, description, inputSchema and annotations."""
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"], "annotations": annotations(t)}
            for t in load_tools()]


# ---- argument validation (the JSON schema subset mcp_tools.json uses) ----------------------------------------------
def _type_ok(v, t: str) -> bool:
    if t == "object":
        return isinstance(v, dict)
    if t == "array":
        return isinstance(v, list)
    if t == "string":
        return isinstance(v, str)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer())
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return True


def _check(v, schema: dict, label: str):
    """The first problem with `v` against `schema` as a sentence naming the field, or None."""
    t = schema.get("type")
    if t is not None and not _type_ok(v, t):
        return f"field '{label}' must be {'an' if t[0] in 'aeiou' else 'a'} {t}"
    if "const" in schema and v != schema["const"]:
        return f"field '{label}' must be {json.dumps(schema['const'])}"
    if "enum" in schema and v not in schema["enum"]:
        return f"field '{label}' must be one of {', '.join(map(str, schema['enum']))}"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in schema and v < schema["minimum"]:
            return f"field '{label}' must be >= {schema['minimum']}"
        if "maximum" in schema and v > schema["maximum"]:
            return f"field '{label}' must be <= {schema['maximum']}"
    if isinstance(v, str):
        if "minLength" in schema and len(v) < schema["minLength"]:
            return f"field '{label}' must be at least {schema['minLength']} characters"
        if "maxLength" in schema and len(v) > schema["maxLength"]:
            return f"field '{label}' must be at most {schema['maxLength']} characters"
        if "pattern" in schema and not re.search(schema["pattern"], v):
            return f"field '{label}' does not match {schema['pattern']}"
    if isinstance(v, list):
        if "minItems" in schema and len(v) < schema["minItems"]:
            return f"field '{label}' needs at least {schema['minItems']} item(s)"
        if "maxItems" in schema and len(v) > schema["maxItems"]:
            return f"field '{label}' takes at most {schema['maxItems']} item(s)"
        if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in v}) != len(v):
            return f"field '{label}' must not repeat an item"
        if "items" in schema:
            for i, x in enumerate(v):
                e = _check(x, schema["items"], f"{label}[{i}]")
                if e:
                    return e
    if isinstance(v, dict):
        props = schema.get("properties") or {}
        for k in schema.get("required") or []:
            if k not in v:
                return f"field '{k}' is required" if label == "arguments" else f"field '{label}.{k}' is required"
        if schema.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    return f"unknown field '{k}'" if label == "arguments" else f"unknown field '{label}.{k}'"
        for k, x in v.items():
            if k in props:
                e = _check(x, props[k], k if label == "arguments" else f"{label}.{k}")
                if e:
                    return e
    return None


def validate(schema: dict, args) -> str | None:
    """The first schema violation of a tool's arguments, naming the field; None when they are fine."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    return _check(args, schema, "arguments")


# ---- the server -----------------------------------------------------------------------------------------------------
def _is_template(rec: dict) -> bool:
    """A checkpoint record is a template (base image) unless it was captured from a computer (a snapshot)."""
    kind = str(rec.get("kind") or "").lower()
    if kind in ("template", "base", "image", "base_image"):
        return True
    if kind in ("snapshot", "state", "saved_state", "fork"):
        return False
    if rec.get("installed"):
        return True
    if rec.get("captured_from") or str(rec.get("id") or "").startswith("snp-"):
        return False
    return True


def _rows(reply, *keys):
    """A list reply, or the list under the first of `keys` (gateway replies are {items, next_cursor})."""
    if isinstance(reply, list):
        return reply
    if isinstance(reply, dict):
        for k in keys + ("items",):
            if isinstance(reply.get(k), list):
                return reply[k]
    return []


def _slim(rec):
    """A computer record without its full gate report (the validity verdict and failed gates stay)."""
    if isinstance(rec, dict):
        return {k: v for k, v in rec.items() if k != "gates"}
    return rec


def _q(**params) -> str:
    p = {k: ("true" if v is True else "false" if v is False else str(v)) for k, v in params.items() if v is not None}
    return "?" + urllib.parse.urlencode(p) if p else ""


class Server:
    """The MCP server over one Client. `default_computer_id` (from --env) fills computer_id for desktop tools when the
    caller leaves it out. `env_factory(computer_id)` returns the object desktop tools act on (default: an Env over the
    client; tests pass a stand-in)."""

    def __init__(self, client: Client | None = None, default_computer_id: str | None = None, env_factory=None, version: str = __version__):
        self.client = client
        self.default_computer_id = default_computer_id
        self.env_factory = env_factory
        self.version = version
        self.tools = tool_by_name()

    # helpers ----------------------------------------------------------------------------------------------------------
    def _env(self, computer_id: str):
        if self.env_factory is not None:
            return self.env_factory(computer_id)
        return Env(self._client(), {"id": computer_id})

    def _client(self) -> Client:
        if self.client is None:
            raise ToolError("no Drums server configured (set DRUMS_URL and DRUMS_TOKEN)")
        return self.client

    def _api(self, method: str, path: str, body=None, headers=None):
        return self._client().call(method, path, body, headers=headers)

    def _gateway(self, method: str, path: str, body=None, headers=None):
        """A call to a route only the gateway has: a plain drumsd's 404 (no body, or 'no such route') is reported as such."""
        try:
            return self._api(method, path, body, headers)
        except NotFound as e:
            msg = str(getattr(e, "message", "") or "")
            if e.body is None or not isinstance(e.body, dict) or "no such route" in msg or "method not allowed" in msg:
                raise ToolError(NOT_AVAILABLE) from None
            raise

    def _task_doc(self, args: dict, required: bool):
        if isinstance(args.get("task"), dict):
            return args["task"]
        if args.get("task_id"):
            return self._api("GET", f"/v1/tasks/{urllib.parse.quote(str(args['task_id']))}")
        if required:
            raise ToolError("one of task_id or task is required")
        return None

    def _checkpoints(self) -> list:
        return _rows(self._api("GET", "/v1/checkpoints"), "checkpoints")

    # workspace tools ----------------------------------------------------------------------------------------------------
    def list_templates(self, a):
        rows = [dict(r, checkpoint=r.get("id")) for r in self._checkpoints() if isinstance(r, dict) and _is_template(r)]
        return {"templates": rows}

    def list_snapshots(self, a):
        return {"snapshots": [r for r in self._checkpoints() if isinstance(r, dict) and not _is_template(r)]}

    def list_computers(self, a):
        q = _q(include_destroyed=True if a.get("include_destroyed") else None, limit=a.get("limit"))
        r = self._api("GET", "/v1/slots" + q)
        rows = [_slim(x) for x in _rows(r, "computers", "slots")]
        if not a.get("include_destroyed"):
            rows = [x for x in rows if not (isinstance(x, dict) and x.get("state") == "destroyed")]
        out = {"computers": rows}
        if isinstance(r, dict) and (r.get("next_after") or r.get("next_cursor")):
            out["next"] = r.get("next_after") or r.get("next_cursor")
        return out

    def create_computer(self, a):
        body = {"checkpoint": a["template"], "task": self._task_doc(a, required=False)}
        if a.get("ttl_s") is not None:
            body["ttl_s"] = a["ttl_s"]
        if a.get("setup") is not None:
            body["setup"] = a["setup"]
        return _slim(self._api("POST", "/v1/slots", body))

    def get_computer(self, a):
        return self._api("GET", f"/v1/slots/{urllib.parse.quote(a['computer_id'])}")

    def destroy_computer(self, a):
        return _slim(self._api("DELETE", f"/v1/slots/{urllib.parse.quote(a['computer_id'])}"))

    def create_snapshot(self, a):
        body = {"id": a["name"]} if a.get("name") else {}
        return self._api("POST", f"/v1/slots/{urllib.parse.quote(a['computer_id'])}/capture", body)

    def delete_snapshot(self, a):
        return self._api("DELETE", f"/v1/checkpoints/{urllib.parse.quote(a['snapshot_id'])}")

    def list_tasks(self, a):
        rows = _rows(self._api("GET", "/v1/tasks" + _q(domain=a.get("domain"))), "tasks")
        limit = int(a.get("limit") or 200)
        return {"tasks": rows[:limit], "total": len(rows)}

    def get_task(self, a):
        return self._api("GET", f"/v1/tasks/{urllib.parse.quote(a['task_id'])}")

    def _list_runs(self, a, kind=None):
        return self._gateway("GET", "/v1/runs" + _q(kind=kind, status=a.get("status"), limit=a.get("limit") or 50))

    def _create_run(self, spec, key):
        return self._gateway("POST", "/v1/runs", spec, headers={"Idempotency-Key": key} if key else None)

    def list_runs(self, a):
        return self._list_runs(a)

    def create_run(self, a):
        return self._create_run(a["spec"], a.get("idempotency_key"))

    def get_run(self, a):
        return self._gateway("GET", f"/v1/runs/{urllib.parse.quote(a['run_id'])}")

    def list_evaluations(self, a):
        return self._list_runs(a, kind="evaluation")

    def create_evaluation(self, a):
        return self._create_run(dict(a["spec"], kind="evaluation"), a.get("idempotency_key"))

    get_evaluation = get_run

    def list_api_keys(self, a):
        return self._gateway("GET", "/v1/api-keys")

    def create_api_key(self, a):
        body = {"name": a["name"], "scopes": a["scopes"]}
        if a.get("expires_in_days") is not None:
            body["expires_in_days"] = a["expires_in_days"]
        return self._gateway("POST", "/v1/api-keys", body)

    def revoke_api_key(self, a):
        return self._gateway("DELETE", f"/v1/api-keys/{urllib.parse.quote(a['key_id'])}")

    def read_audit_log(self, a):
        return self._gateway("GET", "/v1/audit" + _q(action=a.get("action"), limit=a.get("limit") or 50, cursor=a.get("cursor")))

    # desktop tools ------------------------------------------------------------------------------------------------------
    def verify(self, a):
        r = self._env(a["computer_id"]).verify(strict=False)
        return {"score": r.score, "valid": r.valid, "trusted": r.trusted, "audit_flags": r.audit_flags, "checker_error": r.checker_error}

    def fork(self, a):
        fr = self._env(a["computer_id"]).fork(int(a.get("n") or 1), strict=False)
        out = {"children": [getattr(k, "id", k) for k in fr], "seconds": getattr(fr, "seconds", None)}
        if getattr(fr, "state", None):
            out["state"] = str(fr.state)
        failed = [str(x) for x in (getattr(fr, "failed", None) or [])]
        if failed:
            out["failed"] = failed
        return out

    def reset(self, a):
        env = self._env(a["computer_id"])
        r = env.reset()
        return _slim(getattr(r, "record", r))

    def load_task(self, a):
        doc = self._task_doc(a, required=True)
        env = self._env(a["computer_id"])
        r = env.load_task(doc)
        return _slim(getattr(r, "record", r))

    def _hybrid(self, name, a):
        args = {k: v for k, v in a.items() if k != "computer_id"}
        return HybridTools(self._env(a["computer_id"])).call(name, args)

    # dispatch -----------------------------------------------------------------------------------------------------------
    def call(self, name: str, args) -> list:
        """One tool call: returns the content list, or raises ToolError (isError) with a one-line reason."""
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool {name!r}")
        args = dict(args or {}) if isinstance(args, dict) else args
        if isinstance(args, dict) and "computer_id" in (tool["inputSchema"].get("properties") or {}) \
                and args.get("computer_id") in (None, "") and self.default_computer_id:
            args["computer_id"] = self.default_computer_id
        if tool.get("destructive") and not (isinstance(args, dict) and args.get("confirm") is True):
            raise ToolError(f"refused: pass confirm: true to {name}")
        problem = validate(tool["inputSchema"], args)
        if problem:
            raise ToolError(problem)
        try:
            if tool["layer"] == "desktop" and name not in ("verify", "fork", "reset", "load_task"):
                out = self._hybrid(name, args)
            else:
                out = getattr(self, name)(args)
        except ToolError:
            raise
        except DrumsError as e:
            raise ToolError(str(e)[:1000]) from None
        except Exception as e:  # noqa: BLE001 - a tool failure is a tool result, not a protocol error
            raise ToolError(f"{type(e).__name__}: {e}"[:1000]) from None
        if isinstance(out, (bytes, bytearray)):
            return [{"type": "image", "data": base64.b64encode(bytes(out)).decode(), "mimeType": "image/png"}]
        return [{"type": "text", "text": json.dumps(out, default=str)}]

    def handle(self, msg):
        """One JSON-RPC message -> its reply dict, or None for a notification (or anything that is not a request)."""
        if not isinstance(msg, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None or method is None:     # a notification (notifications/initialized, ...) or a client's response: no reply
            return None
        if not isinstance(params, dict):
            params = {}
        try:
            if method == "initialize":
                res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "drums", "version": self.version}}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": tool_list()}
            elif method == "tools/call":
                try:
                    res = {"content": self.call(params.get("name"), params.get("arguments") or {}), "isError": False}
                except ToolError as e:
                    res = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": res}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)[:500]}}

    def serve(self, inp=None, out=None):
        """Read newline-delimited JSON-RPC from `inp` (stdin) and write replies to `out` (stdout) until EOF."""
        inp = sys.stdin if inp is None else inp
        out = sys.stdout if out is None else out
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                if isinstance(msg, list):          # a batch: one reply array, notifications left out
                    replies = [r for r in (self.handle(m) for m in msg) if r is not None]
                    reply = replies or None
                else:
                    reply = self.handle(msg)
            if reply is not None:
                out.write(json.dumps(reply) + "\n")
                out.flush()


# ---- entry point ----------------------------------------------------------------------------------------------------
def read_token() -> str | None:
    """$DRUMS_TOKEN, else the contents of $DRUMS_TOKEN_FILE, else /etc/drums/token when readable."""
    tok = os.environ.get("DRUMS_TOKEN")
    if tok:
        return tok
    tf = os.environ.get("DRUMS_TOKEN_FILE") or ("/etc/drums/token" if os.access("/etc/drums/token", os.R_OK) else None)
    if tf and os.path.exists(tf):
        with open(tf) as f:
            return f.read().strip() or None
    return None


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    ap.add_argument("--env", metavar="ID", help="default computer_id for the desktop tools")
    ap.add_argument("--url", metavar="URL", help="the gateway or drumsd URL (default $DRUMS_URL)")
    ap.add_argument("--create", action="store_true", help="create one computer, bind it, and close it when the server exits")
    ap.add_argument("--task", metavar="FILE", help="with --create: a task document (JSON file) to load")
    ap.add_argument("--print-config", choices=["claude", "claude-local", "cursor", "codex"], metavar="CLIENT",
                    help="print the client config snippet (claude, claude-local, cursor or codex) and exit")
    return ap


def run(a) -> int:
    """Run the server from parsed arguments (drums.cli builds them too)."""
    if a.print_config:
        from .cli import print_config
        sys.stdout.write(print_config(a.print_config, a.url))
        return 0
    if a.env and a.create:
        sys.stderr.write("drums mcp: --env and --create exclude each other\n")
        return 2
    # retry_s: an unreachable server fails a tool call within seconds (a trainer's 60 s is too long for a chat turn)
    c = connect(a.url or os.environ.get("DRUMS_URL"), read_token(), check=False, close_on_exit=bool(a.create), retry_s=5.0)
    env, bound = None, a.env
    if a.create:
        task = None
        if a.task:
            with open(a.task) as f:
                task = json.load(f)
        env = c.create(task=task)
        env.wait_valid()
        bound = env.id
    try:
        Server(c, default_computer_id=bound).serve()
    finally:
        if env is not None:
            env.close()
    return 0


def main(argv=None) -> int:
    ap = add_arguments(argparse.ArgumentParser(prog="python -m drums.mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
