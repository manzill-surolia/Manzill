#!/usr/bin/env python3
"""Generate www.manzill.com/sitemap.xml by auto-discovering the site's pages.

A route on this static GitHub Pages site is any folder containing an
`index.html` (the repo-root `index.html` is the homepage `/`). This script walks
the repo, discovers those routes, and rewrites the root `sitemap.xml` so a newly
added page is picked up automatically — no more hand-editing the XML.

Per-route <changefreq>/<priority> come from META (below) with a sensible default
for new pages; <lastmod> is the page's last git commit date. The output is
deterministic, so re-running with no page changes leaves sitemap.xml untouched.

Runs from GitHub Actions (.github/workflows/sitemap.yml) on any push that adds or
changes a page; also runnable locally:

    python scripts/build_sitemap.py
"""

from __future__ import annotations

import html
import subprocess
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - fallback if tzdata unavailable
    from datetime import timedelta
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# --------------------------------------------------------------------------- #
# Paths / config
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent
SITEMAP_PATH = ROOT / "sitemap.xml"

SITE = "https://www.manzill.com"

# Top-level folders that are not published pages. Any directory whose path
# contains one of these segments (or a dotfolder like .git/.github) is skipped
# during discovery. `breaking-news` is the retired path the breaking-news bot
# deletes each run; listed here so a stale copy can never leak into the sitemap.
EXCLUDE_DIRS = {".github", "scripts", "docs", "node_modules", "breaking-news"}

# Retired slugs that now hold a redirect stub (moved to a new URL). Their
# index.html only meta-refreshes to the new canonical path, so they must stay
# out of the sitemap. Add the OLD slug here whenever a page is renamed.
REDIRECT_SLUGS = {"cybersecurity-certifications", "security-tooling-landscape"}

# Per-route <changefreq>/<priority>. Keyed by slug ("" is the homepage). Any
# route not listed here — e.g. a page added in the future — uses DEFAULT_META,
# so a new page needs zero config to land in the sitemap. Tune a specific page
# by adding an entry here.
DEFAULT_META = {"changefreq": "weekly", "priority": "0.8"}
META = {
    "": {"changefreq": "monthly", "priority": "1.0"},
    "breaking": {"changefreq": "hourly", "priority": "0.9"},
    "jaipur-news": {"changefreq": "daily", "priority": "0.8"},
    "jaipur-properties": {"changefreq": "weekly", "priority": "0.8"},
}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def discover_slugs(root: Path) -> list[str]:
    """Return route slugs for every folder containing an index.html.

    The repo-root index.html maps to the homepage slug "" (URL "/"); a page at
    `<slug>/index.html` maps to slug "<slug>" (URL "/<slug>"). Excluded and
    hidden directories are skipped at any depth.
    """
    slugs: list[str] = []
    for path in root.rglob("index.html"):
        rel = path.parent.relative_to(root)
        parts = rel.parts  # () for the repo root
        if any(p in EXCLUDE_DIRS or p.startswith(".") for p in parts):
            continue
        slug = "/".join(parts)
        if slug in REDIRECT_SLUGS:
            continue
        slugs.append(slug)
    return slugs


def url_for(slug: str) -> str:
    """Map a slug to its canonical URL (homepage keeps its trailing slash)."""
    return SITE + "/" if slug == "" else f"{SITE}/{slug}"


def git_lastmod(index_path: Path, fallback: str) -> str:
    """Last git commit date (YYYY-MM-DD) of a page's index.html.

    Requires full git history (checkout with fetch-depth: 0 in CI). Falls back to
    `fallback` for untracked pages or when git is unavailable.
    """
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", str(index_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return fallback


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def render_sitemap(root: Path, now: datetime) -> str:
    today = now.astimezone(IST).strftime("%Y-%m-%d")
    # Homepage ("") first, then routes alphabetically — a stable order keeps the
    # output deterministic so unchanged runs produce no diff.
    slugs = sorted(set(discover_slugs(root)), key=lambda s: (s != "", s))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for slug in slugs:
        meta = META.get(slug, DEFAULT_META)
        index_path = root / slug / "index.html" if slug else root / "index.html"
        lastmod = git_lastmod(index_path, today)
        lines += [
            "  <url>",
            f"    <loc>{html.escape(url_for(slug), quote=False)}</loc>",
            f"    <lastmod>{lastmod}</lastmod>",
            f"    <changefreq>{meta['changefreq']}</changefreq>",
            f"    <priority>{meta['priority']}</priority>",
            "  </url>",
        ]
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def main() -> None:
    xml = render_sitemap(ROOT, datetime.now(timezone.utc))
    SITEMAP_PATH.write_text(xml)
    count = xml.count("<url>")
    print(f"Wrote {SITEMAP_PATH.relative_to(ROOT)} with {count} URL(s).")


if __name__ == "__main__":
    main()
