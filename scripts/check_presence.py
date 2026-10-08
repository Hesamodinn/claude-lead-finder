#!/usr/bin/env python3
"""Check a business's social media and directory presence — no API key needed.

Reads the business's own website (home page, then contact/about/team pages) for outbound
links to social platforms and directories.  This is reliable — a business's own site is the
source of truth for its social links.

Usage::

    python check_presence.py "Acme Plumbing" acmeplumbing.ca
    python check_presence.py "Acme Plumbing"                   # no website: prints nothing

Prints a JSON object of platform → URL (only the ones found) to stdout.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse

import _common as util

# id → (label, group, domain fragments that count as "found")
PLATFORMS = {
    "google":       ("Google Business Profile", "search",
                     ("google.com/maps", "maps.google", "g.page", "goo.gl/maps")),
    "yelp":         ("Yelp", "directory", ("yelp.",)),
    "facebook":     ("Facebook", "social",
                     ("facebook.com", "fb.com", "fb.me")),
    "instagram":    ("Instagram", "social", ("instagram.com",)),
    "linkedin":     ("LinkedIn", "social",
                     ("linkedin.com/company", "linkedin.com/in")),
    "tiktok":       ("TikTok", "social", ("tiktok.com",)),
    "youtube":      ("YouTube", "social",
                     ("youtube.com/@", "youtube.com/channel", "youtube.com/c/", "youtube.com/user")),
    "x":            ("X / Twitter", "social", ("twitter.com/", "x.com/")),
    "bbb":          ("Better Business Bureau", "directory", ("bbb.org",)),
    "tripadvisor":  ("TripAdvisor", "directory", ("tripadvisor.",)),
}

_SOCIAL_PAGE_RE = re.compile(r"contact|about|team|location|reach|impressum|kontakt", re.I)
MAX_PAGES = 4


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _scan_hrefs(body: str, found: dict[str, str]) -> None:
    """Find social/directory links in one page's hrefs."""
    for href in re.findall(r'href=["\']([^"\']+)["\']', body, re.I):
        low = href.lower()
        for pid, (_label, _group, frags) in PLATFORMS.items():
            if pid in found:
                continue
            if any(f in low for f in frags) and "share" not in low and "sharer" not in low:
                found[pid] = href


def links_on_site(website: str) -> dict[str, str]:
    """Social / directory links the business put on its own website."""
    site = (website or "").strip()
    if not site:
        return {}
    if not site.startswith(("http://", "https://")):
        site = "https://" + site

    found: dict[str, str] = {}

    # The "website" might itself be a social page (e.g. facebook.com/mybusiness)
    low_site = site.lower()
    for pid, (_label, _group, frags) in PLATFORMS.items():
        if any(f in low_site for f in frags):
            found[pid] = site
    if found:
        return found

    # Fetch home page
    try:
        util.throttle(0.6)
        status, _final, body = util.http_get(site, timeout=15)
    except Exception as exc:  # noqa: BLE001
        _say(f"  Could not read {site}: {str(exc)[:80]}")
        return found
    if status == 0 or status >= 400 or not body:
        return found

    _scan_hrefs(body, found)

    # Crawl a few internal pages for what's still missing
    if set(PLATFORMS) - found.keys():
        host = _host(site)
        base = site if site.endswith("/") else site + "/"
        internal = [urllib.parse.urljoin(site, h)
                    for h in re.findall(r'href=["\']([^"\'#]+)["\']', body, re.I)[:200]
                    if _SOCIAL_PAGE_RE.search(h)]
        internal += [urllib.parse.urljoin(base, p) for p in ("contact", "contact-us", "about")]
        pages_tried = 0
        for link in [u for u in dict.fromkeys(internal) if _host(u) == host]:
            if pages_tried >= MAX_PAGES or not (set(PLATFORMS) - found.keys()):
                break
            try:
                util.throttle(0.6)
                sub_status, _f, sub_body = util.http_get(link, timeout=10)
            except Exception:  # noqa: BLE001
                continue
            pages_tried += 1
            if sub_status and sub_status < 400 and sub_body:
                _scan_hrefs(sub_body, found)

    return found


def check(name: str, website: str = "") -> dict[str, str]:
    """Return {platform_id: url} for every platform found on the business's site."""
    _say(f"  Looking for social/directory links on {website or '(no website)'} ...")
    found = links_on_site(website)
    if found:
        _say("  found: " + ", ".join(PLATFORMS[p][0] for p in found))
    else:
        _say("  none found")
    return found


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="business name")
    p.add_argument("website", nargs="?", default="",
                   help="their website URL (optional)")
    args = p.parse_args()

    result = check(args.name, args.website)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
