#!/usr/bin/env python3
"""Publish a dated daily snapshot of www.manzill.com/breaking.

Copies the current ``breaking/index.html`` into a dated archive folder so each day's front page
stays permanently readable at a stable URL, e.g.:

    breaking/2026/07/20/index.html  ->  https://www.manzill.com/breaking/2026/07/20
    breaking/2026/07/21/index.html  ->  https://www.manzill.com/breaking/2026/07/21

Runs once a day at 12:00 IST from GitHub Actions (see ../.github/workflows/breaking-archive.yml).
The live page uses ABSOLUTE asset paths (``/breaking/favicon.svg``, ``/breaking/rss.xml``), so the
copy renders correctly from the dated subfolder. The page's self-references (canonical, ``og:url``
and the JSON-LD ``url``) are rewritten to point at this dated URL, so each day is a standalone,
indexable archive page rather than a duplicate that canonicalises to the live ``/breaking`` page.
Idempotent: re-running on the same day overwrites that day's snapshot with the latest front page.

Usage:
    python breaking/snapshot.py                        # snapshot today's (IST) page
    python breaking/snapshot.py --date 2026/07/20       # force a specific slug (backfill/testing)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - fallback if tzdata unavailable
    from datetime import timedelta
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "breaking" / "index.html"
LIVE_URL = "https://www.manzill.com/breaking"


def date_slug(d: datetime) -> str:
    """The archive folder path, e.g. ``2026/07/20`` — full year / zero-padded month / zero-padded
    day. Matches manzill.com/breaking/2026/07/20."""
    return f"{d.year}/{d:%m}/{d:%d}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish a dated /breaking snapshot.")
    ap.add_argument("--date", help="override the slug (e.g. 2026/07/20); default = today (IST)")
    args = ap.parse_args()

    if not SRC.exists():
        print(f"  ! {SRC} not found — nothing to snapshot (run the generator first).",
              file=sys.stderr)
        return 0

    slug = args.date.strip() if args.date else date_slug(datetime.now(IST))
    dest_dir = ROOT / "breaking" / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "index.html"

    # Copy the live page byte-for-byte, but repoint its self-references
    # (canonical, og:url and the JSON-LD url — all end in `breaking"`, unlike the
    # `/breaking/...` asset paths) at this dated URL, so the archived day is a
    # standalone indexable page instead of a duplicate that canonicalises to the
    # live /breaking page. Byte-level I/O preserves the source's exact encoding
    # and line endings.
    page_url = f"{LIVE_URL}/{slug}"
    raw = SRC.read_bytes().replace(f'{LIVE_URL}"'.encode(), f'{page_url}"'.encode())
    dest.write_bytes(raw)

    print(f"  snapshot: {SRC.relative_to(ROOT)} -> {dest.relative_to(ROOT)}")
    print(f"  URL: {page_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
