# NopeList 🚫

**Your personal event bouncer.** Keep a private blacklist of people you'd rather not
run into. NopeList scans the Luma events you're registered for, reads each guest list,
matches guests against your blacklist brain, and emits a threat report per event:
🟢 **CLEAR** · 🟡 **AWKWARD** · 🔴 **EVACUATE** — plus a drafted excuse.

Built for *Battle of the (Personal) Brains* — SF, Sept 21 2026.
Plan: [docs/plan.md](docs/plan.md) · Ideas backlog: [docs/ideas.md](docs/ideas.md)

> **Personal avoidance only.** Alerts go to you and nobody else. It reads guest lists you
> can already see as a registrant, at a point in time. No tracking anyone over time, no
> dossiers, no contacting anyone.

## Sponsor stack

| Sponsor | Role | Why it's load-bearing (not bolted on) |
|---|---|---|
| 🧠 **Cognee** | The blacklist **is** the brain | Stores each person as a graph entity (name, handles, petty reason). Fuzzy identity resolution: `Aakash Vidiyala` ≈ `Aakash V` ≈ `@vidiyala99`. |
| 🌐 **Bright Data** | Reads the live guest lists | Luma guest modals are JS-rendered + semi-gated → Browser API (Scraping Browser) via Playwright CDP. Freshness is the whole point. |
| 🤖 **AWS Strands** | The agent harness | Orchestrates `discover → scrape → match → report` as a tool-calling loop. |
| 🐳 **Docker** | The sandbox | Runs browser automation + untrusted-HTML parsing in an isolated, non-root container. |
| 📅 **Luma MCP** | Knows which events you're going to | The official Luma MCP server (`https://mcp.luma.com`, streamable HTTP, OAuth via a Client ID Metadata Document — no API key). `list_events` discovers your registered events; `list_guests` gives structured guest lists for events you **host**. Its tools are attached to the Strands agent natively. |

## Quickstart

### 1. Keys

```bash
cp .env.example .env
# fill in: ANTHROPIC_API_KEY, LLM_API_KEY (same value), BRIGHTDATA_BROWSER_USER/PASS
```

Every variable is documented in [`.env.example`](.env.example), grouped by sponsor with
where-to-get-it notes. Minimum for a live run: an Anthropic key (Strands + Cognee) and a
Bright Data **Browser API** zone (scraper). Optional: Luma MCP login (below), Cognee
Cloud, Bedrock.

### 2. One-time Luma login (optional, no API key)

Luma's MCP server uses OAuth with a **Client ID Metadata Document**: the OAuth `client_id`
is the URL of [`docs/luma-client.json`](docs/luma-client.json). Serve it once, then log in:

1. GitHub repo → Settings → Pages → Source: *Deploy from a branch*, folder **`/docs`**.
   Check `https://boeing23.github.io/cognee_hack/luma-client.json` returns the JSON
   (that URL is `LUMA_CLIENT_METADATA_URL` in `.env`).
2. `python -m src.luma_auth` — opens the Luma consent page, catches the redirect on
   `http://localhost:3030/callback`, saves tokens to `~/.nopelist/luma_tokens.json`
   (override with `LUMA_TOKEN_FILE`), prints the tool list and `Logged in as <you>`.
   `--status` / `--logout` do what they say. Run it in a terminal, not inside the agent:
   Strands' `MCPClient` runs in a background thread, so the browser flow lives here.

What you get: `discover_events()` reads your upcoming events straight from Luma
(`list_events`, merged with `data/events.txt`), the agent gets the Luma tools
(`list_events`, `get_event`, `lookup_entity`, `list_guests`, ...) and events you **host**
skip scraping entirely (`list_guests`). **Why Bright Data still scrapes:** `list_guests`
is host/manager-only (verified live: attended events answer *"You don't have access to
this event"*), so guest lists of events you merely attend come from the Browser API.

### 3. Events + blacklist

- `data/events.txt` — one Luma event URL per line (events you're registered for). Optional
  once you're logged in to Luma; extra URLs here are merged in.
- `data/blacklist.json` — people, handles, aliases, reason, `threat_weight` (1–3), `safe`.

### 4a. Run with Docker (recommended for judging)

```bash
docker compose build
docker compose run --rm nopelist            # full Strands agent loop
docker compose run --rm nopelist --no-llm   # deterministic pipeline, same tools
```

`./data` is bind-mounted, so the scrape cache (`data/cache/`) and the Cognee brain
(`data/.cognee/`) persist between runs and you can edit the blacklist live.

### 4b. Run locally

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # no `playwright install` needed: the browser is remote
python -m src.agent                      # or: python -m src.agent --no-llm
python -m src.agent --events data/events.txt --blacklist data/blacklist.json
```

Pre-warm before the demo (one-off, needs keys):

```bash
python -c 'import asyncio; from src.brain import *; asyncio.run(ingest_blacklist(load_blacklist()))'
python -m src.scraper https://lu.ma/<slug> --no-cache
```

## Dry-run demo (stage wifi insurance)

Zero network calls: fixtures from `data/fixtures/`, deterministic handle/name/alias
matching, no LLM. Add at least one URL to `data/events.txt` (any slug matches
`guests_default.json`).

```bash
# Docker — runs with network_mode: none to prove it
docker compose --profile dry run --rm nopelist-dry

# Local
NOPELIST_DRY_RUN=1 python -m src.agent --no-llm
```

## Where each sponsor lives in the code

| Sponsor | Files | What happens there |
|---|---|---|
| 🧠 Cognee | [`src/brain.py`](src/brain.py) | `cognee.add()` + `cognee.cognify()` ingest the blacklist into dataset `nopelist_blacklist`; `match_guest()` tries handle → name/alias, then falls back to `cognee.search(GRAPH_COMPLETION)` for fuzzy identity resolution. Optional Cognee Cloud via `COGNEE_SERVICE_URL`. |
| 🌐 Bright Data | [`src/scraper.py`](src/scraper.py) | Playwright `connect_over_cdp()` to the Browser API (`wss://…@brd.superproxy.io:9222`), loads the event page, pulls the guest list from the DOM modal (plus `featured_guests` from Luma's public URL endpoint), writes `data/cache/`. Used for every event you attend but don't host. |
| 📅 Luma MCP | [`src/luma_mcp.py`](src/luma_mcp.py), [`src/luma_auth.py`](src/luma_auth.py), [`src/discover.py`](src/discover.py), [`docs/luma-client.json`](docs/luma-client.json) | `OAuthClientProvider` (mcp SDK) with the CIMD as `client_id` + file token storage; `MCPClient` over `streamablehttp_client` so refresh is automatic. `discover.py` calls `list_events`; `scraper.py` calls `get_event` + `list_guests` for hosted events; `agent.py` attaches all Luma tools to the agent. |
| 🤖 AWS Strands | [`src/agent.py`](src/agent.py) | `strands.Agent` with `@tool`s `discover_registered_events`, `scrape_event_guests`, `match_guests_against_brain`, `render_threat_report`, `suggest_excuse`, plus the Luma MCP tools via `strands.tools.mcp.MCPClient` when logged in. `AnthropicModel` first, `BedrockModel` if only AWS creds are set. `--no-llm` calls the same tools directly. |
| 🐳 Docker | [`Dockerfile`](Dockerfile), [`docker-compose.yml`](docker-compose.yml) | `python:3.11-slim`, non-root user, `no-new-privileges`; embeds the fastembed model; `nopelist-dry` profile runs with no network. Optional Bright Data (`mcp/brightdata`, Docker MCP Catalog) and Cognee (`cognee/cognee-mcp`) MCP server containers are stubbed, commented out. |
| shared | [`src/models.py`](src/models.py), [`src/report.py`](src/report.py) | `Person / Guest / Event / Match / EventResult` contract; threat scoring + rendered report. |

## Demo script (60s)

See [docs/plan.md §6](docs/plan.md). Threat levels: 🟢 Clear · 🟡 Awkward · 🔴 Evacuate.
