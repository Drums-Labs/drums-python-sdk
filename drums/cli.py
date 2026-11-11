"""The `drums` command.

    drums mcp [--env ID] [--url URL] [--create] [--task FILE]     # the MCP server on stdio (== python -m drums.mcp)
    drums mcp --print-config claude|claude-local|cursor|codex      # a client config snippet (placeholders, never a key)
    drums version
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__

PLACEHOLDER_URL = "<GATEWAY_URL>"
PLACEHOLDER_KEY = "drums_sk_..."
CLIENTS = ("claude", "claude-local", "cursor", "codex")


def gateway_url(url: str | None = None) -> str:
    """--url, else $DRUMS_URL, else the placeholder <GATEWAY_URL>. Never a token."""
    u = (url or os.environ.get("DRUMS_URL") or "").strip().rstrip("/")
    return u or PLACEHOLDER_URL


def print_config(client: str, url: str | None = None) -> str:
    """The config snippet for one MCP client. The key is always the placeholder `drums_sk_...`: paste your own."""
    g = gateway_url(url)
    if client == "claude":
        return (f'claude mcp add --transport http drums {g}/mcp --header "Authorization: Bearer {PLACEHOLDER_KEY}"\n')
    if client == "claude-local":
        return f"claude mcp add drums -e DRUMS_URL={g} -e DRUMS_TOKEN={PLACEHOLDER_KEY} -- drums mcp\n"
    if client == "cursor":
        return ('{"mcpServers": {"drums": {"url": "' + g + '/mcp", "headers": {"Authorization": "Bearer ' + PLACEHOLDER_KEY + '"}}}}\n')
    if client == "codex":
        return ("[mcp_servers.drums]\n"
                'command = "drums"\n'
                'args = ["mcp"]\n'
                'env = { DRUMS_URL = "' + g + '", DRUMS_TOKEN = "' + PLACEHOLDER_KEY + '" }\n')
    raise ValueError(f"unknown client {client!r}; one of {', '.join(CLIENTS)}")


def build_parser() -> argparse.ArgumentParser:
    from .mcp import add_arguments
    ap = argparse.ArgumentParser(prog="drums", description="Drums: isolated desktops for computer-use agents.")
    sub = ap.add_subparsers(dest="command", metavar="command")
    add_arguments(sub.add_parser("mcp", help="run the MCP server on stdio (or print a client config with --print-config)"))
    sub.add_parser("version", help="print the SDK version")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.command == "version":
        sys.stdout.write(f"drums {__version__}\n")
        return 0
    if a.command == "mcp":
        from .mcp import run
        return run(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
