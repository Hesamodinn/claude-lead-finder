#!/usr/bin/env python3
"""Find and qualify local businesses by trade and location.

Three discovery sources, best available wins:

1. **Google Places API** — fast, structured, ToS-compliant. Needs ``GOOGLE_PLACES_API_KEY``.
2. **Web scrape** — no key needed, scrapes Google search and Maps pages directly.
   ⚠️  Violates Google's ToS; heavy use can rate-limit your IP. See README.
3. **OpenStreetMap** — free, no key, but sparse coverage and no ratings/reviews.

Every result is deduplicated (Place ID → phone → domain), enriched with an email when the
business has a website, and scored 0–100 so the best leads sort to the top.

Usage::

    python find_businesses.py "Hamilton, Ontario" electrician
    python find_businesses.py "Hamilton, Ontario" electrician --review
    python find_businesses.py --query "emergency plumber" --location "Brooklyn, NY"
    python find_businesses.py --source scrape "Miami, FL" dentist
    python find_businesses.py --list-categories

``--review`` writes a self-contained ``results.html`` and opens it in the browser for
interactive filtering, pinning and exporting.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from pathlib import Path

import _common as util
import _scraper as scraper

# ---------------------------------------------------------------------------
# Categories: OSM tags + Google search terms per trade
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, dict] = {
    "restaurant":    {"label": "Restaurant / Cafe",
                      "filters": ['["amenity"~"^(restaurant|cafe|fast_food|bar|pub|ice_cream)$"]'],
                      "google_terms": ("restaurant", "cafe", "bistro", "pizzeria", "bakery cafe", "takeout")},
    "salon":         {"label": "Hair & Beauty Salon",
                      "filters": ['["shop"~"^(hairdresser|beauty|nail_salon|massage)$"]'],
                      "google_terms": ("hair salon", "barber shop", "beauty salon", "nail salon", "spa")},
    "gym":           {"label": "Gym / Fitness Studio",
                      "filters": ['["leisure"="fitness_centre"]', '["amenity"="gym"]'],
                      "google_terms": ("gym", "fitness centre", "yoga studio", "personal trainer")},
    "dentist":       {"label": "Dental Practice",
                      "filters": ['["amenity"="dentist"]', '["healthcare"="dentist"]'],
                      "google_terms": ("dentist", "dental clinic", "family dentist", "orthodontist",
                                       "cosmetic dentist", "emergency dentist")},
    "clinic":        {"label": "Clinic / Medical",
                      "filters": ['["amenity"~"^(clinic|doctors|veterinary)$"]'],
                      "google_terms": ("medical clinic", "walk-in clinic", "family doctor",
                                       "physiotherapy clinic", "chiropractor", "veterinary clinic")},
    "electrician":   {"label": "Electrician",
                      "filters": ['["craft"="electrician"]', '["office"="electrician"]', '["trade"="electrician"]'],
                      "google_terms": ("electrician", "electrical contractor", "residential electrician",
                                       "commercial electrician", "emergency electrician")},
    "plumber":       {"label": "Plumber",
                      "filters": ['["craft"="plumber"]'],
                      "google_terms": ("plumber", "plumbing contractor", "emergency plumber",
                                       "drain cleaning", "residential plumber")},
    "trades":        {"label": "Trades & Contractors",
                      "filters": ['["craft"~"^(plumber|carpenter|builder|roofer|painter|hvac|glaziery)$"]'],
                      "google_terms": ("plumber", "roofer", "hvac contractor", "general contractor",
                                       "painter", "landscaper", "carpenter", "handyman")},
    "auto":          {"label": "Auto Repair",
                      "filters": ['["shop"~"^(car_repair|tyres|car_parts)$"]'],
                      "google_terms": ("auto repair shop", "mechanic", "tire shop", "auto body shop")},
    "lawyer":        {"label": "Law Firm",
                      "filters": ['["office"="lawyer"]'],
                      "google_terms": ("law firm", "lawyer", "attorney", "legal services",
                                       "family lawyer", "real estate lawyer", "immigration lawyer",
                                       "criminal defence lawyer")},
    "realestate":    {"label": "Real Estate Agency",
                      "filters": ['["office"="estate_agent"]'],
                      "google_terms": ("real estate agency", "real estate agent", "realtor",
                                       "property management")},
    "hotel":         {"label": "Hotel / Guesthouse",
                      "filters": ['["tourism"~"^(hotel|guest_house|hostel|apartment)$"]'],
                      "google_terms": ("hotel", "guest house", "hostel", "bed and breakfast")},
    "retail":        {"label": "Retail Shop",
                      "filters": ['["shop"~"^(bakery|butcher|florist|furniture|jewelry|clothes|bicycle|books|pet)$"]'],
                      "google_terms": ("shop", "boutique", "store")},
    "professional":  {"label": "Professional Services",
                      "filters": ['["office"~"^(accountant|consulting|financial|insurance|tax_advisor)$"]'],
                      "google_terms": ("accountant", "financial advisor", "insurance agency",
                                       "tax preparation", "consulting firm")},
    "landscaper":    {"label": "Landscaper / Lawn Care",
                      "filters": ['["craft"="gardener"]', '["shop"="garden_centre"]'],
                      "google_terms": ("landscaper", "lawn care", "landscaping company",
                                       "garden maintenance", "tree service")},
    "cleaning":      {"label": "Cleaning Service",
                      "filters": [],
                      "google_terms": ("cleaning service", "house cleaning", "commercial cleaning",
                                       "janitorial service", "carpet cleaning")},
    "moving":        {"label": "Moving Company",
                      "filters": [],
                      "google_terms": ("moving company", "movers", "relocation service")},
}

WEBSITE_TAGS = ("website", "contact:website", "url", "website:en")
EMAIL_TAGS = ("email", "contact:email")
PHONE_TAGS = ("phone", "contact:phone", "contact:mobile")
CHAIN_TAGS = ("brand", "brand:wikidata", "brand:wikipedia", "franchise")
MODES = ("no_website", "has_website", "both")
SOURCES = ("auto", "api", "scrape", "osm")
WIDEN_MAX_M = 25_000

def _say(msg: str) -> None:
    print(msg, file=sys.stderr)

# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode(place: str) -> tuple[float, float, str]:
    util.throttle()
    status, _final, body = util.http_get(
        util.NOMINATIM_URL, {"q": place, "format": "json", "limit": "1"})
    if status != 200 or body.startswith("__ERROR__"):
        raise RuntimeError(f"Geocoding failed for {place!r} (status {status}). {body[:120]}")
    results = json.loads(body)
    if not results:
        raise RuntimeError(f"No location found for {place!r}. Try 'City, Country'.")
    r = results[0]
    return float(r["lat"]), float(r["lon"]), r["display_name"]

# ---------------------------------------------------------------------------
# OpenStreetMap / Overpass
# ---------------------------------------------------------------------------

def _build_query(lat: float, lon: float, radius_m: int, filters: list[str]) -> str:
    parts = "\n".join(f'  nwr{f}["name"](around:{radius_m},{lat},{lon});' for f in filters)
    return f"[out:json][timeout:25];\n(\n{parts}\n);\nout center tags;"

def _overpass_endpoints() -> list[str]:
    custom = util.get_env("OVERPASS_URL", "").strip()
    mirrors = list(util.OVERPASS_MIRRORS)
    if custom:
        return [custom] + [u for u in mirrors if u.rstrip("/") != custom.rstrip("/")]
    return mirrors

_OVERPASS_RETRY = frozenset({0, 429, 502, 503, 504})

def _fetch_overpass(lat: float, lon: float, radius_m: int, category: str) -> list[dict]:
    cat = CATEGORIES[category]
    if not cat["filters"]:
        return []
    filters = cat["filters"]
    batches = [[f] for f in filters] if len(filters) > 1 else [filters]
    merged: dict[tuple, dict] = {}
    last_detail = ""
    for bi, batch in enumerate(batches):
        if len(batches) > 1:
            _say(f"  Overpass part {bi + 1}/{len(batches)} ...")
        query = _build_query(lat, lon, radius_m, batch)
        parsed = None
        deadline = time.monotonic() + util.OVERPASS_TOTAL_BUDGET_S
        for ei, endpoint in enumerate(_overpass_endpoints()):
            for attempt in range(2):
                if time.monotonic() > deadline:
                    break
                if attempt:
                    _say("  Overpass busy — retrying ...")
                    time.sleep(2)
                util.throttle(2.0)
                status, body = util.http_post(endpoint, f"data={query}",
                                              timeout=util.OVERPASS_HTTP_TIMEOUT)
                if status == 200 and body and not body.startswith("__ERROR__"):
                    try:
                        parsed = json.loads(body)
                    except json.JSONDecodeError as exc:
                        last_detail = str(exc)
                        parsed = None
                    break
                snippet = (body or "").strip()[:160]
                last_detail = f"{endpoint} → HTTP {status}. {snippet}"
                if status not in _OVERPASS_RETRY:
                    break
            if parsed is not None:
                break
            if time.monotonic() > deadline:
                break
            if ei < len(_overpass_endpoints()) - 1:
                _say("  Trying another Overpass mirror ...")
        if parsed is None:
            raise RuntimeError(f"Overpass query failed. {last_detail}")
        for el in parsed.get("elements", []):
            merged[(el.get("type"), el.get("id"))] = el
    return list(merged.values())

def _first_tag(tags: dict, keys: tuple) -> str:
    for k in keys:
        if tags.get(k):
            return tags[k].strip()
    return ""

def _address(tags: dict) -> str:
    street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    city = " ".join(x for x in (tags.get("addr:postcode"), tags.get("addr:city")) if x)
    return ", ".join(x for x in (street, city) if x)

def _looks_like_chain(tags: dict) -> bool:
    if any(tags.get(t) for t in CHAIN_TAGS):
        return True
    op = (tags.get("operator") or "").strip().lower()
    name = (tags.get("name") or "").strip().lower()
    return bool(op) and op != name

def _lead_from_osm(el: dict, category: str, mode: str, include_chains: bool,
                   place: str) -> dict | None:
    tags = el.get("tags", {})
    name = tags.get("name", "").strip()
    if not name:
        return None
    website = _first_tag(tags, WEBSITE_TAGS)
    if mode == "no_website" and website:
        return None
    if mode == "has_website" and not website:
        return None
    if not include_chains and _looks_like_chain(tags):
        return None
    center = el.get("center") or {}
    return {
        "id": f"{el['type']}/{el['id']}",
        "name": name,
        "category": category,
        "category_label": CATEGORIES[category]["label"],
        "address": _address(tags),
        "city": tags.get("addr:city", "") or place.split(",")[0].strip(),
        "phone": _first_tag(tags, PHONE_TAGS),
        "email": _first_tag(tags, EMAIL_TAGS),
        "website": website,
        "opening_hours": tags.get("opening_hours", ""),
        "lat": el.get("lat") or center.get("lat"),
        "lon": el.get("lon") or center.get("lon"),
        "rating": None, "review_count": None,
        "source": "openstreetmap",
    }

# ---------------------------------------------------------------------------
# Google Places
# ---------------------------------------------------------------------------

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELD_MASK = ",".join((
    "places.id", "places.displayName", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber",
    "places.websiteUri", "places.googleMapsUri", "places.rating",
    "places.userRatingCount", "places.location", "places.businessStatus",
    "nextPageToken"))
PLACES_PAGE_SIZE = 20
PLACES_MAX_PAGES = 3
PLACES_MAX_RADIUS_M = 50_000
PLACES_MAX_REQUESTS = 24

def _google_key() -> str:
    return util.get_env("GOOGLE_PLACES_API_KEY", "").strip()

def _google_lead(p: dict, category: str, place: str) -> dict | None:
    name = str((p.get("displayName") or {}).get("text") or "").strip()
    pid = str(p.get("id") or "")
    if not name or not pid:
        return None
    if p.get("businessStatus") in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
        return None
    loc = p.get("location") or {}
    address = str(p.get("formattedAddress") or "")
    return {
        "id": f"google/{pid}",
        "place_id": pid,
        "name": name,
        "category": category,
        "category_label": CATEGORIES[category]["label"],
        "address": address,
        "city": (address.split(",")[1].strip()
                 if address.count(",") >= 2
                 else place.split(",")[0].strip()),
        "phone": str(p.get("nationalPhoneNumber")
                     or p.get("internationalPhoneNumber") or ""),
        "email": "",
        "website": str(p.get("websiteUri") or ""),
        "opening_hours": "",
        "lat": loc.get("latitude"), "lon": loc.get("longitude"),
        "gbp_url": str(p.get("googleMapsUri") or ""),
        "rating": p.get("rating"),
        "review_count": p.get("userRatingCount"),
        "source": "google_places",
    }

def _google_query(text: str, lat: float, lon: float, radius_m: int,
                  budget: list[int]):
    key = _google_key()
    body = {
        "textQuery": text, "pageSize": PLACES_PAGE_SIZE,
        "locationBias": {"circle": {
            "center": {"latitude": lat, "longitude": lon},
            "radius": float(min(radius_m, PLACES_MAX_RADIUS_M))}}}
    token = ""
    for _ in range(PLACES_MAX_PAGES):
        if budget[0] <= 0:
            return
        budget[0] -= 1
        req = dict(body, **({"pageToken": token} if token else {}))
        try:
            data = util.http_post_json(
                PLACES_URL, req,
                {"X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELD_MASK})
        except urllib.error.HTTPError as exc:
            _say(f"  Google Places HTTP {exc.code}: {str(exc)[:80]}")
            if exc.code in (401, 403):
                _say("  Check your GOOGLE_PLACES_API_KEY.")
            budget[0] = 0
            return
        except Exception as exc:  # noqa: BLE001
            _say(f"  Google Places error: {str(exc)[:100]}")
            budget[0] = 0
            return
        yield from (data.get("places") or [])
        token = str(data.get("nextPageToken") or "")
        if not token:
            return

def _google_search(place: str, category: str | None, query: str | None,
                   lat: float, lon: float, radius_m: int,
                   want: int, mode: str) -> list[dict]:
    if not _google_key() or want <= 0:
        return []
    seen_ids: set[str] = set()
    out: list[dict] = []
    budget = [PLACES_MAX_REQUESTS]
    # build search terms
    if query:
        terms = [query]
    elif category and category in CATEGORIES:
        terms = list(CATEGORIES[category]["google_terms"])
    else:
        terms = [category or "business"]
    # radius rounds
    radii = [radius_m]
    for factor in (2, 4):
        r = min(radius_m * factor, PLACES_MAX_RADIUS_M)
        if r > radii[-1]:
            radii.append(r)
    cat_key = category or "other"
    for step, r_m in enumerate(radii):
        if step:
            _say(f"  Google: {len(out)} so far — widening to {r_m / 1000:.0f} km ...")
        dry, patience = 0, (1 if step else 2)
        for term in terms:
            if budget[0] <= 0 or len(out) >= want:
                break
            if dry >= patience:
                break
            new = 0
            for p in _google_query(f"{term} in {place}", lat, lon, r_m, budget):
                pid = str(p.get("id") or "")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                new += 1
                lead = _google_lead(p, cat_key, place)
                if not lead:
                    continue
                has_site = bool(lead["website"])
                if (mode == "no_website" and has_site) or (mode == "has_website" and not has_site):
                    continue
                out.append(lead)
                if len(out) >= want:
                    break
            dry = dry + 1 if not new else 0
        if len(out) >= want or budget[0] <= 0:
            break
    used = PLACES_MAX_REQUESTS - budget[0]
    _say(f"  Google Places: {len(out)} businesses from {used} request{'s' if used != 1 else ''}")
    return out

# ---------------------------------------------------------------------------
# Deduplication (Place ID → phone → domain)
# ---------------------------------------------------------------------------

def _dedup_keys(lead: dict) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    pid = lead.get("place_id") or ""
    if pid:
        keys.add(("pid", pid))
    phone = util.normalize_phone(lead.get("phone", ""))
    if phone:
        keys.add(("phone", phone))
    domain = util.normalize_domain(lead.get("website", ""))
    if domain:
        keys.add(("domain", domain))
    return keys

def dedup(leads: list[dict]) -> list[dict]:
    """Remove duplicates. A lead that shares any identity key with an earlier one is dropped.
    When both sources found the same business, the Google record wins (more metadata)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    # Google records first so they win ties
    ordered = sorted(leads, key=lambda x: 0 if x.get("source") == "google_places" else 1)
    for lead in ordered:
        keys = _dedup_keys(lead)
        if keys and keys & seen:
            continue
        seen |= keys
        out.append(lead)
    return out

# ---------------------------------------------------------------------------
# Scoring (deterministic, no AI)
# ---------------------------------------------------------------------------

def score(lead: dict, mode: str = "no_website") -> int:
    """0–100 lead quality score. Higher = more worth contacting."""
    s = 0
    # business quality
    rating = lead.get("rating")
    if rating is not None:
        if rating >= 4.5:   s += 18
        elif rating >= 4.0: s += 14
        elif rating >= 3.5: s += 8
        elif rating >= 3.0: s += 4
    reviews = lead.get("review_count") or 0
    if reviews >= 100:  s += 18
    elif reviews >= 50: s += 14
    elif reviews >= 20: s += 10
    elif reviews >= 5:  s += 5
    # contactability
    if lead.get("email"):   s += 22
    if lead.get("phone"):   s += 8
    if lead.get("address"): s += 4
    # opportunity signal
    has_site = bool(lead.get("website"))
    if mode == "no_website" and not has_site:
        s += 12  # the whole point: they need a site
    elif mode in ("has_website", "both") and has_site:
        s += 4   # at least reachable
    # data confidence
    if lead.get("source") == "google_places":
        s += 4
    # base: you were found, you exist
    s += 10
    return min(s, 100)

def _score_reasons(lead: dict, mode: str = "no_website") -> list[str]:
    """Human-readable list of why this lead scored the way it did."""
    reasons = []
    r = lead.get("rating")
    rc = lead.get("review_count") or 0
    if r is not None and rc:
        reasons.append(f"{r}★ · {rc} reviews")
    elif r is not None:
        reasons.append(f"{r}★")
    if lead.get("email"):
        reasons.append("email found")
    else:
        reasons.append("no email")
    if lead.get("phone"):
        reasons.append("phone ✓")
    if not lead.get("website") and mode == "no_website":
        reasons.append("no website (needs one)")
    elif lead.get("website"):
        reasons.append("has website")
    return reasons

# ---------------------------------------------------------------------------
# Email enrichment (site crawl)
# ---------------------------------------------------------------------------

def _enrich_emails(leads: list[dict], find_email: bool) -> None:
    """In-place: crawl each lead's website for an email when it has a site but no email."""
    if not find_email:
        return
    count = 0
    for lead in leads:
        if lead.get("email") or not lead.get("website"):
            continue
        _say(f"  Looking for email on {lead['website'][:60]} ...")
        emails = util.emails_from_site(lead["website"])
        if emails:
            lead["email"] = emails[0]
            if len(emails) > 1:
                lead["alt_emails"] = emails[1:4]
            count += 1
    if count:
        _say(f"  Found emails for {count} businesses")

# ---------------------------------------------------------------------------
# Exclusion list
# ---------------------------------------------------------------------------

def _excluded_path() -> Path:
    return Path(__file__).resolve().parent.parent / "excluded.json"

def load_excluded() -> set[str]:
    p = _excluded_path()
    if p.is_file():
        try:
            return set(json.loads(p.read_text("utf-8")))
        except (json.JSONDecodeError, TypeError):
            pass
    return set()

def save_excluded(ids: set[str]) -> None:
    _excluded_path().write_text(json.dumps(sorted(ids), indent=2), "utf-8")

def _apply_exclusions(leads: list[dict]) -> list[dict]:
    ex = load_excluded()
    if not ex:
        return leads
    before = len(leads)
    leads = [l for l in leads if l.get("id") not in ex and l.get("place_id") not in ex]
    dropped = before - len(leads)
    if dropped:
        _say(f"  {dropped} previously excluded")
    return leads

# ---------------------------------------------------------------------------
# Combined search
# ---------------------------------------------------------------------------

def search(place: str, category: str | None = None, query: str | None = None,
           radius_m: int = 8000, limit: int = 20, mode: str = "no_website",
           include_chains: bool = False, find_email: bool = True,
           source: str = "auto", scrape_depth: int = 5,
           lat: float | None = None, lon: float | None = None) -> list[dict]:
    if category and category not in CATEGORIES:
        raise ValueError(f"Unknown category {category!r}. Use --list-categories or --query.")
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode!r}. Choose from: {', '.join(MODES)}")
    if source not in SOURCES:
        raise ValueError(f"Unknown source {source!r}. Choose from: {', '.join(SOURCES)}")
    if not category and not query:
        raise ValueError("Provide a category or --query.")
    if lat is None or lon is None:
        lat, lon, display = geocode(place)
        _say(f"  📍 {display}")
        place = place or display
    else:
        _say(f"  📍 pin at {lat:.4f}, {lon:.4f}")

    google_api = bool(_google_key()) and source in ("auto", "api")
    use_scraper = source in ("auto", "scrape")
    use_osm = source in ("auto", "osm")
    found: list[dict] = []

    # --- 1. Google Places API (primary when available, compliant) ---
    if google_api:
        label = query or (CATEGORIES[category]["label"] if category else "businesses")
        _say(f"  🔍 Google Places API: {label} near {place.split(',')[0]} ...")
        found = _google_search(place, category, query, lat, lon, radius_m, limit, mode)

    # --- 2. Web scrape (no key needed, but violates Google ToS) ---
    if len(found) < limit and use_scraper:
        scraper_up = scraper.is_available()
        if scraper_up:
            if found:
                _say(f"  API has {len(found)} of {limit} — web scraper tops up the rest")
            else:
                _say("  ⚠ No Google API key — using web scraper (violates Google ToS, "
                     "IP can be rate-limited)")
            search_text = query or (CATEGORIES.get(category, {}).get("google_terms", [category])[0]
                                    if category else "business")
            search_query = f"{search_text} in {place}"
            try:
                scraped = scraper.scrape(
                    search_query, lat, lon, depth=scrape_depth,
                    email=find_email, category=category or "")
                # apply mode filter
                for lead in scraped:
                    has_site = bool(lead.get("website"))
                    if mode == "no_website" and has_site:
                        continue
                    if mode == "has_website" and not has_site:
                        continue
                    if not include_chains:
                        pass  # engine doesn't expose chain tags
                    found.append(lead)
                    if len(found) >= limit:
                        break
            except Exception as exc:  # noqa: BLE001
                _say(f"  ⚠ Web scrape failed: {str(exc)[:100]}")
                if source == "scrape":
                    raise
        elif source == "scrape":
            raise RuntimeError("Web scraper failed. Google may be rate-limiting your IP — wait a few minutes.")
        elif not found and source == "auto":
            pass  # web scraper is always available, failure means rate-limiting

    # --- 3. OpenStreetMap (free fallback, sparse) ---
    if len(found) < limit and use_osm and category and CATEGORIES.get(category, {}).get("filters"):
        if found:
            _say(f"  {len(found)} of {limit} so far — OpenStreetMap tops up the rest")
        else:
            _say(f"  🔍 OpenStreetMap: {CATEGORIES[category]['label']} within "
                 f"{radius_m / 1000:.0f} km ...")
        names = {util.slugify(x["name"]) for x in found}
        radii = [radius_m]
        for factor in (2, 3):
            r = min(radius_m * factor, WIDEN_MAX_M)
            if r > radii[-1]:
                radii.append(r)
        for step, r_m in enumerate(radii):
            if len(found) >= limit:
                break
            if step:
                _say(f"  Widening to {r_m / 1000:.0f} km ...")
            try:
                elements = _fetch_overpass(lat, lon, r_m, category)
            except Exception as exc:  # noqa: BLE001
                if found:
                    _say(f"  OpenStreetMap not answering — stopped ({str(exc)[:60]})")
                    break
                raise
            for el in elements:
                lead = _lead_from_osm(el, category, mode, include_chains, place)
                if lead and util.slugify(lead["name"]) not in names:
                    names.add(util.slugify(lead["name"]))
                    found.append(lead)
                    if len(found) >= limit:
                        break

    # --- nothing worked ---
    if not found:
        has_api_key = bool(_google_key())
        scraper_up = use_scraper and scraper.is_available()
        if not has_api_key:
            _say("  ℹ Set GOOGLE_PLACES_API_KEY for the best results.")
        if query and not has_api_key and not scraper_up:
            _say("  ⚠ Free-text --query needs Google API or the web scraper.")

    # --- pipeline: dedup → exclude → email → score ---
    found = dedup(found)
    found = _apply_exclusions(found)
    _enrich_emails(found, find_email)
    for lead in found:
        lead["score"] = score(lead, mode)
        lead["reasons"] = _score_reasons(lead, mode)
    found.sort(key=lambda x: x["score"], reverse=True)
    return found[:limit]

# ---------------------------------------------------------------------------
# Results browser (--review)
# ---------------------------------------------------------------------------

_RESULTS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lead Finder — Results</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#e8e8ec;padding:20px}
h1{font-size:1.4rem;margin-bottom:4px}
.meta{color:#888;font-size:.85rem;margin-bottom:16px}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.chip{padding:6px 14px;border-radius:20px;font-size:.82rem;cursor:pointer;
      border:1px solid #444;background:#1e1e22;color:#ccc;user-select:none;transition:.15s}
.chip.on{background:#3b82f6;color:#fff;border-color:#3b82f6}
.chip:hover{border-color:#888}
.bar{display:flex;gap:10px;margin-bottom:16px;align-items:center}
.bar input{background:#1e1e22;border:1px solid #444;border-radius:6px;padding:6px 10px;
           color:#e8e8ec;font-size:.85rem;width:220px}
.btn{padding:6px 14px;border-radius:6px;font-size:.82rem;cursor:pointer;border:none;
     background:#3b82f6;color:#fff;transition:.15s}
.btn:hover{background:#2563eb}
.btn.outline{background:transparent;border:1px solid #444;color:#ccc}
.btn.outline:hover{border-color:#888}
.btn.danger{background:#ef4444}
.btn.danger:hover{background:#dc2626}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th{text-align:left;padding:8px 10px;border-bottom:2px solid #333;color:#888;
   font-weight:600;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:#e8e8ec}
th .arr{font-size:.7rem;margin-left:4px}
td{padding:8px 10px;border-bottom:1px solid #222;vertical-align:top}
tr:hover{background:#1a1a1e}
tr.pinned{background:#1e293b}
tr.pinned td:first-child::before{content:"📌 "}
.score{display:inline-block;min-width:32px;text-align:center;padding:2px 8px;
       border-radius:10px;font-weight:700;font-size:.8rem}
.s-high{background:#166534;color:#4ade80}
.s-mid{background:#854d0e;color:#fbbf24}
.s-low{background:#7f1d1d;color:#fca5a5}
a{color:#60a5fa;text-decoration:none}
a:hover{text-decoration:underline}
.tag{font-size:.75rem;color:#888;display:block;margin-top:2px}
.empty{color:#555;text-align:center;padding:40px}
.summary{color:#888;font-size:.82rem;margin-bottom:8px}
footer{margin-top:24px;color:#555;font-size:.78rem;text-align:center}
@media(max-width:800px){
  table{font-size:.78rem}
  td,th{padding:6px}
  .bar input{width:140px}
}
</style>
</head>
<body>
<h1>Lead Finder — Results</h1>
<div class="meta" id="meta"></div>
<div class="filters" id="filters"></div>
<div class="bar">
  <input type="text" id="q" placeholder="Search name, city, email …">
  <button class="btn outline" onclick="exportSelected()">⤓ Export selected</button>
  <button class="btn outline" onclick="exportAll()">⤓ Export all visible</button>
  <button class="btn danger" id="rm-btn" onclick="removeSelected()" style="display:none">
    🗑 Remove selected</button>
</div>
<div class="summary" id="summary"></div>
<table>
<thead><tr>
  <th data-k="score">Score <span class="arr"></span></th>
  <th data-k="name">Name <span class="arr"></span></th>
  <th data-k="city">City <span class="arr"></span></th>
  <th data-k="rating">Rating <span class="arr"></span></th>
  <th data-k="reviews">Reviews <span class="arr"></span></th>
  <th data-k="phone">Phone <span class="arr"></span></th>
  <th data-k="email">Email <span class="arr"></span></th>
  <th data-k="website">Website <span class="arr"></span></th>
  <th data-k="source">Source <span class="arr"></span></th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
<div class="empty" id="empty" style="display:none">No leads match the current filters.</div>
<footer>claude-lead-finder · results generated <span id="ts"></span></footer>

<script>
const DATA = __DATA__;
const EXCLUDED = new Set(__EXCLUDED__);
let leads = DATA.filter(l => !EXCLUDED.has(l.id) && !EXCLUDED.has(l.place_id || ""));
let pinned = new Set();
let sort = {k: "score", d: -1};
let filters = {email: false, phone: false, website: false, no_website: false, high: false};

document.getElementById("ts").textContent = new Date().toLocaleString();
document.getElementById("meta").textContent =
  `${DATA.length} businesses · search: ${DATA[0]?.category_label || "—"} · ${DATA[0]?.city || "—"}`;

// build filter chips
const fc = document.getElementById("filters");
[["email","Has email"],["phone","Has phone"],["website","Has website"],
 ["no_website","No website"],["high","Score ≥ 70"]].forEach(([k,label]) => {
  const c = document.createElement("span");
  c.className = "chip"; c.textContent = label;
  c.onclick = () => { filters[k] = !filters[k]; c.classList.toggle("on"); render(); };
  fc.appendChild(c);
});

// sort headers
document.querySelectorAll("th[data-k]").forEach(th => {
  th.onclick = () => {
    const k = th.dataset.k;
    if (sort.k === k) sort.d *= -1; else { sort.k = k; sort.d = -1; }
    render();
  };
});

document.getElementById("q").oninput = () => render();

function val(l, k) {
  if (k === "score") return l.score || 0;
  if (k === "rating") return l.rating || 0;
  if (k === "reviews") return l.review_count || 0;
  if (k === "phone") return l.phone || "";
  if (k === "email") return l.email || "";
  if (k === "website") return l.website || "";
  if (k === "source") return l.source || "";
  if (k === "city") return l.city || "";
  return (l.name || "").toLowerCase();
}

function render() {
  const q = document.getElementById("q").value.toLowerCase();
  let rows = leads.filter(l => {
    if (filters.email && !l.email) return false;
    if (filters.phone && !l.phone) return false;
    if (filters.website && !l.website) return false;
    if (filters.no_website && l.website) return false;
    if (filters.high && (l.score || 0) < 70) return false;
    if (q && !(l.name||"").toLowerCase().includes(q) && !(l.city||"").toLowerCase().includes(q)
        && !(l.email||"").toLowerCase().includes(q) && !(l.phone||"").includes(q)) return false;
    return true;
  });
  rows.sort((a, b) => {
    const pa = pinned.has(a.id) ? 1 : 0, pb = pinned.has(b.id) ? 1 : 0;
    if (pa !== pb) return pb - pa;
    const va = val(a, sort.k), vb = val(b, sort.k);
    if (va < vb) return sort.d; if (va > vb) return -sort.d; return 0;
  });
  // arrows
  document.querySelectorAll("th[data-k]").forEach(th => {
    th.querySelector(".arr").textContent = th.dataset.k === sort.k ? (sort.d < 0 ? "▼" : "▲") : "";
  });
  const tb = document.getElementById("tbody");
  tb.innerHTML = "";
  const nsel = [...pinned].filter(id => rows.some(r => r.id === id)).length;
  document.getElementById("rm-btn").style.display = nsel ? "" : "none";
  document.getElementById("summary").textContent =
    `${rows.length} shown` + (nsel ? ` · ${nsel} selected` : "");
  document.getElementById("empty").style.display = rows.length ? "none" : "";
  rows.forEach(l => {
    const tr = document.createElement("tr");
    if (pinned.has(l.id)) tr.classList.add("pinned");
    tr.onclick = () => { pinned.has(l.id) ? pinned.delete(l.id) : pinned.add(l.id); render(); };
    const sc = l.score || 0;
    const cls = sc >= 70 ? "s-high" : sc >= 40 ? "s-mid" : "s-low";
    const reasons = (l.reasons || []).join(" · ");
    const domain = (l.website || "").replace(/https?:\\/\\//, "").replace(/\\/.*/, "").slice(0, 30);
    tr.innerHTML = `
      <td><span class="score ${cls}">${sc}</span></td>
      <td>${esc(l.name)}<span class="tag">${esc(reasons)}</span></td>
      <td>${esc(l.city || "")}</td>
      <td>${l.rating != null ? l.rating + "★" : "—"}</td>
      <td>${l.review_count != null ? l.review_count : "—"}</td>
      <td>${l.phone ? esc(l.phone) : "—"}</td>
      <td>${l.email ? '<a href="mailto:'+esc(l.email)+'">'+esc(l.email)+'</a>' : "—"}</td>
      <td>${domain ? '<a href="'+esc(l.website)+'" target="_blank">'+esc(domain)+'</a>' : "—"}</td>
      <td>${esc(l.source === "google_places" ? "Google" : "OSM")}</td>`;
    tb.appendChild(tr);
  });
}
function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function exportSelected() { dl(leads.filter(l => pinned.has(l.id)), "selected-leads.json"); }
function exportAll() {
  const q = document.getElementById("q").value.toLowerCase();
  dl(leads.filter(l => {
    if (filters.email && !l.email) return false;
    if (filters.phone && !l.phone) return false;
    if (filters.website && !l.website) return false;
    if (filters.no_website && l.website) return false;
    if (filters.high && (l.score||0) < 70) return false;
    if (q && !(l.name||"").toLowerCase().includes(q) && !(l.city||"").toLowerCase().includes(q)
        && !(l.email||"").toLowerCase().includes(q)) return false;
    return true;
  }), "leads.json");
}
function dl(data, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
  a.download = name; a.click();
}
function removeSelected() {
  if (!confirm("Remove " + pinned.size + " leads? They won't appear in future searches.")) return;
  const rm = [...pinned];
  leads = leads.filter(l => !pinned.has(l.id));
  rm.forEach(id => EXCLUDED.add(id));
  pinned.clear();
  // persist to excluded.json via a download (the CLI writes it too)
  dl([...EXCLUDED], "excluded.json");
  render();
}
render();
</script>
</body>
</html>
"""

def write_results_html(leads: list[dict], path: str = "results.html") -> str:
    excluded = sorted(load_excluded())
    html = _RESULTS_HTML.replace("__DATA__", json.dumps(leads))
    html = html.replace("__EXCLUDED__", json.dumps(excluded))
    p = Path(path)
    p.write_text(html, encoding="utf-8")
    return str(p.resolve())

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("place", nargs="?",
                   help='e.g. "Hamilton, Ontario" or "Brooklyn, New York"')
    p.add_argument("category", nargs="?", choices=sorted(CATEGORIES),
                   help="trade to search for (see --list-categories)")
    p.add_argument("--query", help="free-text search (Google Places only, any trade)")
    p.add_argument("--radius", type=int, default=8000, help="metres (default 8000)")
    p.add_argument("--limit", type=int, default=20, help="max results (default 20)")
    p.add_argument("--mode", choices=MODES, default="no_website",
                   help="no_website (default) | has_website | both")
    p.add_argument("--source", choices=SOURCES, default="auto",
                   help="auto (default) | api | scrape | osm")
    p.add_argument("--depth", type=int, default=5,
                   help="web scrape depth 1–20 (default 5, higher = more results + slower)")
    p.add_argument("--include-chains", action="store_true")
    p.add_argument("--no-email", action="store_true",
                   help="skip email discovery (faster, less useful)")
    p.add_argument("--review", action="store_true",
                   help="open results in the browser for interactive review")
    p.add_argument("--list-categories", action="store_true")
    args = p.parse_args()

    if args.list_categories:
        for cid, c in sorted(CATEGORIES.items()):
            print(f"  {cid:16s} {c['label']}")
        return

    if not args.place and not args.query:
        p.error("place is required (or pass --list-categories)")
    if not args.category and not args.query:
        p.error("category or --query is required")

    try:
        results = search(
            args.place or "", category=args.category, query=args.query,
            radius_m=args.radius, limit=args.limit, mode=args.mode,
            include_chains=args.include_chains, find_email=not args.no_email,
            source=args.source, scrape_depth=args.depth)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)

    if args.review:
        out = Path(__file__).resolve().parent.parent / "results.html"
        path = write_results_html(results, str(out))
        _say(f"\n  📄 {path}")
        webbrowser.open(f"file:///{path}")
    else:
        print(json.dumps(results, indent=2))

    # summary to stderr
    with_email = sum(1 for r in results if r.get("email"))
    high = sum(1 for r in results if (r.get("score") or 0) >= 70)
    _say(f"\n  Found: {len(results)} | With email: {with_email} | Score ≥ 70: {high}")

if __name__ == "__main__":
    main()
