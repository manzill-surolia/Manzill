# AGENTS.md

Guidance for AI agents (and humans) working in this repository.

## What this repo is

`manzill.com` — a **static site hosted on GitHub Pages** (served straight from this repo's
files; there is no server and no runtime secrets in any page). Most folders are hand-written
static pages (`index.html`, `iso-27001-2022/`, `security-tooling-landscape/`, …).

The one **dynamic** part is the AI-generated Hindi breaking-news page at `manzill.com/breaking`:

- `scripts/build_breaking_news.py` — the generator (fetch Google News RSS → cluster → 30-day
  archive → **one Groq call** writes the Hindi package → render `breaking/index.html`).
- `.github/workflows/breaking-news.yml` — runs it on a ~20-min cron and commits the output.
- Full spec + operator guide: **[`docs/breaking-news.md`](docs/breaking-news.md)**.

## ⚠️ Groq TPM gotcha — read before editing the generator

This account's Groq tier caps **8,000 tokens per minute (TPM)**, and Groq counts
**`prompt_tokens + max_tokens` per request** against that cap (not just the output).

Practical rules for `scripts/build_breaking_news.py`:

- Keep the request's **`max_tokens ≤ ~6000`** (the prompt is ~1.2k tokens, so ~6000 leaves a
  safe margin under 8000). The known-good baseline is `max_tokens = 4500`.
- **Do not run two AI passes** (e.g. a reporter *and* an editor/verify call) per run on this
  tier — two ~5–6k-token requests can't both fit inside one minute's 8000-token budget.
- **Extra API keys from the same Groq org add no headroom** — TPM is billed per org, so all
  keys share the one 8000 cap. Only a higher Groq tier, or keys in *separate* Groq
  accounts/orgs, give independent budgets.
- **Symptom of violating this:** the Groq call returns **HTTP 413 "Request too large … tokens
  per minute (TPM): Limit 8000"**, the script falls back to the Hindi holding page, and
  `manzill.com/breaking` shows an empty scaffold instead of a story. (This is exactly what a
  reverted redesign did — see the TPM-safe follow-up in `docs/breaking-news.md`.)

## Changing the breaking page

- After editing the template/prompt, **bump `RENDER_VERSION`** in the generator so the next
  scheduled run re-renders even when the feed is unchanged.
- Publish immediately (don't wait for cron) via **Actions → Breaking News Update → Run workflow**.
- The planned, budget-safe next improvements live under **"Future work — TPM-safe enhancements"**
  in `docs/breaking-news.md`.
