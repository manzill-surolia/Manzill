# `/breaking` — agent guide

Orientation for AI agents (and humans) working on **`manzill.com/breaking`**. Everything this page
needs lives in **this folder**. For the rest of the site, see the repo-root
[`../AGENTS.md`](../AGENTS.md).

## What this is

An **AI-generated, fully-Hindi live single-story news page** on one beat: government/police
**bribery & policy incompetence** in Rajasthan (Jaipur-first). It is a **citizen-first
accountability tracker** — every post puts *this* government/JDA/police **under question**. The page
is **bot-committed** by a GitHub Actions cron; do **not** hand-edit `index.html` (it is overwritten
each run). Full spec + operator guide: [`breaking-news.md`](breaking-news.md).

## Files in this folder

| Path | What it is |
|------|-----------|
| `build_breaking_news.py` | The generator (fetch → cluster → policy lead → enrich → archive → Groq → render). Owns the HTML template, the Groq prompt, and the pipeline. |
| `check_tpm.py` | Groq TPM budget checker — run before shipping any prompt change. |
| `breaking-news.md` | Full spec + operator guide. |
| `breaking-benchmark.md` | The golden "good-to-follow" **two worked examples** (one per timeline mode) + the standards the output must match. |
| `breaking-cases.json` | The same two use cases in machine-readable form (a **reference**, not runtime-injected). |
| `index.html` | The rendered page (**bot-committed** — do not hand-edit). |
| `rss.xml`, `sitemap.xml`, `favicon.svg` | Hindi RSS feed, news sitemap, Hawa Mahal favicon. |
| `data/state.json`, `data/archive.json`, `data/override.json` | Last-render state (+ `render_version` gate), rolling 30-day story archive, optional manual pin. |

## How it builds & deploys

`build_breaking_news.py` → `../.github/workflows/breaking-news.yml` (cron `*/20` + manual dispatch).
The page is a **single-story accountability desk**: it covers ONE story at a time on one beat —
**government/police bribery & policy incompetence** in Rajasthan, Jaipur-first. Flow: fetch Google
News RSS (bribery/ACB/policy-failure queries + wider-window backfill) → **drop digest/roundup
items** (`is_roundup`, so unrelated stories never merge into one headline) → cluster → keep Rajasthan
stories (`is_local`, Jaipur-first via `is_jaipur`) → pick a single fresh **policy/bribery lead**
(`is_policy_beat` gate + `apply_policy_lead`; **soft** Jaipur preference via `W_JAIPUR` — a Jaipur
story usually leads but a bigger statewide story can overtake it) → **web-enrich**: search related
coverage of that one story and fold it in (`enrich_lead`) → archive **every** story's multi-day arc
(rolling 30 days) → call **Groq** for a Hindi write-up with a rich, **multi-step, sourced** timeline
— a bribery case's process arc (शिकायत → ट्रैप → गिरफ्तारी → एफआईआर → निलंबन → चार्जशीट → अदालत) or,
for any other story type, a generic arc (पृष्ठभूमि → घटना/आरोप → विभाग → आधिकारिक प्रतिक्रिया →
प्रतिक्रियाएँ/माँगें → आगे) as 2–3 sentence dated/relative-labelled steps, not one-liners; a
single-source scoop is padded from its own `key_facts`/`what_next` to `MIN_TIMELINE_STEPS`
(`ensure_timeline_depth`) so the timeline is **never a lone entry** — no fabricated times/facts →
render `index.html` (+ RSS + news sitemap), commit only on change. A story stays eligible to lead for
`FRESH_LEAD_HOURS` (20) so a day-old one-off ages out, and the "feed unchanged → skip" gate is
overridden once the page is older than `MAX_STALE_HOURS` (3) so it never freezes on a stale lead. On
a day with no fresh policy story the **last policy page is kept** — the page never drops to generic
news. Only genuinely-sourced stories are ranked up; the AI never fabricates (theme lists are
top-of-file config).

**Editorial stance:** the desk is a **citizen-first watchdog** — every post must put *this*
government/JDA/police UNDER QUESTION. Sourcing is accountability-first (`FEED_QUERIES`/
`ARCHIVAL_QUERIES` include grievances/protests/victims/compensation/custodial/cover-ups against the
authorities); the lead must `questions_authority` (names an authority + `has_failure_angle`), else a
failure-angle story, else the best fresh policy cluster (never empty); once a topic is picked,
`enrich_lead` runs **accountability-angle** related searches (`ACCOUNTABILITY_ANGLE_TERMS` + the
named authority's handling) so the timeline/title/description are built from coverage that questions
the govt/police. A headline must foreground the accountability & citizen-impact angle
(मुआवज़ा/पुनर्वास/देरी/लापरवाही), never praise a state action; `has_failure_angle` keeps a "govt did
its job" story (`NEUTRAL_ACTION_TERMS`) out of **both** the lead *and* the "यह भी ब्रेकिंग" secondary
cards (`order_secondary` gates on `has_failure_angle`).

**Voice = hard breaking-news, not editorial:** the lead article opens with the newest development
(inverted pyramid) and holds power to account through **attributed** facts/demands ("विपक्ष ने मांग
की") — never the outlet's own "सरकार को करना चाहिए" prescription.

**Timeline has two modes:** **"घटनाक्रम"** = one developing case's chronology (when the lead has
≥`SINGLE_CASE_MIN` own dated points); otherwise **"इस महीने उजागर भ्रष्टाचार"** = the month's
*different* cases (`month_accountability_arc`, one line per case — not a false chronology). The list
is **descending (newest first)** and reveals on scroll (`IntersectionObserver`); स्रोत cards come
from the varied outlets (`arc_sources` + expanded `HINDI_SOURCE`). Target output is the golden
[`breaking-benchmark.md`](breaking-benchmark.md) (two worked examples, one per mode; machine-readable
copy in [`breaking-cases.json`](breaking-cases.json)).

The AI's `key_facts`/`developments` arrays are defensively coerced (`_ai_str`/`_ai_str_list`) so a
malformed (nested) response never dumps raw structures onto the page. **Devanagari-only is enforced
in code**, not just prompted: `to_hindi()` (with the `ORG_HI` acronym map) strips every English
word/acronym and the model's `(analysis)`/`(lead_story)` field-name tags from all visible fields in
`_lead_from_ai`. Needs the repo secret **`GROQ_API_KEY`** (without it, a Hindi holding page shows).

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
- Check the Groq prompt's TPM budget: `python breaking/check_tpm.py`.
