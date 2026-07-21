# `/breaking` — agent guide

Orientation for AI agents (and humans) working on **`manzill.com/breaking`**. Everything this page
needs lives in **this folder**. For the rest of the site, see the repo-root
[`../AGENTS.md`](../AGENTS.md).

## What this is

An **AI-generated, fully-Hindi live single-story breaking-news page** for **Jaipur**: the city's top
**breaking and political news**, one story at a time. Coverage is **Jaipur-only** — city incidents,
crime, accidents, civic issues, administration and politics (parties, the civic body, and the
Jaipur-seated state government). There is **no topic filter**: the strongest fresh Jaipur story of
the day leads, whatever the subject. The page is **bot-committed** by a GitHub Actions cron; do
**not** hand-edit `index.html` (it is overwritten each run). Full spec + operator guide:
[`breaking-news.md`](breaking-news.md).

## Files in this folder

| Path | What it is |
|------|-----------|
| `build_breaking_news.py` | The generator (fetch → cluster → lead → enrich → archive → Groq → render). Owns the HTML template, the Groq prompt, and the pipeline. |
| `snapshot.py` | Copies the live `index.html` into a dated archive folder (`breaking/20-july-2026/`) for the daily 12:00 IST snapshot. |
| `check_tpm.py` | Groq TPM budget checker — run before shipping any prompt change. |
| `breaking-news.md` | Full spec + operator guide. |
| `breaking-benchmark.md` | The golden "good-to-follow" **two worked examples** (one per timeline mode) + the standards the output must match. |
| `breaking-cases.json` | The same two use cases in machine-readable form (a **reference**, not runtime-injected). |
| `index.html` | The rendered page (**bot-committed** — do not hand-edit). |
| `<DD-month-YYYY>/index.html` | Daily archive snapshots (e.g. `20-july-2026/`) — static copies of the live page, published at 12:00 IST. |
| `rss.xml`, `sitemap.xml`, `favicon.svg` | Hindi RSS feed, news sitemap, Hawa Mahal favicon. |
| `data/state.json`, `data/archive.json`, `data/override.json` | Last-render state (+ `render_version` gate), rolling 30-day story archive, optional manual pin. |

## How it builds & deploys

`build_breaking_news.py` → `../.github/workflows/breaking-news.yml` (cron `*/20` + manual dispatch).
The page is a **single-story breaking-news desk for Jaipur**: it covers ONE story at a time — the
strongest fresh Jaipur story of the day, breaking or political. Flow: fetch Google News RSS (Jaipur
breaking + political queries + wider-window backfill) → **drop digest/roundup items** (`is_roundup`,
so unrelated stories never merge into one headline) → cluster → keep **Jaipur** stories (`is_local`,
which is now exactly `is_jaipur`) → pick a single fresh **lead** (`apply_lead`: the top-scored fresh,
non-ceremonial cluster — severity, public-interest strength, sources and recency decide the rank,
with a Jaipur boost baked in) → **web-enrich**: search related coverage of that one story and fold it
in (`enrich_lead`) → archive **every** story's multi-day arc (rolling 30 days) → call **Groq** for a
Hindi write-up with a rich, **multi-step, sourced** timeline — a generic story arc (पृष्ठभूमि →
घटना → कार्रवाई → आधिकारिक प्रतिक्रिया → प्रतिक्रियाएँ → आगे) as 2–3 sentence dated/relative-labelled
steps, not one-liners; a single-source scoop is padded from its own `key_facts`/`what_next` to
`MIN_TIMELINE_STEPS` (`ensure_timeline_depth`) so the timeline is **never a lone entry** — no
fabricated times/facts → render `index.html` (+ RSS + news sitemap), commit only on change. A story
stays eligible to lead for `FRESH_LEAD_HOURS` (20) so a day-old one-off ages out, and the "feed
unchanged → skip" gate is overridden once the page is older than `MAX_STALE_HOURS` (3) so it never
freezes on a stale lead. On a day with no fresh Jaipur story the **last page is kept** rather than
headlining a stale item. Only genuinely-sourced stories are ranked up; the AI never fabricates (theme
lists are top-of-file config).

**Voice = hard breaking-news, balanced:** the lead article opens with the newest development
(inverted pyramid) and reports facts through **attributed** claims/demands ("पुलिस के अनुसार", "विपक्ष
ने कहा") — factual and sensation-free, never the outlet's own opinion, and no taking-sides for or
against any party/authority. Ceremonial/feel-good items (`is_ceremonial`) are kept out of the lead
slot so a festival photo-op never leads over real breaking news. `has_failure_angle` /
`questions_authority` survive only as ranking hints that front-load accountability stories inside the
"यह भी ब्रेकिंग" secondary pool — they no longer gate what may lead.

**Timeline has two modes:** **"घटनाक्रम"** = one developing story's chronology (when the lead has
≥`SINGLE_CASE_MIN` own dated points); otherwise **"इस महीने"** = the month's *different* Jaipur
stories (`month_story_arc`, one line per story — not a false chronology). The list is **descending
(newest first)** and reveals on scroll (`IntersectionObserver`). The bottom **स्रोत (Source)** cards
**always** come from the timeline arc's own events — the varied outlets behind the घटनाक्रम / इस महीने
points (`arc_sources` + expanded `HINDI_SOURCE`) — so the source links always match the timeline
that's shown (they fall back to the lead cluster's own sources only if the arc yields none). Target
output is the golden [`breaking-benchmark.md`](breaking-benchmark.md) (two worked examples, one per
mode; machine-readable copy in [`breaking-cases.json`](breaking-cases.json)).

The AI's `key_facts`/`developments` arrays are defensively coerced (`_ai_str`/`_ai_str_list`) so a
malformed (nested) response never dumps raw structures onto the page. **Devanagari-only is enforced
in code**, not just prompted: `to_hindi()` (with the `ORG_HI` acronym map) strips every English
word/acronym and the model's `(analysis)`/`(lead_story)` field-name tags from all visible fields in
`_lead_from_ai`. Needs the repo secret **`GROQ_API_KEY`** (without it, a Hindi holding page shows).

## Daily archive snapshots

`snapshot.py` → `../.github/workflows/breaking-archive.yml` (cron `30 6 * * *` = **12:00 IST** +
manual dispatch). Once a day it copies the live `breaking/index.html` into a dated folder
`breaking/<DD-month-YYYY>/index.html` (e.g. `breaking/20-july-2026/`) and commits it, so each day's
front page stays permanently readable at a stable URL — `www.manzill.com/breaking/20-july-2026`. The
live page uses **absolute** asset paths (`/breaking/favicon.svg`, `/breaking/rss.xml`) and an
absolute canonical, so a verbatim copy renders correctly from the subfolder and its canonical
consolidates to the live `/breaking` page (the snapshot is an archive, not a duplicate to rank on its
own). Run on demand: **Actions → Breaking News Daily Snapshot → Run workflow** (optional `date` input
to backfill a slug), or `python breaking/snapshot.py --date 20-july-2026`.

## Editing the page

- Change `build_breaking_news.py` (it owns the HTML template, the Groq prompt, and the pipeline) and
  **bump `RENDER_VERSION`** so the next run repaints even if the feed is unchanged. Do **not**
  hand-edit `index.html` — it is overwritten by the bot.
- **Publish immediately** (don't wait for cron): **Actions → Breaking News Update → Run workflow → main**.
- The workflow commits with `git add -A breaking`; only files under `breaking/` are published.

## ⚠️ Groq TPM gotcha — read before touching the generator

The Groq account's tier caps **8,000 tokens per minute (TPM)**, and Groq counts
**`prompt_tokens + max_tokens` per request** against it (not just output). The Hindi prompt tokenizes
expensively, so a prompt edit can silently drift over the cap → `HTTP 413` → the empty holding page.

- **Check every prompt change with the TPM tool:** `python breaking/check_tpm.py` (offline
  conservative estimate of a worst-case request; non-zero exit on FAIL — CI-gateable) or
  `python breaking/check_tpm.py --api` (Groq's exact `usage.prompt_tokens`). The budget knobs live at
  the top of `build_breaking_news.py`: `GROQ_TPM_LIMIT=8000`, `GROQ_MAX_TOKENS=4500`, `TPM_BUDGET=7000`.
- **Self-healing:** `groq_analyze` runs a **preflight** (`estimate_tokens`/`_messages_tokens`) that
  shrinks the request (drop snippets → other-stories → down-sample history → lower max_tokens) to fit
  `TPM_BUDGET` before sending, and **retries once** with a minimal request on a 413 before falling
  back to the holding page. The message building is `_groq_messages` (shared with `check_tpm.py`).
- Keep the request's **`max_tokens ≤ ~5000`**; known-good baseline `4500`.
- Run a **single** AI pass. Two passes (e.g. reporter + editor) can't both fit in one minute.
- Extra API keys from the **same Groq org share the one cap** — no added headroom. Only a higher
  tier or keys in *separate* orgs give independent budgets.
- **Symptom of violating this:** `HTTP 413 "Request too large … TPM: Limit 8000"` → the script falls
  back to the empty Hindi holding page. (A redesign that raised `max_tokens` to 7000 hit exactly this
  and was reverted.) The TPM-safe way to expand is in
  [`breaking-news.md` → "Future work — TPM-safe enhancements"](breaking-news.md).

## Conventions

- **Fully-Hindi output** (Devanagari only; outlet names transliterated). Details in
  [`breaking-news.md`](breaking-news.md).
- **Bot commits use `GITHUB_TOKEN`**, which does **not** trigger other workflows — that's why
  `sitemap.yml` also has a daily cron (so the sitemap's `lastmod` still refreshes after the news bot
  pushes).

## Local dev / testing

- Install deps: `pip install -r ../scripts/requirements.txt` (only `tzdata`).
- Run without AI/secrets: `python breaking/build_breaking_news.py --no-ai` (renders from
  feeds/holding only — no `GROQ_API_KEY` needed).
- Snapshot the current page: `python breaking/snapshot.py` (or `--date 20-july-2026`).
- Check the Groq prompt's TPM budget: `python breaking/check_tpm.py`.
