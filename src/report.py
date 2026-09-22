"""Threat report: pure functions turning scraped events + brain matches into text.

No network, no I/O. Everything here builds on the shared contract in models.py.
"""
from __future__ import annotations

from src.models import Event, EventResult, Match

LEVEL_EMOJI: dict[str, str] = {
    "CLEAR": "\U0001f7e2",     # green circle
    "AWKWARD": "\U0001f7e1",   # yellow circle
    "EVACUATE": "\U0001f534",  # red circle
}
SAFE_EMOJI = "☕"  # coffee cup


def build_results(
    events: list[Event], matches_by_url: dict[str, list[Match]]
) -> list[EventResult]:
    """Pair each event with its matches, splitting blacklist hits from safe-list hits."""
    results: list[EventResult] = []
    for event in events:
        hits: list[Match] = []
        safe_hits: list[Match] = []
        for match in matches_by_url.get(event.url, []):
            (safe_hits if match.person.safe else hits).append(match)
        hits.sort(key=lambda m: (-m.person.threat_weight, -m.confidence))
        safe_hits.sort(key=lambda m: -m.confidence)
        results.append(EventResult(event=event, hits=hits, safe_hits=safe_hits))
    return results


def _handle(match: Match) -> str:
    guest = match.guest
    if guest.instagram:
        return f"@{guest.instagram}"
    if guest.x_handle:
        return f"@{guest.x_handle}"
    return "(no handle)"


def _hit_line(match: Match) -> str:
    return (
        f"    - {match.guest.name} {_handle(match)} "
        f"[{int(round(match.confidence * 100))}% via {match.matched_on}] "
        f"— \"{match.person.reason}\""
    )


def _safe_line(match: Match) -> str:
    return f"    {SAFE_EMOJI} {match.guest.name} {_handle(match)} is going — \"{match.person.reason}\""


def render_report(results: list[EventResult]) -> str:
    """Render the terminal threat report, most dangerous event first."""
    ordered = sorted(
        results, key=lambda r: (-r.threat_score, -len(r.hits), r.event.title.lower())
    )
    n_events = len(ordered)
    n_hot = sum(1 for r in ordered if r.hits)
    n_evac = sum(1 for r in ordered if r.threat_level == "EVACUATE")
    n_safe = sum(len(r.safe_hits) for r in ordered)

    lines: list[str] = []
    lines.append("\U0001f6ab NopeList Threat Report")
    lines.append(
        f"{n_events} event(s) scanned · {n_hot} with blacklist hits · "
        f"{n_evac} to evacuate · {n_safe} friendly face(s)"
    )
    lines.append("=" * 60)

    for r in ordered:
        ev = r.event
        emoji = LEVEL_EMOJI.get(r.threat_level, "")
        cached = " (cached)" if ev.from_cache else ""
        count = f"{len(ev.guests)} guests read"
        if ev.guest_count is not None:
            count += f" of {ev.guest_count} listed"
        lines.append("")
        lines.append(f"{emoji} {r.threat_level} — {ev.title}")
        lines.append(f"    {ev.url}")
        lines.append(f"    {count}{cached} · threat score {r.threat_score}")
        for m in r.hits:
            lines.append(_hit_line(m))
        for m in r.safe_hits:
            lines.append(_safe_line(m))
        if not r.hits and not r.safe_hits:
            lines.append("    nobody on your list. go forth.")

    lines.append("")
    lines.append("=" * 60)
    if n_evac:
        lines.append("Verdict: at least one event is a hard no. Excuse drafting recommended.")
    elif n_hot:
        lines.append("Verdict: survivable, but keep a drink in hand and an exit in sight.")
    else:
        lines.append("Verdict: all clear. Your calendar is safe.")
    return "\n".join(lines)
