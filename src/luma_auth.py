"""One-time Luma MCP OAuth login (runs in the MAIN thread).

    python -m src.luma_auth            # browser login, saves tokens, lists tools
    python -m src.luma_auth --status   # is there a token? where?
    python -m src.luma_auth --logout   # delete the token file

Why a separate entrypoint: strands' MCPClient drives its transport from a
background thread, so an interactive browser flow cannot run there. This script
uses the *same* OAuthClientProvider + FileTokenStorage as src.luma_mcp, so once
it has saved tokens the agent refreshes them silently.

Prerequisite: the CIMD document (docs/luma-client.json) must be reachable at
LUMA_CLIENT_METADATA_URL (GitHub Pages serving /docs). Luma fetches it during
the token exchange and validates the redirect URI against it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional

from src import luma_mcp


async def _login() -> tuple[list[str], dict]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    auth = luma_mcp.build_oauth_provider()
    async with streamablehttp_client(luma_mcp.LUMA_MCP_URL, auth=auth) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            me: dict = {}
            try:
                res = await session.call_tool("get_self", {})
                parsed = luma_mcp._parse_tool_result(
                    {"content": [c.model_dump() for c in res.content], "structuredContent": res.structuredContent}
                )
                if isinstance(parsed, dict):
                    me = parsed
            except Exception as exc:  # noqa: BLE001 - get_self is informational only
                print(f"(get_self failed: {exc})", file=sys.stderr)
            return tools, me


def _display_name(me: dict) -> str:
    for src in (me, me.get("user") or {}, me.get("self") or {}):
        if isinstance(src, dict):
            for key in ("name", "full_name", "username", "email"):
                if src.get(key):
                    return str(src[key])
    return "<unknown>"


def main(argv: Optional[list[str]] = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m src.luma_auth", description=__doc__.split("\n")[0])
    parser.add_argument("--status", action="store_true", help="print whether a token exists and exit")
    parser.add_argument("--logout", action="store_true", help="delete the token file and exit")
    args = parser.parse_args(argv)

    path = luma_mcp.token_file()
    if args.status:
        state = "present" if luma_mcp.has_token() else "missing"
        print(f"Luma token: {state} ({path})")
        print(f"Client ID (CIMD): {luma_mcp.client_metadata_url()}")
        return 0
    if args.logout:
        if path.exists():
            path.unlink()
            print(f"Deleted {path}")
        else:
            print(f"No token file at {path}")
        return 0

    print(f"Logging in to Luma MCP ({luma_mcp.LUMA_MCP_URL}) with client_id {luma_mcp.client_metadata_url()}")
    print(f"Tokens will be saved to {path}")
    try:
        tools, me = asyncio.run(_login())
    except Exception as exc:  # noqa: BLE001 - surface a readable failure
        print(f"\nLogin failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "Check that LUMA_CLIENT_METADATA_URL serves docs/luma-client.json over HTTPS "
            "(GitHub Pages for /docs) and that port 3030 is free.",
            file=sys.stderr,
        )
        return 1
    print(f"\nLuma MCP tools ({len(tools)}): {', '.join(tools)}")
    print(f"Logged in as {_display_name(me)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
