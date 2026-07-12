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
OUT_HTML = ROOT / "breaking" / "index.html"
RSS_PATH = ROOT / "breaking" / "rss.xml"
NEWS_SITEMAP_PATH = ROOT / "breaking" / "sitemap.xml"
STATE_PATH = ROOT / "breaking" / "data" / "state.json"
# The page moved from /breaking-news to /breaking; keep a redirect at the old path.
REDIRECT_PATH = ROOT / "breaking-news" / "index.html"

SITE = "https://www.manzill.com"
PAGE_URL = SITE + "/breaking"
NEWS_SITE = "https://news.manzill.com"

# Bump whenever the rendered output (template/RSS/sitemap format) changes. A mismatch
# with the value stored in state forces a one-time re-render even when the feed is
# unchanged, so a redesign rolls out on the next scheduled run without a manual push.
RENDER_VERSION = "3"

# strftime has no Hindi locale, so map weekday/month names for the date strip.
HINDI_WEEKDAYS = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
HINDI_MONTHS = [
    "जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
    "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर",
]

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
        "website that publishes in HINDI. Use ONLY the information in the supplied "
        "feed items (their titles/snippets may be in English — translate the facts "
        "into natural Hindi). Attribute anything unconfirmed ('पुलिस के अनुसार', "
        "'स्थानीय रिपोर्टों के मुताबिक'). Never invent casualty figures, names, or "
        "facts that are not in the sources. The 'lead_headline', 'analysis' and "
        "'update_text' fields MUST be written in Hindi (Devanagari); the "
        "'event_type' and 'severity' fields stay in the English enums below. "
        "Respond with strict JSON only."
    )
    user = {
        "task": "Select the single top breaking story for Jaipur today and cover it in Hindi.",
        "candidate_stories": candidates,
        "currently_tracked_story": {
            "headline": current.get("headline", ""),
            "recent_updates": recent_timeline,
        },
        "output_schema": {
            "lead_headline": "string (HINDI) - concise Hindi headline for the top story",
            "event_type": "one of: terror, fire, earthquake, flood, accident, "
                          "crime, investigation, protest, civic, weather, other",
            "severity": "one of: critical, high, medium, low",
            "analysis": "2-4 short paragraphs of plain Hindi editorial synthesising "
                        "the feeds; separate paragraphs with \\n\\n",
            "is_same_story_as_current": "boolean - true if this is the same event "
                                        "as currently_tracked_story",
            "has_new_development": "boolean - true if there is a materially new "
                                   "development vs recent_updates",
            "update_text": "one concise Hindi sentence describing the new "
                           "development, else empty string",
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

    # Always keep the old /breaking-news URL redirecting to /breaking (idempotent).
    write_redirect_stub()

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
            f"जयपुर की प्रमुख ताज़ा खबर के रूप में कवरेज शुरू: {headline}"
        )
    elif use_ai and ai_new_dev and ai_update_text:
        append_entry = True
        entry_text = ai_update_text
    elif feed_changed and severity_rank(severity) >= severity_rank("high") \
            and minutes_since(_last_entry_time(timeline)) >= cadence:
        # High-impact event, feed moved, cadence window elapsed: log a check-in.
        append_entry = True
        entry_text = (use_ai and ai_update_text) or f"इस खबर पर नई रिपोर्टिंग: {headline}"

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
    """Hindi summary used when Groq is unavailable — sticks to what the feeds report."""
    n = len(top["items"])
    srcs = sorted({i["source"] for i in top["items"] if i["source"]})[:4]
    src_line = (", ".join(srcs)) if srcs else "कई स्थानीय स्रोत"
    others = [c["headline"] for c in clusters[1:4]]
    para1 = (
        f"{top['headline']} — इस समय जयपुर की खबरों में सबसे अधिक रिपोर्ट की जा रही खबर है, "
        f"जिसे {src_line} ने कवर किया है (पिछले 24 घंटे में {n} रिपोर्ट)।"
    )
    para2 = "आज इनकी भी रिपोर्टिंग हो रही है: " + "; ".join(others) + "।" if others else ""
    return (para1 + "\n\n" + para2).strip()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
SEV_LABEL = {
    "critical": "गंभीर", "high": "विकसित हो रही", "medium": "अपडेट हो रही", "low": "निगरानी में",
}
SEV_COLOR = {
    "critical": "#b71c1c", "high": "#e65100", "medium": "#b26a00", "low": "#6b6b6b",
}


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _hindi_datetime(dt: datetime) -> str:
    d = to_ist(dt)
    return f"{d.day} {HINDI_MONTHS[d.month - 1]} {d.year}, {d.strftime('%I:%M %p')} IST"


def _hindi_date(dt: datetime) -> str:
    d = to_ist(dt)
    return f"{HINDI_WEEKDAYS[d.weekday()]}, {d.day} {HINDI_MONTHS[d.month - 1]} {d.year}"


def _src_time(s: dict) -> str:
    try:
        dt = datetime.fromisoformat(s["published"])
    except Exception:
        return s.get("source", "")
    d = to_ist(dt)
    return f"{d.day} {HINDI_MONTHS[d.month - 1]}, {d.strftime('%I:%M %p')}"


BRAND_SUFFIX = "ब्रेकिंग जयपुर न्यूज़"


def render(state: dict, clusters: list[dict], now: datetime) -> None:
    lead = state.get("lead")
    updated_ist = _hindi_datetime(now)
    today_ist = _hindi_date(now)
    timeline = state.get("timeline", [])

    if lead:
        sev = lead.get("severity", "low")
        badge = SEV_LABEL.get(sev, "निगरानी में")
        color = SEV_COLOR.get(sev, "#6b6b6b")
        headline_html = esc(lead["headline"])
        title = f"{lead['headline']} | {BRAND_SUFFIX}"
        paras = [p.strip() for p in (lead.get("analysis") or "").split("\n\n") if p.strip()]
        analysis_html = "\n        ".join(
            f"<p>{esc(p)}</p>" for p in paras
        ) or "<p>खबर विकसित हो रही है।</p>"

        source_items = "\n".join(
            f'<div class="card-text fade-in">'
            f'<a href="{esc(s["url"])}" target="_blank" rel="noopener nofollow">'
            f'<h3>{esc(s["title"])}</h3>'
            f'<div class="info"><span class="src">{esc(s["source"])}</span>'
            f'<span class="dot"></span><span>{esc(_src_time(s))}</span></div>'
            f'</a></div>'
            for s in lead.get("sources", [])
        ) or '<div class="empty-note">स्रोत जुटाए जा रहे हैं।</div>'

        if timeline:
            timeline_html = "\n".join(
                f'<li class="tl-item sev-{esc(t.get("severity","low"))}">'
                f'<time>{esc(t["time_ist"])} IST</time>'
                f'<p>{esc(t["text"])}</p></li>'
                for t in timeline
            )
        else:
            timeline_html = '<li class="tl-item"><p>खबर के विकसित होते ही लाइव अपडेट यहाँ दिखेंगे।</p></li>'
    else:
        sev, badge, color = "low", "निगरानी में", "#6b6b6b"
        headline_html = "अभी जयपुर में कोई बड़ी ब्रेकिंग खबर नहीं"
        title = f"{BRAND_SUFFIX} — लाइव अपडेट | जयपुर न्यूज़"
        analysis_html = "<p>इस समय जयपुर में कोई एक प्रमुख ब्रेकिंग खबर नहीं है। दिनभर घटनाओं के विकसित होने पर यह पेज अपने-आप अपडेट होता रहता है।</p>"
        source_items = '<div class="empty-note">स्थानीय जयपुर फ़ीड की निगरानी जारी है &mdash; खबर आते ही स्रोत यहाँ दिखेंगे।</div>'
        timeline_html = '<li class="tl-item"><p>खबर आते ही लाइव अपडेट यहाँ दिखेंगे।</p></li>'

    update_count = f"{len(timeline)} अपडेट"

    # JSON-LD: the site publisher (NewsMediaOrganization) + this live coverage
    # (LiveBlogPosting), bundled in one @graph. Both in Hindi (hi-IN).
    news_org = {
        "@type": "NewsMediaOrganization",
        "name": "जयपुर न्यूज़ | Jaipur News",
        "url": NEWS_SITE + "/",
        "inLanguage": "hi-IN",
        "logo": {"@type": "ImageObject", "url": NEWS_SITE + "/icon-512.png",
                 "width": 512, "height": 512},
        "image": NEWS_SITE + "/og-image.png",
        "description": "जयपुर, राजस्थान, भारत और दुनिया की ताज़ा खबरें हिंदी में | Jaipur News",
    }
    liveblog = {
        "@type": "LiveBlogPosting",
        "headline": (lead["headline"] if lead else BRAND_SUFFIX),
        "url": PAGE_URL,
        "inLanguage": "hi-IN",
        "datePublished": (timeline[-1]["time_utc"] if timeline else now.isoformat()),
        "dateModified": now.isoformat(),
        "coverageStartTime": (timeline[-1]["time_utc"] if timeline else now.isoformat()),
        "about": {"@type": "Place", "name": "जयपुर, राजस्थान, भारत"},
        "publisher": {"@type": "NewsMediaOrganization",
                      "name": "जयपुर न्यूज़ | Jaipur News", "url": NEWS_SITE + "/"},
        "liveBlogUpdate": [
            {"@type": "BlogPosting", "headline": t["text"][:110],
             "datePublished": t["time_utc"], "articleBody": t["text"]}
            for t in timeline[:20]
        ],
    }
    ld = {"@context": "https://schema.org", "@graph": [news_org, liveblog]}
    ld_json = json.dumps(ld, ensure_ascii=False, indent=2)

    replacements = {
        "{{TITLE}}": esc(title),
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
        "    <title>जयपुर न्यूज़ &#8212; ब्रेकिंग</title>\n"
        f"    <link>{PAGE_URL}</link>\n"
        f'    <atom:link href="{PAGE_URL}/rss.xml" rel="self" type="application/rss+xml"/>\n'
        "    <description>जयपुर, राजस्थान की ताज़ा ब्रेकिंग खबरें हिंदी में &#8212; दिन की "
        "प्रमुख खबर और लगातार अपडेट।</description>\n"
        "    <language>hi-IN</language>\n"
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


def write_redirect_stub() -> None:
    """Keep the old /breaking-news URL alive by redirecting it to /breaking.
    Idempotent: identical content each run, so it only ever commits once."""
    html_doc = (
        "<!DOCTYPE html>\n"
        '<html lang="hi">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="robots" content="noindex, follow">\n'
        f'<link rel="canonical" href="{PAGE_URL}">\n'
        f'<meta http-equiv="refresh" content="0; url={PAGE_URL}">\n'
        "<title>ब्रेकिंग जयपुर न्यूज़</title>\n"
        f'<script>location.replace("{PAGE_URL}");</script>\n'
        "</head>\n<body>\n"
        f'<p>यह पेज अब <a href="{PAGE_URL}">{PAGE_URL}</a> पर चला गया है।</p>\n'
        "</body>\n</html>\n"
    )
    REDIRECT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REDIRECT_PATH.write_text(html_doc)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="robots" content="index, follow">
    <meta name="theme-color" content="#b71c1c">
    <!-- Page is regenerated on the server every ~20 min; refresh to pull the latest. -->
    <meta http-equiv="refresh" content="180">

    <title>{{TITLE}}</title>
    <meta name="description" content="जयपुर, राजस्थान की ताज़ा ब्रेकिंग खबरें हिंदी में। दिन की प्रमुख खबर और लगातार लाइव अपडेट।">
    <meta name="keywords" content="जयपुर न्यूज़, ब्रेकिंग न्यूज़, जयपुर ब्रेकिंग न्यूज़, राजस्थान न्यूज़, जयपुर आज, हिंदी न्यूज़">
    <meta name="news_keywords" content="जयपुर, जयपुर न्यूज़, राजस्थान, ब्रेकिंग न्यूज़, जयपुर ब्रेकिंग न्यूज़, हिंदी न्यूज़">
    <meta name="author" content="जयपुर न्यूज़">

    <link rel="canonical" href="https://www.manzill.com/breaking">
    <link rel="icon" href="/breaking/favicon.svg" type="image/svg+xml">
    <link rel="apple-touch-icon" href="/breaking/favicon.svg">
    <link rel="alternate" type="application/rss+xml" title="जयपुर न्यूज़ — ब्रेकिंग" href="/breaking/rss.xml">

    <meta property="og:type" content="website">
    <meta property="og:site_name" content="जयपुर न्यूज़">
    <meta property="og:locale" content="hi_IN">
    <meta property="og:title" content="{{TITLE}}">
    <meta property="og:description" content="जयपुर की ताज़ा ब्रेकिंग खबरें हिंदी में — दिन की प्रमुख खबर और लाइव अपडेट।">
    <meta property="og:url" content="https://www.manzill.com/breaking">
    <meta property="og:image" content="https://news.manzill.com/og-image.png">
    <meta property="og:image:width" content="1200">
    <meta property="og:image:height" content="630">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="{{TITLE}}">
    <meta name="twitter:description" content="जयपुर की ताज़ा ब्रेकिंग खबरें हिंदी में — लाइव अपडेट।">
    <meta name="twitter:image" content="https://news.manzill.com/og-image.png">

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
.refresh-btn {
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--brand);
  padding: 3px 10px;
  border-radius: 999px;
  font-size: .76rem;
  cursor: pointer;
  font-family: inherit;
}
.refresh-btn:hover { background: rgba(183,28,28,.08); border-color: var(--brand); }
.refresh-btn[aria-busy="true"] { opacity: .6; pointer-events: none; }
#status { align-items: center; }

.hero { margin: 8px 0 4px; }
.hero h1 { font-size: 1.5rem; margin: 0; font-weight: 800; }

main {
  max-width: var(--maxw);
  margin: 0 auto;
  padding: 20px 16px 60px;
}

section.feed { margin-bottom: 36px; }
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
.section-head .count {
  color: var(--muted);
  font-size: .82rem;
}

.tabs {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: thin;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 6px;
  margin: 4px 0 22px;
  position: sticky;
  top: 8px;
  z-index: 10;
  box-shadow: var(--shadow);
}
.tabs::-webkit-scrollbar { height: 4px; }
.tab {
  flex: 0 0 auto;
  background: transparent;
  border: 0;
  color: var(--text);
  text-decoration: none;
  padding: 8px 14px;
  border-radius: 999px;
  font-family: inherit;
  font-size: .92rem;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  transition: background .15s ease, color .15s ease;
}
.tab:hover { background: rgba(183,28,28,.08); }
.tab[aria-selected="true"] {
  background: var(--brand);
  color: #fff;
}
.tab .badge {
  background: rgba(0,0,0,.08);
  font-size: .72rem;
  padding: 1px 7px;
  border-radius: 999px;
  font-weight: 700;
}
.tab[aria-selected="true"] .badge { background: rgba(255,255,255,.22); }
.tab--cta { color: var(--brand); font-weight: 700; }
.tab--cta:hover { background: rgba(183,28,28,.12); }
.tab-ext { font-size: .78em; opacity: .85; }
@media (prefers-color-scheme: dark) {
  .tab:hover { background: rgba(255,255,255,.06); }
  .tab .badge { background: rgba(255,255,255,.10); }
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 18px;
}
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
.card-text h3 {
  font-size: 1rem;
  margin: 0 0 6px;
  line-height: 1.45;
  font-weight: 700;
}
.card-text .desc {
  font-size: .88rem;
  color: var(--muted);
  margin: 0 0 8px;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.card-text .info {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: .76rem;
  color: var(--muted);
  flex-wrap: wrap;
}
.card-text .info .src { font-weight: 700; color: var(--brand); }
.card-text .info .dot { width: 3px; height: 3px; background: currentColor; border-radius: 50%; opacity: .5; }

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--shadow);
  display: flex;
  flex-direction: column;
  transition: transform .15s ease, box-shadow .15s ease;
}
.card:hover { transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0,0,0,.10); }
.card .thumb {
  aspect-ratio: 16/9;
  background: linear-gradient(135deg, #2a2a2a, #555);
  overflow: hidden;
}
.card .thumb img {
  width: 100%; height: 100%;
  object-fit: cover;
}
.card .body {
  padding: 14px 16px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex: 1;
}
.card h3 {
  font-size: 1.04rem;
  margin: 0;
  line-height: 1.4;
  font-weight: 700;
}
.card .desc {
  font-size: .9rem;
  color: var(--muted);
  margin: 0;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.card .info {
  margin-top: auto;
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: .78rem;
  color: var(--muted);
  flex-wrap: wrap;
}
.card .info .src { font-weight: 700; color: var(--brand); }
.card .info .dot { width: 3px; height: 3px; background: currentColor; border-radius: 50%; opacity: .5; }

.skeleton .card {
  background: var(--surface);
  pointer-events: none;
}
.skeleton .thumb {
  background: linear-gradient(90deg, var(--border) 25%, rgba(0,0,0,.04) 50%, var(--border) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.2s infinite;
}
.skeleton .body > * {
  background: var(--border);
  border-radius: 4px;
  height: 12px;
  animation: shimmer 1.2s infinite;
  background-size: 200% 100%;
}
.skeleton .body h3 { height: 18px; width: 90%; }
.skeleton .body .desc { height: 12px; width: 75%; }
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

.empty-note {
  padding: 16px;
  background: var(--surface);
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--muted);
  font-size: .9rem;
  grid-column: 1/-1;
}

.editorial-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: var(--radius);
  padding: 18px 20px;
  margin-bottom: 18px;
  box-shadow: var(--shadow);
}
.editorial-card h2 {
  margin: 0 0 10px;
  font-size: 1.1rem;
  font-weight: 800;
  color: var(--brand);
}
.editorial-card p {
  margin: 0 0 12px;
  font-size: .95rem;
  line-height: 1.7;
}
.editorial-card .byline {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  font-size: .85rem;
  color: var(--muted);
}
.editorial-card .byline strong { color: var(--text); }

.error-box {
  padding: 24px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  text-align: center;
  color: var(--muted);
}
.error-box button {
  margin-top: 12px;
  background: var(--brand);
  color: #fff;
  border: 0;
  padding: 8px 18px;
  border-radius: 999px;
  cursor: pointer;
  font-family: inherit;
}

.fade-in { animation: fade .35s ease-out both; }
@keyframes fade {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}

/* --- breaking page additions: live badge, editorial byline, timeline, footer --- */
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
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
.livebar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 6px 0 8px; }
.sev-badge {
  display: inline-flex; align-items: center; gap: 7px; color: #fff; font-weight: 800;
  font-size: .72rem; letter-spacing: .04em; padding: 5px 12px; border-radius: 999px;
  background: {{BADGE_COLOR}};
}
.sev-badge .dot { width: 8px; height: 8px; border-radius: 50%; background: #fff; animation: pulse 1.6s infinite; }
.updated { font-size: .82rem; color: var(--muted); }
.editorial-card.fade-in { margin-bottom: 26px; }
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
footer { max-width: var(--maxw); margin: 0 auto; padding: 0 16px 48px; font-size: .82rem; color: var(--muted); }
footer a { color: var(--brand); font-weight: 600; }
@media (max-width: 560px) {
  .date-strip > span:not(.live-pill) { display: none; }
}
</style>

    <script type="application/ld+json">
{{LDJSON}}
    </script>
</head>
<body>
    <header class="site">
      <div class="bar">
        <a class="brand" href="https://news.manzill.com"><span class="logo">ज</span>जयपुर न्यूज़</a>
        <div class="date-strip">
          <span class="live-pill"><span class="dot"></span>लाइव</span>
          <span>{{TODAY}}</span>
          <a class="archive-link" href="https://news.manzill.com">सभी खबरें</a>
        </div>
      </div>
    </header>

    <main>
      <div class="livebar">
        <span class="sev-badge"><span class="dot"></span>{{BADGE}}</span>
        <span class="updated">अंतिम अपडेट {{UPDATED_IST}}</span>
        <button class="refresh-btn" type="button" onclick="location.reload()" aria-label="रिफ्रेश">&#8635; रिफ्रेश</button>
      </div>

      <section class="hero">
        <h1>{{HEADLINE}}</h1>
      </section>

      <div class="editorial-card fade-in">
        <h2>पूरी खबर</h2>
        {{ANALYSIS}}
        <div class="byline">सार्वजनिक जयपुर न्यूज़ फ़ीड से संकलित &middot; समय IST में।</div>
      </div>

      <section class="feed">
        <div class="section-head">
          <h2>लाइव अपडेट</h2>
          <span class="count">{{UPDATE_COUNT}}</span>
        </div>
        <ul class="timeline">
          {{TIMELINE}}
        </ul>
        <p class="note">खबर के विकसित होते ही यह पेज अपने-आप रिफ्रेश होता रहता है।</p>
      </section>

      <section class="feed">
        <div class="section-head">
          <h2>स्रोत</h2>
        </div>
        <div class="grid-text">
          {{SOURCES}}
        </div>
      </section>
    </main>

    <footer>
      <p>जयपुर की ब्रेकिंग खबरें सार्वजनिक न्यूज़ फ़ीड से संकलित। रिपोर्टें प्रारंभिक हो सकती हैं &mdash; महत्वपूर्ण जानकारी की पुष्टि आधिकारिक स्रोतों से करें।</p>
      <p><a href="/breaking/rss.xml">RSS फ़ीड</a> &middot; <a href="https://news.manzill.com">news.manzill.com</a></p>
      <p>&copy; 2026 जयपुर न्यूज़</p>
    </footer>
</body>
</html>
"""

if __name__ == "__main__":
    build()
