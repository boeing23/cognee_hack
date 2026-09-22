"""NopeList web UI: a tiny FastAPI app over the existing pipeline.

Run with:  python -m src.ui      (uvicorn on http://127.0.0.1:8000)

Importing this module must never touch the network: the pipeline modules are
imported (they are all lazy about their own network clients) and nothing is
called until a request arrives.

NOTE: the "Dry run" toggle sets os.environ['NOPELIST_DRY_RUN'] before running a
scan. That is PROCESS-GLOBAL state, not per-request — fine for a single-user
demo server, wrong for anything concurrent.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import fields
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src import brain, discover, report, scraper
from src.models import Event, Match, Person

load_dotenv()

log = logging.getLogger("nopelist.ui")

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
BLACKLIST_PATH = ROOT / "data" / "blacklist.json"

_PERSON_FIELDS = {f.name for f in fields(Person)}

app = FastAPI(title="NopeList")


# --------------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------------- #
class ScanRequest(BaseModel):
    event_urls: list[str] = []
    dry_run: bool = True
    use_luma_mcp: bool = False


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _handle(match: Match) -> Optional[str]:
    g = match.guest
    return g.instagram or g.x_handle or None


def _match_json(m: Match) -> dict[str, Any]:
    return {
        "guest_name": m.guest.name,
        "handle": _handle(m),
        "person_name": m.person.name,
        "reason": m.person.reason,
        "threat_weight": m.person.threat_weight,
        "confidence": m.confidence,
        "matched_on": m.matched_on,
    }


def _local_excuse(event_title: str, person_name: str, reason: str) -> str:
    return (
        f"hey! so sorry, can't make it to {event_title} tonight after all. "
        f"my sourdough starter is having a medical emergency and it needs me. "
        f"(real reason, between us: {person_name} is going and last time it was "
        f"\"{reason}\". i'm not strong enough.) rain check?"
    )


def _excuse_text(event_title: str, person_name: str, reason: str) -> str:
    """Prefer agent.py's own draft; fall back to the local template."""
    try:
        from src.agent import _suggest_excuse  # plain function, not the @tool wrapper

        return _suggest_excuse(event_title, person_name, reason)
    except Exception as exc:  # noqa: BLE001 - excuse is a garnish, never a blocker
        log.debug("agent excuse unavailable (%s); using local template", exc)
        return _local_excuse(event_title, person_name, reason)


def _set_dry_run(enabled: bool) -> None:
    os.environ["NOPELIST_DRY_RUN"] = "1" if enabled else "0"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/blacklist")
def get_blacklist() -> list[dict[str, Any]]:
    try:
        people = brain.load_blacklist(str(BLACKLIST_PATH))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Could not read blacklist: {exc}") from exc
    return [
        {
            "name": p.name,
            "reason": p.reason,
            "aliases": p.aliases,
            "instagram": p.instagram,
            "x_handle": p.x_handle,
            "threat_weight": p.threat_weight,
            "safe": p.safe,
        }
        for p in people
    ]


@app.post("/api/blacklist")
def save_blacklist(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for i, entry in enumerate(people):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=f"row {i + 1}: expected an object")
        unknown = set(entry) - _PERSON_FIELDS
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"row {i + 1}: unknown field(s) {sorted(unknown)}"
            )
        name = str(entry.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail=f"row {i + 1}: name is required")
        try:
            weight = int(entry.get("threat_weight", 2))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"row {i + 1}: threat_weight must be 1-3")
        if weight not in (1, 2, 3):
            raise HTTPException(status_code=400, detail=f"row {i + 1}: threat_weight must be 1-3")
        aliases = entry.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",")]
        if not isinstance(aliases, list):
            raise HTTPException(status_code=400, detail=f"row {i + 1}: aliases must be a list")

        def _h(v: Any) -> Optional[str]:
            s = str(v or "").strip().lstrip("@")
            return s or None

        cleaned.append(
            {
                "name": name,
                "reason": str(entry.get("reason") or "").strip(),
                "aliases": [str(a).strip() for a in aliases if str(a).strip()],
                "instagram": _h(entry.get("instagram")),
                "x_handle": _h(entry.get("x_handle")),
                "threat_weight": weight,
                "safe": bool(entry.get("safe", False)),
            }
        )

    BLACKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLACKLIST_PATH.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("saved %d people to %s", len(cleaned), BLACKLIST_PATH)
    return cleaned


@app.get("/api/events")
def get_events() -> list[str]:
    return discover.read_events_file()


@app.post("/api/scan")
async def scan(req: ScanRequest) -> JSONResponse:
    _set_dry_run(req.dry_run)
    try:
        people = brain.load_blacklist(str(BLACKLIST_PATH))
        if not req.dry_run:
            await brain.ingest_blacklist(people)

        urls = [u.strip() for u in req.event_urls if u.strip()]
        if not urls and req.use_luma_mcp:
            urls = discover.discover_events()
        if not urls:
            raise HTTPException(
                status_code=400,
                detail="No event URLs. Paste at least one Luma URL, or enable Luma MCP discovery.",
            )

        events: list[Event] = []
        matches_by_url: dict[str, list[Match]] = {}
        for url in urls:
            ev = scraper.scrape_guests(url)
            events.append(ev)
            matches_by_url[ev.url] = await brain.match_guests(ev.guests, people)

        results = report.build_results(events, matches_by_url)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a readable message to the UI
        log.exception("scan failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    ordered = sorted(results, key=lambda r: (-r.threat_score, -len(r.hits), r.event.title.lower()))

    events_json = [
        {
            "url": r.event.url,
            "title": r.event.title,
            "guest_count": r.event.guest_count,
            "guests_read": len(r.event.guests),
            "from_cache": r.event.from_cache,
            "threat_level": r.threat_level,
            "threat_score": r.threat_score,
            "hits": [_match_json(m) for m in r.hits],
            "safe_hits": [_match_json(m) for m in r.safe_hits],
        }
        for r in ordered
    ]

    excuse = None
    worst = ordered[0] if ordered and ordered[0].hits else None
    if worst is not None:
        m = worst.hits[0]
        excuse = {
            "event_title": worst.event.title,
            "person_name": m.person.name,
            "text": _excuse_text(worst.event.title, m.person.name, m.person.reason),
        }

    summary = {
        "events_scanned": len(ordered),
        "events_with_hits": sum(1 for r in ordered if r.hits),
        "events_to_evacuate": sum(1 for r in ordered if r.threat_level == "EVACUATE"),
        "total_hits": sum(len(r.hits) for r in ordered),
        "friendly_faces": sum(len(r.safe_hits) for r in ordered),
        "dry_run": req.dry_run,
    }

    return JSONResponse(
        {
            "summary": summary,
            "events": events_json,
            "report_text": report.render_report(results),
            "excuse": excuse,
        }
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("NOPELIST_UI_PORT", "8000")), reload=False)


if __name__ == "__main__":
    main()
