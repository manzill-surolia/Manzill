#!/usr/bin/env python3
"""Generate www.manzill.com/breaking — a live, AI-authored, single-story breaking-news page
for JAIPUR: the city's top breaking and political news, one story at a time.

Pipeline: fetch Google News RSS (Jaipur breaking + political queries) -> drop digest/roundup items
-> cluster and keep Jaipur stories -> pick a single fresh lead (the strongest fresh Jaipur story) ->
search the web for RELATED coverage of that one story and fold it in (enrich_lead) -> archive every
story's multi-day arc (rolling 30 days) -> ask Groq (OpenAI-compatible) for a Hindi write-up with a
rich, timestamped, sourced timeline -> render breaking/index.html (+ RSS + news sitemap) and persist
breaking/data/{state,archive}.json. On a day with no fresh story the last page is kept.

Runs from GitHub Actions on a ~20 min cron. No server, no secrets in the page:
the Groq key is read from the GROQ_API_KEY environment variable only.

Usage:
    python breaking/build_breaking_news.py            # full run (needs GROQ_API_KEY)
    python breaking/build_breaking_news.py --no-ai    # render from feeds only
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
RENDER_VERSION = "23"

# --- Groq TPM budget ------------------------------------------------------- #
# Groq bills prompt_tokens + max_tokens against a per-minute cap; exceeding it returns HTTP 413 and
# the page falls back to the empty Hindi holding scaffold. The prompt is Devanagari-heavy (Hindi
# tokenizes expensively), so a growing prompt silently drifts over the cap. Check any prompt change
# with `python breaking/check_tpm.py` (offline estimate) or `--api` (Groq's exact prompt_tokens).
GROQ_TPM_LIMIT = 8000        # the account tier's tokens-per-minute cap
GROQ_MAX_TOKENS = 4500       # output cap; prompt + this must stay < GROQ_TPM_LIMIT (was 5200 → 413)
TPM_BUDGET = 7000            # design ceiling for (est. prompt + max_tokens); ~1000 tokens of margin

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
    # Rajasthan-desk + wire + regional outlets that show up in the corruption feeds — added so the
    # source cards carry a real outlet name instead of blanking to "ताज़ा रिपोर्ट".
    "the new indian express": "द न्यू इंडियन एक्सप्रेस", "new indian express": "द न्यू इंडियन एक्सप्रेस",
    "udaipurtimes.com": "उदयपुर टाइम्स", "udaipurtimes": "उदयपुर टाइम्स", "udaipur times": "उदयपुर टाइम्स",
    "punjab kesari english": "पंजाब केसरी", "punjab kesari": "पंजाब केसरी",
    "zoomnews.in": "ज़ूम न्यूज़", "zoom news": "ज़ूम न्यूज़",
    "etv bharat": "ईटीवी भारत", "the news mill": "द न्यूज़ मिल", "edexlive": "एडेक्सलाइव",
    "india's news.net": "इंडियाज़ न्यूज़", "india news": "इंडिया न्यूज़",
    "news18 rajasthan": "न्यूज़18 राजस्थान", "news18 hindi": "न्यूज़18 हिंदी",
    "dna": "डीएनए", "dna india": "डीएनए", "ians": "आईएएनएस", "ani": "एएनआई", "pti": "पीटीआई",
    "the statesman": "द स्टेट्समैन", "national herald": "नेशनल हेराल्ड", "the tribune": "द ट्रिब्यून",
    "tribuneindia": "द ट्रिब्यून", "sambad english": "सम्बाद", "daily pioneer": "द पायनियर",
    "the pioneer": "द पायनियर", "catch news": "कैच न्यूज़", "the logical indian": "द लॉजिकल इंडियन",
    "jaipur stuff": "जयपुर स्टफ", "first india news": "फर्स्ट इंडिया", "first india": "फर्स्ट इंडिया",
}

# English acronyms / org names → their conventional Hindi form. The page is Devanagari-only, so the
# to_hindi() sanitizer rewrites any of these that slip through the AI's output. Well-known orgs get
# their full Hindi name; the rest are transliterated (see to_hindi). Keys are lowercased; matching is
# whole-token (Latin-boundary) so digits/dates are never touched. Curated to avoid ambiguous
# two-letter English words.
ORG_HI = {
    "jda": "जयपुर विकास प्राधिकरण", "jmc": "जयपुर नगर निगम", "jdc": "जयपुर विकास प्राधिकरण",
    "bjp": "भाजपा", "congress": "कांग्रेस", "inc": "कांग्रेस", "aap": "आम आदमी पार्टी",
    "ed": "ईडी", "acb": "एसीबी", "cbi": "सीबीआई", "eow": "आर्थिक अपराध शाखा",
    "rghs": "आरजीएचएस", "rpsc": "आरपीएससी", "reet": "रीट", "neet": "नीट", "gst": "जीएसटी",
    "fir": "एफआईआर", "rti": "आरटीआई", "pil": "जनहित याचिका", "ncr": "एनसीआर",
    "ips": "आईपीएस", "ias": "आईएएस", "ras": "आरएएस", "sho": "एसएचओ", "dsp": "डीएसपी",
    "sdm": "एसडीएम", "sp": "एसपी", "dm": "डीएम", "cm": "मुख्यमंत्री", "pm": "प्रधानमंत्री",
    "mla": "विधायक", "acp": "एसीपी", "ssp": "एसएसपी", "adg": "एडीजी", "dgp": "डीजीपी",
}

# --------------------------------------------------------------------------- #
# Feeds & scoring
# --------------------------------------------------------------------------- #
GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
# The beat is JAIPUR: the city's top BREAKING and POLITICAL news. Queries are anchored to Jaipur so
# the feed stays local; the Jaipur locality gate (is_local) is the real safeguard. There is no
# topic filter — any genuine Jaipur story (crime, accident, civic, politics, government) is in scope;
# ranking (severity, issue strength, sources, recency) decides which one leads.
FEED_QUERIES = [
    # Broad Jaipur catch — the day's top developing stories across the city.
    "Jaipur (breaking OR news OR incident OR update OR alert) when:1d",
    # Jaipur crime, accidents & emergencies.
    "Jaipur (crime OR murder OR robbery OR accident OR fire OR blast OR encounter OR arrest "
    "OR clash OR rape OR kidnap OR raid) when:2d",
    # Jaipur politics — parties, leaders, civic body, elections.
    "Jaipur (BJP OR Congress OR MLA OR minister OR politics OR election OR mayor OR councillor "
    "OR \"nagar nigam\" OR JDA OR party) when:2d",
    # Rajasthan state politics seated in Jaipur — assembly, CM, cabinet, secretariat.
    "Jaipur (assembly OR \"vidhan sabha\" OR cabinet OR \"chief minister\" OR government OR governor "
    "OR bill OR session OR protest) when:2d",
    # Jaipur civic life, infrastructure & public affairs.
    "Jaipur (civic OR traffic OR water OR power OR metro OR road OR development OR weather "
    "OR heritage OR health OR school) when:2d",
    # Widest Jaipur net so a big Jaipur story is never missed by the narrower queries.
    "Jaipur news when:1d",
]

# Wider-window BACKFILL queries. Items from these seed a story's multi-week timeline (the archive)
# but are tagged archival and — being older than FRESH_LEAD_HOURS — can never become the "breaking"
# lead or a visible "यह भी ब्रेकिंग" card. They exist so a story that breaks today can show the
# weeks of prior coverage (background, earlier developments, court/probe steps) that preceded it.
ARCHIVAL_QUERIES = [
    "Jaipur (news OR incident OR case OR probe OR investigation OR court OR development) when:14d",
    "Jaipur (politics OR government OR BJP OR Congress OR MLA OR minister OR assembly "
    "OR election OR JDA OR \"nagar nigam\") when:30d",
]

# --------------------------------------------------------------------------- #
# Rajasthan locality gate (Jaipur-first)
# --------------------------------------------------------------------------- #
# Coverage is Rajasthan-wide but Jaipur-first: a story is kept only if it mentions Rajasthan,
# Jaipur, or a well-known Rajasthan city/district (is_local). Without this gate a national item
# that leaks into a broad feed query can outscore every state story and lead the page. Jaipur
# clusters additionally pass is_jaipur() and get a small ranking boost so Jaipur leads whenever a
# Jaipur story is available. Single-word tokens are matched against the item's token set (so
# "camera" never matches "amer"); multi-word phrases are matched as substrings. `normalize()`
# keeps "jaipur"/"rajasthan" — they are stripped only for clustering (STOPWORDS), not the raw text.
JAIPUR_TERMS = {
    "jaipur", "jaipurite", "jaipurites", "sanganer", "sitapura", "jhotwara",
    "mansarovar", "vidyadhar", "amer", "amber", "chomu", "bagru", "chaksu",
    "shahpura", "kotputli",
}
JAIPUR_PHRASES = (
    "pink city", "walled city", "malviya nagar", "vaishali nagar",
    "tonk road", "sindhi camp", "jln marg", "bani park",
)
# Rest of Rajasthan — major cities/districts + the state/agency names. A story anywhere here is
# in-coverage; is_jaipur() (JAIPUR_TERMS/PHRASES) decides the Jaipur-first ranking preference.
RAJASTHAN_TERMS = {
    "rajasthan", "acb", "jodhpur", "udaipur", "kota", "ajmer", "bikaner", "alwar",
    "bharatpur", "sikar", "pali", "nagaur", "churu", "jhunjhunu", "jhunjhunun",
    "barmer", "jaisalmer", "banswara", "bhilwara", "chittorgarh", "chittaurgarh",
    "dausa", "dholpur", "hanumangarh", "jalore", "jhalawar", "karauli", "sirohi",
    "tonk", "bundi", "baran", "dungarpur", "pratapgarh", "rajsamand", "sawai",
    "madhopur", "ganganagar", "sriganganagar", "beawar", "kishangarh", "neemrana",
}
RAJASTHAN_PHRASES = (
    "sawai madhopur", "sri ganganagar", "anti-corruption bureau", "anti corruption bureau",
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

# --------------------------------------------------------------------------- #
# Policy-incompetence / bribery beat gate (the page's editorial focus)
# --------------------------------------------------------------------------- #
# The page leads ONLY with government/police bribery or policy-incompetence stories. A cluster
# passes the gate (is_policy_beat) when it carries a bribery/corruption signal (BRIBE_TERMS) OR a
# governance failure signal (ISSUE_KEYWORDS["governance"], which already covers scam/negligence/
# policy failure/dereliction/…). Purely lexical, like severity_of — it never invents wrongdoing;
# it only lets a real, sourced bribery/misgovernance story lead. Tune this list freely (config).
BRIBE_TERMS = [
    "bribe", "bribery", "bribes", "rishwat", "acb", "anti-corruption", "anti corruption",
    "corrupt", "corruption", "kickback", "graft", "extortion", "extort", "embezzl",
    "misappropriat", "disproportionate assets", "trap case", "caught taking",
    "demanding money", "illegal gratification", "ghoos", "ghus",
]

# Neutral government/police ACTION words (the state DOING ITS JOB: clearing illegal builds, evicting,
# raiding, seizing). Retained as a ranking hint for has_failure_angle/questions_authority, which still
# front-load accountability stories within the secondary pool; they no longer gate what may lead.
NEUTRAL_ACTION_TERMS = {
    "encroachment", "illegal", "flouting", "violation", "bulldozer", "demolition",
    "demolished", "demolish", "eviction", "evicted", "raid", "raided", "seized", "seizure", "razed",
}
# Genuine government/police accountability-FAILURE signals: the governance/disorder failure words
# (minus the neutral-action ones above) + every bribery term + explicit negligence/delay/citizen-harm
# words. has_failure_angle() requires one of these so a "govt did its job" story can't lead.
FAILURE_TERMS = (
    [t for t in ISSUE_KEYWORDS["governance"] if t not in NEUTRAL_ACTION_TERMS]
    + ISSUE_KEYWORDS["disorder"]
    + BRIBE_TERMS
    + [
        "delay", "delayed", "pending", "stalled", "stall", "ignored", "unheeded",
        "no compensation", "without compensation", "no rehabilitation", "unpaid", "denied",
        "victim", "displaced", "homeless", "rendered homeless", "protest", "outcry", "suffer",
        "death", "deaths", "died", "killed", "without notice", "no notice", "arbitrary",
    ]
)

# Ceremonial / feel-good news that must not lead the "breaking" slot (see is_ceremonial &
# apply_lead). Only demoted when the cluster carries no serious severity and no issue
# signal — a stampede or death *at* a procession is never treated as ceremonial.
CEREMONIAL_KEYWORDS = [
    "yatra", "rath", "procession", "shobha", "festival", "mela", "fair ", "celebration",
    "celebrat", "tradition", "heritage", "inaugurat", "foundation stone", "felicitat",
    "cultural", "jubilee", "anniversary", "devotees", "pilgrim", "temple event", "utsav",
    "mahotsav", "ribbon", "launch event", "felicitation",
]

# Digest / roundup articles ("Jaipur top news today: A, B and C") bundle several unrelated stories
# into one item. Left in, they cluster as a strong "lead" and the AI reproduces the bundle — which is
# exactly the merged-headline failure this page must avoid. Any item whose title matches one of these
# markers is dropped in gather_items() so it can never seed a cluster or contaminate keywords. The
# page is single-story: one incident per page. Matched as substrings against the normalized title.
ROUNDUP_MARKERS = [
    "top news", "top stories", "top 10", "top 5", "top ten", "top five", "news roundup",
    "round up", "roundup", "in brief", "news brief", "briefs", "morning headlines",
    "evening headlines", "headlines today", "today headlines", "top headlines", "news wrap",
    "wrap up", "newswrap", "bulletin", "news bulletin", "aaj ki badi khabar", "aaj ki pramukh",
    "badi khabren", "badi khabar", "mukhya samachar", "surkhiyan", "surkhiyaan",
    "din bhar", "day in pics", "weekly wrap", "recap", "at a glance", "key events",
    # "News Today / आज की खबर" digest family — the LatestLY-style "<City> News Today, <date>: A, B and
    # C" bundles that slipped through in the first run and became the lead. Strong digest signals.
    "news today", "today news", "aaj ki khabar", "aaj ka samachar", "aaj ki taaza khabar",
    "aaj ki taza khabar", "taaza khabar", "taza khabar", "news update", "morning news",
    "evening news", "day in pictures", "live updates", "live news",
]

# Digest lead-in words: when one of these appears BEFORE a colon whose tail is a comma-list of
# several topics ("<City> News Today, July 18: wall collapse, murder case and hospital"), the item is
# a multi-story digest even if its exact phrasing isn't in ROUNDUP_MARKERS. See is_roundup().
_ROUNDUP_LEADIN = ("news", "today", "khabar", "samachar", "roundup", "headline", "bulletin", "wrap")

# Scoring weights (tunable). Recency is deliberately no longer the heaviest term — a burning
# issue must outrank a merely-fresh ceremonial item. See cluster_items().
W_ISSUE = 4.0             # weight on issue_rank (0-3) — the accountability boost
W_RECENCY = 3.0           # freshness tiebreaker; raised (was 2.0) so a fresher story overtakes a
                         # day-old one of similar strength instead of the stale lead sticking
W_SOURCES = 2.0           # weight per distinct source (capped at 6); raised (was 1.0) so a concrete,
                         # multi-outlet story outranks a thin single-source scoop (which also freezes
                         # the page — its unchanged title set never trips the feed-hash re-render)
W_JAIPUR = 3.0            # Jaipur-first (soft): a Jaipur story usually leads, but a clearly bigger
                         # Rajasthan story (high issue_rank, many sources) can still overtake it
CEREMONIAL_PENALTY = 4.0  # subtracted from a ceremonial cluster's score
# A cluster may LEAD ("breaking now") only if its newest item is this fresh. Older clusters
# (e.g. pulled by the wider-window backfill queries) still seed the archive/timeline but are
# never presented as breaking. Kept moderate (was 36) so a day-old one-off story ages out of the
# lead by the next day and a fresher story takes over. See cluster_items()/apply_lead().
FRESH_LEAD_HOURS = 20.0

# The feed-hash skip (see build()) avoids a Groq call + commit when the lead's coverage is
# unchanged. But a single-source lead's title set never changes, so without an upper bound the page
# — and its "अंतिम अपडेट" stamp — can sit frozen for the whole FRESH_LEAD_HOURS window. This caps
# that: once the last render is older than MAX_STALE_HOURS the run proceeds anyway (re-ranks the
# lead, re-runs enrichment, refreshes the timestamp). Runs are hours apart, so it stays TPM-safe.
MAX_STALE_HOURS = 3.0

# Floor on how many timeline steps the "घटनाक्रम" must show. A genuinely single-source breaking
# story has only one dated point; ensure_timeline_depth() then narrates a relative-labelled arc from
# the already-sourced key_facts/what_next so the timeline is never a lone entry (no fabricated times).
MIN_TIMELINE_STEPS = 4

# A lead needs at least this many dated points OF ITS OWN to earn a "घटनाक्रम — शुरुआत से अब तक"
# (a single story's chronology). A one-off lead (fewer points) instead shows the month's DIFFERENT
# Jaipur stories under "इस महीने". See build()'s timeline-mode pick.
SINGLE_CASE_MIN = 3

# Max timeline points NARRATED per story. A weeks-long arc is down-sampled to this many points
# (keeping the first and last, see _arc_sample) so the "घटनाक्रम" still spans शुरुआत → अब. Kept
# deliberately modest (was 30) so each development can be a rich 2-3 sentence, sourced entry instead
# of a one-liner while the single Groq pass stays within the 8000 TPM budget. The archive still
# stores ALL points for ARCHIVE_DAYS — this only limits how many are narrated in one render.
TIMELINE_MAX = 14

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


def is_roundup(item: dict) -> bool:
    """True if a feed item is a digest/roundup ("<City> News Today, <date>: A, B and C") that bundles
    several unrelated stories. Such items are dropped before clustering so the page stays single-story.

    Two detectors: (1) any ROUNDUP_MARKERS phrase in the normalized title; (2) a structural check on
    the RAW title — a digest lead-in word (news/today/khabar/…) before a colon whose tail is a
    comma-list of topics. The structural check catches new digest phrasings (e.g. the LatestLY
    "Jaipur News Today, July 18, 2026: wall collapse, murder case and hospital") that (1) would miss,
    while a plain single story ("ACB traps patwari: Rs 50,000 bribe recovered") has no lead-in word
    before its colon and is not flagged."""
    raw = item.get("title", "") or ""
    if any(m in normalize(raw) for m in ROUNDUP_MARKERS):
        return True
    head, sep, tail = raw.lower().partition(":")
    if sep and "," in tail and any(w in head for w in _ROUNDUP_LEADIN):
        return True
    return False


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


def is_policy_beat(cluster: dict) -> bool:
    """True if the cluster is a government/police BRIBERY or POLICY-INCOMPETENCE story — the only
    kind allowed to LEAD this page. Passes on a bribery/corruption signal (BRIBE_TERMS) or a
    governance-failure signal (ISSUE_KEYWORDS["governance"]: scam, negligence, dereliction, policy
    failure, embezzlement, …). Police-misconduct clusters (is_police_misconduct) also qualify, since
    police accountability is in scope. Purely lexical — it never invents wrongdoing."""
    text = " " + _cluster_text(cluster) + " "
    if any(t in text for t in BRIBE_TERMS):
        return True
    if any(t in text for t in ISSUE_KEYWORDS["governance"]):
        return True
    return bool(cluster.get("police_flag"))


def has_failure_angle(cluster: dict) -> bool:
    """True if the cluster carries a government/police accountability-FAILURE signal — negligence,
    delay, dereliction, breakdown, citizen harm, or police misconduct — as opposed to a merely
    neutral state action (a clean demolition/eviction/raid). Used by order_secondary/
    questions_authority to front-load accountability stories in the secondary pool. Purely lexical —
    it never invents wrongdoing."""
    text = " " + _cluster_text(cluster) + " "
    if any(t in text for t in FAILURE_TERMS):
        return True
    return bool(cluster.get("police_flag"))


def questions_authority(cluster: dict) -> bool:
    """True if the cluster puts an ACCOUNTABILITY SUBJECT (state government / JDA / municipal /
    minister / police / administration) UNDER QUESTION — it names such an authority AND carries a
    failure/accountability signal. This is the page's core editorial test: every lead should question
    the government/police, not merely report an incident. Purely lexical — never invents wrongdoing."""
    text = " " + _cluster_text(cluster) + " "
    names_authority = any(s in text for s in ACCOUNTABILITY_SUBJECTS) or bool(cluster.get("police_flag"))
    return names_authority and has_failure_angle(cluster)


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
        if is_roundup(it):
            return  # drop digest/roundup items — they merge unrelated stories into one headline
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
        cl["issue_rank"] = issue_rank(cl)          # burning-issue / public-interest strength (0-3)
        cl["policy_flag"] = is_policy_beat(cl)     # governance/accountability signal (ranking hint)
        cl["ceremonial"] = is_ceremonial(cl)       # feel-good item that must not lead
        cl["jaipur"] = is_jaipur(cl)               # Jaipur story (the locality gate; kept for scoring)
        cl["fresh"] = age_h <= FRESH_LEAD_HOURS    # lead/secondary eligibility (vs archive-only)
        # Newsworthiness score for a Jaipur breaking + political news page: importance (severity) and
        # public-interest strength (issue_rank) dominate; more sources adds weight; recency is a
        # tiebreaker; ceremonial/feel-good stories are penalised. apply_lead then picks the top fresh
        # non-ceremonial story as the lead; order_secondary fills "यह भी ब्रेकिंग".
        cl["score"] = (
            severity_rank(cl["severity"]) * 3.0
            + cl["issue_rank"] * W_ISSUE
            + min(len(cl["items"]), 6) * W_SOURCES
            + recency * W_RECENCY
            + (W_JAIPUR if cl["jaipur"] else 0.0)
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
    """The 'यह भी ब्रेकिंग' pool (max 5), excluding the lead — the day's other fresh Jaipur stories.
    Only fresh, non-ceremonial clusters show; feel-good/ceremonial items are kept out of the breaking
    slot. Higher-impact stories are front-loaded (police-accountability, then stories that name an
    authority alongside a failure signal), with the rest following by score. `clusters` is
    score-sorted, so ordering within each tier already reflects newsworthiness."""
    pool = [c for c in clusters[1:] if c.get("fresh", True) and not c.get("ceremonial")]
    police = [c for c in pool if c.get("police_flag")]
    authority = [c for c in pool if not c.get("police_flag") and questions_authority(c)]
    rest = [c for c in pool if not c.get("police_flag") and not questions_authority(c)]
    return (police + authority + rest)[:5]


def is_jaipur(cluster: dict) -> bool:
    """True if the cluster is about Jaipur city (or a known Jaipur locality). Used for the
    Jaipur-first ranking preference. Reads the raw item text — `jaipur` is only a clustering
    stopword, so it survives in `normalize()`."""
    text = " ".join(
        normalize(i["title"] + " " + i.get("summary", "")) for i in cluster["items"]
    )
    if set(text.split()) & JAIPUR_TERMS:
        return True
    return any(p in text for p in JAIPUR_PHRASES)


def is_local(cluster: dict) -> bool:
    """True if the cluster is a JAIPUR story. Coverage is Jaipur-only, so this is exactly
    is_jaipur() — a story must mention Jaipur (or a known Jaipur locality) to be in scope. The
    RAJASTHAN_TERMS/PHRASES lists are kept only to recognise the Jaipur-seated state capital in
    context; they no longer widen coverage to the rest of the state."""
    return is_jaipur(cluster)


def filter_local(clusters: list[dict]) -> list[dict]:
    """Drop every cluster that is not a Jaipur story (keeps order)."""
    return [c for c in clusters if is_local(c)]


def apply_lead(clusters: list[dict]) -> list[dict]:
    """Choose the lead (clusters[0]): the strongest FRESH, non-ceremonial Jaipur story of the day.

    - Only a FRESH cluster can lead — archive-only backfill items seed the timeline but are never
      presented as "breaking now".
    - `clusters` is already score-sorted (severity, issue strength, sources, recency, with a Jaipur
      boost baked in), so the top fresh, non-ceremonial cluster leads. Ceremonial / feel-good items
      are demoted so a festival photo-op never leads over real breaking news.
    - There is no topic filter: crime, an accident, a civic breakdown, or a political story can all
      lead — whichever is the biggest, freshest Jaipur story. On a day with no fresh story the list
      comes back empty and build() keeps the last page.

    Returns the clusters reordered with the chosen lead first, or [] when nothing fresh qualifies."""
    if not clusters:
        return []
    fresh = [c for c in clusters if c.get("fresh", True) and not c.get("ceremonial")]
    if not fresh:
        # Everything fresh is ceremonial (or nothing is fresh) — fall back to the best fresh cluster
        # of any kind so the page still leads with the day's top story rather than emptying.
        fresh = [c for c in clusters if c.get("fresh", True)]
    if not fresh:
        return []  # nothing fresh at all — build() keeps the last good page
    lead = fresh[0]
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


def estimate_tokens(text: str) -> int:
    """Conservative (over-)estimate of tokens for the o200k-family tokenizer gpt-oss uses. ASCII ≈ 1
    token / 3.5 chars; non-ASCII (Devanagari codepoints + combining marks) ≈ 1 token / 2 chars.
    Over-counting is deliberate — if this says a request FITS, the real call fits. For the exact count
    use `breaking/check_tpm.py --api` (Groq's usage.prompt_tokens). See the Groq TPM budget constants."""
    if not text:
        return 0
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return int(ascii_n / 3.5 + (len(text) - ascii_n) / 2.0)


def _messages_tokens(messages: list[dict]) -> int:
    """Estimated prompt tokens for a chat `messages` list (content + a small per-message overhead)."""
    return sum(estimate_tokens(m.get("content", "")) + 6 for m in messages) + 8


def _groq_messages(clusters: list[dict], points: list[dict], *,
                   snippets: int = 3, others: int = 4, sources: int = 5,
                   history_max: int | None = None, source_objs: list[dict] | None = None,
                   timeline_mode: str = "case") -> list[dict]:
    """Build the Groq chat `messages` (system + user JSON) for the lead's arc. The caps are parameters
    so groq_analyze's TPM preflight can shrink an over-budget request, and check_tpm.py can measure the
    exact request the generator sends. Kept lean — the prompt is Devanagari-heavy (see the TPM budget);
    the hard guarantees (Devanagari-only via to_hindi, accountability via questions_authority/
    enrich_lead) live in code, not in prompt verbosity."""
    lead = clusters[0]
    others_cl = order_secondary(clusters)[:max(others, 0)]
    # Prefer the clubbed-arc sources (the month's varied outlets) so the AI Hindi-titles the same
    # cards the page renders; fall back to the lead cluster's own sources.
    _src = source_objs if source_objs is not None else cluster_sources(lead, limit=sources)
    lead_sources = [s["title"] for s in _src[:max(sources, 6)]]
    lead_snippets = [i["summary"] for i in lead["items"] if i["summary"]][:max(snippets, 0)]
    pts = points if history_max is None else _arc_sample(points, history_max)
    history = [{"when": _hindi_point_label(p), "report": p.get("text_en", "")} for p in pts]

    if timeline_mode == "month":
        mode_hint = (
            "story_history इस महीने की कई अलग-अलग (असंबद्ध) जयपुर खबरों का संग्रह है — इन्हें एक ही घटना की "
            "कालक्रमिक कड़ी न बनाएँ; developments में हर चरण एक अलग खबर हो (एक-एक पंक्ति: क्या हुआ, कहाँ, "
            "किसने, क्या असर)। शीर्षक व पहला पैराग्राफ सबसे नई/बड़ी घटना को लीड बनाएँ। ")
    else:
        mode_hint = (
            "story_history इसी एक विकासशील खबर का सिलसिला है — developments उसी एक मामले का कालक्रमिक "
            "घटनाक्रम हों (पृष्ठभूमि → घटना → कार्रवाई → ताज़ा स्थिति)। शीर्षक व पहला पैराग्राफ सबसे नई "
            "घटना को लीड बनाएँ। ")

    system = (
        "आप जयपुर (राजस्थान, भारत) की एक हिंदी ब्रेकिंग-न्यूज़ वेबसाइट के वरिष्ठ समाचार संपादक हैं। यह पेज "
        "जयपुर की ताज़ा ब्रेकिंग व राजनीतिक खबरें कवर करता है (अपराध, दुर्घटना, नागरिक मुद्दे, प्रशासन, "
        "राजनीति)। शैली हार्ड ब्रेकिंग-न्यूज़ की हो, संपादकीय/राय नहीं: तथ्यात्मक, स्रोत-आधारित, संतुलित "
        "रिपोर्टिंग — सबसे नई/बड़ी घटना सबसे पहले (उल्टा पिरामिड)। कोई भी दावा, आरोप या माँग सम्बंधित पक्ष "
        "के हवाले से दें ('पुलिस के अनुसार', 'विपक्ष ने कहा', 'नागरिकों ने माँग की') — अपनी ओर से राय या "
        "उपदेश ('ऐसा होना चाहिए') कभी नहीं; किसी पक्ष की तरफ़दारी या निंदा नहीं। "
        + mode_hint +
        "developments = "
        "5-12 चरण: story_history का हर दिनांकित बिंदु (date_label = "
        "'when' से हूबहू) + तथ्यों से बने कथानक/घटनाक्रम-चरण। जहाँ पुष्ट तिथि हो वही दें, वरना "
        "सापेक्ष हिंदी लेबल (पृष्ठभूमि/घटना के बाद/जाँच के दौरान/अब तक/आगे) — मनगढ़ंत घड़ी-समय कभी नहीं। "
        "अपुष्ट बात का श्रेय असली प्राधिकरण/आउटलेट के हिंदी नाम से दें या कुछ नहीं। "
        "खबर सीधे पुलिस/प्रशासन की कार्रवाई या जवाबदेही से जुड़ी हो तभी 'police_accountability' भरें। "
        "मर्यादा: केवल स्रोतों में मौजूद तथ्य; किसी नामित व्यक्ति/पार्टी पर मनगढ़ंत आरोप, राशि "
        "या तथ्य कभी न गढ़ें। भाषा (कठोर): हर दृश्य फ़ील्ड पूर्णतः देवनागरी — कोई रोमन/अंग्रेज़ी अक्षर या "
        "संक्षिप्ति नहीं (जैसे JDA→जयपुर विकास प्राधिकरण, BJP→भाजपा, ED→ईडी); किसी इनपुट फ़ील्ड का नाम/टैग "
        "कोष्ठक में कभी नहीं ('(analysis)', '(lead_story)' वर्जित)। केवल event_type व severity अंग्रेज़ी "
        "enum में। सिर्फ़ मान्य JSON लौटाएँ।"
    )
    user = {
        "task": "जयपुर की ताज़ा ब्रेकिंग व राजनीतिक खबरों की एक समेकित, बहु-दिवसीय, तथ्यात्मक हार्ड-न्यूज़ "
                "कवरेज — सबसे नई/बड़ी घटना को लीड बनाकर।",
        "lead_story": {"headline": lead["headline"], "snippets": lead_snippets},
        "story_history": history,
        "lead_sources_en": lead_sources,
        "other_stories_en": [c["headline"] for c in others_cl],
        "output_schema": {
            "lead_headline": "संक्षिप्त, सटीक हिंदी शीर्षक — सबसे नई/बड़ी घटना को लीड बनाकर; तथ्यात्मक व "
                             "सनसनी-रहित; किसी पक्ष की तरफ़दारी या निंदा नहीं",
            "event_type": "one of: politics, crime, accident, civic, protest, court, government, "
                          "development, weather, other",
            "severity": "one of: critical, high, medium, low",
            "analysis": "हार्ड न्यूज़ रिपोर्ट (संपादकीय/राय नहीं), 3-4 पैराग्राफ: पहला पैराग्राफ सबसे नई/बड़ी "
                        "ठोस घटना से शुरू हो (उल्टा पिरामिड); फिर पृष्ठभूमि, कौन/कहाँ, क्या हुआ, शुरुआत से अब "
                        "तक का घटनाक्रम, मौजूदा स्थिति, और असर; कोई भी दावा/माँग/प्रतिक्रिया हवाले से "
                        "('पुलिस/विपक्ष/नागरिकों के अनुसार'), अपनी राय कभी नहीं; पैराग्राफ \\n\\n से अलग",
            "key_facts": "6-8 हिंदी बिंदुओं की array — हर बिंदु एक स्वाभाविक पूरा वाक्य (कौन · कहाँ · "
                         "क्या हुआ · क्या नतीजा/कार्रवाई); डैश या कॉमा से जोड़ी फ़ील्ड-सूची नहीं, और मनगढ़ंत "
                         "तथ्य/धारा नहीं",
            "developments": "[{date_label, text}] की array, oldest→newest, 5-12 चरण (एक ही बिंदु होने पर "
                            "भी एक चरण पर न रुकें)। text = 2-3 हिंदी वाक्य: क्या हुआ, कहाँ/किसने, "
                            "क्या कार्रवाई, क्या असर। date_label = पुष्ट तिथि या सापेक्ष हिंदी "
                            "लेबल; मनगढ़ंत समय/तिथि नहीं; इनपुट फ़ील्ड का नाम कभी नहीं",
            "police_accountability": "हिंदी पैराग्राफ — केवल तभी भरें जब खबर सीधे पुलिस/प्रशासन की "
                                     "कार्रवाई या जवाबदेही से जुड़ी हो; अन्य खबर को ज़बरदस्ती पुलिस-मामला "
                                     "न बनाएँ, वरना खाली स्ट्रिंग",
            "what_next": "1-2 हिंदी वाक्य — आगे की अपेक्षित प्रक्रिया या घटनाक्रम (जाँच/सुनवाई/अगला कदम), "
                         "तथ्यात्मक व हवाले से; अपनी राय/उपदेश नहीं",
            "sources_hi": "हिंदी एक-पंक्ति शीर्षकों की array — lead_sources_en के समान क्रम व संख्या",
            "other_stories": "{headline, summary} की array हिंदी में — other_stories_en के समान क्रम व "
                             "संख्या; हर summary एक तथ्यात्मक हार्ड-न्यूज़ पंक्ति",
        },
    }
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]


def _groq_call(api_key: str, model: str, messages: list[dict], max_tokens: int) -> tuple[dict | None, int]:
    """POST one chat-completion. Returns (parsed_json | None, http_status): 200 on success, the HTTP
    status on an HTTPError (e.g. 413 = over the TPM cap), or -1 on any other failure."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.35,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        f"{GROQ_BASE}/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": GROQ_UA}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read())
        content = body["choices"][0]["message"]["content"]
        print(f"  Groq model {model} responded ({len(content)} chars, max_tokens={max_tokens})")
        return json.loads(content), 200
    except urllib.error.HTTPError as exc:
        print(f"  ! Groq HTTP {exc.code}: {exc.read()[:200]!r}", file=sys.stderr)
        return None, exc.code
    except Exception as exc:
        print(f"  ! Groq call failed: {exc}", file=sys.stderr)
        return None, -1


def groq_analyze(api_key: str, clusters: list[dict], points: list[dict],
                 source_objs: list[dict] | None = None, timeline_mode: str = "case") -> dict | None:
    """Ask Groq for the deep HINDI package (analysis, key facts, dated developments, police
    accountability, what-next, Hindi sources/secondary stories). Preflight-shrinks the request to the
    TPM budget before sending (Groq bills prompt+max_tokens against an 8000/min cap), and retries once
    with a minimal request on HTTP 413, so an unusually large day degrades to a smaller-but-real post
    instead of the empty holding page. `points` is the down-sampled arc — see _arc_sample/TIMELINE_MAX.
    `source_objs` (the clubbed-arc sources) are Hindi-titled by the AI to match the rendered cards."""
    model = groq_pick_model(api_key)
    max_tokens = GROQ_MAX_TOKENS
    snippets, others, hist = 3, 4, None
    messages = _groq_messages(clusters, points, snippets=snippets, others=others, history_max=hist,
                              source_objs=source_objs, timeline_mode=timeline_mode)
    # Preflight: shrink the request until the estimated prompt + output fits the TPM budget. On a
    # normal day the default already fits, so nothing is dropped; only a big enrichment day trims.
    for _ in range(8):
        if _messages_tokens(messages) + max_tokens <= TPM_BUDGET:
            break
        if snippets:
            snippets = 0
        elif others:
            others = 0
        elif hist is None:
            hist = min(8, len(points)) or None
        elif hist and hist > 5:
            hist = 5
        elif max_tokens > 3500:
            max_tokens = 3500
        else:
            break
        messages = _groq_messages(clusters, points, snippets=snippets, others=others,
                                  history_max=hist, source_objs=source_objs, timeline_mode=timeline_mode)
    est = _messages_tokens(messages)
    print(f"  TPM: est. prompt {est} + max_tokens {max_tokens} = {est + max_tokens} "
          f"(budget {TPM_BUDGET}, cap {GROQ_TPM_LIMIT})")

    data, code = _groq_call(api_key, model, messages, max_tokens)
    if data is None and code == 413:
        print("  ! Groq 413 — retrying once with a minimal request.", file=sys.stderr)
        messages = _groq_messages(clusters, points, snippets=0, others=0,
                                  history_max=min(8, len(points)) or None, source_objs=source_objs,
                                  timeline_mode=timeline_mode)
        data, _ = _groq_call(api_key, model, messages, 3500)
    return data


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


def _hours_since(iso: str | None, now: datetime) -> float:
    """Hours between an ISO timestamp and now. A missing or unparseable value reads as very stale
    (large number) so the staleness guard proceeds rather than staying frozen. See build()."""
    if not iso:
        return 1e9
    return _days_ago(iso, now) * 24.0


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


def month_story_arc(archive: dict, now: datetime) -> list[dict]:
    """Club the MONTH's different Jaipur stories into one 'इस महीने' tracker timeline. Gathers dated
    points from EVERY archived story within ARCHIVE_DAYS, dedupes by url/text, sorts oldest→newest and
    down-samples to TIMELINE_MAX. Used when the lead is a one-off with no chronology of its own, so the
    section shows the month's DIFFERENT stories — one line per story, not a false single-case
    chronology (see breaking-benchmark.md). Never fabricates — every point is a real archived, dated,
    sourced item. Returns [] if nothing is archived."""
    seen_url: set[str] = set()
    seen_txt: set[str] = set()
    pts: list[dict] = []
    for st in archive.get("stories", []):
        for p in st.get("points", []):
            if _days_ago(p.get("iso", ""), now) > ARCHIVE_DAYS:
                continue
            url = p.get("url") or ""
            key = normalize(p.get("text_en", ""))[:80]
            if (url and url in seen_url) or (key and key in seen_txt):
                continue
            seen_url.add(url)
            seen_txt.add(key)
            pts.append(p)
    pts.sort(key=lambda p: p.get("iso", ""))
    return _arc_sample(pts, TIMELINE_MAX)


def arc_sources(points: list[dict], limit: int = 6) -> list[dict]:
    """Distinct sources behind a (clubbed) arc — newest first, deduped by url — so the स्रोत cards show
    the varied outlets that reported the month's cases instead of one repeated placeholder. Shape
    matches cluster_sources (title=English text_en for AI Hindi-titling, url, source outlet, published)
    so _groq_messages / _lead_from_ai consume it unchanged."""
    out: list[dict] = []
    seen: set[str] = set()
    for p in sorted(points, key=lambda p: p.get("iso", ""), reverse=True):
        url = p.get("url") or ""
        if url and url in seen:
            continue
        seen.add(url)
        out.append({"title": p.get("text_en", ""), "url": url or PAGE_URL,
                    "source": p.get("source", "") or "Google News", "published": p.get("iso", "")})
        if len(out) >= limit:
            break
    return out


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
    cl["policy_flag"] = is_policy_beat(cl)
    cl["ceremonial"] = is_ceremonial(cl)
    cl["jaipur"] = is_jaipur(cl)
    cl["fresh"] = True  # a manual pin is always eligible to lead
    cl["score"] = 1e6  # pinned to the front regardless of the usual newsworthiness score
    return cl


# Web-enrichment tuning. ENRICH_MAX caps how many related items are folded in per run; a related item
# is kept when it shares ENRICH_MIN_SHARED of the lead's distinctive query terms, OR shares ≥1 term
# AND carries an accountability-failure signal (so coverage questioning the authorities on the same
# topic is admitted) — while unrelated bribery cases still aren't. See enrich_lead.
ENRICH_MAX = 16
ENRICH_MIN_SHARED = 2

# Accountability-angle terms appended to the post-selection related search, so enrichment actively
# pulls coverage that QUESTIONS the government/police on the chosen topic (their negligence, delay,
# the victims, denied compensation, protests, probes) instead of more same-angle/celebratory
# coverage. Used to build the extra enrich_lead queries (English — matched against English feeds).
ACCOUNTABILITY_ANGLE_TERMS = [
    "negligence", "delay", "lapse", "dereliction", "compensation", "rehabilitation",
    "protest", "victim", "probe", "inquiry", "action", "responsibility", "accountability",
    "suspended", "cover up", "custodial", "apathy", "grievance", "demand",
]
# A compact OR-clause of the highest-signal angle terms for a Google News query (kept short so the
# query stays valid and focused).
_ANGLE_OR = ("negligence OR delay OR compensation OR rehabilitation OR protest OR victim OR probe "
             "OR action OR responsibility OR suspended OR dereliction OR custodial")


def _lead_query_terms(cluster: dict, max_terms: int = 5) -> list[str]:
    """The most distinctive keywords for a related-coverage web search. Uses the first non-digest
    article title in the cluster (falling back to the headline) so the search terms describe ONE
    story, not a muddled digest's many topics. Longer, more specific tokens first (a name / department
    / place), stopwords already removed."""
    rep = next((it["title"] for it in cluster.get("items", []) if not is_roundup(it)),
               cluster.get("headline", ""))
    toks = sorted(keywords(rep), key=lambda t: (len(t), t), reverse=True)
    return toks[:max_terms]


def _has_accountability_signal(it: dict) -> bool:
    """True if a single feed item carries a government/police accountability-failure signal
    (FAILURE_TERMS) — used by enrich_lead to admit related coverage that questions the authorities
    on the chosen topic, not just more same-angle reporting."""
    txt = " " + normalize(it["title"] + " " + it.get("summary", "")) + " "
    return any(t in txt for t in FAILURE_TERMS)


def enrich_lead(cluster: dict, items: list[dict]) -> dict:
    """Search Google News for related coverage of the CHOSEN lead story and fold matching items into
    the cluster, so the timeline gains more granular, timestamped points from many outlets. This is
    the 'go search the same news on the web and find related feeds' step. Only items that (a) are
    Jaipur-local, (b) are not digests, and (c) share ≥ENRICH_MIN_SHARED of the lead's distinctive
    terms are kept — so the page stays a single story. Returns the (possibly) enriched cluster; on a
    feed error or no matches, returns it unchanged with its original headline/flags intact."""
    terms = _lead_query_terms(cluster)
    if len(terms) < 2:
        return cluster
    core = " ".join(terms)
    core_terms = set(terms)
    need = min(ENRICH_MIN_SHARED, len(core_terms))
    subject = terms[0]  # the single most distinctive token (a name / place / topic)
    # A Jaipur anchor keeps the related search local; the windows widen the arc from a fresh sighting
    # to the weeks of prior coverage, so the timeline gains earlier, sourced points on the same story.
    queries = [
        f"Jaipur {core} when:7d",
        f"{core} when:30d",
        f"Jaipur {subject} when:30d",
    ]

    seen = {normalize(i["title"])[:80] for i in cluster["items"]}
    extra: list[dict] = []
    for q in queries:
        for it in fetch_feed(q):
            if len(extra) >= ENRICH_MAX:
                break
            key = normalize(it["title"])[:80]
            if not key or key in seen or is_roundup(it):
                continue
            if not is_local({"items": [it]}):
                continue
            it_kw = keywords(it["title"] + " " + it.get("summary", ""))
            shared = len(core_terms & it_kw)
            # Same story: enough shared distinctive terms — never an unrelated item (too few shared
            # terms), so the page stays single-focus.
            if shared < need:
                continue
            extra.append(it)
            seen.add(key)

    if not extra:
        return cluster
    enriched = dict(cluster)
    enriched["items"] = _merge_items(cluster["items"], extra)
    enriched["keywords"] = set(cluster["keywords"])
    for it in extra:
        enriched["keywords"] |= keywords(it["title"] + " " + it.get("summary", ""))
    print(f"  enrich: folded +{len(extra)} related item(s) into the lead "
          f"(search terms: {core!r})")
    return enriched


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
    clusters = filter_local(clusters)  # Jaipur only — drop out-of-area stories
    print(f"  {len(clusters)} Jaipur cluster(s) after locality gate")

    # A manual pin (override.json / FORCE_* inputs) can force a chosen story to lead.
    override = load_override()
    if override:
        clusters = _force_lead(clusters, items, override)
        # Honour the explicit pin as the lead, but keep the secondary pool Jaipur-local.
        if clusters:
            clusters = [clusters[0]] + filter_local(clusters[1:])
    else:
        # Lead with the strongest fresh, non-ceremonial Jaipur story of the day (breaking or
        # political). On a day with nothing fresh, the list comes back empty and we keep the last
        # good page rather than headline a stale item.
        clusters = apply_lead(clusters)
    if not clusters:
        print("No fresh Jaipur lead; keeping last page (no commit).")
        return

    top = clusters[0]
    # Never headline a stale item as "breaking now": if the auto-picked lead isn't fresh (e.g. only
    # the wider-window backfill produced clusters), keep the last good page. Manual pins are exempt.
    if not override and not top.get("fresh", True):
        print("No fresh Jaipur lead; keeping existing page (no commit).")
        return

    # Web enrichment: search for related coverage of the CHOSEN story across many outlets and fold it
    # into the lead cluster, so the timeline gains more granular, timestamped points from multiple
    # sources (and the AI has richer, well-attributed material). Only for the auto-picked lead — a
    # manual pin already ran its own targeted query in _force_lead().
    if not override:
        top = enrich_lead(top, items)
        clusters[0] = top

    # Order-independent hash of the lead's (base + enriched) headline set: it changes when genuinely
    # new coverage appears — base feed or related-coverage enrichment — so the timeline grows, but not
    # on mere RSS reordering. A RENDER_VERSION bump still forces a one-time re-render.
    feed_hash = hashlib.sha1(
        "|".join(sorted({normalize(i["title"]) for i in top["items"] if i["title"]})).encode()
    ).hexdigest()[:16]

    # Skip when the top-story feed is unchanged AND already rendered by this output
    # version (no Groq call, no commit). A RENDER_VERSION bump forces a one-time re-render.
    # An active override always re-renders so the pinned story takes effect immediately.
    # Exception: never sit frozen past MAX_STALE_HOURS — a single-source lead's title set never
    # changes, so without this the page and its "अंतिम अपडेट" stamp would stall for the whole
    # FRESH_LEAD_HOURS window. Once stale we proceed anyway to re-rank, re-enrich and re-stamp.
    feed_changed = feed_hash != state.get("last_feed_hash")
    up_to_date = state.get("render_version") == RENDER_VERSION
    stale_h = _hours_since(state.get("last_updated"), now)
    too_stale = stale_h >= MAX_STALE_HOURS
    if (OUT_HTML.exists() and state.get("lead") and not feed_changed
            and up_to_date and not override and not too_stale):
        print(f"No change in top-story feed and page is fresh ({stale_h:.1f}h old); "
              "skipping update (no commit).")
        return

    # Accumulate EVERY local story's multi-day history — not just the lead — so ongoing stories keep
    # building their arc even on days they aren't the headline, and the wider-window backfill seeds
    # the weeks that preceded today. Then narrate the lead's full arc.
    archive = load_archive()
    story = ingest_cluster(archive, top, now)   # the lead — capture its arc for the AI
    for cl in clusters[1:]:
        ingest_cluster(archive, cl, now)
    prune_archive(archive, now)
    # "घटनाक्रम — शुरुआत से अब तक" is ONE developing story's chronology, so it only applies when the
    # lead has a real arc of its own. A one-off lead (a single incident) has no chronology — then the
    # section becomes "इस महीने", clubbing the month's DIFFERENT Jaipur stories.
    # Pick the mode from the lead's own COHESIVE dated-point count (the cohesion filter below).
    own_pts = story.get("points", [])
    # A lead earns the single-case "घटनाक्रम" heading only for points that are genuinely ITS OWN
    # story. Match on the lead's CASE-IDENTIFYING terms — its distinctive title tokens MINUS the
    # generic beat/failure vocabulary that many stories share — and require ≥ENRICH_MIN_SHARED of
    # them. One shared generic word is too loose and would keep unrelated stories together;
    # case-specific terms (the person/department/place) hold a real developing story together while
    # shedding a different story or an off-topic item folded in by loose archive matching. Too few
    # cohere → this isn't a single developing story → fall through to the month's different stories.
    case_terms = set(_lead_query_terms(top, max_terms=6)) - set(FAILURE_TERMS)
    need = min(ENRICH_MIN_SHARED, len(case_terms))
    cohesive_pts = ([p for p in own_pts if len(case_terms & keywords(p.get("text_en", ""))) >= need]
                    if need else own_pts)
    clubbed = month_story_arc(archive, now)
    if len(cohesive_pts) >= SINGLE_CASE_MIN:
        timeline_mode = "case"
        arc = _arc_sample(cohesive_pts, TIMELINE_MAX)
        tl_heading, tl_note = "घटनाक्रम", "इसी खबर का सिलसिला — नवीनतम अपडेट सबसे ऊपर।"
    elif clubbed:
        timeline_mode = "month"
        arc = clubbed
        tl_heading, tl_note = "इस महीने", "इस महीने की अलग-अलग खबरें — नवीनतम सबसे ऊपर।"
    else:
        timeline_mode = "case"
        arc = _arc_sample(cohesive_pts or own_pts, TIMELINE_MAX)
        tl_heading, tl_note = "घटनाक्रम", "नवीनतम अपडेट सबसे ऊपर।"
    print(f"  archive: {len(archive['stories'])} story(ies); lead carries {len(own_pts)} own "
          f"point(s) ({len(cohesive_pts)} cohesive); mode={timeline_mode}, narrating {len(arc)}")

    # The स्रोत (Source) cards ALWAYS come from the timeline arc's own events — the varied outlets
    # behind the घटनाक्रम (case mode) or इस महीने (month mode) points — so the bottom links always
    # match the timeline that's shown. Falls back to the lead cluster's own sources only if the arc
    # somehow yields none (see _lead_from_ai).
    src_objs = arc_sources(arc) or None

    lead = None
    other_stories: list[dict] = []
    if use_ai:
        print("Asking Groq for analysis...")
        ai = groq_analyze(api_key, clusters, arc, source_objs=src_objs, timeline_mode=timeline_mode)
        if ai:
            lead, other_stories = _lead_from_ai(ai, clusters, source_objs=src_objs)

    if lead is None:
        # No AI / Groq failed: a clean Hindi holding page — never English feed text.
        print("Groq unavailable — rendering Hindi holding page.")
        lead, other_stories = _holding_lead(), []

    # Stamp the timeline with real archived date+time where the mapping is unambiguous.
    attach_dev_times(lead, arc)
    # Safety net: a single-source story can still come back with one development — expand it into a
    # relative-labelled arc from the already-sourced key_facts/what_next so the timeline is never a
    # lone entry (no fabricated times or facts).
    ensure_timeline_depth(lead)

    state.update(
        {
            "ist_day": ist_day,
            "last_updated": now.isoformat(),
            "last_feed_hash": feed_hash,
            "render_version": RENDER_VERSION,
            "lead": lead,
            "other_stories": other_stories,
            "timeline_heading": tl_heading,
            "timeline_note": tl_note,
        }
    )
    render(state, now)
    save_state(state)
    save_archive(archive)
    print(f"Done. Lead: {lead.get('headline','')!r} [{lead.get('severity','')}] "
          f"— {len(lead.get('developments', []))} development(s), "
          f"{len(other_stories)} other stor(y/ies).")


def _lead_from_ai(ai: dict, clusters: list[dict],
                  source_objs: list[dict] | None = None) -> tuple[dict | None, list[dict]]:
    """Map the Groq Hindi JSON onto the render model. Returns (lead, other_stories). `source_objs`
    (the clubbed-arc sources) render as the स्रोत cards when given, else the lead cluster's sources."""
    top = clusters[0]
    # to_hindi() strips English/acronyms/field-name tags from every VISIBLE field (not the
    # event_type/severity enums, which drive CSS/cadence) so the page stays Devanagari-only.
    headline = to_hindi((ai.get("lead_headline") or "").strip())
    if not headline:
        headline = to_hindi(top.get("headline", ""))   # de-Latinised cluster headline as fallback
    if not headline:
        return None, []
    severity = (ai.get("severity") or top["severity"]).strip().lower()
    if severity not in CADENCE_MINUTES:
        severity = top["severity"]
    event_type = (ai.get("event_type") or "other").strip().lower()
    analysis = to_hindi((ai.get("analysis") or "").strip())

    # key_facts / developments must be flat arrays of strings / {date_label,text} objects. The model
    # sometimes nests them (an array-in-an-array, or the whole developments list dropped into
    # key_facts); coerce defensively so a raw structure repr NEVER reaches the page.
    key_facts = _ai_str_list(ai.get("key_facts"), 8)
    police = to_hindi((ai.get("police_accountability") or "").strip())
    what_next = to_hindi((ai.get("what_next") or "").strip())

    developments: list[dict] = []

    def add_dev(d) -> None:
        if len(developments) >= TIMELINE_MAX:
            return
        if isinstance(d, dict):
            text = _ai_str(d.get("text") or d.get("") or "")
            label = _ai_str(d.get("date_label") or d.get("label") or d.get("_") or "")
            if text:
                developments.append({"label": label, "text": text})
        elif isinstance(d, (list, tuple)):
            for i in d:            # recover a nested [{date_label,text}, …] the model wrapped in a list
                add_dev(i)
        elif isinstance(d, str):
            t = to_hindi(d.strip())
            if t:
                developments.append({"label": "", "text": t})
        # any other type: skip — never str() it onto the page

    for d in (ai.get("developments") or []):
        add_dev(d)

    src_objs = source_objs if source_objs is not None else cluster_sources(top, limit=6)
    sources_hi = ai.get("sources_hi") or []
    sources = []
    for i, s in enumerate(src_objs):
        hi = None
        if i < len(sources_hi) and isinstance(sources_hi[i], str) and sources_hi[i].strip():
            hi = to_hindi(sources_hi[i].strip()) or None
        # Drop sources the AI didn't give a real Hindi title — a blank/pale card is worse than one
        # fewer source (benchmark: varied NAMED outlets, each with a real Hindi title).
        if not hi:
            continue
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
        hl = to_hindi((os_ai[i].get("headline") or "").strip())
        if not hl:
            continue
        src = cluster_sources(c, limit=1)
        other_stories.append({
            "headline": hl,
            "summary": to_hindi((os_ai[i].get("summary") or "").strip()),
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


# Internal JSON field names the model sometimes echoes as a bogus "(source)" tag — always stripped.
_PROVENANCE_TAGS = {
    "analysis", "lead_story", "lead story", "other_stories", "other stories", "story_history",
    "story history", "key_facts", "key facts", "what_next", "developments", "development",
    "sources", "source", "sources_hi", "lead_sources_en", "other_stories_en", "output_schema",
    "लीड स्रोत", "लीड-स्रोत", "स्रोत", "मुख्य स्रोत",
}
_LATIN_RE = re.compile(r"[A-Za-z]")
_PAREN_RE = re.compile(r"[（(]\s*([^()（）]*?)\s*[)）]")
# Whole-token match of a known acronym/org (Latin boundaries so digits/dates are untouched).
_ORG_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(k) for k in sorted(ORG_HI, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def to_hindi(text: str) -> str:
    """Force a visible AI text field to Devanagari-only.

    The /breaking page must never show English. The model sometimes (a) appends its input field
    name as a fake citation — '(analysis)', '(lead_story)', '(other_stories)' — and (b) leaves
    English acronyms (JDA, BJP, ED, ACB, FIR…) in the prose. This deterministic pass, applied to
    every visible field in _lead_from_ai, guarantees a clean page regardless of the model:
      1. drop provenance/Latin parenthetical tags — '(analysis)', '(JDA)', '(लीड स्रोत)';
      2. rewrite known acronyms to their conventional Hindi form (ORG_HI);
      3. strip any residual Latin run (unknown English) — 'not allowed at all';
      4. tidy whitespace, empty brackets and stray space before Hindi punctuation.
    Numbers and dates (10-12, 9:32) are preserved; newlines (paragraph breaks) are kept."""
    if not text:
        return text

    def _drop_paren(m: "re.Match") -> str:
        inner = m.group(1).strip()
        low = inner.lower()
        if low in _PROVENANCE_TAGS:
            return ""
        # a parenthetical that is purely Latin/acronym (e.g. '(analysis)', '(JDA)', '(policy)')
        if inner and _LATIN_RE.search(inner) and re.fullmatch(r"[A-Za-z0-9 _./&'\-]+", inner):
            return ""
        return m.group(0)

    text = _PAREN_RE.sub(_drop_paren, text)
    text = _ORG_RE.sub(lambda m: ORG_HI[m.group(1).lower()], text)
    text = _LATIN_RE.sub("", text)               # strip any leftover English letters
    text = re.sub(r"[（(]\s*[)）]", "", text)      # drop now-empty brackets
    text = re.sub(r"[（(]\s+", "(", text)
    text = re.sub(r"\s+[)）]", ")", text)
    text = re.sub(r"[^\S\n]+", " ", text)         # collapse spaces/tabs, keep newlines
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\s+([।,;:.!?])", r"\1", text)  # no space before punctuation
    return text.strip()


def _ai_str(x) -> str:
    """Coerce ONE AI value to a clean Hindi string. A str is sanitized via to_hindi; anything else
    (a list/dict the model wrongly nested) becomes '' — we NEVER str() a list/dict, because that dumps
    a raw JSON/Python repr onto the page (the '[{\'_\': …}]' / 'key","key2"' garbage seen when Groq
    returns a mis-shaped array). See _ai_str_list and _lead_from_ai."""
    return to_hindi(x.strip()) if isinstance(x, str) else ""


def _ai_str_list(x, limit: int) -> list[str]:
    """Flatten an AI 'array of strings' field into clean Hindi strings. Tolerates nested lists (the
    model sometimes wraps items in sub-lists) and silently drops dicts/None, so a mis-shaped
    key_facts/sources array yields real strings instead of a stringified structure."""
    out: list[str] = []

    def walk(v) -> None:
        if len(out) >= limit:
            return
        if isinstance(v, str):
            s = to_hindi(v.strip())
            if s:
                out.append(s)
        elif isinstance(v, (list, tuple)):
            for i in v:
                walk(i)
        # dicts / other types: skip — a dict is the wrong shape for a strings array

    walk(x)
    return out[:limit]


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
    """Stamp each timeline development with a real archived timestamp + source outlet where the mapping
    is unambiguous — never fabricated.

    Two cases:
    - Counts match (devs == points): the AI narrated exactly one development per dated point in order,
      so align by index and use each point's exact IST date+time and reporting outlet.
    - Richer timeline (devs != points): the AI added process-milestone steps beyond the dated points
      (relative labels like 'शिकायत के बाद' for undated ones). We can't align by index, so we attach
      the outlet only to steps whose date_label exactly matches an archived point's Hindi label, and
      leave the relative-label steps as the AI wrote them (text still shows; no invented time)."""
    devs = lead.get("developments") or []
    if not devs or not points:
        return
    if len(devs) == len(points):
        for dev, point in zip(devs, points):
            label = _hindi_point_label(point)
            if label:
                dev["label"] = label
            src_hi = hindi_source(point.get("source", ""))
            if src_hi:
                dev["source_hi"] = src_hi
        return
    # Richer timeline: map each archived point's Hindi label -> its outlet, then stamp the outlet onto
    # any development the AI dated to that exact label.
    src_by_label: dict[str, str] = {}
    for point in points:
        label = _hindi_point_label(point)
        if label:
            src_by_label.setdefault(label, hindi_source(point.get("source", "")))
    for dev in devs:
        src_hi = src_by_label.get((dev.get("label") or "").strip())
        if src_hi:
            dev["source_hi"] = src_hi


def ensure_timeline_depth(lead: dict) -> None:
    """Guarantee the timeline is never a lone entry.

    A genuinely single-source breaking story has only one dated archive point, so the AI can hand
    back just one development even though the (now generalized) prompt asks for a multi-step arc.
    When there are fewer than MIN_TIMELINE_STEPS, expand into a narrative arc using ONLY content the
    AI already produced from sources — the lead's own key_facts and what_next — with RELATIVE Hindi
    labels and NO timestamps or outlet stamps. This invents no facts and no times; it re-narrates
    already-sourced content as ordered steps so 'घटनाक्रम' reads as शुरुआत → अब. The one real dated
    development keeps its real time/outlet at the top; the forward-looking 'आगे' step sits last as
    the live/developing point. The prompt is the primary path — this is a best-effort safety net."""
    devs = lead.get("developments") or []
    if len(devs) >= MIN_TIMELINE_STEPS:
        return
    seen = {(d.get("text") or "").strip() for d in devs if (d.get("text") or "").strip()}

    context_steps: list[dict] = []
    for fact in (lead.get("key_facts") or []):
        if not isinstance(fact, str):   # key_facts are already clean strings; skip any stray non-string
            continue
        text = fact.strip()
        if text and text not in seen:
            context_steps.append({"label": "मामले में", "text": text})
            seen.add(text)

    what_next = (lead.get("what_next") or "").strip()
    tail = [{"label": "आगे", "text": what_next}] if what_next and what_next not in seen else []

    need = MIN_TIMELINE_STEPS - len(devs)
    context_steps = context_steps[:max(need - len(tail), 0)]
    if context_steps or tail:
        lead["developments"] = devs + context_steps + tail


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

    # Developments — a past→present chain (oldest at top, newest at bottom). Each entry carries a
    # real timestamp (<time>), a 2-3 sentence account (<p>), and, when known, the reporting outlet.
    developments = lead.get("developments", [])
    if developments:
        # Newest first (descending) — the live/developing dot pulses on the FIRST (top) item. --i gives
        # each item a tiny stagger for the scroll-triggered reveal (see the observer <script> + CSS).
        timeline_html = "\n".join(
            f'<li class="tl-item sev-{esc(sev)}" style="--i:{i}">'
            + (f'<time>{esc(d.get("label"))}</time>' if d.get("label") else "")
            + f'<p>{esc(d.get("text"))}</p>'
            + (f'<span class="tl-src">{esc(d.get("source_hi"))}</span>' if d.get("source_hi") else "")
            + '</li>'
            for i, d in enumerate(reversed(developments))
        )
    else:
        timeline_html = '<li class="tl-item"><p>अपडेट हो रहा है।</p></li>'
    # Heading/note adapt: "घटनाक्रम" (a single story's chronology) vs "इस महीने" (the month's
    # different Jaipur stories). Set by build()'s timeline-mode pick.
    tl_heading = (state.get("timeline_heading") or "घटनाक्रम").strip()
    tl_note = (state.get("timeline_note") or "नवीनतम अपडेट सबसे ऊपर।").strip()
    count_word = "खबरें" if tl_heading.startswith("इस महीने") else "घटनाक्रम"
    update_count = f"{len(developments)} {count_word}" if developments else "अपडेट हो रहा है"

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
        "{{TIMELINE_HEADING}}": esc(tl_heading),
        "{{TIMELINE_NOTE}}": esc(tl_note),
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
  /* scroll-triggered reveal: hidden until the item enters the viewport, then a fade-in-up (the
     observer <script> adds .in). --i gives a tiny per-item stagger. */
  opacity: 0; transform: translateY(10px);
  transition: opacity .5s ease, transform .5s ease; transition-delay: calc(var(--i, 0) * 0.05s);
}
.tl-item.in { opacity: 1; transform: none; }
.tl-item:last-child { padding-bottom: 0; }
.tl-item::before {
  content: ""; position: absolute; left: -7px; top: 3px;
  width: 12px; height: 12px; border-radius: 50%;
  background: var(--accent); border: 2px solid var(--surface);
}
.tl-item.sev-critical::before { background: var(--brand); }
.tl-item.sev-high::before { background: #e65100; }
/* newest development (TOP of the chain, since the list is newest-first) = the "live/developing"
   step — glowing pulse */
.tl-item:first-child::before {
  background: var(--brand); animation: tl-glow 1.3s ease-in-out infinite;
}
@keyframes tl-glow {
  0%, 100% { box-shadow: 0 0 0 0 rgba(183,28,28,.55), 0 0 4px var(--brand); }
  50%      { box-shadow: 0 0 0 5px rgba(183,28,28,0), 0 0 16px var(--brand); }
}
.tl-item time { display: block; font-size: .74rem; font-weight: 800; color: var(--brand); letter-spacing: .03em; }
/* multi-sentence, sourced developments — give them room to read as short paragraphs */
.tl-item p { margin: 4px 0 0; line-height: 1.7; }
.tl-item .tl-src {
  display: inline-block; margin-top: 5px; font-size: .72rem; font-weight: 700;
  color: var(--muted); letter-spacing: .02em;
}
.tl-item .tl-src::before { content: "\2014\00a0"; opacity: .7; }
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
          <h2>{{TIMELINE_HEADING}}</h2>
          <span class="count">{{UPDATE_COUNT}}</span>
        </div>
        <ul class="timeline">
          {{TIMELINE}}
        </ul>
        <p class="note">{{TIMELINE_NOTE}} खबर के विकसित होते ही यह पेज अपने-आप रिफ्रेश होता रहता है।</p>
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
    <script>
      // Scroll-triggered timeline reveal: each .tl-item animates in as it enters the viewport.
      // Graceful fallback (no IntersectionObserver, or reduced motion) → reveal everything at once.
      (function () {
        var items = document.querySelectorAll('.tl-item');
        var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (!('IntersectionObserver' in window) || reduce) {
          for (var i = 0; i < items.length; i++) items[i].classList.add('in');
          return;
        }
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (e) {
            if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
          });
        }, { threshold: 0.15, rootMargin: '0px 0px -8% 0px' });
        items.forEach(function (el) { io.observe(el); });
      })();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    build()
