"""Discover which Luma events the user is registered for.

Option A (default): read data/events.txt -- one Luma URL per line, '#' comments.
Option B (LUMA_SESSION_COOKIE set): open the user's Luma home page in the Bright
Data Browser API session with that cookie and collect upcoming events.

Option B details (verified 2026-09-21): the logged-in home page lives at
https://luma.com/home (lu.ma/home 301-redirects there) and it is fed by the
internal endpoint

    GET https://api.lu.ma/home/get-events?period=future&pagination_limit=<n>

which answers 401 "You are not signed in." without the ``luma.auth-session-key``
cookie. We call that endpoint from inside the page and, if its shape surprises
us, read the event links off the rendered home page instead.

DRY RUN (NOPELIST_DRY_RUN=1) always uses Option A and never touches the network.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from src.scraper import (
    PAGE_TIMEOUT_MS,
    ROOT,
    _run_in_thread,
    dry_run,
    event_slug,
    luma_api_get,
    open_luma_page,
)

log = logging.getLogger("nopelist.discover")

EVENTS_FILE = ROOT / "data" / "events.txt"
LUMA_HOME = "https://luma.com/home"
_LUMA_URL_RE = re.compile(r"https?://(?:www\.)?(?:lu\.ma|luma\.com)/[A-Za-z0-9_\-]+")
_NON_EVENT_PATHS = {"home", "discover", "signin", "create", "settings", "calendars", "user", "p", "u", "api"}


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
# Option B: Luma session cookie via Bright Data Browser API
# --------------------------------------------------------------------------- #
def _walk_for_events(node: Any, found: dict[str, str]) -> None:
    """Recursively pull {slug -> url} out of whatever shape home/get-events returns."""
    if isinstance(node, dict):
        if "api_id" in node and str(node.get("api_id", "")).startswith("evt-"):
            url = node.get("url")
            slug = event_slug(url) if url else None
            if slug and slug not in _NON_EVENT_PATHS:
                found[slug] = f"https://lu.ma/{slug}"
        for v in node.values():
            _walk_for_events(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_for_events(v, found)


def _discover_live() -> list[str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser, page = open_luma_page(pw)
        try:
            page.goto(LUMA_HOME, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            found: dict[str, str] = {}

            data = luma_api_get(page, "home/get-events", {"period": "future", "pagination_limit": "50"})
            if data:
                _walk_for_events(data, found)
                log.info("home/get-events returned %d upcoming events", len(found))

            if not found:
                # The rendered home page links each upcoming event card by slug.
                page.wait_for_timeout(3000)
                hrefs: list[str] = page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
                )
                for href in hrefs:
                    m = _LUMA_URL_RE.match(href)
                    if not m:
                        continue
                    slug = event_slug(m.group(0))
                    if slug and slug not in _NON_EVENT_PATHS:
                        found[slug] = f"https://lu.ma/{slug}"
                log.info("home page DOM yielded %d event links", len(found))
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return list(found.values())


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def discover_events() -> list[str]:
    """Return the Luma event URLs the user is going to."""
    if dry_run():
        urls = read_events_file()
        log.info("DRY RUN -> Option A (data/events.txt): %d events", len(urls))
        return urls

    if os.getenv("LUMA_SESSION_COOKIE", "").strip():
        log.info("Option B: auto-discovering registered events via Luma session cookie")
        try:
            urls = _run_in_thread(_discover_live)
            if urls:
                return urls
            log.warning("Option B found no events; using data/events.txt")
        except Exception as exc:
            log.error("Option B failed (%s); using data/events.txt", exc)

    urls = read_events_file()
    log.info("Option A (data/events.txt): %d events", len(urls))
    return urls


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for u in discover_events():
        print(u)
