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
import shutil
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
# The page moved to /breaking; the retired /breaking-news folder is deleted each run.
OLD_DIR = ROOT / "breaking-news"

SITE = "https://www.manzill.com"
PAGE_URL = SITE + "/breaking"
NEWS_SITE = "https://news.manzill.com"

# Bump whenever the rendered output (template/RSS/sitemap format) changes. A mismatch
# with the value stored in state forces a one-time re-render even when the feed is
# unchanged, so a redesign rolls out on the next scheduled run without a manual push.
RENDER_VERSION = "5"

# strftime has no Hindi locale, so map weekday/month names for the date strip.
HINDI_WEEKDAYS = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
HINDI_MONTHS = [
    "जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
    "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर",
]

# Outlet brand names come from the feeds in English; transliterate the common ones to
# Devanagari so the page stays fully Hindi. Unknown outlets are dropped (no English label).
HINDI_SOURCE = {
    "times of india": "टाइम्स ऑफ इंडिया", "the times of india": "टाइम्स ऑफ इंडिया",
    "hindustan times": "हिंदुस्तान टाइम्स", "ndtv": "एनडीटीवी", "ndtv profit": "एनडीटीवी",
    "india today": "इंडिया टुडे", "the hindu": "द हिंदू", "news18": "न्यूज़18",
    "cnn-news18": "न्यूज़18", "zee news": "ज़ी न्यूज़", "abp news": "एबीपी न्यूज़",
    "abp live": "एबीपी लाइव", "amar ujala": "अमर उजाला", "dainik bhaskar": "दैनिक भास्कर",
    "dainik jagran": "दैनिक जागरण", "jagran": "जागरण", "patrika": "पत्रिका",
    "rajasthan patrika": "राजस्थान पत्रिका", "msn": "एमएसएन", "aaj tak": "आज तक",
    "the indian express": "द इंडियन एक्सप्रेस", "indian express": "इंडियन एक्सप्रेस",
    "economic times": "इकोनॉमिक टाइम्स", "the economic times": "इकोनॉमिक टाइम्स",
    "business standard": "बिज़नेस स्टैंडर्ड", "livemint": "लाइवमिंट", "mint": "मिंट",
    "deccan herald": "डेक्कन हेराल्ड", "firstpost": "फर्स्टपोस्ट", "oneindia": "वनइंडिया",
    "oneindia hindi": "वनइंडिया हिंदी", "jansatta": "जनसत्ता", "navbharat times": "नवभारत टाइम्स",
    "tv9": "टीवी9", "tv9 bharatvarsh": "टीवी9 भारतवर्ष", "the print": "द प्रिंट",
    "theprint": "द प्रिंट", "the wire": "द वायर", "news nation": "न्यूज़ नेशन",
    "free press journal": "फ्री प्रेस जर्नल", "the free press journal": "फ्री प्रेस जर्नल",
    "et now": "ईटी नाउ", "zee business": "ज़ी बिज़नेस", "outlook": "आउटलुक",
    "outlook india": "आउटलुक", "lokmat": "लोकमत", "lokmat times": "लोकमत",
}

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
# Groq sits behind Cloudflare, which returns 403 "error code 1010" (banned browser
# signature) to requests that spoof a browser UA. Send Groq a plain client UA instead;
# the feeds keep using the browser-like USER_AGENT that Google News prefers.
GROQ_UA = "Manzill-BreakingNews/1.0 (+https://www.manzill.com)"
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
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": GROQ_UA},
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


def groq_analyze(api_key: str, clusters: list[dict]) -> dict | None:
    """Ask Groq for a full HINDI package: headline, detailed analysis, a past→present
    developments chain, Hindi source titles, and Hindi secondary stories."""
    model = groq_pick_model(api_key)
    lead = clusters[0]
    others = clusters[1:6]
    lead_sources = [s["title"] for s in cluster_sources(lead, limit=6)]
    lead_snippets = [i["summary"] for i in lead["items"][:4] if i["summary"]][:4]

    system = (
        "आप जयपुर (राजस्थान, भारत) की एक हिंदी न्यूज़ वेबसाइट के समाचार संपादक हैं। "
        "दिए गए फ़ीड आइटम (शीर्षक/स्निपेट अंग्रेज़ी में हो सकते हैं) की जानकारी का ही उपयोग करें "
        "और तथ्यों को स्वाभाविक, शुद्ध हिंदी में लिखें। अपुष्ट बातों का श्रेय दें "
        "('पुलिस के अनुसार', 'स्थानीय रिपोर्टों के मुताबिक')। स्रोतों में मौजूद न होने वाले आँकड़े, "
        "नाम या तथ्य कभी न गढ़ें। सभी टेक्स्ट फ़ील्ड (lead_headline, analysis, developments, "
        "sources_hi, other_stories) पूरी तरह देवनागरी हिंदी में हों — कोई अंग्रेज़ी वाक्य नहीं; "
        "केवल event_type और severity अंग्रेज़ी enum में रहें। सिर्फ़ मान्य JSON लौटाएँ।"
    )
    user = {
        "task": "जयपुर की आज की सबसे बड़ी ब्रेकिंग खबर की विस्तृत हिंदी कवरेज तैयार करें।",
        "lead_story": {"headline": lead["headline"], "snippets": lead_snippets},
        "lead_sources_en": lead_sources,
        "other_stories_en": [c["headline"] for c in others],
        "output_schema": {
            "lead_headline": "संक्षिप्त हिंदी शीर्षक",
            "event_type": "one of: terror, fire, earthquake, flood, accident, "
                          "crime, investigation, protest, civic, weather, other",
            "severity": "one of: critical, high, medium, low",
            "analysis": "3-5 पैराग्राफ की विस्तृत हिंदी रिपोर्ट; पैराग्राफ \\n\\n से अलग करें",
            "developments": "4-8 objects की array [{label, text}] — घटनाक्रम पुराने से नए क्रम "
                            "में (oldest first); label = छोटा हिंदी समय/चरण संकेत (जैसे "
                            "'शुरुआती रिपोर्ट', 'जाँच आगे बढ़ी', 'ताज़ा अपडेट'); text = एक हिंदी वाक्य",
            "sources_hi": "हिंदी एक-पंक्ति शीर्षकों की array — lead_sources_en के समान क्रम व संख्या में",
            "other_stories": "objects {headline, summary} की array हिंदी में — "
                             "other_stories_en के समान क्रम व संख्या में",
        },
    }
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "temperature": 0.35,
            "max_tokens": 2400,
            "response_format": {"type": "json_object"},
        }
    ).encode()

    req = urllib.request.Request(
        f"{GROQ_BASE}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": GROQ_UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
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

    # The page moved to /breaking; delete the retired /breaking-news folder.
    cleanup_old_dir()

    print("Fetching feeds...")
    items = gather_items()
    print(f"  {len(items)} unique items")
    now = now_utc()
    ist_day = to_ist(now).strftime("%Y-%m-%d")

    state = load_state()
    if state.get("ist_day") != ist_day:
        state = {"ist_day": ist_day, "lead": None, "other_stories": [], "last_feed_hash": None}

    if not items:
        # Feeds unreachable/empty: keep the last good page untouched (no commit).
        if not OUT_HTML.exists():
            render(state, now)
            save_state({**state, "last_updated": now.isoformat(),
                        "render_version": RENDER_VERSION})
            print("No feed items; wrote initial placeholder.")
        else:
            print("No feed items; keeping existing page (no commit).")
        return

    clusters = cluster_items(items)
    top = clusters[0]
    feed_hash = hashlib.sha1(
        "|".join(normalize(i["title"]) for i in top["items"]).encode()
    ).hexdigest()[:16]

    # Skip when the top-story feed is unchanged AND already rendered by this output
    # version (no Groq call, no commit). A RENDER_VERSION bump forces a one-time re-render.
    feed_changed = feed_hash != state.get("last_feed_hash")
    up_to_date = state.get("render_version") == RENDER_VERSION
    if OUT_HTML.exists() and state.get("lead") and not feed_changed and up_to_date:
        print("No change in top-story feed; skipping update (no commit).")
        return

    lead = None
    other_stories: list[dict] = []
    if use_ai:
        print("Asking Groq for analysis...")
        ai = groq_analyze(api_key, clusters)
        if ai:
            lead, other_stories = _lead_from_ai(ai, clusters)

    if lead is None:
        # No AI / Groq failed: a clean Hindi holding page — never English feed text.
        print("Groq unavailable — rendering Hindi holding page.")
        lead, other_stories = _holding_lead(), []

    state.update(
        {
            "ist_day": ist_day,
            "last_updated": now.isoformat(),
            "last_feed_hash": feed_hash,
            "render_version": RENDER_VERSION,
            "lead": lead,
            "other_stories": other_stories,
        }
    )
    render(state, now)
    save_state(state)
    print(f"Done. Lead: {lead.get('headline','')!r} [{lead.get('severity','')}] "
          f"— {len(lead.get('developments', []))} development(s), "
          f"{len(other_stories)} other stor(y/ies).")


def _lead_from_ai(ai: dict, clusters: list[dict]) -> tuple[dict | None, list[dict]]:
    """Map the Groq Hindi JSON onto the render model. Returns (lead, other_stories)."""
    top = clusters[0]
    headline = (ai.get("lead_headline") or "").strip()
    if not headline:
        return None, []
    severity = (ai.get("severity") or top["severity"]).strip().lower()
    if severity not in CADENCE_MINUTES:
        severity = top["severity"]
    event_type = (ai.get("event_type") or "other").strip().lower()
    analysis = (ai.get("analysis") or "").strip()

    developments = []
    for d in (ai.get("developments") or [])[:8]:
        if isinstance(d, dict):
            text = (d.get("text") or "").strip()
            label = (d.get("label") or "").strip()
        else:
            text, label = str(d).strip(), ""
        if text:
            developments.append({"label": label, "text": text})

    src_objs = cluster_sources(top, limit=6)
    sources_hi = ai.get("sources_hi") or []
    sources = []
    for i, s in enumerate(src_objs):
        hi = None
        if i < len(sources_hi) and isinstance(sources_hi[i], str) and sources_hi[i].strip():
            hi = sources_hi[i].strip()
        sources.append({"title_hi": hi, "url": s["url"],
                        "source": s["source"], "published": s["published"]})

    lead = {
        "id": cluster_id(top),
        "headline": headline,
        "event_type": event_type,
        "severity": severity,
        "analysis": analysis,
        "developments": developments,
        "sources": sources,
    }

    others = clusters[1:6]
    os_ai = ai.get("other_stories") or []
    other_stories = []
    for i, c in enumerate(others):
        if i >= len(os_ai) or not isinstance(os_ai[i], dict):
            continue
        hl = (os_ai[i].get("headline") or "").strip()
        if not hl:
            continue
        src = cluster_sources(c, limit=1)
        other_stories.append({
            "headline": hl,
            "summary": (os_ai[i].get("summary") or "").strip(),
            "url": src[0]["url"] if src else PAGE_URL,
            "source": src[0]["source"] if src else "",
        })
    return lead, other_stories


def _holding_lead() -> dict:
    """A fully-Hindi holding lead used only when Groq is unreachable (no English)."""
    return {
        "id": "holding",
        "headline": "जयपुर: ताज़ा खबरें अपडेट हो रही हैं",
        "event_type": "other",
        "severity": "low",
        "analysis": "विस्तृत हिंदी कवरेज तैयार हो रहा है। कृपया कुछ ही देर में दोबारा देखें — "
                    "यह पेज दिनभर अपने-आप अपडेट होता रहता है।",
        "developments": [],
        "sources": [],
    }


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


def _hindi_clock(d: datetime) -> str:
    """Fully-Hindi clock, e.g. 'शाम 5:22 बजे' (no AM/PM/IST)."""
    h = d.hour
    period = ("रात" if (h < 4 or h >= 19) else "सुबह" if h < 12
              else "दोपहर" if h < 16 else "शाम")
    return f"{period} {h % 12 or 12}:{d.minute:02d} बजे"


def _hindi_datetime(dt: datetime) -> str:
    d = to_ist(dt)
    return f"{d.day} {HINDI_MONTHS[d.month - 1]} {d.year}, {_hindi_clock(d)}"


def _hindi_date(dt: datetime) -> str:
    d = to_ist(dt)
    return f"{HINDI_WEEKDAYS[d.weekday()]}, {d.day} {HINDI_MONTHS[d.month - 1]} {d.year}"


def hindi_source(name: str) -> str:
    """Devanagari outlet name for the common outlets; empty for unknown ones so no
    English brand text leaks onto the page."""
    return HINDI_SOURCE.get((name or "").strip().lower(), "")


def _src_time(s: dict) -> str:
    try:
        dt = datetime.fromisoformat(s["published"])
    except Exception:
        return ""
    d = to_ist(dt)
    return f"{d.day} {HINDI_MONTHS[d.month - 1]}, {_hindi_clock(d)}"


def _src_meta(name_hi: str, time_hi: str) -> str:
    """Build the '.info' row from a Hindi source name + Hindi time, omitting blanks."""
    parts = []
    if name_hi:
        parts.append(f'<span class="src">{esc(name_hi)}</span>')
    if time_hi:
        parts.append(f'<span>{esc(time_hi)}</span>')
    return '<span class="dot"></span>'.join(parts)


BRAND_SUFFIX = "ब्रेकिंग जयपुर न्यूज़"


def render(state: dict, now: datetime) -> None:
    lead = state.get("lead") or {}
    other_stories = state.get("other_stories") or []
    updated_ist = _hindi_datetime(now)
    today_ist = _hindi_date(now)

    sev = lead.get("severity", "low")
    badge = SEV_LABEL.get(sev, "निगरानी में")
    color = SEV_COLOR.get(sev, "#6b6b6b")
    headline = lead.get("headline") or "जयपुर: ताज़ा खबरें अपडेट हो रही हैं"
    headline_html = esc(headline)
    title = (f"{headline} | {BRAND_SUFFIX}" if lead.get("headline")
             else f"{BRAND_SUFFIX} — लाइव अपडेट | जयपुर न्यूज़")

    paras = [p.strip() for p in (lead.get("analysis") or "").split("\n\n") if p.strip()]
    analysis_html = "\n        ".join(
        f"<p>{esc(p)}</p>" for p in paras
    ) or "<p>खबर विकसित हो रही है।</p>"

    # Sources — Hindi titles; links open in the SAME tab.
    sources = lead.get("sources", [])
    if sources:
        source_items = "\n".join(
            f'<div class="card-text fade-in">'
            f'<a href="{esc(s["url"])}" rel="nofollow">'
            f'<h3>{esc(s.get("title_hi") or hindi_source(s.get("source", "")) or "ताज़ा रिपोर्ट")}</h3>'
            f'<div class="info">{_src_meta(hindi_source(s.get("source", "")), _src_time(s))}</div>'
            f'</a></div>'
            for s in sources
        )
    else:
        source_items = '<div class="empty-note">स्रोत जुटाए जा रहे हैं।</div>'

    # Developments — a past→present chain (oldest at top, newest at bottom).
    developments = lead.get("developments", [])
    if developments:
        timeline_html = "\n".join(
            f'<li class="tl-item sev-{esc(sev)}">'
            + (f'<time>{esc(d.get("label"))}</time>' if d.get("label") else "")
            + f'<p>{esc(d.get("text"))}</p></li>'
            for d in developments
        )
    else:
        timeline_html = '<li class="tl-item"><p>घटनाक्रम अपडेट हो रहा है।</p></li>'
    update_count = f"{len(developments)} घटनाक्रम" if developments else "अपडेट हो रहा है"

    # Secondary "अन्य ताज़ा खबरें" — Hindi, links in the same tab.
    if other_stories:
        cards = "\n".join(
            f'<div class="card-text fade-in">'
            f'<a href="{esc(o["url"])}" rel="nofollow">'
            f'<h3>{esc(o["headline"])}</h3>'
            + (f'<p class="desc">{esc(o["summary"])}</p>' if o.get("summary") else "")
            + f'<div class="info">{_src_meta(hindi_source(o.get("source", "")), "")}</div>'
            f'</a></div>'
            for o in other_stories
        )
        other_section = (
            '<section class="feed">\n'
            '        <div class="section-head"><h2>अन्य ताज़ा खबरें</h2></div>\n'
            f'        <div class="grid-text">\n{cards}\n        </div>\n'
            '      </section>'
        )
    else:
        other_section = ""

    # JSON-LD: publisher (NewsMediaOrganization) + this live coverage (LiveBlogPosting), hi-IN.
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
        "headline": headline,
        "url": PAGE_URL,
        "inLanguage": "hi-IN",
        "datePublished": now.isoformat(),
        "dateModified": now.isoformat(),
        "coverageStartTime": now.isoformat(),
        "about": {"@type": "Place", "name": "जयपुर, राजस्थान, भारत"},
        "publisher": {"@type": "NewsMediaOrganization",
                      "name": "जयपुर न्यूज़ | Jaipur News", "url": NEWS_SITE + "/"},
        "liveBlogUpdate": [
            {"@type": "BlogPosting", "headline": d.get("text", "")[:110],
             "datePublished": now.isoformat(), "articleBody": d.get("text", "")}
            for d in developments[:20]
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
        "{{OTHER_STORIES}}": other_section,
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
    """Emit breaking/rss.xml (Hindi): the lead plus one item per development."""
    lead = state.get("lead") or {}
    lead_id = lead.get("id", "bn")
    items = []
    if lead.get("headline"):
        analysis = (lead.get("analysis") or "").replace("\n\n", " ")
        items.append(_rss_item(lead["headline"], analysis or lead["headline"],
                               f"{lead_id}-lead", now))
        for i, d in enumerate(lead.get("developments", [])):
            text = d.get("text", "")
            if text:
                items.append(_rss_item(text, text, f"{lead_id}-dev-{i}", now))
    if not items:
        items.append(_rss_item(
            "जयपुर की ताज़ा खबरें अपडेट हो रही हैं",
            "विस्तृत हिंदी कवरेज तैयार हो रहा है। यह फ़ीड घटनाओं के विकसित होने पर अपडेट होता रहता है।",
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


def cleanup_old_dir() -> None:
    """Delete the retired /breaking-news folder. Idempotent — the workflow stages the
    removal with `git add -A`, so the first post-merge run drops it from the repo."""
    if OLD_DIR.exists():
        shutil.rmtree(OLD_DIR, ignore_errors=True)


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
        <div class="byline">सार्वजनिक जयपुर न्यूज़ फ़ीड से संकलित &middot; समय भारतीय मानक समयानुसार।</div>
      </div>

      <section class="feed">
        <div class="section-head">
          <h2>घटनाक्रम &mdash; शुरुआत से अब तक</h2>
          <span class="count">{{UPDATE_COUNT}}</span>
        </div>
        <ul class="timeline">
          {{TIMELINE}}
        </ul>
        <p class="note">ऊपर से नीचे: पुराने से नए घटनाक्रम। खबर के विकसित होते ही यह पेज अपने-आप रिफ्रेश होता रहता है।</p>
      </section>

      <section class="feed">
        <div class="section-head">
          <h2>स्रोत</h2>
        </div>
        <div class="grid-text">
          {{SOURCES}}
        </div>
      </section>

      {{OTHER_STORIES}}
    </main>

    <footer>
      <p>जयपुर की ब्रेकिंग खबरें सार्वजनिक न्यूज़ फ़ीड से संकलित। रिपोर्टें प्रारंभिक हो सकती हैं &mdash; महत्वपूर्ण जानकारी की पुष्टि आधिकारिक स्रोतों से करें।</p>
      <p><a href="/breaking/rss.xml">आरएसएस फ़ीड</a> &middot; <a href="https://news.manzill.com">जयपुर न्यूज़ हिंदी में</a></p>
      <p>&copy; 2026 जयपुर न्यूज़</p>
    </footer>
</body>
</html>
"""

if __name__ == "__main__":
    build()
