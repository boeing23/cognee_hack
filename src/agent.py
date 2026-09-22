"""NopeList agent: AWS Strands Agents harness over discover -> scrape -> match -> report.

Run:
    python -m src.agent                      # Strands agent loop (needs a model key)
    python -m src.agent --no-llm             # deterministic pipeline, no model calls
    NOPELIST_DRY_RUN=1 python -m src.agent --no-llm   # fixtures only, no network

Model selection (env), first match wins:
    MODEL_API_KEY       -> strands.models.openai.OpenAIModel against the Meta Model API
                           (Muse Spark, OpenAI-compatible: https://api.meta.ai/v1)  [PRIMARY]
    ANTHROPIC_API_KEY   -> strands.models.anthropic.AnthropicModel                  [fallback]
    AWS_ACCESS_KEY_ID / AWS_PROFILE / AWS_BEARER_TOKEN_BEDROCK -> BedrockModel      [fallback]
    NOPELIST_MODEL_PROVIDER=meta|anthropic|bedrock -> force one provider
    STRANDS_MODEL_ID    -> override the model id for whichever provider is active
    META_API_BASE_URL   -> override the Meta base URL (default https://api.meta.ai/v1)

Luma MCP (src/luma_mcp.py): after `python -m src.luma_auth`, the official Luma
MCP server's tools (list_events, get_event, lookup_entity, list_guests, ...) are
attached to the Strands agent next to the local @tools, via
strands.tools.mcp.MCPClient. Skipped in NOPELIST_DRY_RUN or when not logged in.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional, TypeVar

from src.models import Event, EventResult, Match, Person
from src.report import build_results, render_report

T = TypeVar("T")

DEFAULT_META_MODEL = "muse-spark-1.3"          # https://dev.meta.ai/docs (also: muse-spark-1.1)
DEFAULT_META_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


# --------------------------------------------------------------------------- #
# Shared state between tools
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    people: list[Person] = field(default_factory=list)
    event_urls: list[str] = field(default_factory=list)
    events: dict[str, Event] = field(default_factory=dict)          # url -> Event
    matches: dict[str, list[Match]] = field(default_factory=dict)   # url -> matches
    events_file: Optional[str] = None
    blacklist_path: str = "data/blacklist.json"

    def results(self) -> list[EventResult]:
        ordered = [self.events[u] for u in self.event_urls if u in self.events]
        return build_results(ordered, self.matches)


SESSION = Session()


def _run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from sync code, even if a loop is already running in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - propagate to caller
            box["error"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Plain implementations (used directly by --no-llm and wrapped as Strands tools)
# --------------------------------------------------------------------------- #
def _discover_registered_events() -> str:
    from src import discover

    if SESSION.events_file:
        os.environ["NOPELIST_EVENTS_FILE"] = SESSION.events_file
    entries = discover.discover_events_detailed()
    SESSION.event_urls = [e["url"] for e in entries]
    if not entries:
        return "No registered events found."
    lines = ["Registered events:"]
    for e in entries:
        role = "hosting" if e.get("is_host") else (e.get("status") or "attending")
        lines.append(f"- {e['url']}  ({role}; {e.get('name', '')}; source={e.get('source', 'events.txt')})")
    return "\n".join(lines)


def _scrape_event_guests(event_url: str) -> str:
    from src import scraper

    event_url = event_url.strip()
    event = scraper.scrape_guests(event_url, use_cache=True)
    # Key on the URL the caller passed so match_guests_against_brain(event_url) finds it.
    SESSION.events[event_url] = event
    if event_url not in SESSION.event_urls:
        SESSION.event_urls.append(event_url)
    from src import luma_mcp

    if event.from_cache:
        src = "cache"
    elif luma_mcp.is_hosted_event(event_url):
        src = "luma-mcp"
    else:
        src = "live"
    total = f" of {event.guest_count}" if event.guest_count is not None else ""
    return f"Scraped '{event.title}' ({src}): {len(event.guests)} guests{total}."


def _match_guests_against_brain(event_url: str) -> str:
    from src import brain

    event_url = event_url.strip()
    event = SESSION.events.get(event_url)
    if event is None:
        return f"Event {event_url} has not been scraped yet. Call scrape_event_guests first."
    if not SESSION.people:
        SESSION.people = brain.load_blacklist(SESSION.blacklist_path)
    matches = _run_async(brain.match_guests(event.guests, SESSION.people))
    SESSION.matches[event_url] = list(matches)
    if not matches:
        return f"'{event.title}': no one from your list is going."
    lines = [f"'{event.title}': {len(matches)} match(es)."]
    for m in matches:
        tag = "SAFE" if m.person.safe else f"threat {m.person.threat_weight}"
        lines.append(
            f"- {m.guest.name} -> {m.person.name} ({tag}, {m.confidence:.0%} via {m.matched_on}): {m.person.reason}"
        )
    return "\n".join(lines)


def _render_threat_report() -> str:
    return render_report(SESSION.results())


def _worst_event() -> Optional[EventResult]:
    results = [r for r in SESSION.results() if r.hits]
    if not results:
        return None
    return max(results, key=lambda r: (r.threat_score, len(r.hits)))


def _suggest_excuse(event_title: str, person_name: str, reason: str) -> str:
    """Deterministic excuse text. In LLM mode the agent is asked to punch it up."""
    return (
        f"hey! so sorry, can't make it to {event_title} tonight after all. "
        f"my sourdough starter is having a medical emergency and it needs me. "
        f"(real reason, between us: {person_name} is going and last time it was "
        f"\"{reason}\". i'm not strong enough.) rain check?"
    )


# --------------------------------------------------------------------------- #
# Strands tools
# --------------------------------------------------------------------------- #
def _build_tools() -> list[Callable[..., Any]]:
    from strands import tool

    @tool
    def discover_registered_events() -> str:
        """List the Luma event URLs the user is registered for. Always call this first."""
        return _discover_registered_events()

    @tool
    def scrape_event_guests(event_url: str) -> str:
        """Read the guest list of one Luma event: Luma MCP list_guests for events the
        user hosts, otherwise Bright Data (or the cache).

        Args:
            event_url: The Luma event URL, exactly as returned by discover_registered_events.
        """
        return _scrape_event_guests(event_url)

    @tool
    def match_guests_against_brain(event_url: str) -> str:
        """Resolve a scraped event's guests against the Cognee blacklist brain.

        Args:
            event_url: The Luma event URL that has already been scraped.
        """
        return _match_guests_against_brain(event_url)

    @tool
    def render_threat_report() -> str:
        """Render the final threat report (CLEAR / AWKWARD / EVACUATE) for all scanned events."""
        return _render_threat_report()

    @tool
    def suggest_excuse(event_title: str, person_name: str, reason: str) -> str:
        """Draft a short, funny text-message excuse for skipping an event.

        Returns a baseline draft; rewrite it in your own words so it is funnier,
        under 60 words, and never names the person being avoided.

        Args:
            event_title: The event the user wants to skip.
            person_name: The blacklisted person who is going.
            reason: The petty reason on file for avoiding them.
        """
        return _suggest_excuse(event_title, person_name, reason)

    return [
        discover_registered_events,
        scrape_event_guests,
        match_guests_against_brain,
        render_threat_report,
        suggest_excuse,
    ]


SYSTEM_PROMPT = """You are NopeList, a personal event bouncer. The user keeps a private
blacklist of people they'd rather not run into. Your job, in this exact order:

1. discover_registered_events - get the Luma events the user is registered for.
2. scrape_event_guests for EVERY event URL, one call per event.
3. match_guests_against_brain for EVERY scraped event, one call per event.
4. render_threat_report once, and include its full output verbatim in your answer.
5. If any event is AWKWARD or EVACUATE, call suggest_excuse for the single worst
   event (highest threat) and finish with a punched-up, under-60-word excuse text
   the user can send to the host. Never name the person being avoided in the excuse.

When the official Luma MCP tools are attached (list_events, get_event,
lookup_entity, list_guests, get_self, ...) you may use them to check event
details or confirm the user's role. list_guests works ONLY for events the user
hosts or manages; for events they merely attend it is denied, and
scrape_event_guests (Bright Data) is the way to read the guest list. Do not
create, edit, invite, message or otherwise mutate anything on Luma.

Rules: alerts go only to the user. Do not suggest contacting, tracking, or
investigating anyone. Keep commentary short and dry; the report speaks for itself.
"""


def _has_aws_creds() -> bool:
    return any(os.getenv(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK"))


def _pick_provider() -> str:
    """Explicit NOPELIST_MODEL_PROVIDER wins; else MODEL_API_KEY -> ANTHROPIC_API_KEY -> AWS."""
    forced = os.getenv("NOPELIST_MODEL_PROVIDER", "").strip().lower()
    if forced:
        if forced not in {"meta", "anthropic", "bedrock"}:
            raise SystemExit(
                f"NOPELIST_MODEL_PROVIDER={forced!r} is not one of meta|anthropic|bedrock."
            )
        return forced
    if os.getenv("MODEL_API_KEY"):
        return "meta"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if _has_aws_creds():
        return "bedrock"
    raise SystemExit(
        "No model credentials. Set MODEL_API_KEY (Meta Model API, https://dev.meta.ai), "
        "or ANTHROPIC_API_KEY / AWS creds for the fallback providers, or run with --no-llm."
    )


def build_model() -> Any:
    """Construct the Strands model for the chosen provider. Never touches the network.

    Meta (primary): OpenAI-compatible chat completions at https://api.meta.ai/v1 via
    strands.models.openai.OpenAIModel (docs: strandsagents.com model-providers/openai,
    dev.meta.ai/docs/protocols/chat-completions).
    """
    provider = _pick_provider()
    model_id = os.getenv("STRANDS_MODEL_ID")
    max_tokens = int(os.getenv("STRANDS_MAX_TOKENS", "2048"))

    if provider == "meta":
        api_key = os.getenv("MODEL_API_KEY")
        if not api_key:
            raise SystemExit("NOPELIST_MODEL_PROVIDER=meta but MODEL_API_KEY is not set.")
        from strands.models.openai import OpenAIModel

        base_url = os.getenv("META_API_BASE_URL", DEFAULT_META_BASE_URL)
        model_id = model_id or DEFAULT_META_MODEL
        _log(f"[model] meta (Muse Spark) {model_id} via {base_url}")
        return OpenAIModel(
            client_args={"api_key": api_key, "base_url": base_url},
            model_id=model_id,
            params={"max_tokens": max_tokens},
        )

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise SystemExit("NOPELIST_MODEL_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
        from strands.models.anthropic import AnthropicModel

        model_id = model_id or DEFAULT_ANTHROPIC_MODEL
        _log(f"[model] anthropic {model_id}")
        return AnthropicModel(
            client_args={"api_key": api_key},
            model_id=model_id,
            max_tokens=max_tokens,
        )

    # bedrock
    if not _has_aws_creds():
        raise SystemExit("NOPELIST_MODEL_PROVIDER=bedrock but no AWS credentials are set.")
    from strands.models import BedrockModel

    model_id = model_id or DEFAULT_BEDROCK_MODEL
    region = os.getenv("AWS_REGION", "us-west-2")
    _log(f"[model] bedrock {model_id} ({region})")
    return BedrockModel(model_id=model_id, region_name=region)


# Backwards-compatible alias.
_build_model = build_model


_SEEN_TOOL_USES: set[str] = set()


def _callback_handler(**kwargs: Any) -> None:
    """Stream model text to stdout and announce each tool step once on stderr."""
    if "data" in kwargs:
        print(kwargs["data"], end="", flush=True)
    tool_use = kwargs.get("current_tool_use")
    if tool_use and tool_use.get("name"):
        key = str(tool_use.get("toolUseId") or tool_use["name"])
        if key not in _SEEN_TOOL_USES:
            _SEEN_TOOL_USES.add(key)
            _log(f"\n[tool] {tool_use['name']}")


PROMPT = (
    "Scan every event I'm registered for, tell me where my nemeses are, "
    "and draft me an excuse for the worst one."
)


def _run_agent(extra_tools: list[Any]) -> str:
    from strands import Agent

    agent = Agent(
        model=build_model(),
        tools=_build_tools() + list(extra_tools),
        system_prompt=SYSTEM_PROMPT,
        callback_handler=_callback_handler,
    )
    result = agent(PROMPT)
    print()
    return str(result)


def run_with_agent() -> str:
    """Strands loop. With a Luma login (and not dry-run) the official Luma MCP
    tools are attached; the agent is built and run INSIDE the MCPClient context."""
    from src import luma_mcp
    from src.brain import dry_run

    if dry_run() or not luma_mcp.has_token():
        if not dry_run():
            _log(f"[luma] not logged in; Luma MCP tools not attached. {luma_mcp.LOGIN_HINT}")
        return _run_agent([])

    _log("[luma] attaching official Luma MCP tools (https://mcp.luma.com)")
    with luma_mcp.make_mcp_client() as luma:
        mcp_tools = luma.list_tools_sync()
        _log(f"[luma] {len(mcp_tools)} MCP tools: " + ", ".join(t.tool_name for t in mcp_tools))
        return _run_agent(mcp_tools)


def run_without_llm() -> str:
    """Same pipeline, no model loop. This is the demo-safety path."""
    _log("[1/4] discovering registered events")
    _log(_discover_registered_events())
    for url in list(SESSION.event_urls):
        _log(f"[2/4] scraping {url}")
        _log(_scrape_event_guests(url))
    for url in list(SESSION.event_urls):
        _log(f"[3/4] matching {url}")
        _log(_match_guests_against_brain(url))
    _log("[4/4] rendering report")
    report = _render_threat_report()
    worst = _worst_event()
    if worst is not None:
        top = worst.hits[0]
        excuse = _suggest_excuse(worst.event.title, top.person.name, top.person.reason)
        report += f"\n\n\U0001f4f1 Excuse draft for '{worst.event.title}':\n{excuse}"
    return report


def _ingest(blacklist_path: str) -> None:
    from src import brain

    SESSION.blacklist_path = blacklist_path
    SESSION.people = brain.load_blacklist(blacklist_path)
    n = _run_async(brain.ingest_blacklist(SESSION.people))
    _log(f"[brain] {n} people ingested into Cognee from {blacklist_path}")


def main(argv: Optional[list[str]] = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m src.agent", description=__doc__.split("\n")[0])
    parser.add_argument("--no-llm", action="store_true", help="run the pipeline without the Strands agent loop")
    parser.add_argument("--events", default=None, help="path to a file of registered Luma event URLs")
    parser.add_argument("--blacklist", default=os.getenv("NOPELIST_BLACKLIST", "data/blacklist.json"))
    args = parser.parse_args(argv)

    SESSION.events_file = args.events
    from src.brain import dry_run

    if dry_run():
        _log("[dry-run] NOPELIST_DRY_RUN=1: fixtures only, no network")

    _ingest(args.blacklist)
    output = run_without_llm() if args.no_llm else run_with_agent()
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
