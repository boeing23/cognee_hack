# NopeList 🚫 — Build Plan

> **Your personal event bouncer.** Maintain a private blacklist of people you'd
> rather not run into. NopeList scans the Luma events you're registered for,
> reads each guest list, and alerts you when someone on your list is going —
> so you know exactly which mixer to skip.

**Hackathon:** Battle of the (Personal) Brains — SF, Sept 21 2026
**Theme fit:** A Personal Brain (who you avoid + why) + live web data (who's
attending) + an agent that reasons and acts (raises a threat report).
**One-liner pitch:** *"It's a chatbot that gets you out of small talk."*

---

## 0. Why this wins the room

A funny premise is a Trojan horse for a serious stack. NopeList uses **all four
sponsors naturally** — the thing judges actually score — while giving a demo the
room will laugh at and remember.

| Sponsor | Role | Why it's load-bearing (not bolted on) |
|---|---|---|
| 🧠 **Cognee** | The blacklist **is** the brain | Stores each person as a graph entity (name, handles, photo, petty reason). Fuzzy identity resolution: `Aakash Vidiyala` ≈ `Aakash V` ≈ `@vidiyala99`. |
| 🌐 **Bright Data** | Reads the live guest lists | Luma guest modals are JS-rendered + semi-gated → Scraping Browser / Web Unlocker. Freshness is the whole point. |
| 🤖 **AWS Strands** | The agent harness | Orchestrates `discover → scrape → match → score → report` as a tool-calling loop. |
| 🐳 **Docker** | The sandbox | Runs browser automation + untrusted-HTML parsing in isolation. Legit safety story. |

---

## 1. Scope & guardrails

**In scope (4 hours):** blacklist brain, guest-list scrape for a handful of
events, fuzzy matching, a threat report. **Personal avoidance only.**

**Ethical framing (say this on stage — judges respect it):**
- Alerts go **only to you**. It tells you where *not* to go.
- It reads guest lists you can already see as a registrant.
- It does **not** track a person over time, across the web, or off-platform.
  No location history, no contacting anyone, no surveillance. Point-in-time
  "are they at an event I'm also registered for," nothing more.

**Explicitly out of scope:** DMing anyone, following someone between events,
compiling a dossier on a person, anything that isn't "help me dodge an awkward
evening."

---

## 2. Architecture

```
                        ┌──────────────────────────────┐
                        │        NopeList Agent         │
                        │      (AWS Strands loop)        │
                        └──────────────┬───────────────┘
                                       │ tool calls
        ┌──────────────────────┬───────┴────────┬──────────────────────┐
        ▼                      ▼                ▼                      ▼
  discover_events()     scrape_guests()    match_brain()         render_report()
   (my registered      (Bright Data       (Cognee graph          (threat levels
    Luma events)        Scraping Browser)   query / resolve)       + excuses)
        │                      │                │
        │                      ▼                ▼
        │              ┌───────────────┐  ┌──────────────┐
        │              │ Docker sandbox │  │ Cognee brain │
        │              │ browser+parse  │  │  (blacklist) │
        │              └───────────────┘  └──────────────┘
        ▼
   Option A: pasted event URLs   |   Option B: Luma MCP list_events (auto-discover)
                                     + list_guests for events you HOST (no scrape)
```

**Data flow:** seed blacklist → for each registered event, scrape the guest list
in a sandboxed browser → resolve each guest against the Cognee brain → score →
emit a report of which events are "hot."

---

## 3. The auth fork (decide first)

Two things live behind your Luma login: (a) the list of events you're registered
for, and (b) each event's full guest list. This is the only real technical risk,
so pick a lane up front.

- **Option A — Paste URLs (default; safest for a live demo).** You provide 3–5
  Luma event URLs you're registered for. The agent scrapes each guest list. No
  auth flow to break on stage. **Recommended for the demo.**
- **Option B — Official Luma MCP server (the "wow", and it's clean).**
  `https://mcp.luma.com` speaks streamable HTTP and authenticates with OAuth
  **Client ID Metadata Documents**: no API key, no dynamic client registration;
  the `client_id` is the URL of our hosted `docs/luma-client.json` (GitHub Pages,
  `https://boeing23.github.io/cognee_hack/luma-client.json`, redirect
  `http://localhost:3030/callback`). One-time `python -m src.luma_auth` in the
  main thread, tokens persist in `~/.nopelist/luma_tokens.json`, refresh is
  automatic. `list_events` answers (a) with the user's role per event; the Luma
  tools are also attached to the Strands agent via `MCPClient`.
  For (b): `list_guests` is **host/manager-only** (verified live), so hosted
  events get a structured guest list from MCP and attended events are still
  scraped with Bright Data. A is the fallback whenever there is no token.

> Guest lists hide "guests who have not completed their Luma profile," so treat
> the scrape as best-effort coverage, not a complete roster — note this on stage.

---

## 4. Sponsor integration details

### 🧠 Cognee — the blacklist brain
- **Ingest:** one JSON/markdown file of blacklisted people →
  `cognee.add()` → `cognee.cognify()` to build the graph.
- **Entity shape:** `name`, `aliases[]`, `instagram`, `x_handle`, `photo_url`,
  `reason`, `threat_weight` (1–3).
- **Query:** for each scraped guest, `cognee.search()` (GRAPH_COMPLETION) to ask
  *"is this person on my blacklist, allowing for name variants and handles?"* The
  graph beats a flat string match when someone writes their name differently or
  only exposes a handle.
- **Bonus:** persists across runs, and can store a `safe_list` (people you
  actually want to see) for the green-list feature.
- Credit: $50, promo `PERSONALBRAIN0926` → https://platform.cognee.ai/billing
- Examples: https://github.com/topoteretes/cognee/tree/main/examples

### 🌐 Bright Data — the guest-list reader
- **Product:** Scraping Browser (for the JS-rendered guest modal) and/or Web
  Unlocker for gated pages; Bright Data MCP server exposes these as agent tools.
- **Per event:** open the event page → open the "N Guests" modal → paginate →
  extract `{name, instagram, x_handle}` per guest.
- **Resilience:** pre-warm a cache of the demo events' guest lists so a flaky
  live scrape can't kill the demo (fall back to cache, still "live-shaped").
- Credit: $50, promo `cognee50` → https://brightdata.com?promo=cognee50&hs_signup=1

### 🤖 AWS Strands — the agent
- **Shape:** `Agent(model=..., tools=[discover_events, scrape_guests,
  match_brain, render_report])`. Model-driven loop over the four tools.
- **Model:** runs against an Anthropic API key directly or via Bedrock ($25 AWS
  credit). Anthropic key is the fastest path for the hackathon.
- **Each tool** is a plain `@tool`-decorated Python function; the Bright Data MCP
  server can also be attached as tools directly.
- Docs: https://strandsagents.com · SDK: https://github.com/strands-agents/sdk-python

### 🐳 Docker — the sandbox
- Run the Bright Data browser session + HTML→struct parsing inside a container so
  untrusted event pages are processed in isolation.
- Package the whole app as one `docker compose up` for reproducible judging.
- Bright Data / Cognee MCP servers can be run as containers via the Docker MCP
  catalog.

---

## 5. Four-hour timeline

| Time | Milestone | Definition of done |
|---|---|---|
| **0:00–0:30** | Repo + creds | `.env` with Cognee, Bright Data, Anthropic keys; `docker compose` skeleton boots. |
| **0:30–1:15** | Cognee brain | `blacklist.json` (incl. the demo nemesis) ingested; `match_brain("Aakash V")` returns a hit with the reason. |
| **1:15–2:15** | Bright Data scraper | `scrape_guests(event_url)` returns a clean `[{name, instagram, x_handle}]` for one real event. Cache the result. |
| **2:15–3:15** | Strands agent | Agent chains discover→scrape→match→report over 3 pasted event URLs end to end. |
| **3:15–3:45** | Threat report + jokes | Threat levels, petty-reason quotes, optional "suggest an excuse" via the model. |
| **3:45–4:00** | Demo polish | Pre-warm cache, rehearse the 60s script, seed a guaranteed live hit. |

**Stretch (only if ahead):** Option B Luma MCP auto-discovery · green-list safe
events · a one-screen web UI instead of terminal output.

---

## 6. The demo script (60 seconds)

1. *"Ever registered for an event, then realized *that* person is going?"*
2. Type a name into the blacklist live — with a reason: *"cornered me about his
   seed round for 40 minutes."*
3. Hit run. Agent streams: *discovering events… reading guest lists… matching…*
4. A real event you're registered for lights up:
   > ⚠️ **NopeList Alert** — "Model Distillation & Post-Training" mixer
   > **Aakash Vidiyala** (@vidiyala99) is going.
   > Reason on file: *"seed-round ambush, 40 min."*
   > Threat level: 🔴 **EVACUATE**
5. Kicker: *"…and here's your excuse text, already drafted."* (Strands output.)
6. Land it: *"Funny premise — but it's a real Personal Brain reading the live web
   and taking an action. All four sponsors, in a tool you'd actually use."*

**Threat levels:** 🟢 Clear · 🟡 Awkward · 🔴 Evacuate.

---

## 7. Repo layout (target)

```
nopelist/
├─ docker-compose.yml        # app + (optional) MCP server containers
├─ .env.example              # COGNEE_API_KEY, BRIGHTDATA_API_KEY, ANTHROPIC_API_KEY
├─ data/
│  ├─ blacklist.json         # people + reasons + handles
│  └─ events.txt             # Option A: pasted registered-event URLs
├─ src/
│  ├─ agent.py               # Strands Agent + tool registration + run loop
│  ├─ brain.py               # Cognee ingest + match_brain()
│  ├─ scraper.py             # Bright Data scrape_guests(event_url)
│  ├─ discover.py            # Option A reader / Option B Luma MCP auto-discover
│  ├─ luma_mcp.py            # Luma MCP client: OAuth (CIMD) + typed tool helpers
│  ├─ luma_auth.py           # one-time browser login (python -m src.luma_auth)
│  └─ report.py              # threat scoring + rendered alert + excuse gen
├─ docs/luma-client.json     # OAuth Client ID Metadata Document (served by GitHub Pages)
└─ README.md                 # quickstart + demo script
```

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Live scrape slow / rate-limited on stage | Pre-warm a cache keyed to the demo events; fall back silently. |
| Guest list incomplete (hidden profiles) | Say so on stage; frame as best-effort. It's a feature, not a bug. |
| Name collisions / false positives | Match on handle first, then name; store aliases in the graph; show confidence. |
| Luma auth eats build time (Option B) | Default to Option A; MCP login is a one-off (`src.luma_auth`) and discovery falls back to A without a token. |
| "Is a blacklist creepy?" from a judge | Lean into the guardrails: personal, alerts-only, no tracking, no contact. It's avoidance, not surveillance. |

---

## 9. Definition of done (demo-ready)

- [ ] Cognee brain returns a match + reason for the demo nemesis, incl. a name variant.
- [ ] Bright Data scrapes a real event's guest list into clean structured rows.
- [ ] Strands agent runs discover→scrape→match→report over 3 events unattended.
- [ ] Report shows threat levels and quotes the petty reason.
- [ ] Runs via `docker compose up`; 60s demo rehearsed with a guaranteed live hit.

---

*Build the brain. Build the agent. Dodge the nemesis. Enter the battle.* 🧠🚫
