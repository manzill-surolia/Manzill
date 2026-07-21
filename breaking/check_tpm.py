#!/usr/bin/env python3
"""Check the Groq request's tokens-per-minute (TPM) footprint for the /breaking generator.

Groq bills ``prompt_tokens + max_tokens`` against an 8000-tokens/minute cap on this tier. If the
prompt drifts over it the API returns HTTP 413 and the page falls back to the empty Hindi holding
scaffold. The Devanagari prompt tokenizes expensively, so a prompt edit can silently blow the budget.

Run this before shipping any change to the Groq prompt in ``build_breaking_news.py``:

    python breaking/check_tpm.py            # offline conservative estimate (no network, CI-gateable)
    python breaking/check_tpm.py --api      # exact: probes Groq for usage.prompt_tokens (needs key)

It builds a **synthetic worst-case** request (the caps ``_groq_messages`` sends by default, filled
with representative-length Hindi+English content and a full ``TIMELINE_MAX`` history), so a PASS is an
upper bound: if the worst case fits, real runs fit. Exit code is non-zero on FAIL so CI can gate on it.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_generator():
    spec = importlib.util.spec_from_file_location("build_breaking_news", _HERE / "build_breaking_news.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _worst_case(bn):
    """Fabricate a lead + secondaries + a full history that fill the default message caps with
    representative-length strings, so the measured request is a conservative upper bound."""
    now = datetime.datetime.now(datetime.timezone.utc)
    long_summary = ("The development touched several parts of the city; officials, the opposition and "
                    "residents gave differing accounts, and authorities said a detailed report would follow.")
    long_url = "https://news.google.com/rss/articles/" + ("X" * 48)

    def it(title, summary=long_summary):
        return {"title": title, "summary": summary, "link": long_url,
                "source": "Rajasthan Patrika", "published": now}

    lead = {
        "headline": "जयपुर विधानसभा में जल आपूर्ति विधेयक पर लंबी बहस के बाद पारित, विपक्ष का वॉकआउट — "
                    "शहर के कई इलाकों में असर और प्रशासन की अगली कार्रवाई पर नज़र",
        "items": [it(f"Jaipur assembly passes water supply bill after long debate, opposition walkout {i}")
                  for i in range(6)],
        "keywords": {"jaipur", "assembly", "water", "bill"}, "severity": "high",
        "fresh": True, "ceremonial": False, "score": 12.0,
    }
    others = [{
        "headline": "जयपुर में सड़क दुर्घटना, यातायात प्रभावित और पुलिस जाँच में जुटी — प्रत्यक्षदर्शियों "
                    "के अलग-अलग बयान, आधिकारिक रिपोर्ट का इंतज़ार क्रमांक " + str(i),
        "items": [it(f"Jaipur road accident disrupts traffic, police begin probe {i}", "traffic")],
        "keywords": {"jaipur", "accident", "traffic"}, "severity": "high", "fresh": True,
        "ceremonial": False, "score": 6.0 - i * 0.1,
    } for i in range(6)]

    points = [{
        "date": "2026-07-%02d" % (1 + i), "time_ist": "16:10", "iso": now.isoformat(),
        "text_en": ("Detail %d: the story developed through the day; officials, the opposition and "
                    "residents gave differing accounts, and authorities said a report would follow." % i),
        "source": "ETV Bharat", "url": long_url,
    } for i in range(bn.TIMELINE_MAX)]
    return [lead] + others, points


def _fmt(prompt_tokens: int, max_tokens: int, cap: int, budget: int, exact: bool) -> int:
    total = prompt_tokens + max_tokens
    kind = "exact (Groq usage)" if exact else "estimate (conservative)"
    print("  Groq TPM check — worst-case request")
    print(f"    prompt_tokens   : {prompt_tokens:>6}   [{kind}]")
    print(f"    max_tokens      : {max_tokens:>6}")
    print(f"    request total   : {total:>6}")
    print(f"    design budget   : {budget:>6}   (margin {budget - total:+})")
    print(f"    hard TPM cap    : {cap:>6}   (margin {cap - total:+})")
    ok = total <= cap
    if ok and total > budget:
        print("  VERDICT: PASS (under the 8000 cap) — but over the design budget; preflight would trim.")
    elif ok:
        print("  VERDICT: PASS ✓  (fits with margin)")
    else:
        print("  VERDICT: FAIL ✗  request exceeds the 8000 TPM cap — trim the prompt / lower max_tokens.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Check the Groq request TPM footprint for /breaking.")
    ap.add_argument("--api", action="store_true",
                    help="Probe Groq for the EXACT prompt_tokens (max_tokens=1 call; needs GROQ_API_KEY).")
    args = ap.parse_args()

    bn = _load_generator()
    clusters, points = _worst_case(bn)
    messages = bn._groq_messages(clusters, points)   # exactly what groq_analyze sends first
    cap, budget, max_tokens = bn.GROQ_TPM_LIMIT, bn.TPM_BUDGET, bn.GROQ_MAX_TOKENS

    if args.api:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            print("  --api needs GROQ_API_KEY in the environment.", file=sys.stderr)
            return 2
        model = bn.groq_pick_model(api_key)
        payload = json.dumps({"model": model, "messages": messages, "max_tokens": 1,
                              "temperature": 0}).encode()
        req = urllib.request.Request(
            f"{bn.GROQ_BASE}/chat/completions", data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "User-Agent": bn.GROQ_UA}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
            prompt_tokens = int(body["usage"]["prompt_tokens"])
            print(f"  model: {model}")
            return _fmt(prompt_tokens, max_tokens, cap, budget, exact=True)
        except urllib.error.HTTPError as exc:
            print(f"  ! Groq HTTP {exc.code}: {exc.read()[:200]!r}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"  ! Groq probe failed: {exc}", file=sys.stderr)
            return 2

    est = bn._messages_tokens(messages)
    return _fmt(est, max_tokens, cap, budget, exact=False)


if __name__ == "__main__":
    raise SystemExit(main())
