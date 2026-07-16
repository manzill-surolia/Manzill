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
# Rolling ~30-day archive of each ongoing story's development points, so the AI can
# narrate the full multi-day arc (Google News RSS only exposes ~24-48h).
ARCHIVE_PATH = ROOT / "breaking" / "data" / "archive.json"
ARCHIVE_DAYS = 30
# Optional manual pin: force a chosen story to lead (by keywords or an explicit URL).
# See load_override(); also fed by the FORCE_QUERY/FORCE_URL/FORCE_HEADLINE workflow inputs.
OVERRIDE_PATH = ROOT / "breaking" / "data" / "override.json"
# The page moved to /breaking; the retired /breaking-news folder is deleted each run.
OLD_DIR = ROOT / "breaking-news"

SITE = "https://www.manzill.com"
PAGE_URL = SITE + "/breaking"
NEWS_SITE = "https://news.manzill.com"

# Bump whenever the rendered output (template/RSS/sitemap format) changes. A mismatch
# with the value stored in state forces a one-time re-render even when the feed is
# unchanged, so a redesign rolls out on the next scheduled run without a manual push.
RENDER_VERSION = "12"

# strftime has no Hindi locale, so map month names for the Hindi date/time strings.
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
    # Event terms are parenthesised so they stay anchored to "Jaipur" and don't pull
    # national stories; the Jaipur-locality gate (is_local) is the real safeguard.
    "Jaipur (fire OR accident OR blast) when:1d",
    "Jaipur (protest OR clash OR crime) when:1d",
    "Jaipur (weather OR rain OR IMD) when:1d",
    # Police accountability / misconduct — surfaced with high priority in "यह भी ब्रेकिंग".
    "Jaipur OR Rajasthan police lathicharge OR beaten OR custodial OR negligence "
    "OR misconduct OR suspended when:2d",
    # Burning-issue / accountability beat (fresh) — disorder, misgovernance, economic distress,
    # rights, elections, civic breakdown. Anchored to Jaipur/Rajasthan; is_local() is the gate.
    "Jaipur OR Rajasthan (unemployment OR hunger OR starvation OR corruption OR scam "
    "OR negligence OR mismanagement) when:2d",
    "Jaipur OR Rajasthan (EVM OR \"human rights\" OR custodial OR eviction OR encroachment "
    "OR bulldozer OR waterlogging OR \"power cut\") when:2d",
]

# Wider-window BACKFILL queries. Items from these seed a story's multi-week timeline (the archive)
# but are tagged archival and — being older than FRESH_LEAD_HOURS — can never become the "breaking"
# lead or a visible "यह भी ब्रेकिंग" card. They exist so a story that becomes prominent today can
# show the weeks of coverage that preceded it, instead of starting the timeline at "today".
ARCHIVAL_QUERIES = [
    "Jaipur OR Rajasthan (corruption OR scam OR negligence OR unemployment OR starvation "
    "OR EVM OR custodial OR \"human rights\" OR eviction OR encroachment) when:14d",
    "Jaipur OR Rajasthan (school OR student OR bullying OR death OR probe OR investigation "
    "OR court OR petition) when:30d",
]

# --------------------------------------------------------------------------- #
# Jaipur-city locality gate
# --------------------------------------------------------------------------- #
# The page is Jaipur-local: a story is kept only if it actually mentions Jaipur (or a
# well-known Jaipur locality). Without this gate a national item that leaks into a broad
# feed query (e.g. an Assam flood — "flood" is a critical keyword) can outscore every local
# story and lead the page. Single-word tokens are matched against the item's token set (so
# "camera" never matches "amer"); multi-word phrases are matched as substrings. `normalize()`
# keeps "jaipur" — it is stripped only for clustering (STOPWORDS), not from the raw text.
JAIPUR_TERMS = {
    "jaipur", "jaipurite", "jaipurites", "sanganer", "sitapura", "jhotwara",
    "mansarovar", "vidyadhar", "amer", "amber", "chomu", "bagru", "chaksu",
    "shahpura", "kotputli",
}
JAIPUR_PHRASES = (
    "pink city", "walled city", "malviya nagar", "vaishali nagar",
    "tonk road", "sindhi camp", "jln marg", "bani park",
)

# Facts about police incompetence/misconduct are a standing priority: flagged clusters are
# pulled to the front of the "यह भी ब्रेकिंग" section (see order_secondary). Precision matters —
# ordinary crime reporting always names the police (as investigator), so we don't flag on a
# bare "police" mention. Two tiers, both gated on a police reference:
#   STRONG  — police-context misconduct words (substring match); flag on their own.
#   FORCE   — physical-force verbs that only count as misconduct when the police are the
#             grammatical subject/agent ("police beat …", "… beaten by police"), matched
#             by POLICE_FORCE_RE so a civilian "man beaten to death, police said" is ignored.
POLICE_TERMS = ("police", "cop", "cops", "policemen", "policeman", "constable",
                "constables", "sho", "thana", "sub-inspector", "jawan", "jawans")
POLICE_MISCONDUCT_STRONG = [
    "lathicharge", "lathi charge", "lathi-charge", "baton charge", "custod",
    "negligen", "misconduct", "dereliction", "cover up", "coverup", "cover-up",
    "inaction", "botch", "mishandl", "brutal", "excessive force", "manhandl",
    "high-handed", "highhanded", "third degree", "custodial torture", "suspend",
]
_POLICE_WORD = (r"(?:police|cops?|policemen|policeman|constables?|sho|"
                r"sub-?inspectors?|jawans?)")
_FORCE_ACT = (r"(?:beat|beaten|beating|thrash(?:ed|ing)?|assault(?:ed|ing)?|"
              r"manhandl(?:ed|ing)?|lathi\s*charg\w*|baton\s*charg\w*|lathi|baton|cane[d]?)")
POLICE_FORCE_RE = re.compile(
    rf"\b{_POLICE_WORD}\b\W+(?:\w+\W+){{0,2}}{_FORCE_ACT}\b"        # police (brutally) beat …
    rf"|\b{_FORCE_ACT}\b\W+(?:\w+\W+){{0,2}}by\W+{_POLICE_WORD}\b",  # … beaten (up) by police
    re.IGNORECASE,
)

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

# --------------------------------------------------------------------------- #
# Burning-issue / जन-सरोकार beat (editorial priority)
# --------------------------------------------------------------------------- #
# This page is an accountability-first outlet: it leads with the *burning issue* of the day —
# disorder, misgovernance, economic distress, rights violations, civic breakdown — over
# ceremonial / feel-good news. These keyword sets steer ranking ONLY; they never change what is
# true. The pipeline still surfaces only stories that actually appear in the feeds, and the Groq
# prompt keeps its hard rules (attribute unconfirmed facts, never fabricate). Boosting a theme
# raises a *real, sourced* story's rank — it never invents allegations about any person, party or
# company. All three lists below are plain config: edit them to tune the beat, no logic changes.
#
# ISSUE_KEYWORDS is grouped so a story that touches several *distinct* groups scores higher than
# one that merely repeats a single theme (see issue_rank()). English terms, because the feeds are
# English (severity_of already covers crime/deaths via SEVERITY_KEYWORDS[high]).
ISSUE_KEYWORDS = {
    "disorder": [
        "chaos", "anarchy", "mayhem", "lawlessness", "lawless", "unrest", "disorder",
        "mismanagement", "mismanaged", "breakdown", "collapse", "haphazard", "disarray",
    ],
    "governance": [
        "corruption", "corrupt", "scam", "bribe", "bribery", "kickback", "scandal",
        "negligence", "negligent", "apathy", "lapse", "dereliction", "inaction",
        "cover up", "cover-up", "coverup", "red tape", "irregularit", "embezzl",
        "misappropriat", "policy failure", "governance failure", "government failure",
        "encroachment", "illegal", "flouting", "violation", "bulldozer", "demolition",
        "demolished", "eviction", "evicted",
    ],
    "economy": [
        "unemployment", "unemployed", "jobless", "job loss", "layoff", "lay-off",
        "retrenchment", "hunger", "starvation", "starve", "malnutrition", "malnourish",
        "poverty", "destitute", "inflation", "price rise", "price hike", "farmer distress",
        "farmers protest", "msp", "crop loss", "debt",
    ],
    "rights": [
        "human rights", "custodial", "atrocity", "atrocities", "caste violence",
        "dalit", "adivasi", "minorit", "hate crime", "discrimination", "harassment",
        "trafficking", "bonded labour", "child labour",
    ],
    "democracy": [
        "evm", "electoral roll", "voter list", "booth capturing", "voter fraud",
        "poll irregularit", "vote rigging", "electoral fraud", "voter suppression",
    ],
    "civic": [
        "waterlogging", "water logging", "sewage", "garbage", "sanitation", "pothole",
        "power cut", "outage", "blackout", "water crisis", "gridlock", "shortage",
        "crumbling", "dilapidated", "stranded", "overflow",
    ],
}

# Named public-accountability subjects — governments, offices and big-business houses whose
# conduct is a matter of public interest. Public figures/entities, NOT private individuals. A
# subject term only lifts a story's rank when it co-occurs with a failure/wrongdoing signal
# (see issue_rank) — a bare mention is not enough, and framing always comes from the sourced facts.
ACCOUNTABILITY_SUBJECTS = [
    "government", "govt", "sarkar", "administration", "minister", "mantri", "cabinet",
    "chief minister", "cm ", "bhajanlal", "bhajan lal", "bjp", "modi", "mla", "mp ",
    "municipal", "nagar nigam", "jda", "jaipur development authority", "collector",
    "corporation", "state government", "rajasthan government", "adani", "ambani", "reliance",
]

# Ceremonial / feel-good news that must not lead the "breaking" slot (see is_ceremonial &
# apply_lead_policy). Only demoted when the cluster carries no serious severity and no issue
# signal — a stampede or death *at* a procession is never treated as ceremonial.
CEREMONIAL_KEYWORDS = [
    "yatra", "rath", "procession", "shobha", "festival", "mela", "fair ", "celebration",
    "celebrat", "tradition", "heritage", "inaugurat", "foundation stone", "felicitat",
    "cultural", "jubilee", "anniversary", "devotees", "pilgrim", "temple event", "utsav",
    "mahotsav", "ribbon", "launch event", "felicitation",
]

# Scoring weights (tunable). Recency is deliberately no longer the heaviest term — a burning
# issue must outrank a merely-fresh ceremonial item. See cluster_items().
W_ISSUE = 4.0             # weight on issue_rank (0-3) — the accountability boost
W_RECENCY = 2.0           # was 4.0; freshness is now a tiebreaker, not the dominant term
CEREMONIAL_PENALTY = 4.0  # subtracted from a ceremonial cluster's score
# A cluster may LEAD ("breaking now") only if its newest item is this fresh. Older clusters
# (e.g. pulled by the wider-window backfill queries) still seed the archive/timeline but are
# never presented as breaking. See cluster_items()/apply_lead_policy().
FRESH_LEAD_HOURS = 36.0

# Max timeline points narrated per story. A weeks-long arc is down-sampled to this many points
# (keeping the first and last, see _arc_sample) so the "घटनाक्रम" still spans शुरुआत → अब while the
# Groq request stays within the TPM budget.
TIMELINE_MAX = 30

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


def _cluster_text(cluster: dict) -> str:
    return " ".join(
        normalize(i["title"] + " " + i.get("summary", "")) for i in cluster["items"]
    )


def issue_rank(cluster: dict) -> int:
    """0-3 'burning issue' score: how many DISTINCT accountability/disorder themes the story
    touches (ISSUE_KEYWORDS groups), +1 when a named accountability subject (government/BJP/
    Modi/Bhajanlal/Adani/Ambani…) co-occurs with an issue signal. Purely lexical, like
    severity_of — it lifts the rank of a real, sourced story; it never invents one."""
    text = " " + _cluster_text(cluster) + " "
    groups = sum(1 for terms in ISSUE_KEYWORDS.values() if any(t in text for t in terms))
    rank = groups
    if groups and any(s in text for s in ACCOUNTABILITY_SUBJECTS):
        rank += 1  # a public-accountability subject named alongside a failure/wrongdoing signal
    return min(rank, 3)


def is_ceremonial(cluster: dict) -> bool:
    """True for feel-good / ceremonial news (yatra, festival, inauguration…) that must not lead
    the breaking slot — but ONLY when the story carries no serious severity and no issue signal,
    so a stampede or death *at* a procession is never demoted. Expects `severity` and `issue_rank`
    already set on the cluster (see cluster_items)."""
    text = " " + _cluster_text(cluster) + " "
    if not any(c in text for c in CEREMONIAL_KEYWORDS):
        return False
    if cluster.get("severity", "low") in ("high", "critical"):
        return False
    return cluster.get("issue_rank", 0) == 0


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


def fetch_feed(query: str, archival: bool = False) -> list[dict]:
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
                # archival=True items come from the wider-window backfill queries: they seed the
                # timeline/archive but are too old to lead or show as a "breaking" card.
                "archival": archival,
            }
        )
    return items


def gather_items() -> list[dict]:
    seen: dict[str, dict] = {}

    def consider(it: dict) -> None:
        key = normalize(it["title"])[:80]
        if not key:
            return
        prev = seen.get(key)
        # Prefer a fresh (non-archival) sighting over a backfill duplicate; otherwise the newest.
        # A tuple compare: (is_fresh, published) — True sorts above False, then newer wins.
        if prev is None or (not it.get("archival", False), it["published"]) > (
            not prev.get("archival", False), prev["published"]
        ):
            seen[key] = it

    for query in FEED_QUERIES:          # fresh sightings first (when:1d/2d)
        for it in fetch_feed(query, archival=False):
            consider(it)
    for query in ARCHIVAL_QUERIES:      # then wider-window backfill (when:14d/30d)
        for it in fetch_feed(query, archival=True):
            consider(it)
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
        cl["police_flag"] = is_police_misconduct(cl)
        cl["issue_rank"] = issue_rank(cl)          # burning-issue / accountability strength (0-3)
        cl["ceremonial"] = is_ceremonial(cl)       # feel-good item that must not lead
        cl["fresh"] = age_h <= FRESH_LEAD_HOURS    # lead/secondary eligibility (vs archive-only)
        # Newsworthiness score. The page leads with the BURNING ISSUE, not merely the newest item:
        # importance (severity) and the accountability/disorder signal (issue_rank) dominate;
        # recency is a tiebreaker (W_RECENCY, was 4.0); ceremonial/feel-good stories are penalised.
        # apply_lead_policy then enforces the lead rules; order_secondary front-loads issue stories.
        cl["score"] = (
            severity_rank(cl["severity"]) * 3.0
            + cl["issue_rank"] * W_ISSUE
            + min(len(cl["items"]), 6) * 1.0
            + recency * W_RECENCY
            - (CEREMONIAL_PENALTY if cl["ceremonial"] else 0.0)
        )
    clusters.sort(key=lambda c: c["score"], reverse=True)
    return clusters


def is_police_misconduct(cluster: dict) -> bool:
    """True if the cluster reports Jaipur/Rajasthan police incompetence/misconduct.

    Needs a police reference AND either a strong police-context misconduct word or a
    force verb with the police as its subject/agent — so an ordinary crime story that
    merely quotes the police ("man beaten to death, police said") is never flagged.
    """
    text = " ".join(normalize(i["title"] + " " + i.get("summary", "")) for i in cluster["items"])
    words = set(text.split())
    if not any(t in words for t in POLICE_TERMS):
        return False
    if any(kw in text for kw in POLICE_MISCONDUCT_STRONG):
        return True
    return bool(POLICE_FORCE_RE.search(text))


def order_secondary(clusters: list[dict]) -> list[dict]:
    """The 'यह भी ब्रेकिंग' pool (max 5), excluding the lead. Only fresh (current) clusters show —
    archive-only backfill items never appear as breaking cards. Police-accountability and
    burning-issue stories are pulled to the front (standing priority) so they keep a slot even when
    other stories outrank them; the rest follow by score. `clusters` is already sorted by score, so
    each group keeps its order."""
    pool = [c for c in clusters[1:] if c.get("fresh", True)]
    police = [c for c in pool if c.get("police_flag")]
    issue = [c for c in pool if not c.get("police_flag") and c.get("issue_rank", 0) > 0]
    rest = [c for c in pool if not c.get("police_flag") and c.get("issue_rank", 0) == 0]
    return (police + issue + rest)[:5]


def is_local(cluster: dict) -> bool:
    """True if the cluster is about Jaipur city (or a known Jaipur locality). Reads the raw
    item text — `jaipur` is only a clustering stopword, so it survives in `normalize()`."""
    text = " ".join(
        normalize(i["title"] + " " + i.get("summary", "")) for i in cluster["items"]
    )
    if set(text.split()) & JAIPUR_TERMS:
        return True
    return any(p in text for p in JAIPUR_PHRASES)


def filter_local(clusters: list[dict]) -> list[dict]:
    """Drop every cluster that is not a Jaipur-city story (keeps order)."""
    return [c for c in clusters if is_local(c)]


# "Major event" bar: severity high/critical (disaster, terror, fatal accident, big fire, murder,
# rape, riot). Used to decide when a genuine disaster outranks the standing police-accountability
# promotion. severity_rank: critical=3, high=2, medium=1, low=0.
MAJOR_MIN_RANK = 2  # 'high'


def apply_lead_policy(clusters: list[dict]) -> list[dict]:
    """Choose the lead (clusters[0]) under the burning-issue-first editorial policy.

    - Only a FRESH cluster can lead — archive-only backfill items seed the timeline but are never
      presented as "breaking now".
    - A ceremonial / feel-good story leads ONLY on a day with no disorder / accountability / serious
      (injury-death) story at all; otherwise the burning issue leads.
    - Standing priority: on a day with no MAJOR event (high/critical), a police-accountability story
      is promoted to lead; a genuine disaster still leads when present.

    `clusters` is already score-sorted (issue-boosted, ceremonial-penalised), so each filtered list
    keeps its order and `[0]` is the strongest of its kind."""
    if not clusters:
        return clusters
    fresh = [c for c in clusters if c.get("fresh", True)]
    if not fresh:
        return clusters  # nothing current; build() keeps the last good page rather than lead stale

    def qualifies(c: dict) -> bool:  # a burning issue / accountability / serious story
        return bool(
            c.get("police_flag")
            or c.get("issue_rank", 0) > 0
            or severity_rank(c.get("severity", "low")) >= MAJOR_MIN_RANK
        )

    serious = [c for c in fresh if qualifies(c) and not c.get("ceremonial")]
    if serious:
        police = [c for c in serious if c.get("police_flag")]
        major = any(severity_rank(c.get("severity", "low")) >= MAJOR_MIN_RANK for c in serious)
        lead = police[0] if (police and not major) else serious[0]
    else:
        lead = fresh[0]  # quiet day — nothing but ceremonial/neutral news; never blank

    return [lead] + [c for c in clusters if c is not lead]


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


def groq_analyze(api_key: str, clusters: list[dict], points: list[dict]) -> dict | None:
    """Ask Groq for a deep HINDI package covering the story's full multi-week arc: long analysis,
    key facts, dated developments (one per archived timeline point, oldest→newest), police
    accountability, what-next, Hindi source titles and Hindi secondary stories. `points` is the
    already down-sampled archived arc (oldest first) — see _arc_sample()/TIMELINE_MAX."""
    model = groq_pick_model(api_key)
    lead = clusters[0]
    others = order_secondary(clusters)  # police + burning-issue stories first
    lead_sources = [s["title"] for s in cluster_sources(lead, limit=6)]
    lead_snippets = [i["summary"] for i in lead["items"][:5] if i["summary"]][:5]
    # The archived arc, oldest→newest. `when` carries the exact Hindi date+time so the AI labels
    # each development with a real timestamp; the payload is kept lean to respect the Groq TPM cap.
    history = [
        {"when": _hindi_point_label(p), "report": p.get("text_en", "")}
        for p in points
    ]

    system = (
        "आप जयपुर (राजस्थान, भारत) की एक हिंदी न्यूज़ वेबसाइट के वरिष्ठ समाचार संपादक हैं। "
        "दिए गए फ़ीड आइटम और घटनाक्रम इतिहास (story_history — शीर्षक अंग्रेज़ी में हो सकते हैं) की "
        "जानकारी का ही उपयोग करें और तथ्यों को स्वाभाविक, शुद्ध, विस्तृत हिंदी में लिखें। "
        "story_history कई दिनों में फैला हो सकता है — पूरी कहानी शुरुआत से अब तक क्रमवार समझाएँ ताकि "
        "पाठक को end-to-end समझ मिले। अपुष्ट बातों का श्रेय दें ('पुलिस के अनुसार', "
        "'स्थानीय रिपोर्टों के मुताबिक')। स्रोतों में मौजूद न होने वाले आँकड़े, नाम या तथ्य कभी न गढ़ें। "
        "अनिवार्य संपादकीय नियम: यदि स्रोतों में जयपुर/राजस्थान पुलिस की जाँच में किसी भी तरह की "
        "लापरवाही, देरी, चूक, नाकामी या आलोचना का ज़िक्र हो, तो उसे 'police_accountability' में "
        "स्पष्ट व प्रमुखता से, तथ्यों के साथ उजागर करें — लेकिन कभी मनगढ़ंत आरोप न लगाएँ। "
        "यदि प्रमुख खबर (lead_story) स्वयं पुलिस की लापरवाही/नाकामी से जुड़ी हो, तो उसे मुख्य खबर मानकर "
        "analysis में उसका पूरा, तथ्यपरक विवरण दें। "
        "सभी टेक्स्ट फ़ील्ड पूरी तरह देवनागरी हिंदी में हों — कोई अंग्रेज़ी वाक्य नहीं; केवल event_type "
        "और severity अंग्रेज़ी enum में रहें। सिर्फ़ मान्य JSON लौटाएँ।"
    )
    user = {
        "task": "जयपुर की प्रमुख ब्रेकिंग खबर की गहराई से, बहु-दिवसीय, end-to-end हिंदी कवरेज तैयार करें।",
        "lead_story": {"headline": lead["headline"], "snippets": lead_snippets},
        "story_history": history,
        "lead_sources_en": lead_sources,
        "other_stories_en": [c["headline"] for c in others],
        "output_schema": {
            "lead_headline": "संक्षिप्त, सटीक हिंदी शीर्षक",
            "event_type": "one of: terror, fire, earthquake, flood, accident, "
                          "crime, investigation, protest, civic, weather, other",
            "severity": "one of: critical, high, medium, low",
            "analysis": "3-4 सुसंगत, बहु-वाक्य पैराग्राफ की विस्तृत हिंदी रिपोर्ट (प्रवाहमय गद्य, "
                        "टुकड़ों में नहीं) — पृष्ठभूमि, शुरुआत से अब तक का पूरा घटनाक्रम, मौजूदा स्थिति; "
                        "हर पैराग्राफ में कई वाक्य हों; पैराग्राफ \\n\\n से अलग करें",
            "key_facts": "4-8 हिंदी बिंदुओं की array (छोटे तथ्य)",
            "developments": "objects की array [{date_label, text}] — story_history के हर बिंदु के लिए "
                            "ठीक एक प्रविष्टि, उसी क्रम में (oldest→newest; कोई बिंदु न छोड़ें, न जोड़ें); "
                            "date_label = उसी बिंदु के story_history 'when' से हूबहू (जैसे "
                            "'13 जुलाई, दोपहर 4:06 बजे'); समय ज्ञात न हो तो केवल तिथि; समय कभी न गढ़ें; "
                            "text = 1 संक्षिप्त, सुस्पष्ट हिंदी वाक्य",
            "police_accountability": "हिंदी पैराग्राफ — जयपुर/राजस्थान पुलिस की लापरवाही/देरी/चूक/"
                                     "आलोचना के प्रमाणित तथ्य; यदि स्रोतों में कुछ नहीं तो खाली स्ट्रिंग",
            "what_next": "1-2 हिंदी वाक्य — आगे क्या संभावित/अपेक्षित है",
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
            # TPM-safe: the archived arc is down-sampled (TIMELINE_MAX) and the history payload is
            # lean, so prompt + max_tokens stays comfortably under Groq's 8000 TPM cap. Do NOT raise
            # this past ~5500 on this tier — see AGENTS.md "Groq TPM gotcha".
            "max_tokens": 5000,
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


# --------------------------------------------------------------------------- #
# Story archive — a rolling ~30-day memory of how each story developed
# --------------------------------------------------------------------------- #
def load_archive() -> dict:
    if ARCHIVE_PATH.exists():
        try:
            data = json.loads(ARCHIVE_PATH.read_text())
            if isinstance(data, dict) and "stories" in data:
                return data
        except Exception:
            pass
    return {"stories": []}


def save_archive(archive: dict) -> None:
    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PATH.write_text(json.dumps(archive, indent=2, ensure_ascii=False) + "\n")


def _days_ago(iso: str, now: datetime) -> float:
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return 1e9
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400.0


# Cross-run "same story" matching. A slightly looser threshold and larger keyword memory than the
# same-run clustering keep a weeks-long arc mapped to ONE archive entry, instead of fragmenting into
# several one-day stories as the headline wording drifts day to day.
ARCHIVE_MATCH_MIN = 0.24   # was 0.30
ARCHIVE_KW_CAP = 40        # was 24


def ingest_cluster(archive: dict, cluster: dict, now: datetime) -> dict:
    """Match one cluster to an existing archived story (or start a new one) and append its items as
    new dated development points. Called for EVERY Jaipur-local cluster each run — not just the lead
    — so an ongoing story keeps gaining timeline points even on days it is not the headline, and the
    wider-window backfill items seed the weeks that preceded it. Returns the matched story. Does not
    prune — call prune_archive() once after all clusters are ingested."""
    kw = set(cluster["keywords"])
    best, best_sim = None, 0.0
    for story in archive["stories"]:
        sim = jaccard(kw, set(story.get("keywords", [])))
        if sim > best_sim:
            best, best_sim = story, sim
    if best is None or best_sim < ARCHIVE_MATCH_MIN:
        best = {
            "id": cluster_id(cluster),
            "first_seen": now.isoformat(),
            "keywords": sorted(kw)[:ARCHIVE_KW_CAP],
            "points": [],
        }
        archive["stories"].append(best)
    else:
        # keep the keyword set fresh as the story evolves
        best["keywords"] = sorted(set(best.get("keywords", [])) | kw)[:ARCHIVE_KW_CAP]

    best["last_seen"] = now.isoformat()
    seen_urls = {p.get("url") for p in best["points"]}
    seen_titles = {normalize(p.get("text_en", ""))[:80] for p in best["points"]}
    for it in cluster["items"]:
        key = normalize(it["title"])[:80]
        if it["link"] in seen_urls or key in seen_titles:
            continue
        best["points"].append({
            "date": to_ist(it["published"]).strftime("%Y-%m-%d"),
            "time_ist": to_ist(it["published"]).strftime("%H:%M"),
            "iso": it["published"].isoformat(),
            "text_en": it["title"],
            "source": it["source"],
            "url": it["link"],
        })
        seen_urls.add(it["link"])
        seen_titles.add(key)
    # Chronological history, oldest first.
    best["points"].sort(key=lambda p: p.get("iso", ""))
    return best


def prune_archive(archive: dict, now: datetime) -> None:
    """Drop development points older than ARCHIVE_DAYS, then drop stories with no recent activity
    or no points. Run ONCE, after every cluster has been ingested."""
    for story in archive["stories"]:
        story["points"] = [p for p in story.get("points", [])
                           if _days_ago(p.get("iso", ""), now) <= ARCHIVE_DAYS]
    archive["stories"] = [s for s in archive["stories"]
                          if _days_ago(s.get("last_seen", ""), now) <= ARCHIVE_DAYS
                          and s.get("points")]


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


# --------------------------------------------------------------------------- #
# Manual override — force a chosen story to lead
# --------------------------------------------------------------------------- #
def load_override() -> dict:
    """A manual pin that forces a chosen story to lead — by keywords (`query`) or an explicit
    `url`+`headline`. Read from breaking/data/override.json, with the FORCE_QUERY/FORCE_URL/
    FORCE_HEADLINE env vars (workflow inputs) taking precedence. Returns {} when absent,
    empty, past its optional `expires`, or not actionable."""
    ov: dict = {}
    if OVERRIDE_PATH.exists():
        try:
            ov = json.loads(OVERRIDE_PATH.read_text() or "{}") or {}
        except Exception as exc:
            print(f"  ! override.json unreadable, ignoring: {exc}", file=sys.stderr)
            ov = {}

    env_query = os.environ.get("FORCE_QUERY", "").strip()
    env_url = os.environ.get("FORCE_URL", "").strip()
    env_headline = os.environ.get("FORCE_HEADLINE", "").strip()
    if env_query:
        ov = {**ov, "query": env_query}
    if env_url:
        ov = {**ov, "url": env_url}
    if env_headline:
        ov = {**ov, "headline": env_headline}
    if not ov:
        return {}

    expires = ov.get("expires")
    if expires:
        try:
            if now_utc() >= datetime.fromisoformat(str(expires).replace("Z", "+00:00")):
                print("  override present but expired; ignoring.")
                return {}
        except Exception:
            pass  # unparseable expiry -> treat the pin as still active

    if ov.get("query") or (ov.get("url") and ov.get("headline")):
        return ov
    print("  override present but not actionable (needs `query` or `url`+`headline`); ignoring.",
          file=sys.stderr)
    return {}


def _merge_items(items: list[dict], extra: list[dict]) -> list[dict]:
    """Fold an extra targeted feed into the main item list, deduping by title (newest wins)."""
    seen = {normalize(i["title"])[:80]: i for i in items if normalize(i["title"])[:80]}
    for it in extra:
        key = normalize(it["title"])[:80]
        if not key:
            continue
        prev = seen.get(key)
        if prev is None or it["published"] > prev["published"]:
            seen[key] = it
    merged = list(seen.values())
    merged.sort(key=lambda x: x["published"], reverse=True)
    return merged


def _build_cluster(items: list[dict]) -> dict:
    """Assemble a cluster dict (same shape cluster_items produces) from a list of items."""
    kw: set[str] = set()
    for it in items:
        kw |= keywords(it["title"] + " " + it.get("summary", ""))
    cl = {"items": items, "keywords": kw, "headline": items[0]["title"]}
    cl["severity"] = max(
        (severity_of(i["title"] + " " + i.get("summary", "")) for i in items),
        key=severity_rank,
    )
    cl["police_flag"] = is_police_misconduct(cl)
    cl["issue_rank"] = issue_rank(cl)
    cl["ceremonial"] = is_ceremonial(cl)
    cl["fresh"] = True  # a manual pin is always eligible to lead
    cl["score"] = 1e6  # pinned to the front regardless of the usual newsworthiness score
    return cl


def _force_lead(clusters: list[dict], items: list[dict], ov: dict) -> list[dict]:
    """Return clusters reordered so the pinned story leads. `query` promotes the best-matching
    cluster (pulling an extra targeted feed so it still gets real multi-source coverage);
    `url`+`headline` injects a synthetic one-item cluster."""
    query = (ov.get("query") or "").strip()
    if query:
        merged = _merge_items(items, fetch_feed(query))
        clusters = cluster_items(merged)
        qkw = keywords(query)
        qnorm = normalize(query)
        best_i, best_sim = 0, -1.0
        for i, cl in enumerate(clusters):
            sim = jaccard(qkw, cl["keywords"])
            if qnorm and qnorm in normalize(cl["headline"]):
                sim += 0.5  # nudge an exact phrase match ahead of a loose keyword overlap
            if sim > best_sim:
                best_i, best_sim = i, sim
        if clusters and best_sim > 0:
            clusters.insert(0, clusters.pop(best_i))
        print(f"  override: pinned lead by query {query!r} (match={best_sim:.2f})")
        return clusters

    url = (ov.get("url") or "").strip()
    headline = (ov.get("headline") or "").strip()
    if url and headline:
        item = {
            "title": headline,
            "link": url,
            "source": (ov.get("source") or "").strip(),
            "published": now_utc(),
            "summary": (ov.get("summary") or "").strip(),
        }
        forced = _build_cluster([item])
        # Drop any existing cluster that is really the same story, then lead with the pin.
        clusters = [c for c in clusters if jaccard(forced["keywords"], c["keywords"]) < 0.6]
        clusters.insert(0, forced)
        print(f"  override: pinned manual lead {headline!r}")
        return clusters

    return clusters


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
    clusters = filter_local(clusters)  # Jaipur-city only — drop national/out-of-area stories
    print(f"  {len(clusters)} Jaipur-local cluster(s) after locality gate")

    # A manual pin (override.json / FORCE_* inputs) can force a chosen story to lead.
    override = load_override()
    if override:
        clusters = _force_lead(clusters, items, override)
        # Honour the explicit pin as the lead, but keep the secondary pool Jaipur-local.
        if clusters:
            clusters = [clusters[0]] + filter_local(clusters[1:])
    else:
        # Burning-issue-first lead policy: the disorder/accountability/serious story of the day
        # leads; ceremonial/feel-good items are kept out of the lead unless nothing else qualifies.
        clusters = apply_lead_policy(clusters)
    if not clusters:
        print("No clusters to render; keeping existing page (no commit).")
        return

    top = clusters[0]
    # Never headline a stale item as "breaking now": if the auto-picked lead isn't fresh (e.g. only
    # the wider-window backfill produced clusters), keep the last good page. Manual pins are exempt.
    if not override and not top.get("fresh", True):
        print("No fresh Jaipur-local lead; keeping existing page (no commit).")
        return

    feed_hash = hashlib.sha1(
        "|".join(normalize(i["title"]) for i in top["items"]).encode()
    ).hexdigest()[:16]

    # Skip when the top-story feed is unchanged AND already rendered by this output
    # version (no Groq call, no commit). A RENDER_VERSION bump forces a one-time re-render.
    # An active override always re-renders so the pinned story takes effect immediately.
    feed_changed = feed_hash != state.get("last_feed_hash")
    up_to_date = state.get("render_version") == RENDER_VERSION
    if (OUT_HTML.exists() and state.get("lead") and not feed_changed
            and up_to_date and not override):
        print("No change in top-story feed; skipping update (no commit).")
        return

    # Accumulate EVERY local story's multi-day history — not just the lead — so ongoing stories keep
    # building their arc even on days they aren't the headline, and the wider-window backfill seeds
    # the weeks that preceded today. Then narrate the lead's full arc.
    archive = load_archive()
    story = ingest_cluster(archive, top, now)   # the lead — capture its arc for the AI
    for cl in clusters[1:]:
        ingest_cluster(archive, cl, now)
    prune_archive(archive, now)
    # Down-sample a weeks-long arc so the timeline still spans शुरुआत → अब within the TPM budget.
    arc = _arc_sample(story.get("points", []), TIMELINE_MAX)
    print(f"  archive: {len(archive['stories'])} story(ies); lead carries "
          f"{len(story['points'])} dated point(s) (≤{ARCHIVE_DAYS}d), narrating {len(arc)}")

    lead = None
    other_stories: list[dict] = []
    if use_ai:
        print("Asking Groq for analysis...")
        ai = groq_analyze(api_key, clusters, arc)
        if ai:
            lead, other_stories = _lead_from_ai(ai, clusters)

    if lead is None:
        # No AI / Groq failed: a clean Hindi holding page — never English feed text.
        print("Groq unavailable — rendering Hindi holding page.")
        lead, other_stories = _holding_lead(), []

    # Stamp the timeline with real archived date+time where the mapping is unambiguous.
    attach_dev_times(lead, arc)

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
    save_archive(archive)
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

    key_facts = [str(f).strip() for f in (ai.get("key_facts") or []) if str(f).strip()][:8]
    police = (ai.get("police_accountability") or "").strip()
    what_next = (ai.get("what_next") or "").strip()

    developments = []
    for d in (ai.get("developments") or [])[:TIMELINE_MAX]:
        if isinstance(d, dict):
            text = (d.get("text") or "").strip()
            label = (d.get("date_label") or d.get("label") or "").strip()
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
        "key_facts": key_facts,
        "police_accountability": police,
        "what_next": what_next,
        "developments": developments,
        "sources": sources,
    }

    others = order_secondary(clusters)  # police-misconduct stories first
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


def _hindi_point_label(point: dict) -> str:
    """Compact Hindi date+time (IST) for one archived development point, e.g.
    '13 जुलाई, दोपहर 4:06 बजे'. Uses the stored `iso`; falls back to `date`+`time_ist`.
    Returns "" only when the point carries no usable timestamp."""
    iso = point.get("iso")
    if iso:
        try:
            d = to_ist(datetime.fromisoformat(iso))
            return f"{d.day} {HINDI_MONTHS[d.month - 1]}, {_hindi_clock(d)}"
        except Exception:
            pass
    date, time_ist = point.get("date", ""), point.get("time_ist", "")
    try:
        y, m, day = (int(x) for x in date.split("-"))
    except Exception:
        return date or ""
    label = f"{day} {HINDI_MONTHS[m - 1]}"
    try:
        hh, mm = (int(x) for x in time_ist.split(":"))
        return f"{label}, {_hindi_clock(datetime(y, m, day, hh, mm))}"
    except Exception:
        return label


def _arc_sample(points: list[dict], n: int) -> list[dict]:
    """Down-sample a long point list to <=n items while keeping chronological order and always the
    first and last points, so a weeks-long arc narrated to the AI still spans शुरुआत → अब."""
    if len(points) <= n or n <= 0:
        return points
    step = (len(points) - 1) / (n - 1)
    idxs = sorted({round(i * step) for i in range(n)})  # includes 0 and len-1 (first & last)
    return [points[i] for i in idxs]


def attach_dev_times(lead: dict, points: list[dict]) -> None:
    """Give each timeline development a real archived timestamp when the mapping is unambiguous. The
    AI narrates the (down-sampled) arc oldest-first, one development per point, so when the counts
    match we align by index and use the point's exact IST date+time (never fabricated). Otherwise
    the AI's own `date_label` is left untouched."""
    devs = lead.get("developments") or []
    if devs and len(devs) == len(points):
        for dev, point in zip(devs, points):
            label = _hindi_point_label(point)
            if label:
                dev["label"] = label


BRAND_SUFFIX = "ब्रेकिंग जयपुर न्यूज़"


def render(state: dict, now: datetime) -> None:
    lead = state.get("lead") or {}
    other_stories = state.get("other_stories") or []
    updated_ist = _hindi_datetime(now)

    sev = lead.get("severity", "low")
    headline = lead.get("headline") or "जयपुर: ताज़ा खबरें अपडेट हो रही हैं"
    headline_html = esc(headline)
    title = (f"{headline} | {BRAND_SUFFIX}" if lead.get("headline")
             else f"{BRAND_SUFFIX} — लाइव अपडेट | जयपुर न्यूज़")

    paras = [p.strip() for p in (lead.get("analysis") or "").split("\n\n") if p.strip()]
    analysis_html = "\n        ".join(
        f"<p>{esc(p)}</p>" for p in paras
    ) or "<p>खबर विकसित हो रही है।</p>"

    # मुख्य तथ्य (key facts)
    key_facts = lead.get("key_facts", [])
    if key_facts:
        kf = "\n          ".join(f"<li>{esc(f)}</li>" for f in key_facts)
        key_facts_html = (
            '<section class="feed">\n'
            '        <div class="section-head"><h2>मुख्य तथ्य</h2></div>\n'
            f'        <ul class="facts">\n          {kf}\n        </ul>\n'
            '      </section>'
        )
    else:
        key_facts_html = ""

    # पुलिस की जवाबदेही (police accountability) — accent-styled, shown only when sourced.
    police = (lead.get("police_accountability") or "").strip()
    if police:
        pp = "\n        ".join(
            f"<p>{esc(p)}</p>" for p in police.split("\n\n") if p.strip()
        )
        police_html = (
            '<section class="accountability fade-in">\n'
            '        <h2>पुलिस की जवाबदेही</h2>\n'
            f'        {pp}\n'
            '      </section>'
        )
    else:
        police_html = ""

    # आगे क्या (what next)
    what_next = (lead.get("what_next") or "").strip()
    what_next_html = (
        '<section class="feed">\n'
        '        <div class="section-head"><h2>आगे क्या</h2></div>\n'
        f'        <p class="whatnext">{esc(what_next)}</p>\n'
        '      </section>'
    ) if what_next else ""

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
        # --i carries the item's index so the CSS can stagger each item's reveal animation.
        timeline_html = "\n".join(
            f'<li class="tl-item sev-{esc(sev)}" style="--i:{i}">'
            + (f'<time>{esc(d.get("label"))}</time>' if d.get("label") else "")
            + f'<p>{esc(d.get("text"))}</p></li>'
            for i, d in enumerate(developments)
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
            '        <div class="section-head"><h2>यह भी ब्रेकिंग</h2></div>\n'
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
        "{{HEADLINE}}": headline_html,
        "{{ANALYSIS}}": analysis_html,
        "{{KEY_FACTS}}": key_facts_html,
        "{{POLICE}}": police_html,
        "{{WHAT_NEXT}}": what_next_html,
        "{{SOURCES}}": source_items,
        "{{TIMELINE}}": timeline_html,
        "{{OTHER_STORIES}}": other_section,
        "{{UPDATED_IST}}": esc(updated_ist),
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
  /* animations are off here, so make sure the reveal-animated timeline stays visible */
  .tl-item { opacity: 1 !important; transform: none !important; }
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
/* Meta strip under the red header: अंतिम अपडेट · रिफ्रेश · brand link, above the headline. */
.livebar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 6px 0 8px; }
.livebar .brand-link { margin-left: auto; color: var(--brand); font-weight: 800; font-size: .9rem; }
.updated { font-size: .82rem; color: var(--muted); }
.editorial-card.fade-in { margin-bottom: 26px; }
ul.timeline { list-style: none; margin: 0; padding: 0; max-width: 760px; }
.tl-item {
  position: relative; padding: 0 0 18px 22px; border-left: 2px solid var(--border);
  /* each item reveals with a staggered fade-in-up (delay from the inline --i) */
  opacity: 0; animation: tl-reveal .5s ease both; animation-delay: calc(var(--i, 0) * 0.12s);
}
.tl-item:last-child { padding-bottom: 0; }
.tl-item::before {
  content: ""; position: absolute; left: -7px; top: 3px;
  width: 12px; height: 12px; border-radius: 50%;
  background: var(--accent); border: 2px solid var(--surface);
}
.tl-item.sev-critical::before { background: var(--brand); }
.tl-item.sev-high::before { background: #e65100; }
/* newest development (bottom of the chain) = the "live/developing" step — glowing pulse */
.tl-item:last-child::before {
  background: var(--brand); animation: tl-glow 1.3s ease-in-out infinite;
}
@keyframes tl-reveal { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes tl-glow {
  0%, 100% { box-shadow: 0 0 0 0 rgba(183,28,28,.55), 0 0 4px var(--brand); }
  50%      { box-shadow: 0 0 0 5px rgba(183,28,28,0), 0 0 16px var(--brand); }
}
.tl-item time { display: block; font-size: .74rem; font-weight: 800; color: var(--brand); letter-spacing: .03em; }
.tl-item p { margin: 3px 0 0; }
.note { font-size: .8rem; color: var(--muted); margin-top: 14px; }
footer { max-width: var(--maxw); margin: 0 auto; padding: 0 16px 48px; font-size: .82rem; color: var(--muted); }
footer a { color: var(--brand); font-weight: 600; }
@media (max-width: 560px) {
  .date-strip > span:not(.live-pill) { display: none; }
}

/* Red LIVE breaking banner — this is the page header (full-width, scrolls with the page) */
.breaking-banner {
  background: linear-gradient(90deg, #e0112b, #b71c1c);
  color: #fff; box-shadow: var(--shadow);
  border-bottom: 3px solid var(--accent);
}
.breaking-banner .bn-inner {
  max-width: var(--maxw); margin: 0 auto; padding: 12px 16px;
  display: flex; align-items: center; gap: 10px 12px; flex-wrap: wrap;
  font-weight: 800; letter-spacing: .03em;
}
.breaking-banner .live-chip {
  display: inline-flex; align-items: center; gap: 7px;
  background: #fff; color: #b71c1c; padding: 4px 10px; border-radius: 5px;
  font-size: .72rem; font-weight: 900; text-transform: uppercase;
  animation: blink 1s steps(1) infinite; flex: 0 0 auto;
}
.breaking-banner .live-chip .ping {
  width: 8px; height: 8px; border-radius: 50%; background: #b71c1c;
  animation: pulse 1.2s infinite;
}
.breaking-banner .bn-label {
  font-size: .98rem; text-transform: uppercase; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
@keyframes blink { 50% { opacity: .4; } }

/* Key facts list */
ul.facts { margin: 0; padding-left: 20px; max-width: 760px; }
ul.facts li { margin: 0 0 8px; line-height: 1.6; }

/* Police accountability — accent-bordered card, only shown when sourced */
.accountability {
  background: var(--surface); border: 1px solid var(--border);
  border-left: 5px solid var(--brand); border-radius: var(--radius);
  padding: 16px 20px; margin-bottom: 32px; box-shadow: var(--shadow);
}
.accountability h2 { margin: 0 0 8px; font-size: 1.1rem; font-weight: 800; color: var(--brand); }
.accountability p { margin: 0 0 10px; line-height: 1.7; }
.accountability p:last-child { margin-bottom: 0; }
.whatnext { max-width: 760px; line-height: 1.7; }
</style>

    <script type="application/ld+json">
{{LDJSON}}
    </script>
</head>
<body>
    <header class="breaking-banner">
      <div class="bn-inner">
        <span class="live-chip"><span class="ping"></span>लाइव</span>
        <span class="bn-label">लाइव ब्रेकिंग न्यूज़</span>
      </div>
    </header>

    <main>
      <div class="livebar">
        <span class="updated">अंतिम अपडेट {{UPDATED_IST}}</span>
        <button class="refresh-btn" type="button" onclick="location.reload()" aria-label="रिफ्रेश">&#8635; रिफ्रेश</button>
        <a class="brand-link" href="https://news.manzill.com">जयपुर न्यूज़</a>
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

      {{KEY_FACTS}}

      {{POLICE}}

      {{WHAT_NEXT}}

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
