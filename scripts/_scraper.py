"""Google Maps scraper — Playwright (headless browser) or stdlib fallback.

Playwright renders the full Google Maps page like a real browser, scrolls through
results, and extracts structured business data. Falls back to stdlib HTTP parsing
when Playwright is not installed.

Setup (optional, for best scraping results):
    pip install playwright
    playwright install chromium

Without Playwright, the scraper parses Google web search results (limited coverage).

⚠️  Scraping Google Maps violates Google's Terms of Service.
    Heavy use can get your IP rate-limited (minutes to hours).
    Use the Google Places API (GOOGLE_PLACES_API_KEY) for compliant discovery.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request

import _common as util

_HAS_PLAYWRIGHT = False
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    pass


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


def is_available() -> bool:
    """Always true — falls back to stdlib when Playwright is not installed."""
    return True


def has_playwright() -> bool:
    return _HAS_PLAYWRIGHT


# ---------------------------------------------------------------------------
# Headers for stdlib fallback
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.6613.120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}


def _fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Playwright scraper (full browser rendering)
# ---------------------------------------------------------------------------

def _scrape_playwright(query: str, lat: float, lon: float, depth: int,
                       email: bool) -> list[dict]:
    """Use Playwright headless Chromium to scrape Google Maps."""
    results: list[dict] = []
    maps_url = (f"https://www.google.com/maps/search/"
                f"{urllib.parse.quote(query)}/@{lat},{lon},13z")

    _say(f"  🌐 Playwright: opening Google Maps ...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1280, "height": 900},
            user_agent=_HEADERS["User-Agent"],
        )
        page.set_default_timeout(15000)

        try:
            page.goto(maps_url, wait_until="networkidle", timeout=20000)
        except Exception:
            page.goto(maps_url, wait_until="domcontentloaded", timeout=20000)

        # consent dialog (GDPR regions)
        try:
            consent = page.locator("button:has-text('Accept all')")
            if consent.is_visible(timeout=2000):
                consent.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

        # scroll the results panel to load more
        feed = page.locator('[role="feed"]')
        scroll_count = min(depth, 15)
        for i in range(scroll_count):
            try:
                feed.evaluate("el => el.scrollTop = el.scrollHeight")
                page.wait_for_timeout(1200)
            except Exception:
                break
            # check for "end of list"
            if page.locator("text=You've reached the end").is_visible(timeout=500):
                break
            if i % 3 == 2:
                _say(f"    scrolling ... ({i+1}/{scroll_count})")

        # extract business data from the rendered page
        items = page.locator('[data-result-index], .Nv2PK, a.hfpxzc').all()
        if not items:
            # try broader selector
            items = page.locator('.fontHeadlineSmall').all()

        _say(f"    found {len(items)} listings on page")

        for item in items:
            try:
                biz = _extract_listing(page, item)
                if biz and biz.get("name"):
                    results.append(biz)
            except Exception:
                continue

        browser.close()

    return results


def _extract_listing(page, item) -> dict | None:
    """Extract one business listing from a Playwright element."""
    parent = item
    # try to get the card container
    try:
        text = parent.inner_text(timeout=2000)
    except Exception:
        return None

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 2:
        return None

    name = lines[0]
    rating = None
    review_count = None
    address = ""
    phone = ""
    website = ""
    category = ""

    for line in lines[1:]:
        # rating: "4.7(123)"  or  "4.7 (123)"  or  "4.7★ 123 reviews"
        rm = re.match(r'(\d\.\d)\s*[\(★]?\s*(\d[\d,]*)\)?', line)
        if rm and rating is None:
            rating = float(rm.group(1))
            review_count = int(rm.group(2).replace(",", ""))
            continue
        # phone
        if re.match(r'^[+\d\s().-]{7,20}$', line) and not phone:
            phone = line
            continue
        # address (contains a number and letters)
        if re.search(r'\d', line) and re.search(r'[A-Za-z]', line) and len(line) > 8:
            if not address and not re.match(r'^\d\.\d', line):
                address = line
                continue
        # category (short, no digits)
        if len(line) < 40 and not re.search(r'\d', line) and not category:
            category = line

    # try to get the website from the listing's action buttons
    try:
        links = parent.locator('a[href]').all()
        for link in links:
            href = link.get_attribute("href", timeout=1000) or ""
            if href and "google." not in href and href.startswith("http"):
                website = href
                break
    except Exception:
        pass

    if not name or len(name) < 2:
        return None

    return {
        "name": name,
        "address": address,
        "phone": phone,
        "website": website,
        "rating": rating,
        "review_count": review_count,
        "category": category,
    }


# ---------------------------------------------------------------------------
# Stdlib fallback (Google web search parsing)
# ---------------------------------------------------------------------------

def _scrape_stdlib(query: str, lat: float, lon: float, depth: int) -> list[dict]:
    """Parse Google web search local results — no browser needed."""
    results: list[dict] = []
    _say(f"  🌐 Web search fallback (install Playwright for better results) ...")

    # Google web search with local intent
    search_queries = [query]
    if depth > 3:
        search_queries.append(f"best {query}")

    for i, sq in enumerate(search_queries):
        if len(results) >= depth * 4:
            break
        full_q = f"{sq} near {lat:.4f},{lon:.4f}"
        url = (f"https://www.google.com/search?"
               f"q={urllib.parse.quote(full_q)}&num=20&gl=us&hl=en")
        util.throttle(2.0 if i == 0 else 3.0)
        html = _fetch(url)
        if not html:
            _say("    Google returned empty (may be rate-limited)")
            continue

        # parse local pack results
        pack = _parse_local_results(html)
        seen = {r["name"].lower() for r in results}
        for biz in pack:
            if biz["name"].lower() not in seen:
                results.append(biz)
                seen.add(biz["name"].lower())

        if i == 0:
            _say(f"    web search: {len(pack)} local results")

    return results


def _parse_local_results(html: str) -> list[dict]:
    """Extract businesses from Google web search local pack."""
    results: list[dict] = []

    # split on data-cid which marks individual business cards
    blocks = re.split(r'data-cid="[^"]*"', html)
    for block in blocks[1:]:  # skip everything before the first result
        if len(block) < 100:
            continue

        name = ""
        rating = None
        review_count = None
        address = ""
        phone = ""
        website = ""

        # name from aria-label or heading
        nm = re.search(r'aria-label="([^"]{2,80})"', block)
        if not nm:
            nm = re.search(r'<span[^>]*class="[^"]*fontHeadline[^"]*"[^>]*>([^<]{2,80})</span>', block)
        if not nm:
            nm = re.search(r'<div[^>]*role="heading"[^>]*>([^<]{2,80})</div>', block)
        if nm:
            name = _unescape(nm.group(1).strip())

        # rating
        rm = re.search(r'(\d\.\d)\s*(?:stars?|\(|<)', block)
        if rm:
            try: rating = float(rm.group(1))
            except ValueError: pass

        # review count
        rcm = re.search(r'\((\d[\d,]*)\)', block)
        if rcm:
            try: review_count = int(rcm.group(1).replace(",", ""))
            except ValueError: pass

        # phone
        pm = re.search(r'>(\(?(?:\+1\s?)?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})<', block)
        if pm:
            phone = pm.group(1).strip()

        # address
        am = re.search(r'>(\d+\s+[A-Z][a-zA-Z\s]+(?:St|Ave|Rd|Dr|Blvd|Way|Ln|Ct|Pl|Hwy|Pike)[^<]{0,60})<', block)
        if am:
            address = _unescape(am.group(1).strip())

        # website
        for href in re.findall(r'href="(https?://[^"]+)"', block):
            if ".google." not in href and ".gstatic." not in href:
                website = href
                break

        if name and len(name) > 2:
            results.append({
                "name": name,
                "address": address,
                "phone": phone,
                "website": website,
                "rating": rating,
                "review_count": review_count,
            })

    return results


def _unescape(s: str) -> str:
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&#39;", "'").replace("&quot;", '"')
    return s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape(query: str, lat: float, lon: float, depth: int = 5,
           email: bool = True, max_time: int = 600,
           category: str = "") -> list[dict]:
    """Scrape Google for business listings.

    Uses Playwright (headless browser) when installed, else falls back to
    stdlib HTTP parsing of Google web search results.
    """
    raw: list[dict] = []

    if _HAS_PLAYWRIGHT:
        try:
            raw = _scrape_playwright(query, lat, lon, depth, email)
        except Exception as exc:
            _say(f"  Playwright error: {str(exc)[:100]} — falling back to web search")
            raw = _scrape_stdlib(query, lat, lon, depth)
    else:
        raw = _scrape_stdlib(query, lat, lon, depth)

    # normalise to the standard lead shape
    leads: list[dict] = []
    for biz in raw:
        name = (biz.get("name") or "").strip()
        if not name or len(name) < 2:
            continue

        # extract email from website
        biz_email = ""
        if email and biz.get("website"):
            _say(f"    email lookup: {biz['website'][:50]} ...")
            emails = util.emails_from_site(biz["website"], max_pages=2)
            if emails:
                biz_email = emails[0]

        leads.append({
            "id": f"scrape/{util.slugify(name)}-{len(leads)}",
            "name": name,
            "category": category or biz.get("category", ""),
            "category_label": category or biz.get("category", ""),
            "address": biz.get("address", ""),
            "city": "",
            "phone": biz.get("phone", ""),
            "email": biz_email,
            "website": biz.get("website", ""),
            "opening_hours": "",
            "lat": None, "lon": None,
            "rating": biz.get("rating"),
            "review_count": biz.get("review_count"),
            "source": "playwright" if _HAS_PLAYWRIGHT else "web_scrape",
        })

    _say(f"  🌐 Scrape done: {len(leads)} businesses")
    return leads
