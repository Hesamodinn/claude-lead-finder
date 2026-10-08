"""Shared, dependency-free helpers: polite HTTP over the standard library only, a slug
function, email/phone/domain normalisers, and the handful of settings the scripts need.

Nothing here reads a key or a setting from anywhere but this process's own environment
variables (optionally loaded from a local ``.env`` file next to this script, which is
gitignored and never committed).
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --- settings ---------------------------------------------------------------

_ENV_FILE: dict[str, str] | None = None

def _load_env_file() -> dict[str, str]:
    global _ENV_FILE
    if _ENV_FILE is not None:
        return _ENV_FILE
    _ENV_FILE = {}
    path = Path(__file__).resolve().parent.parent / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            _ENV_FILE[k.strip()] = v.strip().strip('"').strip("'")
    return _ENV_FILE

def get_env(key: str, default: str = "") -> str:
    """A real environment variable first, else the local .env file, else ``default``."""
    v = os.environ.get(key, "")
    if v:
        return v
    return _load_env_file().get(key, default)

REQUEST_DELAY_SEC = 1.2
HTTP_TIMEOUT = 45
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_HTTP_TIMEOUT = 40
OVERPASS_TOTAL_BUDGET_S = 90
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

_CONTACT = get_env("CONTACT_URL", "https://github.com/Hesamodinn/claude-lead-finder")
USER_AGENT = f"claude-lead-finder/2.0 (+{_CONTACT})"

# --- HTTP --------------------------------------------------------------------

_last_call = {"t": 0.0}

def throttle(seconds: float = REQUEST_DELAY_SEC) -> None:
    elapsed = time.time() - _last_call["t"]
    if elapsed < seconds:
        time.sleep(seconds - elapsed)
    _last_call["t"] = time.time()

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def _get(url: str, timeout: int, headers: dict) -> tuple[int, str, str]:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=timeout
        ) as resp:
            raw = resp.read(1_500_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.geturl(), raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:  # noqa: BLE001
        return 0, url, f"__ERROR__ {type(e).__name__}: {e}"

def http_get(url: str, params: dict | None = None, timeout: int = HTTP_TIMEOUT,
             headers: dict | None = None) -> tuple[int, str, str]:
    """Return (status, final_url, body). Never raises on HTTP errors."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    status, final, body = _get(url, timeout, {"User-Agent": USER_AGENT, "Accept": "*/*",
                                               **(headers or {})})
    if status in (401, 403, 406, 503) and not headers:
        status, final, body = _get(url, timeout, _BROWSER_HEADERS)
    return status, final, body

def http_post(url: str, data: str, timeout: int = HTTP_TIMEOUT) -> tuple[int, str]:
    req = urllib.request.Request(
        url, data=data.encode("utf-8"),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"__ERROR__ {type(e).__name__}: {e}"

def http_post_json(url: str, body: dict, headers: dict, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# --- text helpers ------------------------------------------------------------

def slugify(text: str, maxlen: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "business")[:maxlen]

# --- identity helpers (dedup) ------------------------------------------------

_PHONE_STRIP = re.compile(r"[^0-9+]")

def normalize_phone(phone: str) -> str:
    """Strip a phone to its digits (keeping a leading +). '' if too short to be real."""
    digits = _PHONE_STRIP.sub("", (phone or "").strip())
    return digits if len(digits) >= 7 else ""

def normalize_domain(url: str) -> str:
    """'https://www.acme-plumbing.ca/contact' -> 'acme-plumbing.ca'. '' if no real domain."""
    if not url:
        return ""
    try:
        host = urllib.parse.urlparse(url if "://" in url else f"https://{url}").netloc
    except ValueError:
        return ""
    host = host.lower().split(":")[0]
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host if "." in host else ""

# --- email extraction --------------------------------------------------------

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)
_JUNK_EMAIL_PREFIXES = frozenset(("noreply", "no-reply", "donotreply", "mailer-daemon",
                                   "postmaster", "webmaster", "example", "test", "root"))
_JUNK_EMAIL_DOMAINS = frozenset(("example.com", "example.org", "sentry.io", "wixpress.com",
                                  "squarespace.com", "wordpress.com", "googleapis.com",
                                  "w3.org", "schema.org", "gravatar.com"))

def extract_emails(html: str) -> list[str]:
    """Pull real-looking email addresses out of HTML. Deduped, junk filtered, best first."""
    raw = _EMAIL_RE.findall(html or "")
    seen: set[str] = set()
    good: list[str] = []
    for addr in raw:
        low = addr.lower()
        if low in seen:
            continue
        seen.add(low)
        local = low.split("@")[0]
        domain = low.split("@")[1] if "@" in low else ""
        if local in _JUNK_EMAIL_PREFIXES or domain in _JUNK_EMAIL_DOMAINS:
            continue
        if domain.endswith(".png") or domain.endswith(".jpg") or domain.endswith(".svg"):
            continue
        good.append(addr)
    # prefer personal-looking addresses over generic info@/contact@
    def _rank(a: str) -> int:
        p = a.split("@")[0].lower()
        if p in ("info", "contact", "office", "admin", "hello", "enquiries", "support"):
            return 1
        return 0  # personal = better
    good.sort(key=_rank)
    return good

def emails_from_site(url: str, max_pages: int = 3) -> list[str]:
    """Crawl a business's home + contact/about pages for email addresses."""
    if not url:
        return []
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    found: list[str] = []
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return []
    pages_tried: set[str] = set()

    def _try(page_url: str) -> None:
        if page_url in pages_tried or len(pages_tried) >= max_pages:
            return
        pages_tried.add(page_url)
        try:
            throttle(0.6)
            status, _final, body = http_get(page_url, timeout=12)
        except Exception:  # noqa: BLE001
            return
        if not status or status >= 400 or not body:
            return
        for addr in extract_emails(body):
            if addr.lower() not in {e.lower() for e in found}:
                found.append(addr)
        # find internal contact/about links to crawl next
        if len(pages_tried) < max_pages:
            for href in re.findall(r'href=["\']([^"\'#]+)["\']', body, re.I)[:200]:
                full = urllib.parse.urljoin(page_url, href)
                try:
                    if urllib.parse.urlparse(full).netloc.lower() != host:
                        continue
                except ValueError:
                    continue
                if re.search(r"contact|about|team|staff|reach|impressum", full, re.I):
                    _try(full)

    _try(url)
    base = url.rstrip("/") + "/"
    for slug in ("contact", "contact-us", "about", "about-us", "team"):
        if len(pages_tried) >= max_pages:
            break
        _try(urllib.parse.urljoin(base, slug))
    return found
