"""Official Luma MCP server (https://mcp.luma.com) client for NopeList.

Luma exposes its API only as an MCP server over streamable HTTP, authenticated
with OAuth **Client ID Metadata Documents** (CIMD). There are no API keys and no
dynamic client registration: the OAuth ``client_id`` *is* the URL of a hosted
JSON document (ours: docs/luma-client.json, served by GitHub Pages at
``LUMA_CLIENT_METADATA_URL``). Verified live 2026-09-21:

  * ``GET https://mcp.luma.com/.well-known/oauth-protected-resource`` ->
    ``authorization_servers: ["https://api.luma.com"]``
  * ``https://api.luma.com/.well-known/oauth-authorization-server`` ->
    ``client_id_metadata_document_supported: true``, PKCE S256, grants
    ``authorization_code`` + ``refresh_token``, auth method ``none`` OK,
    **no** ``registration_endpoint``.
  * ``list_guests`` is HOST/MANAGER-ONLY: for an event the user merely attends
    it answers "You don't have access to this event." That is why Bright Data
    scraping stays the path for attended events (see src/scraper.py).

Threading note: :class:`strands.tools.mcp.MCPClient` runs its transport in a
background thread with its own event loop. The interactive browser login
(``redirect_handler`` / ``callback_handler``) therefore must run once, up front,
in the main thread via ``python -m src.luma_auth``; afterwards the same
:class:`OAuthClientProvider` + :class:`FileTokenStorage` refresh silently.

Import safety: importing this module never touches the network or requires a
token. Clients are built lazily inside each helper.

Environment (loaded from .env by the entrypoints):
  LUMA_CLIENT_METADATA_URL  hosted CIMD JSON (default: the GitHub Pages URL below)
  LUMA_TOKEN_FILE           where OAuth tokens persist (default ~/.nopelist/luma_tokens.json)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from src.models import Guest

log = logging.getLogger("nopelist.luma_mcp")

LUMA_MCP_URL = "https://mcp.luma.com"
DEFAULT_CLIENT_METADATA_URL = "https://boeing23.github.io/cognee_hack/luma-client.json"
DEFAULT_TOKEN_FILE = Path.home() / ".nopelist" / "luma_tokens.json"
CALLBACK_HOST, CALLBACK_PORT = "localhost", 3030
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"
LOGIN_HINT = "Run 'python -m src.luma_auth' once (in a terminal, not inside the agent) to log in to Luma."

HOST_STATUSES = {"host", "manager", "cohost"}
_ACCESS_DENIED_MARKERS = ("don't have access", "do not have access", "not authorized", "forbidden")

#: Event api_ids / URLs / slugs the user hosts, populated by src.discover so the
#: scraper can route hosted events through MCP list_guests instead of Bright Data.
hosted_event_ids: set[str] = set()
#: slug -> 'evt-...' api_id for hosted events (also filled by src.discover).
hosted_event_api_ids: dict[str, str] = {}


class LumaAccessError(RuntimeError):
    """Raised when Luma refuses a call (e.g. list_guests on an event you only attend)."""


class LumaNotLoggedIn(RuntimeError):
    """Raised when a call needs a token and no token file exists."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def token_file() -> Path:
    override = os.getenv("LUMA_TOKEN_FILE", "").strip()
    return Path(override).expanduser() if override else DEFAULT_TOKEN_FILE


def client_metadata_url() -> str:
    return os.getenv("LUMA_CLIENT_METADATA_URL", "").strip() or DEFAULT_CLIENT_METADATA_URL


def has_token() -> bool:
    """True when a persisted OAuth token exists (no network, no validation)."""
    path = token_file()
    try:
        return path.is_file() and bool(json.loads(path.read_text()).get("tokens", {}).get("access_token"))
    except (OSError, ValueError):
        return False


def event_id_from_url(url: str) -> str:
    """'https://luma.com/abc?x=1' -> 'abc' (matches scraper.event_slug semantics)."""
    path = urlparse(url.strip()).path.strip("/")
    return path.split("/")[-1] if path else url.strip()


def is_hosted_event(url_or_id: str) -> bool:
    """Did discover_events_detailed() mark this event (url, slug or evt- id) as hosted?"""
    key = url_or_id.strip()
    return key in hosted_event_ids or event_id_from_url(key) in hosted_event_ids


def hosted_event_api_id(url_or_slug: str) -> Optional[str]:
    """The 'evt-...' id discover recorded for a hosted event, if any."""
    return hosted_event_api_ids.get(event_id_from_url(url_or_slug))


def register_hosted_event(url: str, api_id: Optional[str]) -> None:
    slug = event_id_from_url(url)
    hosted_event_ids.update(k for k in (url.strip(), slug, api_id) if k)
    if api_id:
        hosted_event_api_ids[slug] = str(api_id)


# --------------------------------------------------------------------------- #
# OAuth: token storage + provider
# --------------------------------------------------------------------------- #
class FileTokenStorage(TokenStorage):
    """Persist the OAuthToken as JSON at LUMA_TOKEN_FILE (mode 0600).

    Client info is never stored: with CIMD the SDK synthesizes it from
    ``client_metadata_url``, so get/set_client_info are no-ops.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or token_file()

    async def get_tokens(self) -> Optional[OAuthToken]:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text())
            return OAuthToken.model_validate(data["tokens"])
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Unreadable token file %s (%s); ignoring", self.path, exc)
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"server": LUMA_MCP_URL, "tokens": tokens.model_dump(exclude_none=True)}
        self.path.write_text(json.dumps(payload, indent=2))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        log.info("Saved Luma OAuth tokens to %s", self.path)

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        return None


def _wait_for_callback() -> tuple[str, Optional[str]]:
    """One-shot HTTP server on localhost:3030 that captures ?code=&state= from Luma."""
    got: dict[str, Optional[str]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urlparse(self.path)
            q = parse_qs(parsed.query)
            if parsed.path != "/callback" or "code" not in q:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Waiting for the Luma OAuth callback at /callback?code=...")
                return
            got["code"] = q["code"][0]
            got["state"] = q.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>NopeList is logged in to Luma.</h2><p>You can close this tab.</p>")

        def log_message(self, *args: Any) -> None:  # silence http.server
            return None

    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    try:
        while "code" not in got:
            server.handle_request()
    finally:
        server.server_close()
    return got["code"] or "", got.get("state")


async def _redirect_handler(url: str) -> None:
    print(f"\nOpen this URL to authorize NopeList with Luma:\n  {url}\n", flush=True)
    await asyncio.to_thread(webbrowser.open, url)


async def _callback_handler() -> tuple[str, Optional[str]]:
    print(f"Waiting for Luma to redirect to {REDIRECT_URI} ...", flush=True)
    return await asyncio.to_thread(_wait_for_callback)


def build_oauth_provider() -> OAuthClientProvider:
    """OAuthClientProvider for mcp.luma.com using the hosted CIMD as client_id.

    Tokens are loaded from / saved to LUMA_TOKEN_FILE, so refresh is automatic.
    The browser flow only fires when no usable token (or refresh token) exists.
    """
    return OAuthClientProvider(
        server_url=LUMA_MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="NopeList",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=FileTokenStorage(),
        redirect_handler=_redirect_handler,
        callback_handler=_callback_handler,
        client_metadata_url=client_metadata_url(),
    )


# --------------------------------------------------------------------------- #
# Strands MCPClient
# --------------------------------------------------------------------------- #
def make_mcp_client(require_token: bool = True):
    """A strands MCPClient over streamable HTTP with OAuth auto-refresh.

    Use as a context manager::

        with make_mcp_client() as luma:
            tools = luma.list_tools_sync()

    With ``require_token`` (default) a missing token file raises LumaNotLoggedIn
    instead of letting the browser flow fire inside MCPClient's background thread.
    """
    if require_token and not has_token():
        raise LumaNotLoggedIn(f"No Luma token at {token_file()}. {LOGIN_HINT}")
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    return MCPClient(lambda: streamablehttp_client(LUMA_MCP_URL, auth=build_oauth_provider()))


# --------------------------------------------------------------------------- #
# Result parsing
# --------------------------------------------------------------------------- #
def _parse_tool_result(result: Any) -> Any:
    """Turn an MCPToolResult (dict) into JSON data, tolerating text-block payloads."""
    if not isinstance(result, dict):
        return result
    if result.get("structuredContent"):
        sc = result["structuredContent"]
        # FastMCP-style servers wrap non-object results as {"result": ...}.
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    texts: list[str] = []
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif isinstance(block, dict) and "json" in block:
            return block["json"]
    joined = "\n".join(texts).strip()
    if not joined:
        return None
    try:
        return json.loads(joined)
    except ValueError:
        return joined


def _call(client: Any, name: str, arguments: dict[str, Any]) -> Any:
    """call_tool_sync + defensive parsing; raises LumaAccessError on access errors."""
    args = {k: v for k, v in arguments.items() if v is not None}
    result = client.call_tool_sync(tool_use_id=f"nopelist-{uuid4().hex[:8]}", name=name, arguments=args)
    data = _parse_tool_result(result)
    is_error = isinstance(result, dict) and (result.get("status") == "error" or bool(result.get("isError")))
    message = data if isinstance(data, str) else ("" if data is None else json.dumps(data))
    # Some servers answer 200 with {"error": "..."} / {"message": "..."} instead of isError.
    payload_keys = ("entries", "entity", "api_id", "event", "name", "guests")
    if isinstance(data, dict) and ("error" in data or "message" in data) and not any(k in data for k in payload_keys):
        is_error = True
        message = str(data.get("error") or data.get("message") or message)
    if is_error:
        if any(m in message.lower() for m in _ACCESS_DENIED_MARKERS):
            raise LumaAccessError(
                f"Luma refused {name}: {message.strip().rstrip('.') or 'access denied'}. "
                "list_guests is host/manager-only; for events you merely attend NopeList "
                "reads the guest list via Bright Data instead."
            )
        raise RuntimeError(f"Luma MCP tool {name} failed: {message[:300]}")
    return data


def _entries(data: Any) -> list[dict]:
    if isinstance(data, dict):
        for key in ("entries", "guests", "events", "results", "items"):
            if isinstance(data.get(key), list):
                return [e for e in data[key] if isinstance(e, dict)]
        return []
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    return []


def _paginate(client: Any, name: str, arguments: dict[str, Any], max_pages: int) -> Iterator[dict]:
    cursor: Optional[str] = None
    for _ in range(max(1, max_pages)):
        data = _call(client, name, {**arguments, "cursor": cursor})
        yield from _entries(data)
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor or (isinstance(data, dict) and data.get("has_more") is False):
            break


def _with_client(fn):
    """Run fn(client) inside a fresh MCPClient context (lazy, token required)."""
    with make_mcp_client() as client:
        return fn(client)


# --------------------------------------------------------------------------- #
# Typed helpers (synchronous)
# --------------------------------------------------------------------------- #
def entry_is_hosted(entry: dict) -> bool:
    """True when a list_events entry says the user hosts/manages this event."""
    if entry.get("host_info") or entry.get("manager_info"):
        return True
    status = str((entry.get("guest_info") or {}).get("approval_status") or "").lower()
    return status in HOST_STATUSES or str(entry.get("role") or "").lower() in HOST_STATUSES


def list_my_events(period: str = "future", status: Optional[str] = None, max_pages: int = 3) -> list[dict]:
    """MCP list_events: events the user hosts or attends, with guest_info/host_info."""
    return _with_client(
        lambda c: list(_paginate(c, "list_events", {"period": period, "status": status, "limit": 50}, max_pages))
    )


def get_event(event_id: str) -> dict:
    """MCP get_event for an 'evt-...' id (role-scoped detail)."""
    data = _with_client(lambda c: _call(c, "get_event", {"event_id": event_id}))
    if isinstance(data, dict):
        return data.get("event", data) if isinstance(data.get("event"), dict) and len(data) == 1 else data
    return {"raw": data}


def lookup_entity(slug_or_url: str) -> Optional[dict]:
    """MCP lookup_entity: resolve a luma.com / lu.ma URL or slug to {type, id, ...} or None."""
    data = _with_client(lambda c: _call(c, "lookup_entity", {"slug": slug_or_url.strip()}))
    if isinstance(data, dict):
        entity = data.get("entity", data)
        return entity if isinstance(entity, dict) and entity else None
    return None


def list_event_guests(event_id: str, max_pages: int = 5) -> list[dict]:
    """MCP list_guests (HOST-ONLY). Raises LumaAccessError for merely-attended events."""
    return _with_client(lambda c: list(_paginate(c, "list_guests", {"event_id": event_id, "limit": 50}, max_pages)))


def get_self() -> dict:
    data = _with_client(lambda c: _call(c, "get_self", {}))
    return data if isinstance(data, dict) else {"raw": data}


# --------------------------------------------------------------------------- #
# Mapping to models
# --------------------------------------------------------------------------- #
_INSTA_KEYS = ("instagram_handle", "instagram", "instagram_url")
_X_KEYS = ("twitter_handle", "x_handle", "twitter", "x", "twitter_url", "x_url")


def _clean_handle(value: Any) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if "://" in v or v.startswith("www."):
        v = urlparse(v if "://" in v else f"https://{v}").path
    v = v.strip("/").split("/")[-1].split("?")[0].lstrip("@").strip()
    return v or None


def hosted_event_guests_as_guests(entries: list[dict]) -> list[Guest]:
    """Map Luma guest summaries (list_guests entries) to models.Guest."""
    guests: list[Guest] = []
    for entry in entries:
        src = {**entry, **(entry.get("user") or {}), **(entry.get("guest") or {})}
        name = src.get("name") or " ".join(p for p in (src.get("first_name"), src.get("last_name")) if p).strip()
        if not name:
            continue
        insta = next((_clean_handle(src.get(k)) for k in _INSTA_KEYS if src.get(k)), None)
        x = next((_clean_handle(src.get(k)) for k in _X_KEYS if src.get(k)), None)
        guests.append(Guest(name=str(name).strip(), instagram=insta, x_handle=x))
    return guests
