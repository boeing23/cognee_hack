"""NopeList brain: the blacklist lives in Cognee.

Responsibilities
----------------
* ``load_blacklist``   read data/blacklist.json into ``Person`` objects
* ``ingest_blacklist`` push every person into Cognee (add + cognify)
* ``match_guest``      resolve one scraped ``Guest`` against the blacklist
* ``match_guests``     same, over a list

Matching is layered so the cheap, exact answers come first and the graph is
only consulted for ambiguous cases:

1. handle match (instagram / x_handle, case-insensitive, '@' stripped) -> 1.0
2. exact name or alias match (normalized)                             -> 0.9
3. Cognee GRAPH_COMPLETION "is this plausibly one of them?"           -> 0.6

Dry-run mode
------------
``NOPELIST_DRY_RUN=1`` makes this module fully offline: ingest is a no-op and
step 3 is skipped. Steps 1 and 2 are pure Python and always run, so the demo
still produces hits with no network at all.

Cognee configuration (all read from the environment by cognee itself)
---------------------------------------------------------------------
LLM_PROVIDER=anthropic          LLM_MODEL=anthropic/claude-sonnet-5   LLM_API_KEY=sk-ant-...
EMBEDDING_PROVIDER=fastembed    EMBEDDING_MODEL=all-MiniLM-L6-v2     EMBEDDING_DIMENSIONS=384
Optional Cognee Cloud: COGNEE_SERVICE_URL + COGNEE_API_KEY (we call cognee.serve()).

Docs consulted: https://docs.cognee.ai/python-api/add, /python-api/cognify,
/python-api/search, /setup-configuration/llm-providers, /python-api/serve.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import fields
from pathlib import Path
from typing import Any, Optional

from src.models import Guest, Match, Person

log = logging.getLogger("nopelist.brain")

DATASET_NAME = os.getenv("COGNEE_DATASET", "nopelist_blacklist")

_PERSON_FIELDS = {f.name for f in fields(Person)}
_YES_RE = re.compile(r"^\s*(yes|match)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def dry_run() -> bool:
    """True when NOPELIST_DRY_RUN is set to a truthy value."""
    return os.getenv("NOPELIST_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}


def _norm_handle(handle: Optional[str]) -> Optional[str]:
    if not handle:
        return None
    h = handle.strip().lstrip("@").lower()
    return h or None


def _norm_name(name: Optional[str]) -> str:
    if not name:
        return ""
    # collapse whitespace, drop punctuation, lower-case
    cleaned = re.sub(r"[^\w\s]", " ", name, flags=re.UNICODE)
    return " ".join(cleaned.lower().split())


def _person_to_text(p: Person) -> str:
    """Render a Person as a small natural-language record for the graph."""
    kind = "SAFE-LISTED (someone I want to see)" if p.safe else "BLACKLISTED (someone I avoid)"
    parts = [f"{p.name} is a {kind} person on my NopeList."]
    if p.aliases:
        parts.append(f"{p.name} is also known as: {', '.join(p.aliases)}.")
    if p.instagram:
        parts.append(f"{p.name}'s Instagram handle is @{p.instagram}.")
    if p.x_handle:
        parts.append(f"{p.name}'s X (Twitter) handle is @{p.x_handle}.")
    parts.append(f"Reason on file: {p.reason}.")
    parts.append(f"Threat weight: {p.threat_weight} out of 3.")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 1. load
# --------------------------------------------------------------------------- #
def load_blacklist(path: str = "data/blacklist.json") -> list[Person]:
    """Read the blacklist JSON file into Person objects.

    Unknown keys are ignored so the file can carry extra notes without
    breaking the dataclass contract.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON list of people")
    people: list[Person] = []
    for entry in raw:
        kwargs = {k: v for k, v in entry.items() if k in _PERSON_FIELDS}
        kwargs.setdefault("aliases", [])
        kwargs["aliases"] = list(kwargs["aliases"] or [])
        kwargs["instagram"] = _norm_handle(kwargs.get("instagram"))
        kwargs["x_handle"] = _norm_handle(kwargs.get("x_handle"))
        people.append(Person(**kwargs))
    log.info("loaded %d people from %s", len(people), path)
    return people


# --------------------------------------------------------------------------- #
# 2. ingest
# --------------------------------------------------------------------------- #
async def _connect_cloud_if_configured() -> None:
    """Point the SDK at Cognee Cloud when COGNEE_SERVICE_URL/COGNEE_API_KEY are set."""
    if os.getenv("COGNEE_SERVICE_URL") and os.getenv("COGNEE_API_KEY"):
        import cognee

        await cognee.serve()  # reads COGNEE_SERVICE_URL + COGNEE_API_KEY
        log.info("connected to Cognee Cloud at %s", os.getenv("COGNEE_SERVICE_URL"))


async def ingest_blacklist(people: list[Person]) -> int:
    """Add every person to Cognee and cognify the dataset. Returns count.

    No-op (returns len(people)) in DRY_RUN so the demo never touches the
    network. Set NOPELIST_RESET_BRAIN=1 to wipe the dataset before ingesting.
    """
    if dry_run():
        log.info("DRY_RUN: skipping Cognee ingest of %d people", len(people))
        return len(people)

    import cognee

    await _connect_cloud_if_configured()

    if os.getenv("NOPELIST_RESET_BRAIN", "").strip() == "1":
        log.info("resetting Cognee data + system state")
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)

    docs = [_person_to_text(p) for p in people]
    await cognee.add(docs, dataset_name=DATASET_NAME)
    await cognee.cognify(datasets=[DATASET_NAME])
    log.info("ingested %d people into Cognee dataset %r", len(people), DATASET_NAME)
    return len(people)


# --------------------------------------------------------------------------- #
# 3. match
# --------------------------------------------------------------------------- #
def _deterministic_match(guest: Guest, people: list[Person]) -> Match | None:
    """Handle match (1.0) then exact name/alias match (0.9)."""
    g_ig = _norm_handle(guest.instagram)
    g_x = _norm_handle(guest.x_handle)

    # (a) handles: unambiguous identity, highest confidence
    for p in people:
        if g_ig and p.instagram and g_ig == p.instagram:
            return Match(person=p, guest=guest, confidence=1.0, matched_on="instagram")
        if g_x and p.x_handle and g_x == p.x_handle:
            return Match(person=p, guest=guest, confidence=1.0, matched_on="x_handle")

    # (b) exact normalized name or alias
    g_name = _norm_name(guest.name)
    if not g_name:
        return None
    for p in people:
        if g_name == _norm_name(p.name):
            return Match(person=p, guest=guest, confidence=0.9, matched_on="name")
        if any(g_name == _norm_name(a) for a in p.aliases):
            return Match(person=p, guest=guest, confidence=0.9, matched_on="alias")
    return None


def _search_result_text(results: Any) -> str:
    """Flatten whatever cognee.search returns into one string."""
    if results is None:
        return ""
    if isinstance(results, str):
        return results
    if isinstance(results, dict):
        for key in ("search_result", "result", "text", "answer"):
            if key in results:
                return _search_result_text(results[key])
        return json.dumps(results)
    if isinstance(results, (list, tuple)):
        return "\n".join(_search_result_text(r) for r in results)
    for attr in ("search_result", "result", "text", "answer"):
        if hasattr(results, attr):
            return _search_result_text(getattr(results, attr))
    return str(results)


def _parse_graph_answer(answer: str, people: list[Person]) -> Person | None:
    """Expect 'YES: <name>' or 'NO'. Fall back to scanning for a known name."""
    if not answer or not _YES_RE.match(answer):
        return None
    lowered = _norm_name(answer)
    # prefer the longest name that appears in the answer to avoid partial hits
    best: Person | None = None
    best_len = 0
    for p in people:
        for candidate in [p.name, *p.aliases]:
            n = _norm_name(candidate)
            if n and n in lowered and len(n) > best_len:
                best, best_len = p, len(n)
    return best


async def _graph_match(guest: Guest, people: list[Person]) -> Match | None:
    """Ask Cognee whether this guest is plausibly one of the listed people."""
    import cognee
    from cognee import SearchType

    await _connect_cloud_if_configured()

    roster = "; ".join(
        f"{p.name}" + (f" (aka {', '.join(p.aliases)})" if p.aliases else "") for p in people
    )
    handles = ", ".join(
        h for h in (f"@{guest.instagram}" if guest.instagram else None,
                    f"@{guest.x_handle}" if guest.x_handle else None) if h
    )
    query = (
        f"An event guest is listed as '{guest.name}'"
        + (f" with handles {handles}" if handles else "")
        + ". Is this guest plausibly the same person as anyone on my NopeList "
        f"({roster}), allowing for nicknames, initials, transliterations, or "
        "reordered names? Answer on one line exactly as 'YES: <full name from the list>' "
        "or 'NO'."
    )
    try:
        results = await cognee.search(
            query_text=query,
            query_type=SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
            top_k=10,
        )
    except Exception as exc:  # network / provider errors must never kill the run
        log.warning("cognee.search failed for %r: %s", guest.name, exc)
        return None

    answer = _search_result_text(results)
    log.debug("graph answer for %r: %s", guest.name, answer[:200])
    person = _parse_graph_answer(answer, people)
    if person is None:
        return None
    return Match(person=person, guest=guest, confidence=0.6, matched_on="graph")


async def match_guest(guest: Guest, people: list[Person]) -> Match | None:
    """Resolve a single guest. Safe-listed people still match; caller checks .safe."""
    hit = _deterministic_match(guest, people)
    if hit is not None:
        return hit
    if dry_run():
        return None
    return await _graph_match(guest, people)


async def match_guests(guests: list[Guest], people: list[Person]) -> list[Match]:
    """Resolve every guest; returns only the matches (misses are dropped)."""
    matches: list[Match] = []
    for guest in guests:
        m = await match_guest(guest, people)
        if m is not None:
            matches.append(m)
    return matches


__all__ = [
    "DATASET_NAME",
    "dry_run",
    "load_blacklist",
    "ingest_blacklist",
    "match_guest",
    "match_guests",
]
