# SEO Blog Agent

A Slack-native, multi-tenant agent that researches, writes, checks, illustrates and publishes SEO blog posts to WordPress for several small businesses at once.

It runs in two modes from the same codebase:

- **Auto** — on a schedule per merchant: pull trending search phrases, pick a topic that has not been covered, write, review, generate images, publish, and post a preview card to the merchant's Slack channel.
- **Chat** — a merchant talks to the bot in a Slack thread to shape a post (topic, tone, structure, their own photos), approves a preview, and publishes with one click.

> Built as an internal tool at a small AI hardware company and run for real local-business clients. This public copy replaces every client with a fictional one (`Harbor Auto Spa`, `Summit HVAC`); no client data, credentials or history are included.

---

## What is interesting here

Most "AI blog writer" demos are one prompt and a publish call. The work in this repo is mostly about **not trusting that prompt**:

### 1. Deterministic checks before any LLM judge

`eval/deterministic.py` runs 15 checks on every draft in milliseconds with no model calls: one `h1`, image placeholders match the image set, no invented image URLs, no emoji, no absolute claims ("guaranteed", "best in the world"), brand-voice banned words, prices grounded in the merchant catalog, excerpt length, heading count, keyword stuffing in headings, internal/external link counts, primary keyword in the first 100 words.

Each check has a severity in one table:

- **hard** — blocks the draft and is sent back to the writer as a precise instruction ("excerpt is 138 chars, needs 150–160"). The LLM reviewer is **not called** for a draft that already failed hard checks, which saves its cost.
- **soft** — reported and fixed opportunistically, never blocks publishing.

A check that was added but forgotten in the severity table defaults to **hard**: it is better to block a good post than to publish a bad one.

### 2. Repair what code can repair, rewrite only what it can't

`eval/repair.py` came from a real run: the excerpt was 161 characters against a limit of 160, which triggered two full rewrites for one character. Now:

- **Too long** → code trims to a word boundary (removing text needs no new information).
- **Too short** → goes back to the model (adding text needs information code doesn't have).

`agents/heading_fixer.py` applies the same idea to headings: if only the `h2`s are stuffed with the keyword, it rewrites **only the headings** (~$0.0003) instead of the whole post (~$0.03). The fixer sees all headings, not just the flagged ones — the first version only sent the flagged three and the model created new duplicates with the five it couldn't see.

### 3. Model routing by task, with a cross-family judge

`llm/router.py` maps a task to a model (`write` → strong model, `topic`/`review`/`artist` → cheap model, `judge` → a different model family than the writer, so the judge doesn't share the writer's blind spots). A merchant can override any task. Adding a provider means adding one file under `llm/providers/`; agents never import a vendor SDK directly. Every call is metered (`llm/cost.py`, `services/usage_tracker.py`, `tools/cost_report.py`).

### 4. One article, many channels, one set of checks

`channels/derive.py` turns a finished article into an email subject/preview and a social caption. Each derived item goes through **the same check functions** as the blog (banned words, absolute claims, price grounding); only emoji rules differ per channel. A failed derivation is retried once and then marked blocked — it never blocks the article itself.

### 5. Retrieval with an exit check

`retrieval/catalog.py` injects the relevant products into the prompt (keyword overlap, deliberately no vector DB for a catalog of a few dozen items), and `eval` verifies every price in the output against the **full** catalog. Retrieval only makes a correct answer more likely; the exit check is what guarantees it.

### 6. Slack concurrency done carefully

- Chat sessions are a state machine (`GATHERING → CONFIRMING → GENERATING → REVIEWING → DONE`) keyed by channel + thread, persisted to disk and restored on restart (`core/session.py`).
- Starting a generation and publishing a draft are **atomic claims** (`try_begin_generating`, `store/blog_store.try_claim_publish`), so a double-click or a retried Slack event cannot generate twice or publish the same post twice.

---

## Architecture

```
Slack (Socket Mode)
   │
main.py ── core/merchant_config.py ──► each merchant's Slack channel maps to its folder
   │
   ├─ channel commands (core/channel_router.py): "auto 3", "auto on 09:00 14:00", "publish"
   │     └─ scheduler.py ──► pipeline/blog_generator.py          (auto mode)
   └─ @mention in a thread: agents/conversation.py ──► pipeline/chat_generator.py   (chat mode)
   │
   ▼
pipeline (both modes share the same review loop)
   trend_scraper ──► researcher (topic) ──► web_researcher (competitors)
        │
        ▼
   copywriter ──► eval.check_seo ──hard fail──► rewrite with exact instructions
        │              │ pass
        │              ▼
        │         eval.repair (trim) ─► heading_fixer (soft) ─► reviewer (score ≥ 80)
        ▼
   artist ──► Seedream images ──► preview_server (HTML preview)
        │
        ▼
   wordpress_publisher (media upload, featured image, tags) ──► Slack preview card
        │
        ▼
   channels/derive (email, social) ── same eval checks
```

| Package | Role |
|---|---|
| `agents/` | One LLM call each: researcher, copywriter, reviewer, artist, heading fixer, chat intent |
| `eval/` | Pure functions, no network: deterministic checks + deterministic repair |
| `retrieval/` | Facts fed into prompts (product catalog) |
| `llm/` | Task → model routing, providers (OpenAI, Anthropic), cost |
| `pipeline/` | Auto and chat pipelines, shared review loop, preview server |
| `channels/` | Derive email / social copy from an article |
| `core/` | Merchant config, channel commands, chat session state machine, i18n (English/Chinese UI) |
| `merchants/<id>/` | Per-tenant config: `merchant.json`, brand "souls" for each agent, layouts, HTML templates, seed keywords |

## Adding a merchant

A merchant is a folder, not code:

```
merchants/harbor_auto_spa/
  merchant.json          # name, site URL, Slack channel, brand colors, post times
  scrape_targets.yaml    # seed keywords for trend scraping
  souls/                 # per-agent brand instructions (_shared.md + one per agent)
  layouts/               # article structures (how-to, comparison, FAQ, ...)
  templates/             # HTML templates for previews
  catalog.json           # optional: products with prices, enables price grounding
```

WordPress credentials are read from environment variables (`WP_<MERCHANT_ID>_USER` / `_PASSWORD`), never from `merchant.json`.

## Running

```bash
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                  # fill in tokens
python main.py
```

## Tests

```bash
pytest -q        # 154 tests, no API keys or network needed
```

The tests cover the parts that must be deterministic: every eval check and its severity, the repair rules, the review loop (including that a hard failure skips the paid reviewer), heading fixer edge cases, channel derivation and catalog grounding. LLM calls are replaced with fakes.

## Stack

Python 3.12 · Slack Bolt (Socket Mode) · OpenAI GPT-4.1 / 4.1-mini · Anthropic (judge) · Volcengine Seedream (images) · WordPress REST API · APScheduler

## Notes

- Code comments are mostly in Chinese (the team worked in Chinese); identifiers, prompts and this README are in English.
- The two merchants and all their details (addresses, phone numbers, prices) are fictional.
