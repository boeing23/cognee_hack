# The Agentic Stack

Technologies you can combine to build your agent. Together they give your agent:
**Data + Memory + Reasoning + Tools + Actions**.

## 🧠 Cognee — The Brain

Create long-term AI memory and a Personal Brain your agent can search,
understand, and reason over.

- $50 Cognee Cloud credits (promo `PERSONALBRAIN0926`)
- Billing: https://platform.cognee.ai/billing
- Company Brain: https://www.cognee.ai/company-brain
- Examples: https://github.com/topoteretes/cognee/tree/main/examples
- Discord: https://discord.gg/jrxTg4QDW (#hackathon-sf-sept-26)

## 🌐 Bright Data — The Web

Give your agent access to fresh public information from across the web.

- $50 credits (promo `cognee50`, 60 days)
- https://brightdata.com?promo=cognee50&hs_signup=1

## 📁 Your Data — The Personal Brain

Bring your own files and information from your laptop, Google Drive, and other
sources.

## 🤖 AWS Strands Agents — The Agent Harness

Build and orchestrate the agent that reasons, uses tools, and decides what to do
next.

- $25 AWS credits: https://pulse.amazon/promotion/ZENKK7R1
- Docs: https://strandsagents.com
- GitHub: https://github.com/strands-agents/sdk-python

## 🐳 Docker Sandboxes — The Sandbox

Give your agent a secure environment where it can run code and perform tasks.

## How the pieces fit

```
   Bright Data ──▶ public web data ─┐
   Your files  ──▶ local/cloud data ┼─▶  Cognee (memory/brain)
   Email/Drive ──▶ personal data ────┘            │
                                                   ▼
                            AWS Strands Agent (reason + orchestrate)
                                                   │
                                    ┌──────────────┴──────────────┐
                                    ▼                             ▼
                            Docker sandbox                  Tools / Actions
                          (run code safely)          (calendar, email, notes...)
```
