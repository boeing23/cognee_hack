# NopeList — hackathon submission

## Inspiration

Luma shows you who's going to an event. We all scroll that list looking for one specific name. So we built the agent that scrolls it for you: **NopeList**, your personal event bouncer. Keep a private list of people you'd rather not run into, and it tells you which events to skip, why, and drafts the excuse text. A funny premise on top of a serious stack: a Personal Brain that remembers your petty grudges, live web data on who's attending, and an agent that turns that into an action.

## What it does

- **Your blacklist is the brain.** Each person is stored in Cognee with their name, nicknames, Instagram and X handles, a threat level, and the reason ("cornered me about his seed round for 40 minutes").
- **It reads the guest lists.** For every Luma event you're registered for, it pulls the real attendee list with Bright Data and your Luma session (289 of 291 guests on the hackathon event tonight, 193 with social handles).
- **It matches like a person would.** Handle first, then name and nicknames, then a Cognee graph query for fuzzy variants like "Aakash V".
- **It gives a verdict per event:** 🟢 Clear, 🟡 Awkward, 🔴 Evacuate, with the quoted reason and a confidence score, then a copy-ready excuse text for the worst one.
- **One-screen UI.** Edit the list, paste event links or discover them from your Luma account, scan, read the verdict.

It's for avoiding events, not tracking people: results go only to you, it's a point-in-time check on events you're already registered for, and it never contacts anyone.

## How we built it

- **Cognee** stores the blacklist as a knowledge graph and answers "is this guest plausibly one of these people?" via `GRAPH_COMPLETION` search. Runs with local fastembed embeddings, so no extra embedding key.
- **Bright Data Browser API** (remote Chromium over CDP via Playwright) loads the Luma event page, title, and guest count; with the user's Luma session, the full paginated guest list comes from Luma's guest-list API.
- **AWS Strands** is the agent harness: tools for discover, scrape, match, render, and `suggest_excuse`, orchestrated by **Meta Muse Spark** through Strands' OpenAI-compatible model provider. A `--no-llm` mode runs the same pipeline deterministically for a demo that can't fail on stage.
- **Luma MCP** (`mcp.luma.com`) lists the events the user is attending, attached to the Strands agent as native tools. Luma uses OAuth with Client ID Metadata Documents, so we host the client metadata from the repo's GitHub Pages.
- **Docker** packages it as a non-root container that acts as the sandbox for untrusted event HTML and browser automation, with an offline `network_mode: none` profile.
- **FastAPI + one HTML file** for the UI.

Most of the code was written by coding agents in parallel workflows, with a shared `models.py` contract fixed up front so the modules fit together, then a verifier agent that installed deps and smoke-ran the pipeline before anything was committed.

## Challenges we ran into

- **Luma only shows guest lists to logged-in registrants.** The Bright Data browser saw the logged-out page and the parser happily returned event description bullets as guest names. We fixed the parser and added the user's session.
- **Bright Data's Browser API forbids injecting auth cookies over CDP.** So the authenticated guest list is fetched from Luma's API directly with the session cookie, while Bright Data still handles the page.
- **Luma MCP has no API keys and no dynamic client registration.** Getting a headless Python client through OAuth with Client ID Metadata Documents meant hosting a public client metadata JSON and pinning the MCP SDK to 1.x.
- **Luma MCP's `list_guests` is host-only.** We verified this live, which is exactly why Bright Data stays essential for events you merely attend.
- **Small config landmines**: fastembed wanted `sentence-transformers/all-MiniLM-L6-v2`, not the short name; Cognee's custom-endpoint path needs an `openai/` model prefix.

## Accomplishments that we're proud of

- A live, end-to-end run against tonight's actual event: real guests, real handles, and a real 🔴 Evacuate hit.
- All five integrations are load-bearing, not decorative: Cognee (memory), Bright Data (web), Strands + Muse Spark (agent), Luma MCP (your calendar), Docker (sandbox).
- A dry-run mode that demos the whole product with zero keys and zero network.
- Fuzzy matching that catches "Aakash V" as well as @vidiyala99.
- The excuse generator. It blames a sourdough starter.

## What we learned

- Pin the interface contract before fanning work out to parallel agents; it's the difference between modules that fit and an afternoon of glue.
- Verify sponsor APIs against live probes, not docs alone. Two of our design decisions (host-only guest lists, cookie injection being blocked) came from a real call failing.
- The "responsible scope" is often the demoable scope: staging an excuse text instead of auto-sending it made the product both safer and funnier.
- Luma's MCP is a genuinely good primitive for personal-calendar agents once you get through the OAuth model.

## What's next for NopeList

- A green list: "friendly faces" that make an event worth going to, and a verdict that weighs both.
- Photo matching for guests who show up with no handle.
- Notifications when someone on your list RSVPs to an event you're already going to.
- Confidence tuning with Cognee's graph over time: learn which name variants are the same person.
- Group mode: a shared list with friends, so the excuse texts can coordinate.
