# Top 5 Agent Ideas — judged

Sponsor research → ideation → a 3-judge panel scored 16 candidates on
feasibility (4h), stack fit, wow/demo, winning potential, and usefulness
(max 50). Every idea uses **Cognee** (brain) + **Bright Data** (live web) +
**AWS Strands** (agent) + **Docker** (sandbox).

| # | Idea | Avg | feas | stack | wow | win | use |
|---|---|---|---|---|---|---|---|
| 1 | **WarmIntro** — pre-meeting dossier + drafted intro email | **40.3** | 8.0 | 8.0 | 8.0 | 8.3 | 8.0 |
| 2 | **Chief of Staff** — inbox → reply + calendar hold | **40.0** | 8.3 | 8.0 | 7.7 | 7.7 | 8.3 |
| 3 | **60-Second Due-Diligence Analyst** — sourced brief + chart | **39.3** | 6.7 | 8.7 | 9.0 | 8.0 | 7.0 |
| 4 | **Meeting-to-Momentum** — transcript → scheduled to-dos | **38.7** | 6.7 | 7.3 | 8.7 | 8.0 | 8.0 |
| 5 | **Second Brain That Computes** — writes code to answer | **37.0** | 6.0 | 9.0 | 8.0 | 6.7 | 7.3 |

## 1. WarmIntro (winner)
Before every meeting, your brain + the live web fuse into a one-page dossier and
a drafted intro email. **Demo:** judge names an attendee → 30s later "raised a
Series B 6 days ago; you emailed their cofounder in March" + a Gmail draft.
Cleanest 4h scope, low legal risk, Bright Data is the visible star.

## 2. Chief of Staff: Inbox-to-Action
Reads a messy inbox, recalls a past commitment from Cognee, enriches with a fresh
Bright Data fact, drafts a reply, drops a calendar hold. Seed `.eml` files + local
ICS = no OAuth, no live-send risk. Highest reliability-to-wow.

## 3. 60-Second Due-Diligence Analyst
Type a company → 3 Strands sub-agents fan out (streamed), a graph populates, a
brief renders with 8 citations + a chart. Best Bright Data showcase; keep a cache
fallback for the demo companies.

## 4. Meeting-to-Momentum
Transcript in → real calendar blocks out, one enriched with a live web fact.
Strongest "it acted" verb; watch calendar OAuth time.

## 5. Second Brain That Computes
Cross-source question → agent pulls a private figure (Cognee) + a live number
(Bright Data), writes Python, runs it in the Docker sandbox, returns the answer +
chart. The only idea where Docker is the star.

## The job-search auto-apply idea (requested)

Judged head-to-head as a required candidate. The panel also generated 3 more
job-apply variants; the whole cluster landed mid-pack.

| Variant | Avg | Rank |
|---|---|---|
| AutoApply **Copilot** (live form, pause on Submit) | 36.7 | 6th |
| AutoApply (required — stage drafts) | 35.0 | 9th |
| Career Autopilot | 35.0 | 8th |
| AutoApply (risky auto-submit) | 31.0 | 14th |

**Unanimous verdict:** most *useful* concept in the field, but capped for this
hackathon because the responsible 4h scope stages drafts and stops before
Submit — removing the "take an action" verb judges reward. Live job-board
scraping is also the most anti-bot-hardened source (most demo-brittle), and
auto-tailoring invites embellishment (an ethics flag industry judges probe.)

**If you build it:** ship the **Copilot** — drive a real Greenhouse/Lever form in
a Docker-sandboxed browser and **pause on Submit** (human-in-the-loop). That
reframe turns the ethics liability into a design virtue, and "watch it type into
a real application form and stop" is the cluster's highest-wow moment. Never
auto-submit, never embellish, pre-test the exact posting.
