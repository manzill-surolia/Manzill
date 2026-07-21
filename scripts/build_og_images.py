#!/usr/bin/env python3
"""Generate a per-article Open Graph / link-preview card for every cyber-security
article, so shared links highlight the *article title* instead of a single shared
"Manzill Surolia" brand card.

For each article folder `<slug>/index.html`, this reads the page's
`<meta property="og:title">`, renders `scripts/og/card.html` with that title (and
the slug) at 1200x630 via headless Chromium, and writes `<slug>/og.png`.

The site is a hand-authored static site (no build framework), so — like
`build_sitemap.py` — this is a *local/manual* generator: run it after adding or
renaming an article, then point that article's `og:image`/`twitter:image` at
`https://www.manzill.com/<slug>/og.png`. Output is deterministic; commit the PNGs.

Usage:
    python scripts/build_og_images.py            # all articles
    python scripts/build_og_images.py <slug> ..  # only the named slugs

Chromium comes from the pre-installed Playwright browser bundle
(PLAYWRIGHT_BROWSERS_PATH, e.g. /opt/pw-browsers); no `playwright install` needed.
Override the binary with the CHROME env var.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "og" / "card.html"

# Folders that are not cyber-security articles (kept off the OG-card pipeline).
# The two retired slugs now hold redirect stubs (also skipped below).
NON_ARTICLES = {"jaipur-news", "jaipur-properties", "breaking", "scripts",
                ".github", "docs", "node_modules"}

OG_TITLE_RE = re.compile(
    r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']\s*/?>',
    re.IGNORECASE | re.DOTALL,
)
REDIRECT_RE = re.compile(r'http-equiv=["\']refresh["\']', re.IGNORECASE)


def find_chrome() -> str:
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    # Prefer chrome-headless-shell: it maps --window-size to the screenshot
    # exactly, whereas full-Chromium --headless=new crops ~55px off the bottom.
    for pat in ("chromium_headless_shell-*/chrome-linux/headless_shell",
                "chromium-*/chrome-linux/chrome"):
        hits = sorted(base.glob(pat))
        if hits:
            return str(hits[-1])
    for name in ("chrome-headless-shell", "chromium", "chromium-browser",
                 "google-chrome", "chrome"):
        from shutil import which
        p = which(name)
        if p:
            return p
    raise SystemExit("No Chromium binary found; set CHROME=/path/to/chrome")


def discover_article_slugs() -> list[str]:
    slugs = []
    for child in sorted(ROOT.iterdir()):
        if not child.is_dir() or child.name in NON_ARTICLES or child.name.startswith("."):
            continue
        index = child / "index.html"
        if not index.is_file():
            continue
        html = index.read_text(encoding="utf-8", errors="replace")
        if REDIRECT_RE.search(html):          # redirect stub, not an article
            continue
        slugs.append(child.name)
    return slugs


def og_title(slug: str) -> str:
    html = (ROOT / slug / "index.html").read_text(encoding="utf-8", errors="replace")
    m = OG_TITLE_RE.search(html)
    if not m:
        raise SystemExit(f"No og:title found in {slug}/index.html")
    return m.group(1).strip()


def render(slug: str, title: str, chrome: str, template: str) -> None:
    html = template.replace("__TITLE__", title).replace("__SLUG__", slug)
    out = ROOT / slug / "og.png"
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "card.html"
        page.write_text(html, encoding="utf-8")
        cmd = [chrome]
        # chrome-headless-shell is always headless; full Chromium needs the flag.
        if "headless_shell" not in Path(chrome).name and "headless-shell" not in Path(chrome).name:
            cmd.append("--headless=new")
        cmd += [
            "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", "--window-size=1200,630",
            "--virtual-time-budget=2000",
            f"--screenshot={out}", page.as_uri(),
        ]
        subprocess.run(cmd, check=True, cwd=td,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  {slug}/og.png  <-  {title}")


def main(argv: list[str]) -> int:
    chrome = find_chrome()
    template = TEMPLATE.read_text(encoding="utf-8")
    slugs = argv[1:] or discover_article_slugs()
    print(f"Chromium: {chrome}\nRendering {len(slugs)} card(s):")
    for slug in slugs:
        render(slug, og_title(slug), chrome, template)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
