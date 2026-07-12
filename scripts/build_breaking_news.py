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
from email.utils import format_datetime, parsedate_to_datetime
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
RSS_PATH = ROOT / "breaking-news" / "rss.xml"
NEWS_SITEMAP_PATH = ROOT / "breaking-news" / "sitemap.xml"
STATE_PATH = ROOT / "breaking-news" / "data" / "state.json"

SITE = "https://www.manzill.com"
PAGE_URL = SITE + "/breaking-news"

# Bump whenever the rendered output (template/RSS/sitemap format) changes. A mismatch
# with the value stored in state forces a one-time re-render even when the feed is
# unchanged, so a redesign rolls out on the next scheduled run without a manual push.
RENDER_VERSION = "2"

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
        # Feeds unreachable/empty: keep the last good page untouched (no commit).
        # Only write once, to create the initial placeholder.
        if not OUT_HTML.exists():
            render(state, [], now)
            save_state({**state, "last_updated": now.isoformat(),
                        "render_version": RENDER_VERSION})
            print("No feed items; wrote initial placeholder.")
        else:
            print("No feed items; keeping existing page (no commit).")
        return

    clusters = cluster_items(items)
    top = clusters[0]
    top["_top_clusters"] = clusters[:3]
    feed_hash = hashlib.sha1(
        "|".join(normalize(i["title"]) for i in top["items"]).encode()
    ).hexdigest()[:16]

    # If the top story's feed is unchanged AND the page was already rendered by this
    # output version, there is nothing new to say: skip the whole update (no Groq call,
    # no re-render) so quiet periods produce no commit. A RENDER_VERSION mismatch (a
    # redesign) forces a one-time re-render even when the feed is unchanged.
    feed_changed = feed_hash != state.get("last_feed_hash")
    up_to_date = state.get("render_version") == RENDER_VERSION
    if OUT_HTML.exists() and state.get("lead") and not feed_changed and up_to_date:
        print("No change in top-story feed; skipping update (no commit).")
        return

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
            "render_version": RENDER_VERSION,
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
    "critical": "#b71c1c", "high": "#e65100", "medium": "#b26a00", "low": "#6b6b6b",
}


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _src_time(s: dict) -> str:
    try:
        dt = datetime.fromisoformat(s["published"])
    except Exception:
        return s.get("source", "")
    return to_ist(dt).strftime("%d %b, %I:%M %p")


def render(state: dict, clusters: list[dict], now: datetime) -> None:
    lead = state.get("lead")
    updated_ist = fmt_ist(now)
    today_ist = to_ist(now).strftime("%A, %d %B %Y")
    timeline = state.get("timeline", [])

    if lead:
        sev = lead.get("severity", "low")
        badge = SEV_LABEL.get(sev, "MONITORING")
        color = SEV_COLOR.get(sev, "#6b6b6b")
        headline_html = esc(lead["headline"])
        paras = [p.strip() for p in (lead.get("analysis") or "").split("\n\n") if p.strip()]
        analysis_html = "\n        ".join(
            f"<p>{esc(p)}</p>" for p in paras
        ) or "<p>Coverage is developing.</p>"

        source_items = "\n".join(
            f'<div class="card-text fade-in">'
            f'<a href="{esc(s["url"])}" target="_blank" rel="noopener nofollow">'
            f'<h3>{esc(s["title"])}</h3>'
            f'<div class="info"><span class="src">{esc(s["source"])}</span>'
            f'<span class="dot"></span><span>{esc(_src_time(s))}</span></div>'
            f'</a></div>'
            for s in lead.get("sources", [])
        ) or '<div class="empty-note">Sources are being gathered.</div>'

        if timeline:
            timeline_html = "\n".join(
                f'<li class="tl-item sev-{esc(t.get("severity","low"))}">'
                f'<time>{esc(t["time_ist"])} IST</time>'
                f'<p>{esc(t["text"])}</p></li>'
                for t in timeline
            )
        else:
            timeline_html = '<li class="tl-item"><p>Live updates will appear here as the story develops.</p></li>'
    else:
        sev, badge, color = "low", "MONITORING", "#6b6b6b"
        headline_html = "No major breaking story in Jaipur right now"
        analysis_html = "<p>There is no single dominant breaking story in Jaipur at the moment. This page updates automatically as events develop through the day.</p>"
        source_items = '<div class="empty-note">Monitoring local Jaipur feeds &mdash; sources will appear here when a story breaks.</div>'
        timeline_html = '<li class="tl-item"><p>Live updates will appear here as news breaks.</p></li>'

    update_count = f"{len(timeline)} update" + ("s" if len(timeline) != 1 else "")

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
        "url": PAGE_URL,
        "inLanguage": "en-IN",
        "datePublished": (timeline[-1]["time_utc"] if timeline else now.isoformat()),
        "dateModified": now.isoformat(),
        "coverageStartTime": (timeline[-1]["time_utc"] if timeline else now.isoformat()),
        "about": {"@type": "Place", "name": "Jaipur, Rajasthan, India"},
        "publisher": {"@type": "Organization", "name": "Manzill", "url": SITE + "/"},
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
        "{{TODAY}}": esc(today_ist),
        "{{UPDATE_COUNT}}": esc(update_count),
        "{{LDJSON}}": ld_json,
    }
    page = PAGE_TEMPLATE
    for token, value in replacements.items():
        page = page.replace(token, value)

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(page)
    render_rss(state, now)
    render_news_sitemap(now)


def _xml_esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _rss_item(title: str, desc: str, guid: str, pub: datetime) -> str:
    return (
        "    <item>\n"
        f"      <title>{_xml_esc(title)}</title>\n"
        f"      <link>{PAGE_URL}</link>\n"
        f'      <guid isPermaLink="false">{_xml_esc(guid)}</guid>\n'
        f"      <pubDate>{format_datetime(pub)}</pubDate>\n"
        f"      <description>{_xml_esc(desc)}</description>\n"
        "    </item>"
    )


def render_rss(state: dict, now: datetime) -> None:
    """Emit breaking-news/rss.xml: one item per timeline development."""
    lead = state.get("lead")
    timeline = state.get("timeline", [])
    items = []
    if lead:
        for t in timeline:
            try:
                dt = datetime.fromisoformat(t["time_utc"])
            except Exception:
                dt = now
            items.append(_rss_item(t["text"], t["text"],
                                   f'{lead.get("id", "bn")}-{t["time_utc"]}', dt))
        if not timeline:
            analysis = (lead.get("analysis") or "").replace("\n\n", " ")
            items.append(_rss_item(lead["headline"], analysis,
                                   f'{lead.get("id", "bn")}-{now.isoformat()}', now))
    if not items:
        items.append(_rss_item(
            "Monitoring Jaipur for breaking news",
            "No major breaking story in Jaipur right now. This feed updates as events develop.",
            f"bn-idle-{to_ist(now).strftime('%Y%m%d')}", now))

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>Manzill &#8212; Jaipur Breaking News</title>\n"
        f"    <link>{PAGE_URL}</link>\n"
        f'    <atom:link href="{PAGE_URL}/rss.xml" rel="self" type="application/rss+xml"/>\n'
        "    <description>Live breaking news from Jaipur, Rajasthan &#8212; the top "
        "developing story of the day with running updates.</description>\n"
        "    <language>en-IN</language>\n"
        f"    <lastBuildDate>{format_datetime(now)}</lastBuildDate>\n"
        "    <ttl>20</ttl>\n"
        + "\n".join(items) + "\n"
        "  </channel>\n"
        "</rss>\n"
    )
    RSS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RSS_PATH.write_text(feed)


def render_news_sitemap(now: datetime) -> None:
    """Emit breaking-news/sitemap.xml with a fresh lastmod each run."""
    lastmod = to_ist(now).strftime("%Y-%m-%dT%H:%M:%S+05:30")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        f"    <loc>{PAGE_URL}</loc>\n"
        f"    <lastmod>{lastmod}</lastmod>\n"
        "    <changefreq>hourly</changefreq>\n"
        "    <priority>0.9</priority>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    NEWS_SITEMAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    NEWS_SITEMAP_PATH.write_text(xml)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="robots" content="index, follow">
    <meta name="theme-color" content="#b71c1c">
    <!-- Page is regenerated on the server every ~20 min; refresh to pull the latest. -->
    <meta http-equiv="refresh" content="180">

    <title>Jaipur Breaking News &mdash; Live Updates | Manzill</title>
    <meta name="description" content="Live breaking news from Jaipur, Rajasthan. The top developing story of the day with a running timeline of updates, refreshed automatically through the day.">
    <meta name="keywords" content="Jaipur Breaking News, Jaipur Live News, Rajasthan Breaking News, Jaipur Today, Pink City News, Jaipur Latest">
    <meta name="news_keywords" content="Jaipur, Jaipur news, Rajasthan, breaking news, Jaipur breaking news, Pink City, Jaipur today">
    <meta name="author" content="Manzill Surolia">

    <link rel="canonical" href="https://www.manzill.com/breaking-news">
    <link rel="icon" href="/breaking-news/favicon.svg" type="image/svg+xml">
    <link rel="apple-touch-icon" href="/breaking-news/favicon.svg">
    <link rel="alternate" type="application/rss+xml" title="Jaipur Breaking News &mdash; Manzill" href="/breaking-news/rss.xml">

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
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+Devanagari:wght@400;600;700;800;900&display=swap">

    <style>
:root {
  --brand: #b71c1c;
  --brand-dark: #7f0000;
  --accent: #f5b400;
  --bg: #fafafa;
  --surface: #ffffff;
  --text: #1a1a1a;
  --muted: #6b6b6b;
  --border: #e5e5e5;
  --shadow: 0 1px 3px rgba(0,0,0,.06), 0 4px 12px rgba(0,0,0,.04);
  --radius: 12px;
  --maxw: 1100px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f0f10;
    --surface: #18181b;
    --text: #f1f1f1;
    --muted: #a1a1aa;
    --border: #2a2a2e;
    --shadow: 0 1px 3px rgba(0,0,0,.4), 0 4px 12px rgba(0,0,0,.3);
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  font-family: 'Noto Sans Devanagari', system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
a { color: inherit; text-decoration: none; }
img { display: block; max-width: 100%; height: auto; }

header.site {
  background: var(--brand);
  color: #fff;
  border-bottom: 3px solid var(--accent);
  box-shadow: var(--shadow);
}
.bar {
  max-width: var(--maxw);
  margin: 0 auto;
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 800;
  font-size: 1.4rem;
}
.brand .logo {
  width: 34px; height: 34px;
  background: #fff;
  color: var(--brand);
  border-radius: 8px;
  display: grid;
  place-items: center;
  font-weight: 900;
  font-size: 1.15rem;
}
.date-strip {
  margin-left: auto;
  font-size: .82rem;
  opacity: .92;
  display: flex;
  gap: 10px;
  align-items: center;
}
.date-strip .archive-link {
  background: rgba(255,255,255,.15);
  border: 1px solid rgba(255,255,255,.28);
  color: #fff;
  padding: 6px 12px;
  border-radius: 999px;
}
.date-strip .archive-link:hover { background: rgba(255,255,255,.25); }
.date-strip .live-pill {
  display: inline-flex; align-items: center; gap: 6px;
  background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.30);
  padding: 5px 11px; border-radius: 999px; font-weight: 800;
  letter-spacing: .05em; font-size: .72rem;
}
.date-strip .live-pill .dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--accent);
  animation: pulse 1.4s infinite;
}
.refresh-btn {
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--brand);
  padding: 4px 12px;
  border-radius: 999px;
  font-size: .76rem;
  cursor: pointer;
  font-family: inherit;
  font-weight: 700;
}
.refresh-btn:hover { background: rgba(183,28,28,.08); border-color: var(--brand); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }

main {
  max-width: var(--maxw);
  margin: 0 auto;
  padding: 20px 16px 60px;
}

.livebar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 6px 0 8px; }
.sev-badge {
  display: inline-flex; align-items: center; gap: 7px; color: #fff; font-weight: 800;
  font-size: .72rem; letter-spacing: .06em; padding: 5px 12px; border-radius: 999px;
  background: {{BADGE_COLOR}};
}
.sev-badge .dot { width: 8px; height: 8px; border-radius: 50%; background: #fff; animation: pulse 1.6s infinite; }
.updated { font-size: .82rem; color: var(--muted); }

.hero { margin: 8px 0 4px; }
.hero h1 { font-size: 2rem; line-height: 1.18; margin: 6px 0 16px; font-weight: 800; }
@media (max-width: 560px) {
  .hero h1 { font-size: 1.55rem; }
  .date-strip > span:not(.live-pill) { display: none; }
}

section.feed { margin-bottom: 32px; }
.section-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin: 8px 0 14px;
}
.section-head h2 {
  font-size: 1.25rem;
  margin: 0;
  padding-left: 12px;
  border-left: 4px solid var(--brand);
  font-weight: 800;
}
.section-head .count { color: var(--muted); font-size: .82rem; }

.editorial-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: var(--radius);
  padding: 18px 20px;
  margin-bottom: 26px;
  box-shadow: var(--shadow);
}
.editorial-card h2 {
  margin: 0 0 10px;
  font-size: 1.1rem;
  font-weight: 800;
  color: var(--brand);
}
.editorial-card p { margin: 0 0 12px; font-size: .98rem; line-height: 1.7; }
.editorial-card p:last-of-type { margin-bottom: 0; }
.editorial-card .byline {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  font-size: .82rem;
  color: var(--muted);
}

ul.timeline { list-style: none; margin: 0; padding: 0; max-width: 760px; }
.tl-item { position: relative; padding: 0 0 18px 22px; border-left: 2px solid var(--border); }
.tl-item:last-child { padding-bottom: 0; }
.tl-item::before {
  content: ""; position: absolute; left: -7px; top: 3px;
  width: 12px; height: 12px; border-radius: 50%;
  background: var(--accent); border: 2px solid var(--surface);
}
.tl-item.sev-critical::before { background: var(--brand); }
.tl-item.sev-high::before { background: #e65100; }
.tl-item time { display: block; font-size: .74rem; font-weight: 800; color: var(--brand); letter-spacing: .03em; }
.tl-item p { margin: 3px 0 0; }
.note { font-size: .8rem; color: var(--muted); margin-top: 14px; }

.grid-text {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}
.card-text {
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 4px solid var(--brand);
  border-radius: 10px;
  padding: 14px 16px;
  box-shadow: var(--shadow);
  transition: transform .15s ease, box-shadow .15s ease;
}
.card-text:hover { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(0,0,0,.10); }
.card-text a { display: block; }
.card-text h3 { font-size: 1rem; margin: 0 0 6px; line-height: 1.45; font-weight: 700; }
.card-text .info {
  display: flex; gap: 8px; align-items: center;
  font-size: .76rem; color: var(--muted); flex-wrap: wrap;
}
.card-text .info .src { font-weight: 700; color: var(--brand); }
.card-text .info .dot { width: 3px; height: 3px; background: currentColor; border-radius: 50%; opacity: .5; }

.empty-note {
  padding: 16px;
  background: var(--surface);
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--muted);
  font-size: .9rem;
  grid-column: 1/-1;
}

.fade-in { animation: fade .35s ease-out both; }
@keyframes fade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}

footer { max-width: var(--maxw); margin: 0 auto; padding: 0 16px 48px; font-size: .82rem; color: var(--muted); }
footer a { color: var(--brand); font-weight: 600; }
</style>

    <script type="application/ld+json">
{{LDJSON}}
    </script>
</head>
<body>
    <header class="site">
      <div class="bar">
        <div class="brand"><span class="logo">M</span>Manzill</div>
        <div class="date-strip">
          <span class="live-pill"><span class="dot"></span>LIVE</span>
          <span>{{TODAY}}</span>
          <a class="archive-link" href="https://news.manzill.com">All news</a>
        </div>
      </div>
    </header>

    <main>
      <div class="livebar">
        <span class="sev-badge"><span class="dot"></span>{{BADGE}}</span>
        <span class="updated">Updated {{UPDATED_IST}}</span>
        <button class="refresh-btn" type="button" onclick="location.reload()" aria-label="Refresh">&#8635; Refresh</button>
      </div>

      <section class="hero">
        <h1>{{HEADLINE}}</h1>
      </section>

      <div class="editorial-card fade-in">
        <h2>The story</h2>
        {{ANALYSIS}}
        <div class="byline">Compiled from public Jaipur news feeds &middot; times shown in IST.</div>
      </div>

      <section class="feed">
        <div class="section-head">
          <h2>Live updates</h2>
          <span class="count">{{UPDATE_COUNT}}</span>
        </div>
        <ul class="timeline">
          {{TIMELINE}}
        </ul>
        <p class="note">This page refreshes automatically as the story develops.</p>
      </section>

      <section class="feed">
        <div class="section-head">
          <h2>Sources</h2>
        </div>
        <div class="grid-text">
          {{SOURCES}}
        </div>
      </section>
    </main>

    <footer>
      <p>Jaipur breaking-news coverage compiled from public news feeds. Reports may be preliminary &mdash; verify critical details with official sources.</p>
      <p><a href="/breaking-news/rss.xml">RSS feed</a> &middot; <a href="https://news.manzill.com">news.manzill.com</a> &middot; <a href="/">manzill.com</a></p>
      <p>&copy; 2026 Manzill Surolia. All rights reserved.</p>
    </footer>
</body>
</html>
"""


if __name__ == "__main__":
    build()
