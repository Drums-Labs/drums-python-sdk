"""An MCP server (stdio, JSON-RPC 2.0) that gives an agent one Drums desktop with hybrid tools.

    python -m drums.mcp --env s123                  # bind an existing env
    python -m drums.mcp --create [--task task.json] # create one (closed when the server exits)

Tools: everything in drums.hybrid.TOOLS (gui, screenshot, shell, read_file, write_file, list_dir, office_convert,
spreadsheet_read, browser_tabs, browser_open) plus `verify` (the task's reward with its audit). Screenshots come back as
MCP image content. Uses DRUMS_URL / DRUMS_TOKEN / DRUMS_TOKEN_FILE like drums.connect(). Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys

from . import connect
from .hybrid import TOOLS, HybridTools

PROTOCOL = "2025-06-18"
VERIFY = {"name": "verify", "description": "Score the desktop's current state with the task's checker; returns score, valid and the reward audit.",
          "parameters": {"type": "object", "properties": {}}}


def tool_list():
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["parameters"]} for t in TOOLS + [VERIFY]]


class Server:
    def __init__(self, env):
        self.env = env
        self.tools = HybridTools(env)

    def call(self, name, args):
        if name == "verify":
            r = self.env.verify(strict=False)
            return [{"type": "text", "text": json.dumps({"score": r.score, "valid": r.valid, "trusted": r.trusted,
                                                         "audit_flags": r.audit_flags, "checker_error": r.checker_error})}]
        out = self.tools.call(name, args)
        if isinstance(out, (bytes, bytearray)):
            return [{"type": "image", "data": base64.b64encode(out).decode(), "mimeType": "image/png"}]
        return [{"type": "text", "text": json.dumps(out)}]

    def handle(self, msg):
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:          # a notification (e.g. notifications/initialized): no reply
            return None
        try:
            if method == "initialize":
                res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "drums", "version": "0.2"}}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": tool_list()}
            elif method == "tools/call":
                try:
                    res = {"content": self.call(params.get("name"), params.get("arguments") or {}), "isError": False}
                except Exception as e:  # noqa: BLE001 - a tool failure is a tool result, not a protocol error
                    res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"[:1000]}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": res}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)[:500]}}

    def serve(self, inp=sys.stdin, out=sys.stdout):
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                out.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
                out.flush()
                continue
            r = self.handle(msg)
            if r is not None:
                out.write(json.dumps(r) + "\n")
                out.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--env")
    g.add_argument("--create", action="store_true")
    ap.add_argument("--task")
    a = ap.parse_args(argv)
    c = connect()
    if a.env:
        from . import Env
        env = Env(c, c.call("GET", f"/v1/slots/{a.env}"))
    else:
        env = c.create(task=json.load(open(a.task)) if a.task else None)
        env.wait_valid()
    try:
        Server(env).serve()
    finally:
        if a.create:
            env.close()


if __name__ == "__main__":
    main()
