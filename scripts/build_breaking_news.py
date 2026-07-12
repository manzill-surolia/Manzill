#!/usr/bin/env python3
"""Generate www.manzill.com/breaking-news — a live, AI-authored breaking-news
page for Jaipur.

Pipeline: fetch Google News RSS -> rank/cluster into a lead "story of the day"
-> ask Groq (OpenAI-compatible) for editorial + a timeline development -> gate
updates by severity-driven cadence -> render breaking-news/index.html and
persist breaking-news/data/state.json.

Runs from GitHub Actions on a ~20 min cron. No server, no secrets in the page:
the Groq key is read from the GROQ_API_KEY environment variable only.

Usage:
    python scripts/build_breaking_news.py            # full run (needs GROQ_API_KEY)
    python scripts/build_breaking_news.py --no-ai    # render from feeds only
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - fallback if tzdata unavailable
    from datetime import timedelta
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent
OUT_HTML = ROOT / "breaking-news" / "index.html"
STATE_PATH = ROOT / "breaking-news" / "data" / "state.json"

# --------------------------------------------------------------------------- #
# Feeds & scoring
# --------------------------------------------------------------------------- #
GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
FEED_QUERIES = [
    "Jaipur when:1d",
    "Jaipur breaking when:1d",
    "Rajasthan Jaipur police when:1d",
    "Jaipur fire OR accident OR blast when:1d",
    "Jaipur protest OR clash OR crime when:1d",
    "Jaipur weather OR IMD OR rain warning when:1d",
]

# Words that mark a high-impact, fast-moving event. Order = descending severity.
SEVERITY_KEYWORDS = {
    "critical": [
        "terror", "terrorist", "blast", "bomb", "explosion", "firing", "shooting",
        "hostage", "earthquake", "quake", "tremor", "stampede", "riot", "flood",
    ],
    "high": [
        "fire", "accident", "crash", "collapse", "murder", "killed", "death",
        "dead", "encounter", "clash", "protest", "rape", "kidnap", "assault",
        "arrest", "raid", "flash flood", "cloudburst",
    ],
    "medium": [
        "police", "probe", "investigation", "court", "fir", "case", "traffic",
        "power cut", "water", "civic", "strike", "alert", "warning",
    ],
}

# Severity -> minimum minutes between *forced* (feed-changed) timeline updates.
CADENCE_MINUTES = {"critical": 20, "high": 30, "medium": 60, "low": 120}

STOPWORDS = set(
    "the a an of to in on for and or with at by from as is are was were be been "
    "over after amid new latest news update jaipur rajasthan india said says say "
    "will has have had its it this that these those into out up down off near "
    "day today man woman year old two three four five".split()
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "manzill-breaking-news/1.0"
)

GROQ_BASE = "https://api.groq.com/openai/v1"
MODEL_PREFERENCE = [
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
]


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def fmt_ist(dt: datetime) -> str:
    return to_ist(dt).strftime("%d %b %Y, %I:%M %p IST")


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def keywords(text: str) -> set[str]:
    toks = normalize(text).split()
    return {t for t in toks if len(t) > 2 and t not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def severity_of(text: str) -> str:
    low = " " + normalize(text) + " "
    for level in ("critical", "high", "medium"):
        for kw in SEVERITY_KEYWORDS[level]:
            if " " + kw + " " in low or kw in low:
                return level
    return "low"


def severity_rank(level: str) -> int:
    return {"critical": 3, "high": 2, "medium": 1, "low": 0}.get(level, 0)


# --------------------------------------------------------------------------- #
# Feed fetching
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: int = 20, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_pubdate(raw: str | None) -> datetime:
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    return now_utc()


def clean_summary(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:400]


def fetch_feed(query: str) -> list[dict]:
    url = GNEWS.format(q=urllib.request.quote(query))
    try:
        raw = http_get(url)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"  ! feed failed ({query}): {exc}", file=sys.stderr)
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        print(f"  ! feed parse error ({query}): {exc}", file=sys.stderr)
        return []

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        # Google News titles look like "Headline - Source"; drop the suffix.
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        elif " - " in title:
            head, _, tail = title.rpartition(" - ")
            if head and len(tail) < 40:
                title, source = head.strip(), source or tail.strip()
        items.append(
            {
                "title": title,
                "link": link,
                "source": source,
                "published": parse_pubdate(item.findtext("pubDate")),
                "summary": clean_summary(item.findtext("description")),
            }
        )
    return items


def gather_items() -> list[dict]:
    seen: dict[str, dict] = {}
    for query in FEED_QUERIES:
        for it in fetch_feed(query):
            key = normalize(it["title"])[:80]
            if not key:
                continue
            prev = seen.get(key)
            if prev is None or it["published"] > prev["published"]:
                seen[key] = it
    items = list(seen.values())
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


# --------------------------------------------------------------------------- #
# Clustering / ranking
# --------------------------------------------------------------------------- #
def cluster_items(items: list[dict], threshold: float = 0.28) -> list[dict]:
    clusters: list[dict] = []
    for it in items:  # newest-first order preserves recency inside clusters
        kw = keywords(it["title"] + " " + it["summary"])
        best, best_sim = None, 0.0
        for cl in clusters:
            sim = jaccard(kw, cl["keywords"])
            if sim > best_sim:
                best, best_sim = cl, sim
        if best is not None and best_sim >= threshold:
            best["items"].append(it)
            best["keywords"] |= kw
        else:
            clusters.append({"items": [it], "keywords": set(kw)})

    for cl in clusters:
        head = cl["items"][0]
        cl["headline"] = head["title"]
        cl["severity"] = max(
            (severity_of(i["title"] + " " + i["summary"]) for i in cl["items"]),
            key=severity_rank,
        )
        newest = max(i["published"] for i in cl["items"])
        age_h = max((now_utc() - newest).total_seconds() / 3600, 0.0)
        recency = max(0.0, 24.0 - age_h) / 24.0  # 1.0 = brand new, 0 = ~24h old
        cl["score"] = (
            severity_rank(cl["severity"]) * 3.0
            + min(len(cl["items"]), 6) * 1.0
            + recency * 4.0
        )
    clusters.sort(key=lambda c: c["score"], reverse=True)
    return clusters


def cluster_id(cluster: dict) -> str:
    sig = " ".join(sorted(list(cluster["keywords"]))[:12])
    return hashlib.sha1(sig.encode()).hexdigest()[:12]


def cluster_sources(cluster: dict, limit: int = 8) -> list[dict]:
    out, seen = [], set()
    for it in cluster["items"]:
        key = normalize(it["title"])[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "title": it["title"],
                "url": it["link"],
                "source": it["source"] or "Google News",
                "published": it["published"].isoformat(),
            }
        )
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Groq (OpenAI-compatible)
# --------------------------------------------------------------------------- #
def groq_pick_model(api_key: str) -> str:
    env_model = os.environ.get("GROQ_MODEL")
    try:
        raw = http_get(
            f"{GROQ_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = {m.get("id") for m in json.loads(raw).get("data", [])}
    except Exception as exc:
        print(f"  ! model list failed, using default: {exc}", file=sys.stderr)
        return env_model or MODEL_PREFERENCE[0]
    if env_model and env_model in ids:
        return env_model
    for m in MODEL_PREFERENCE:
        if m in ids:
            return m
    # last resort: first non-embedding/whisper chat model
    for mid in sorted(ids):
        if mid and not any(x in mid for x in ("whisper", "embed", "tts", "guard")):
            return mid
    return env_model or MODEL_PREFERENCE[0]


def groq_analyze(api_key: str, cluster: dict, state: dict) -> dict | None:
    model = groq_pick_model(api_key)
    candidates = [
        {"headline": c["headline"], "severity": c["severity"],
         "snippets": [i["summary"] for i in c["items"][:3] if i["summary"]][:3]}
        for c in cluster["_top_clusters"]
    ]
    current = state.get("lead") or {}
    recent_timeline = [t["text"] for t in state.get("timeline", [])[:4]]

    system = (
        "You are the duty news editor for a Jaipur (Rajasthan, India) local-news "
        "website. Write clear, factual editorial copy in plain English. Use ONLY "
        "the information in the supplied feed items. Attribute anything unconfirmed "
        "('according to police', 'local reports say'). Never invent casualty "
        "figures, names, or facts that are not in the sources. Respond with strict "
        "JSON only."
    )
    user = {
        "task": "Select the single top breaking story for Jaipur today and cover it.",
        "candidate_stories": candidates,
        "currently_tracked_story": {
            "headline": current.get("headline", ""),
            "recent_updates": recent_timeline,
        },
        "output_schema": {
            "lead_headline": "string - concise headline for the top story",
            "event_type": "one of: terror, fire, earthquake, flood, accident, "
                          "crime, investigation, protest, civic, weather, other",
            "severity": "one of: critical, high, medium, low",
            "analysis": "2-4 short paragraphs of plain editorial synthesising the "
                        "feeds; separate paragraphs with \\n\\n",
            "is_same_story_as_current": "boolean - true if this is the same event "
                                        "as currently_tracked_story",
            "has_new_development": "boolean - true if there is a materially new "
                                   "development vs recent_updates",
            "update_text": "one concise sentence describing the new development, "
                           "else empty string",
        },
    }
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "temperature": 0.3,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        }
    ).encode()

    req = urllib.request.Request(
        f"{GROQ_BASE}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)
        print(f"  Groq model {model} responded ({len(content)} chars)")
        return data
    except urllib.error.HTTPError as exc:
        print(f"  ! Groq HTTP {exc.code}: {exc.read()[:300]!r}", file=sys.stderr)
    except Exception as exc:
        print(f"  ! Groq call failed: {exc}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------- #
# State + update decision
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def minutes_since(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return 1e9
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now_utc() - dt).total_seconds() / 60.0


def build() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-ai", action="store_true", help="skip Groq, render from feeds only")
    args = parser.parse_args()

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    use_ai = bool(api_key) and not args.no_ai
    if args.no_ai:
        print("Running in --no-ai mode (feeds only).")
    elif not api_key:
        print("GROQ_API_KEY not set — running in feeds-only fallback mode.")

    print("Fetching feeds...")
    items = gather_items()
    print(f"  {len(items)} unique items")
    now = now_utc()
    ist_day = to_ist(now).strftime("%Y-%m-%d")

    state = load_state()
    # Daily rollover: a new IST day starts a fresh story of the day.
    if state.get("ist_day") != ist_day:
        state = {"ist_day": ist_day, "timeline": [], "lead": None, "last_feed_hash": None}

    if not items:
        # Nothing to work with: keep existing state, refresh timestamp only.
        state["last_updated"] = now.isoformat()
        render(state, [], now)
        save_state(state)
        print("No feed items; page timestamp refreshed.")
        return

    clusters = cluster_items(items)
    top = clusters[0]
    top["_top_clusters"] = clusters[:3]
    feed_hash = hashlib.sha1(
        "|".join(normalize(i["title"]) for i in top["items"]).encode()
    ).hexdigest()[:16]

    prev_lead = state.get("lead") or {}
    prev_kw = keywords(prev_lead.get("headline", ""))
    same_by_kw = jaccard(prev_kw, top["keywords"]) >= 0.25 if prev_lead else False

    severity = top["severity"]
    headline = top["headline"]
    event_type = "other"
    analysis = None
    ai_new_dev = False
    ai_update_text = ""
    ai_same = same_by_kw

    if use_ai:
        print("Asking Groq for analysis...")
        ai = groq_analyze(api_key, top, state)
        if ai:
            headline = (ai.get("lead_headline") or headline).strip()
            severity = (ai.get("severity") or severity).strip().lower()
            if severity not in CADENCE_MINUTES:
                severity = top["severity"]
            event_type = (ai.get("event_type") or "other").strip().lower()
            analysis = (ai.get("analysis") or "").strip() or None
            ai_new_dev = bool(ai.get("has_new_development"))
            ai_update_text = (ai.get("update_text") or "").strip()
            ai_same = bool(ai.get("is_same_story_as_current")) if prev_lead else False

    is_same_story = ai_same if use_ai else same_by_kw
    cadence = CADENCE_MINUTES.get(severity, 120)
    feed_changed = feed_hash != state.get("last_feed_hash")

    timeline = state.get("timeline", []) if is_same_story else []

    # Decide whether to append a timeline entry.
    append_entry = False
    entry_text = ""
    if not is_same_story or not timeline:
        # New/first lead of the day -> seed the timeline.
        append_entry = True
        entry_text = ai_update_text if (use_ai and ai_update_text) else (
            f"Now tracking as Jaipur's top developing story: {headline}."
        )
    elif use_ai and ai_new_dev and ai_update_text:
        append_entry = True
        entry_text = ai_update_text
    elif feed_changed and severity_rank(severity) >= severity_rank("high") \
            and minutes_since(_last_entry_time(timeline)) >= cadence:
        # High-impact event, feed moved, cadence window elapsed: log a check-in.
        append_entry = True
        entry_text = (use_ai and ai_update_text) or f"New reporting on: {headline}."

    if append_entry and entry_text:
        timeline.insert(
            0,
            {
                "time_utc": now.isoformat(),
                "time_ist": to_ist(now).strftime("%I:%M %p"),
                "text": entry_text,
                "severity": severity,
            },
        )
        timeline = timeline[:40]

    if not use_ai and not analysis:
        analysis = _fallback_analysis(top, clusters)

    state.update(
        {
            "ist_day": ist_day,
            "last_updated": now.isoformat(),
            "last_feed_hash": feed_hash,
            "lead": {
                "id": cluster_id(top),
                "headline": headline,
                "event_type": event_type,
                "severity": severity,
                "analysis": analysis or _fallback_analysis(top, clusters),
                "sources": cluster_sources(top),
            },
            "timeline": timeline,
        }
    )

    render(state, clusters, now)
    save_state(state)
    print(f"Done. Lead: {headline!r} [{severity}] — {len(timeline)} update(s).")


def _last_entry_time(timeline: list[dict]) -> str | None:
    return timeline[0]["time_utc"] if timeline else None


def _fallback_analysis(top: dict, clusters: list[dict]) -> str:
    n = len(top["items"])
    srcs = sorted({i["source"] for i in top["items"] if i["source"]})[:4]
    src_line = (", ".join(srcs)) if srcs else "multiple local outlets"
    others = [c["headline"] for c in clusters[1:4]]
    para1 = (
        f"{top['headline']} is the most widely reported story across Jaipur feeds "
        f"right now, carried by {src_line} ({n} report(s) in the last day)."
    )
    para2 = "Also being reported today: " + "; ".join(others) + "." if others else ""
    return (para1 + "\n\n" + para2).strip()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
SEV_LABEL = {
    "critical": "CRITICAL", "high": "DEVELOPING", "medium": "UPDATING", "low": "MONITORING",
}
SEV_COLOR = {
    "critical": "#c62828", "high": "#e65100", "medium": "#0d5d92", "low": "#546e7a",
}


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def render(state: dict, clusters: list[dict], now: datetime) -> None:
    lead = state.get("lead")
    updated_ist = fmt_ist(now)
    timeline = state.get("timeline", [])

    if lead:
        sev = lead.get("severity", "low")
        badge = SEV_LABEL.get(sev, "MONITORING")
        color = SEV_COLOR.get(sev, "#546e7a")
        headline_html = esc(lead["headline"])
        paras = [p.strip() for p in (lead.get("analysis") or "").split("\n\n") if p.strip()]
        analysis_html = "\n".join(f"<p>{esc(p)}</p>" for p in paras) or "<p>Coverage is developing.</p>"

        source_items = "\n".join(
            f'<li><a href="{esc(s["url"])}" target="_blank" rel="noopener nofollow">'
            f'{esc(s["title"])}</a> <span class="src">— {esc(s["source"])}</span></li>'
            for s in lead.get("sources", [])
        ) or "<li>Sources being gathered.</li>"

        if timeline:
            timeline_html = "\n".join(
                f'<li class="tl-item sev-{esc(t.get("severity","low"))}">'
                f'<time>{esc(t["time_ist"])} IST</time>'
                f'<p>{esc(t["text"])}</p></li>'
                for t in timeline
            )
        else:
            timeline_html = '<li class="tl-item"><p>Live updates will appear here as the story develops.</p></li>'
        lead_id = esc(lead.get("id", ""))
    else:
        sev, badge, color = "low", "MONITORING", "#546e7a"
        headline_html = "No major breaking story in Jaipur right now"
        analysis_html = "<p>There is no single dominant breaking story in Jaipur at the moment. This page will update automatically as events develop through the day.</p>"
        source_items = "<li>Monitoring local feeds.</li>"
        timeline_html = '<li class="tl-item"><p>Live updates will appear here as news breaks.</p></li>'
        lead_id = ""

    # LiveBlogPosting JSON-LD for SEO on live coverage.
    ld_updates = [
        {
            "@type": "BlogPosting",
            "headline": t["text"][:110],
            "datePublished": t["time_utc"],
            "articleBody": t["text"],
        }
        for t in timeline[:20]
    ]
    ld = {
        "@context": "https://schema.org",
        "@type": "LiveBlogPosting",
        "headline": (lead["headline"] if lead else "Jaipur Breaking News"),
        "url": "https://www.manzill.com/breaking-news",
        "datePublished": (timeline[-1]["time_utc"] if timeline else now.isoformat()),
        "dateModified": now.isoformat(),
        "coverageStartTime": (timeline[-1]["time_utc"] if timeline else now.isoformat()),
        "about": {"@type": "Place", "name": "Jaipur, Rajasthan, India"},
        "publisher": {"@type": "Organization", "name": "Manzill",
                      "url": "https://www.manzill.com/"},
        "liveBlogUpdate": ld_updates,
    }
    ld_json = json.dumps(ld, ensure_ascii=False, indent=2)

    replacements = {
        "{{BADGE}}": esc(badge),
        "{{BADGE_COLOR}}": color,
        "{{HEADLINE}}": headline_html,
        "{{ANALYSIS}}": analysis_html,
        "{{SOURCES}}": source_items,
        "{{TIMELINE}}": timeline_html,
        "{{UPDATED_IST}}": esc(updated_ist),
        "{{LEAD_ID}}": lead_id,
        "{{LDJSON}}": ld_json,
    }
    page = PAGE_TEMPLATE
    for token, value in replacements.items():
        page = page.replace(token, value)

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(page)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="robots" content="index, follow">
    <meta name="theme-color" content="#0e3a5f">
    <!-- Page is regenerated on the server every ~20 min; refresh to pull the latest. -->
    <meta http-equiv="refresh" content="180">

    <title>Jaipur Breaking News &mdash; Live Updates | Manzill</title>
    <meta name="description" content="Live breaking news from Jaipur, Rajasthan. The top developing story of the day with a running timeline of updates, refreshed automatically through the day.">
    <meta name="keywords" content="Jaipur Breaking News, Jaipur Live News, Rajasthan Breaking News, Jaipur Today, Pink City News, Jaipur Latest">
    <meta name="author" content="Manzill Surolia">

    <link rel="canonical" href="https://www.manzill.com/breaking-news">
    <link rel="icon" href="/favicon.svg" type="image/svg+xml">
    <link rel="icon" type="image/png" sizes="512x512" href="/icon.png">
    <link rel="apple-touch-icon" href="/icon.png">

    <meta property="og:type" content="website">
    <meta property="og:site_name" content="Manzill">
    <meta property="og:title" content="Jaipur Breaking News &mdash; Live Updates">
    <meta property="og:description" content="The top developing story in Jaipur today, with a live timeline of updates.">
    <meta property="og:url" content="https://www.manzill.com/breaking-news">
    <meta property="og:image" content="https://www.manzill.com/manzill-og.png">
    <meta property="og:image:width" content="1200">
    <meta property="og:image:height" content="630">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="Jaipur Breaking News &mdash; Live Updates">
    <meta name="twitter:description" content="The top developing story in Jaipur today, with a live timeline of updates.">
    <meta name="twitter:image" content="https://www.manzill.com/manzill-og.png">

    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap">

    <style>
:root {
  --brand: #0e3a5f; --brand-dark: #082438; --link: #0d5d92; --accent: #12a5b8;
  --bg: #f6f8fa; --surface: #ffffff; --text: #0f1a24; --muted: #55636e;
  --border: #dde4ea; --shadow: 0 1px 3px rgba(0,0,0,.06), 0 4px 12px rgba(0,0,0,.04);
  --radius: 12px; --maxw: 820px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --link: #5cb3e0; --bg: #0d1117; --surface: #161b22; --text: #e6edf3;
    --muted: #8b98a5; --border: #26303b;
    --shadow: 0 1px 3px rgba(0,0,0,.4), 0 4px 12px rgba(0,0,0,.3);
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text); line-height: 1.6;
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }

header.site { background: var(--brand); color: #fff; border-bottom: 3px solid var(--accent); box-shadow: var(--shadow); }
.bar { max-width: var(--maxw); margin: 0 auto; padding: 12px 16px; display: flex; align-items: center; gap: 12px; }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 800; font-size: 1.25rem; color: #fff; }
.brand .logo { width: 32px; height: 32px; background: #fff; color: var(--brand); border-radius: 8px; display: grid; place-items: center; font-weight: 900; }
.brand a { color: #fff; }

main { max-width: var(--maxw); margin: 0 auto; padding: 20px 16px 64px; }
nav.crumb { font-size: .85rem; color: var(--muted); margin: 14px 0 4px; }
nav.crumb a { color: var(--link); }

.livebar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 14px 0 6px; }
.badge {
  display: inline-flex; align-items: center; gap: 7px; color: #fff; font-weight: 800;
  font-size: .72rem; letter-spacing: .06em; padding: 5px 11px; border-radius: 999px;
  background: {{BADGE_COLOR}};
}
.badge .dot { width: 8px; height: 8px; border-radius: 50%; background: #fff; animation: pulse 1.6s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
.updated { font-size: .82rem; color: var(--muted); }

h1.lead { font-size: 1.9rem; line-height: 1.2; font-weight: 800; margin: 6px 0 14px; }
@media (max-width: 560px) { h1.lead { font-size: 1.5rem; } }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px 18px; margin: 18px 0; }
.card h2 { font-size: 1.1rem; font-weight: 800; margin: 0 0 10px; padding-left: 11px; border-left: 4px solid var(--brand); }
.analysis p { margin: 0 0 12px; }
.analysis p:last-child { margin-bottom: 0; }

ul.timeline { list-style: none; margin: 0; padding: 0; }
.tl-item { position: relative; padding: 0 0 16px 20px; border-left: 2px solid var(--border); }
.tl-item:last-child { padding-bottom: 0; }
.tl-item::before { content: ""; position: absolute; left: -7px; top: 4px; width: 12px; height: 12px; border-radius: 50%; background: var(--accent); border: 2px solid var(--surface); }
.tl-item.sev-critical::before { background: #c62828; }
.tl-item.sev-high::before { background: #e65100; }
.tl-item time { display: block; font-size: .74rem; font-weight: 700; color: var(--muted); letter-spacing: .03em; }
.tl-item p { margin: 2px 0 0; }

ul.sources { list-style: none; margin: 0; padding: 0; }
ul.sources li { padding: 7px 0; border-bottom: 1px solid var(--border); font-size: .93rem; }
ul.sources li:last-child { border-bottom: 0; }
ul.sources .src { color: var(--muted); font-size: .82rem; }

.note { font-size: .8rem; color: var(--muted); margin-top: 6px; }
footer { max-width: var(--maxw); margin: 0 auto; padding: 0 16px 48px; font-size: .82rem; color: var(--muted); }
footer a { color: var(--link); }
</style>

    <script type="application/ld+json">
{{LDJSON}}
    </script>
</head>
<body>
    <header class="site">
      <div class="bar">
        <div class="brand"><span class="logo">M</span><a href="/">Manzill</a></div>
      </div>
    </header>

    <main>
      <nav class="crumb" aria-label="Breadcrumb">
        <a href="/">Home</a> &rsaquo; <a href="/jaipur-news">Jaipur News</a> &rsaquo; <span>Breaking</span>
      </nav>

      <div class="livebar">
        <span class="badge"><span class="dot"></span>{{BADGE}}</span>
        <span class="updated">Updated {{UPDATED_IST}}</span>
      </div>

      <h1 class="lead">{{HEADLINE}}</h1>

      <section class="card analysis">
        <h2>The story</h2>
        {{ANALYSIS}}
      </section>

      <section class="card">
        <h2>Live updates</h2>
        <ul class="timeline">
          {{TIMELINE}}
        </ul>
        <p class="note">This page refreshes automatically as the story develops. Times shown in IST.</p>
      </section>

      <section class="card">
        <h2>Sources</h2>
        <ul class="sources">
          {{SOURCES}}
        </ul>
      </section>
    </main>

    <footer>
      <p>Jaipur breaking-news coverage compiled from public news feeds. Reports may be preliminary &mdash; verify critical details with official sources.</p>
      <p><a href="/jaipur-news">&larr; All Jaipur news</a> &middot; <a href="/">manzill.com</a></p>
      <p>&copy; 2026 Manzill Surolia. All rights reserved.</p>
    </footer>
</body>
</html>
"""


if __name__ == "__main__":
    build()
