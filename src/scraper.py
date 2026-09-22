"""Bright Data guest-list scraper for Luma events.

Primary path: Bright Data **Browser API** (a.k.a. Scraping Browser) -- a remote
Chromium we drive over CDP with Playwright. Luma event pages are JS-rendered and
the guest modal is semi-gated, so a real browser (with Bright Data's unblocking
and proxying) is the honest tool.

Inside the remote browser we prefer Luma's own internal JSON API over DOM
scraping, because it is what the page itself calls (verified 2026-09-21):

  * GET https://api.lu.ma/url?url=<slug>
        public; returns {"kind": "event", "data": {api_id, event.name,
        guest_count, featured_guests, ...}}

Then we open the "N Guests" modal and read the DOM, which yields the subset
Luma shows to visitors (the internal get-guest-list endpoint needs a login).

Events the user HOSTS skip Bright Data entirely: src/discover.py marks them in
``src.luma_mcp.hosted_event_ids`` and scrape_guests() pulls the structured guest
list from the official Luma MCP server (``list_guests``, host-only). Events the
user merely attends cannot use that tool (verified live), so they are scraped.

Alternative Bright Data products: Web Unlocker / Web Scraper API can fetch the
raw event HTML, but the guest list is loaded client-side, so they only expose
the few "featured" avatars. Browser API is the right product here.

Resilience layers (the plan's pre-warm strategy):
  1. NOPELIST_DRY_RUN=1  -> data/fixtures/guests_<slug>.json (or guests_default.json), no network.
  2. data/cache/<sha1(url)>.json -> served when ``use_cache`` is true.
  3. Live scrape -> written to the cache on success.

Environment variables (read here, loaded from .env by the entrypoint):
  BRIGHTDATA_BROWSER_USER   Browser API zone username, e.g. brd-customer-hl_xxxx-zone-nopelist
  BRIGHTDATA_BROWSER_PASS   Browser API zone password
  BRIGHTDATA_BROWSER_AUTH   optional "USER:PASS" in one string (overrides the two above)
  BRIGHTDATA_BROWSER_WSS    optional full endpoint override (default wss://<AUTH>@brd.superproxy.io:9222)
  LUMA_SESSION_COOKIE       optional 'luma.auth-session-key' cookie value; lets the remote
                            browser see the FULL guest list (Luma renders it only for
                            logged-in registrants). Scraper only -- discovery still uses
                            the Luma MCP server / events.txt.
  NOPELIST_DRY_RUN          "1" -> fixtures only, never touch the network
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from src import luma_mcp
from src.models import Event, Guest

log = logging.getLogger("nopelist.scraper")

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache"
FIXTURE_DIR = ROOT / "data" / "fixtures"

LUMA_API = "https://api.lu.ma"
BRD_DEFAULT_HOST = "brd.superproxy.io:9222"
PAGE_TIMEOUT_MS = 2 * 60 * 1000  # Bright Data recommends a generous goto timeout
GUEST_PAGE_SIZE = 100
GUEST_PAGE_CAP = 500  # stop scrolling the modal after this many rows

LUMA_COOKIE_NAME = "luma.auth-session-key"
LUMA_COOKIE_DOMAINS = (".lu.ma", ".luma.com")

# Zero-width / bidi junk Luma sprinkles through its rich-text description blocks.
_ZERO_WIDTH_RE = re.compile(r"[​-‏  ﻿­]")
# Description / metadata lines that must never be mistaken for a guest.
_NOT_A_NAME_RE = re.compile(
    r"(\band\s+\d[\d,]*\s+others?\b|\b\d[\d,]*\s+(guests?|going|attendees?)\b"
    r"|^(when|where|what|who|why|how|register|approval|hosted by|presented by)\b"
    r"|\b(am|pm)\b.*\d|\d{1,2}:\d{2}|https?://)",
    re.I,
)
MAX_NAME_LEN = 40


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def dry_run() -> bool:
    return os.getenv("NOPELIST_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}


def event_slug(event_url: str) -> str:
    """'https://lu.ma/abc123?x=1' -> 'abc123'. Works for lu.ma and luma.com."""
    path = urlparse(event_url.strip()).path.strip("/")
    slug = path.split("/")[-1] if path else event_url.strip()
    return re.sub(r"[^A-Za-z0-9_\-]", "", slug)


def cache_path(event_url: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha1(event_url.strip().encode()).hexdigest()}.json"


def _clean_handle(value: Any) -> Optional[str]:
    """Normalise '@foo', 'https://instagram.com/foo/', 'x.com/foo?s=1' -> 'foo'."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if "://" in v or v.startswith("www."):
        v = urlparse(v if "://" in v else f"https://{v}").path
    v = v.strip("/").split("/")[-1].split("?")[0].lstrip("@").strip()
    if not v or v.lower() in {"instagram.com", "x.com", "twitter.com"}:
        return None
    return v


def _clean_text(value: Any) -> str:
    """Strip zero-width/bidi junk and collapse whitespace."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", _ZERO_WIDTH_RE.sub("", value)).strip()


def _looks_like_person(name: str) -> bool:
    """True when ``name`` could plausibly be a guest rather than description text."""
    if not name or len(name) > MAX_NAME_LEN:
        return False
    if _NOT_A_NAME_RE.search(name):
        return False
    # Sentences / bullets end in punctuation; names do not.
    if name.endswith((".", ":", "!", "?", ",")):
        return False
    return len(name.split()) <= 5


def _dedupe(guests: list[Guest]) -> list[Guest]:
    out: list[Guest] = []
    seen: set[tuple] = set()
    for g in guests:
        key = (g.name.casefold(), g.instagram, g.x_handle)
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
    return out


def luma_session_cookie() -> Optional[str]:
    value = (os.getenv("LUMA_SESSION_COOKIE") or "").strip().strip('"').strip("'")
    # Tolerate a pasted 'luma.auth-session-key=abc' pair.
    if value.startswith(f"{LUMA_COOKIE_NAME}="):
        value = value.split("=", 1)[1].strip()
    return value or None


def apply_luma_cookie(context) -> bool:
    """Inject LUMA_SESSION_COOKIE into the remote browser context (scraper only).

    Luma serves both lu.ma and luma.com, and redirects between them, so the cookie
    is registered on both apexes. Returns True when a cookie was applied.
    """
    value = luma_session_cookie()
    if not value:
        return False
    cookies = [
        {"name": LUMA_COOKIE_NAME, "value": value, "domain": d, "path": "/",
         "httpOnly": True, "secure": True, "sameSite": "Lax"}
        for d in LUMA_COOKIE_DOMAINS
    ]
    try:
        context.add_cookies(cookies)
    except Exception as exc:  # noqa: BLE001 - a bad cookie must not kill the scrape
        log.warning("Could not apply LUMA_SESSION_COOKIE (%s); continuing logged out", exc)
        return False
    log.info("Applied LUMA_SESSION_COOKIE to %s", " and ".join(LUMA_COOKIE_DOMAINS))
    return True


def _event_to_json(event: Event) -> dict:
    return asdict(event)


def _event_from_json(data: dict, url: str, from_cache: bool) -> Event:
    guests = _dedupe([
        Guest(name=_clean_text(g.get("name")), instagram=_clean_handle(g.get("instagram")),
              x_handle=_clean_handle(g.get("x_handle")))
        for g in data.get("guests", [])
        if _clean_text(g.get("name"))
    ])
    # Always key on the URL that was asked for: a shared fixture (guests_default.json)
    # can serve several requested events, and agent.py indexes events by that URL.
    return Event(
        url=url,
        title=data.get("title") or event_slug(url),
        guests=guests,
        guest_count=data.get("guest_count"),
        from_cache=from_cache,
    )


def _load_fixture(event_url: str) -> Event:
    slug = event_slug(event_url)
    candidate = FIXTURE_DIR / f"guests_{slug}.json"
    path = candidate if candidate.exists() else FIXTURE_DIR / "guests_default.json"
    log.info("DRY RUN: loading fixture %s", path.name)
    data = json.loads(path.read_text())
    return _event_from_json(data, event_url, from_cache=True)


def _write_cache(event: Event) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(event.url).write_text(json.dumps(_event_to_json(event), indent=2))


# --------------------------------------------------------------------------- #
# Bright Data Browser API connection
# --------------------------------------------------------------------------- #
def brightdata_endpoint() -> str:
    """Build the Browser API CDP endpoint from env.

    Format per https://docs.brightdata.com/scraping-automation/scraping-browser/configuration :
        wss://<ZONE_USERNAME>:<ZONE_PASSWORD>@brd.superproxy.io:9222
    """
    override = os.getenv("BRIGHTDATA_BROWSER_WSS")
    if override:
        return override
    auth = os.getenv("BRIGHTDATA_BROWSER_AUTH")
    if not auth:
        user = os.getenv("BRIGHTDATA_BROWSER_USER")
        pw = os.getenv("BRIGHTDATA_BROWSER_PASS")
        if not (user and pw):
            raise RuntimeError(
                "Bright Data Browser API credentials missing: set BRIGHTDATA_BROWSER_USER "
                "and BRIGHTDATA_BROWSER_PASS (Bright Data dashboard -> Proxies & Scraping "
                "-> your Browser API zone -> Overview), or set NOPELIST_DRY_RUN=1."
            )
        auth = f"{user}:{pw}"
    return f"wss://{auth}@{BRD_DEFAULT_HOST}"


def open_luma_page(playwright_ctx):  # -> (browser, page)
    """Connect to Bright Data Browser API and return (browser, page)."""
    endpoint = brightdata_endpoint()
    log.info("Connecting to Bright Data Browser API at %s", BRD_DEFAULT_HOST)
    browser = playwright_ctx.chromium.connect_over_cdp(endpoint, timeout=PAGE_TIMEOUT_MS)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    apply_luma_cookie(context)  # BEFORE any navigation, so the first render is logged in
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    return browser, page


def luma_api_get(page, path: str, params: dict[str, str]) -> Optional[dict]:
    """Call Luma's internal JSON API *from inside the page* so cookies/CORS behave
    exactly like the site itself. Returns None on non-2xx."""
    js = """
    async ({url}) => {
      const r = await fetch(url, {credentials: 'include', headers: {accept: 'application/json'}});
      const text = await r.text();
      return {status: r.status, text};
    }
    """
    from urllib.parse import urlencode
    url = f"{LUMA_API}/{path}?{urlencode(params)}"
    res = page.evaluate(js, {"url": url})
    if res["status"] // 100 != 2:
        log.warning("Luma API %s -> HTTP %s: %s", path, res["status"], res["text"][:120])
        return None
    try:
        return json.loads(res["text"])
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# Guest extraction (DOM modal, featured_guests fallback)
# --------------------------------------------------------------------------- #
_INSTA_KEYS = ("instagram_handle", "instagram", "instagram_url")
_X_KEYS = ("twitter_handle", "x_handle", "twitter", "x", "twitter_url", "x_url")


def _guest_from_api_entry(entry: dict) -> Optional[Guest]:
    # Luma nests the profile under "user" in some responses; flatten tolerantly.
    src = {**entry, **(entry.get("user") or {})}
    name = _clean_text(src.get("name")) or _clean_text(" ".join(
        p for p in (src.get("first_name"), src.get("last_name")) if p
    ))
    if not name:
        return None
    insta = next((_clean_handle(src.get(k)) for k in _INSTA_KEYS if src.get(k)), None)
    x = next((_clean_handle(src.get(k)) for k in _X_KEYS if src.get(k)), None)
    return Guest(name=name, instagram=insta, x_handle=x)


def watch_guest_api(page) -> list[dict]:
    """Record every JSON response from Luma's API whose URL mentions 'guest'.

    Opening the modal makes the page call its own endpoint (api.lu.ma /
    api.luma.com, e.g. .../event/get-guest-list); that JSON is far cleaner than
    the DOM, so we capture it opportunistically. The returned list fills in
    place as responses arrive.
    """
    captured: list[dict] = []

    def on_response(response) -> None:
        try:
            url = response.url
            host = urlparse(url).netloc.lower()
            if "guest" not in url.lower():
                return
            if not (host.endswith("lu.ma") or host.endswith("luma.com")):
                return
            if "json" not in (response.headers.get("content-type") or "").lower():
                return
            captured.append(response.json())
            log.info("Captured Luma guest API response: %s", url.split("?")[0])
        except Exception:  # noqa: BLE001 - a response we cannot read is just skipped
            pass

    page.on("response", on_response)
    return captured


def _iter_guest_entries(payload: Any):
    """Yield dicts that look like guest records anywhere inside an API payload."""
    if isinstance(payload, dict):
        if isinstance(payload.get("user"), dict) or (
            payload.get("name") and ("api_id" in payload or "user_api_id" in payload)
        ):
            yield payload
        for value in payload.values():
            yield from _iter_guest_entries(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_guest_entries(item)


def guests_from_api_payloads(payloads: list[dict]) -> list[Guest]:
    guests: list[Guest] = []
    for payload in payloads:
        for entry in _iter_guest_entries(payload):
            guest = _guest_from_api_entry(entry)
            if guest and _looks_like_person(guest.name):
                guests.append(guest)
    return _dedupe(guests)


def open_guest_modal(page) -> bool:
    """Click the '<N> Guests' / '<N> Going' trigger (or the avatar row) and wait
    for the dialog. Returns False when Luma never renders one (logged out)."""
    triggers = [
        lambda: page.get_by_text(re.compile(r"\d[\d,]*\s+(Guests|Going)", re.I)).first,
        lambda: page.locator('a[href*="/guests"]').first,
        lambda: page.locator("div.avatar-wrapper, div[class*='avatars'] , div[class*='guest']").first,
    ]
    for make in triggers:
        try:
            make().click(timeout=8000)
            page.wait_for_selector('[role="dialog"]', timeout=8000)
            return True
        except Exception:
            continue
    return False


def _scroll_dialog(page) -> None:
    """Scroll the dialog's scrollable element until the row count stops growing."""
    js_count = """
    () => {
      const d = document.querySelector('[role="dialog"]');
      return d ? d.querySelectorAll('a[href*="/user/"], a[href^="/u/"]').length : 0;
    }
    """
    js_scroll = """
    () => {
      const d = document.querySelector('[role="dialog"]');
      if (!d) return;
      const scroller = Array.from(d.querySelectorAll('*'))
        .find(e => e.scrollHeight > e.clientHeight + 40) || d;
      scroller.scrollTop = scroller.scrollHeight;
    }
    """
    previous = -1
    for _ in range(40):
        count = page.evaluate(js_count) or 0
        if count == previous or count >= GUEST_PAGE_CAP:
            break
        previous = count
        page.evaluate(js_scroll)
        page.wait_for_timeout(500)


def guests_from_dialog(page) -> list[Guest]:
    """Parse ONLY the rows inside the open guest dialog."""
    js = """
    () => {
      const d = document.querySelector('[role="dialog"]');
      if (!d) return [];
      const rows = [];
      d.querySelectorAll('a[href*="/user/"], a[href^="/u/"]').forEach(el => {
        const row = el.closest('div[class*="row"], li, tr') || el;
        const name = (el.innerText || row.innerText || '')
          .split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
        let instagram = null, x = null;
        row.querySelectorAll('a[href]').forEach(a => {
          const h = a.getAttribute('href') || '';
          if (/instagram\\.com\\//i.test(h)) instagram = h;
          else if (/(twitter|x)\\.com\\//i.test(h)) x = h;
        });
        rows.push({name, instagram, x});
      });
      return rows;
    }
    """
    rows = page.evaluate(js) or []
    guests = []
    for r in rows:
        name = _clean_text(r.get("name"))
        if not _looks_like_person(name):
            continue
        guests.append(Guest(name=name, instagram=_clean_handle(r.get("instagram")),
                            x_handle=_clean_handle(r.get("x"))))
    return _dedupe(guests)


def guests_visible_logged_out(page) -> list[Guest]:
    """Hosts + featured avatars only -- what Luma renders without a session.

    Everything that is description prose, a teaser line ('... and 326 others'),
    or a date/location row is dropped by ``_looks_like_person``.
    """
    js = """
    () => {
      const rows = [];
      document.querySelectorAll(
        '.host-row a, [class*="host"] a[href*="/user/"], a[href*="/user/"], a[href^="/u/"]'
      ).forEach(el => {
        const name = (el.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
        let instagram = null, x = null;
        const row = el.closest('div[class*="row"], li') || el;
        row.querySelectorAll('a[href]').forEach(a => {
          const h = a.getAttribute('href') || '';
          if (/instagram\\.com\\//i.test(h)) instagram = h;
          else if (/(twitter|x)\\.com\\//i.test(h)) x = h;
        });
        rows.push({name, instagram, x});
      });
      return rows;
    }
    """
    rows = page.evaluate(js) or []
    guests = []
    for r in rows:
        name = _clean_text(r.get("name"))
        if not _looks_like_person(name):
            continue
        guests.append(Guest(name=name, instagram=_clean_handle(r.get("instagram")),
                            x_handle=_clean_handle(r.get("x"))))
    return _dedupe(guests)


def _scrape_live(event_url: str) -> Event:
    from playwright.sync_api import sync_playwright  # imported lazily: dry-run needs no playwright

    slug = event_slug(event_url)
    with sync_playwright() as pw:
        browser, page = open_luma_page(pw)
        try:
            captured = watch_guest_api(page)
            log.info("Navigating to %s", event_url)
            page.goto(event_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")

            title, guest_count, api_id = slug, None, None
            meta = luma_api_get(page, "url", {"url": slug})
            if meta and meta.get("kind") == "event":
                d = meta.get("data", {})
                api_id = d.get("api_id")
                title = (d.get("event") or {}).get("name") or title
                guest_count = d.get("guest_count")
            if not title or title == slug:
                try:
                    title = page.title().replace(" · Luma", "").strip() or slug
                except Exception:
                    pass

            if open_guest_modal(page):
                _scroll_dialog(page)
                page.wait_for_timeout(500)
                guests = guests_from_api_payloads(captured)
                if guests:
                    log.info("Guest list via Luma guest API: %d guests", len(guests))
                else:
                    guests = guests_from_dialog(page)
                    log.info("Guest list via guest dialog DOM: %d guests", len(guests))
            else:
                guests = []

            if not guests:
                # Logged out: Luma renders hosts + a few featured avatars and nothing else.
                guests = guests_visible_logged_out(page)
                if meta:
                    featured = [
                        g for g in (_guest_from_api_entry(e)
                                    for e in (meta.get("data") or {}).get("featured_guests", []))
                        if g and _looks_like_person(g.name)
                    ]
                    guests = _dedupe(guests + featured)
                if not luma_session_cookie():
                    log.warning(
                        "Luma guest list requires login: set LUMA_SESSION_COOKIE to see all %s guests",
                        guest_count if guest_count is not None else "?",
                    )
                log.info("Visible (logged-out) hosts/featured guests: %d", len(guests))

            guests = _dedupe([g for g in guests if _looks_like_person(g.name)])
        finally:
            try:
                browser.close()
            except Exception:
                pass

    return Event(url=event_url, title=title, guests=guests, guest_count=guest_count, from_cache=False)


def _run_in_thread(fn, *args):
    """Playwright's sync API refuses to run inside a live asyncio loop (Strands may
    call tools from one), so always hop to a plain thread."""
    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            result["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


# --------------------------------------------------------------------------- #
# Luma MCP path (hosted events only)
# --------------------------------------------------------------------------- #
def _guests_via_luma_mcp(event_url: str) -> Event:
    """Structured guest list via the official Luma MCP server (host-only tool)."""
    slug = event_slug(event_url)
    api_id = luma_mcp.hosted_event_api_id(event_url)  # recorded by discover_events_detailed()
    if not api_id and slug.startswith("evt-"):
        api_id = slug
    if not api_id:
        entity = luma_mcp.lookup_entity(event_url) or {}
        api_id = entity.get("id") or entity.get("api_id") or (entity.get("event") or {}).get("api_id")
    if not api_id:
        raise RuntimeError(f"Could not resolve a Luma event id for {event_url}")
    detail = luma_mcp.get_event(str(api_id))
    title = detail.get("name") or (detail.get("event") or {}).get("name") or slug
    guest_count = detail.get("guest_count") or (detail.get("event") or {}).get("guest_count")
    entries = luma_mcp.list_event_guests(str(api_id))
    guests = luma_mcp.hosted_event_guests_as_guests(entries)
    return Event(url=event_url, title=str(title), guests=guests, guest_count=guest_count, from_cache=False)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def scrape_guests(event_url: str, use_cache: bool = True) -> Event:
    """Return the guest list for a Luma event.

    Order of precedence: dry-run fixture -> Luma MCP list_guests (events the
    user HOSTS, when logged in) -> cache (if ``use_cache``) -> live Bright Data
    Browser API scrape (which then populates the cache).
    """
    event_url = event_url.strip()
    if dry_run():
        return _load_fixture(event_url)

    if luma_mcp.is_hosted_event(event_url) and luma_mcp.has_token():
        try:
            event = _guests_via_luma_mcp(event_url)
            log.info("source=luma-mcp: '%s' %d guests (hosted event, no scrape)", event.title, len(event.guests))
            _write_cache(event)
            return event
        except luma_mcp.LumaAccessError as exc:
            log.warning("%s -> falling back to Bright Data", exc)
        except Exception as exc:  # noqa: BLE001 - MCP is an optimisation, never a blocker
            log.error("Luma MCP guest list failed (%s); falling back to Bright Data", exc)

    path = cache_path(event_url)
    if use_cache and path.exists():
        log.info("Cache hit for %s (%s)", event_url, path.name)
        return _event_from_json(json.loads(path.read_text()), event_url, from_cache=True)

    try:
        event = _run_in_thread(_scrape_live, event_url)
    except Exception as exc:
        if path.exists():
            log.error("Live scrape failed (%s); falling back to stale cache", exc)
            return _event_from_json(json.loads(path.read_text()), event_url, from_cache=True)
        raise
    _write_cache(event)
    return event


if __name__ == "__main__":  # quick manual check: python -m src.scraper https://lu.ma/<slug>
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ev = scrape_guests(sys.argv[1] if len(sys.argv) > 1 else "https://lu.ma/demo", use_cache="--no-cache" not in sys.argv)
    print(json.dumps(_event_to_json(ev), indent=2))
