"""Discover which Luma events the user is registered for.

Option A: read data/events.txt -- one Luma URL per line, '#' comments.
Option B: the official Luma MCP server (src/luma_mcp.py). After a one-time
          ``python -m src.luma_auth`` login, ``list_events(period='future')``
          returns every event the user hosts or attends, structured, with
          ``guest_info.approval_status`` / ``host_info`` telling us the role.

Order in discover_events():
  1. NOPELIST_DRY_RUN=1  -> Option A only, zero network.
  2. Logged in (token)   -> Option B, merged with any URLs in events.txt.
  3. Otherwise           -> Option A, with a hint to run src.luma_auth.

Events the user HOSTS are recorded in ``src.luma_mcp.hosted_event_ids`` so the
scraper can pull their guest list via MCP ``list_guests`` (host-only) instead of
Bright Data.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from src import luma_mcp
from src.scraper import ROOT, dry_run, event_slug

log = logging.getLogger("nopelist.discover")

EVENTS_FILE = ROOT / "data" / "events.txt"
GOING_STATUSES = {"approved", "invited", "pending_approval", "waitlist"}


# --------------------------------------------------------------------------- #
# Option A: events.txt
# --------------------------------------------------------------------------- #
def events_file_path() -> Path:
    """data/events.txt unless NOPELIST_EVENTS_FILE overrides it (agent.py --events)."""
    override = os.getenv("NOPELIST_EVENTS_FILE", "").strip()
    return Path(override) if override else EVENTS_FILE


def read_events_file(path: Path | None = None) -> list[str]:
    path = path or events_file_path()
    if not path.exists():
        log.warning("%s not found; no events to scan", path)
        return []
    urls: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            urls.append(line)
    return urls


# --------------------------------------------------------------------------- #
# Option B: Luma MCP
# --------------------------------------------------------------------------- #
def _entry_url(entry: dict[str, Any]) -> str | None:
    url = entry.get("url")
    if url:
        return str(url)
    slug = entry.get("slug") or entry.get("api_id")
    return f"https://luma.com/{slug}" if slug else None


def _mcp_entries() -> list[dict[str, Any]]:
    """Normalise list_events entries to {url, api_id, name, is_host, status}."""
    out: list[dict[str, Any]] = []
    for entry in luma_mcp.list_my_events(period="future"):
        url = _entry_url(entry)
        if not url:
            continue
        status = str((entry.get("guest_info") or {}).get("approval_status") or "").lower()
        is_host = luma_mcp.entry_is_hosted(entry)
        if not (is_host or status in GOING_STATUSES):
            log.debug("skipping %s (status=%r, not going)", url, status)
            continue
        out.append(
            {"url": url, "api_id": entry.get("api_id"), "name": entry.get("name") or event_slug(url),
             "is_host": is_host, "status": status or ("host" if is_host else "")}
        )
    return out


def _file_entries(source: str = "events.txt") -> list[dict[str, Any]]:
    return [{"url": u, "api_id": None, "name": event_slug(u), "is_host": False, "status": "", "source": source}
            for u in read_events_file()]


def _register_hosted(entries: list[dict[str, Any]]) -> None:
    for e in entries:
        if e.get("is_host"):
            luma_mcp.register_hosted_event(e["url"], e.get("api_id"))


def _dedupe(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for e in entries:
        key = event_slug(e["url"]).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def discover_events_detailed() -> list[dict[str, Any]]:
    """Raw entries: {url, api_id, name, is_host, status, source} per event."""
    if dry_run():
        entries = _file_entries()
        log.info("DRY RUN -> Option A (%s): %d events", events_file_path().name, len(entries))
        return entries

    if luma_mcp.has_token():
        try:
            mcp_entries = _mcp_entries()
            for e in mcp_entries:
                e["source"] = "luma-mcp"
            hosted = sum(1 for e in mcp_entries if e["is_host"])
            log.info("Option B (Luma MCP list_events): %d upcoming events (%d hosted)", len(mcp_entries), hosted)
            _register_hosted(mcp_entries)
            file_entries = _file_entries()
            if file_entries:
                log.info("Option A (%s): merging %d more URL(s)", events_file_path().name, len(file_entries))
            return _dedupe(mcp_entries + file_entries)
        except luma_mcp.LumaNotLoggedIn as exc:
            log.warning("%s", exc)
        except Exception as exc:  # noqa: BLE001 - never let discovery kill the demo
            log.error("Option B (Luma MCP) failed (%s); using %s", exc, events_file_path().name)
    else:
        log.info("Not logged in to Luma (%s); using %s", luma_mcp.LOGIN_HINT, events_file_path().name)

    entries = _file_entries()
    log.info("Option A (%s): %d events", events_file_path().name, len(entries))
    return entries


def discover_events() -> list[str]:
    """Return the Luma event URLs the user is going to (hosting or attending)."""
    return [e["url"] for e in discover_events_detailed()]


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for e in discover_events_detailed():
        role = "HOST" if e["is_host"] else (e["status"] or "listed")
        print(f"{e['url']}  [{role}] {e['name']}  ({e.get('source', '?')})")
