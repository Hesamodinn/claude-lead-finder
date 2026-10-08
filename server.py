#!/usr/bin/env python3
"""Local server for the Lead Finder map UI.

    python server.py

Opens http://localhost:9100 — the map page with a Run button that executes searches
and streams logs live. No external dependencies.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Make scripts/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

PORT = int(os.environ.get("PORT", "9100"))
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _cache_key(query, place, radius, mode, limit):
    raw = f"{query}|{place}|{radius}|{mode}|{limit}"
    return hashlib.md5(raw.lower().encode()).hexdigest()


def _load_cache(key):
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_cache(key, results):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f)


class Handler(SimpleHTTPRequestHandler):
    """Serves static files + API endpoints."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.path = "/map.html"
            return super().do_GET()
        if parsed.path == "/api/search":
            return self._handle_search(parse_qs(parsed.query))
        if parsed.path == "/api/sources":
            return self._handle_sources()
        if parsed.path == "/api/settings":
            return self._handle_get_settings()
        if parsed.path == "/api/cache-list":
            return self._handle_cache_list()
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if parsed.path == "/api/settings":
            return self._handle_set_settings(body)
        if parsed.path == "/api/export-csv":
            return self._handle_export_csv(body)
        self.send_error(404)

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- settings (read/write .env) ---

    def _handle_get_settings(self):
        env = _read_env()
        self._json_response({
            "google_api_key": env.get("GOOGLE_PLACES_API_KEY", ""),
        })

    def _handle_set_settings(self, body):
        env = _read_env()
        if "google_api_key" in body:
            key = body["google_api_key"].strip()
            env["GOOGLE_PLACES_API_KEY"] = key
            os.environ["GOOGLE_PLACES_API_KEY"] = key
        _write_env(env)
        self._json_response({"ok": True})

    # --- sources ---

    def _handle_sources(self):
        import _scraper as scr
        self._json_response({
            "api": bool(os.environ.get("GOOGLE_PLACES_API_KEY", "")),
            "scraper": True,  # built-in, always available
            "playwright": scr.has_playwright(),
            "osm": True,
        })

    # --- CSV export ---

    def _handle_export_csv(self, body):
        rows = body.get("results", [])
        buf = io.StringIO()
        fields = ["name", "score", "rating", "review_count", "email", "phone",
                   "website", "address", "city", "category_label", "source"]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        data = buf.getvalue().encode("utf-8-sig")  # BOM for Excel
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=leads.csv")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- cache list ---

    def _handle_cache_list(self):
        items = []
        if os.path.isdir(CACHE_DIR):
            for f in os.listdir(CACHE_DIR):
                if f.endswith(".json"):
                    path = os.path.join(CACHE_DIR, f)
                    try:
                        with open(path, encoding="utf-8") as fh:
                            data = json.load(fh)
                        items.append({
                            "key": f.replace(".json", ""),
                            "count": len(data),
                            "modified": os.path.getmtime(path),
                        })
                    except Exception:
                        pass
        self._json_response({"cache": items})

    # --- search (SSE stream) ---

    def _handle_search(self, params: dict):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def send_event(event: str, data: str):
            try:
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        # parse params
        query = (params.get("query") or [""])[0]
        place = (params.get("place") or [""])[0]
        radius = int((params.get("radius") or ["8000"])[0])
        mode = (params.get("mode") or ["no_website"])[0]
        limit = int((params.get("limit") or ["20"])[0])
        source = (params.get("source") or ["auto"])[0]
        depth = int((params.get("depth") or ["5"])[0])
        find_email = (params.get("email") or ["1"])[0] == "1"
        use_cache = (params.get("cache") or ["1"])[0] == "1"

        # check cache
        ck = _cache_key(query, place, radius, mode, limit)
        if use_cache:
            cached = _load_cache(ck)
            if cached is not None:
                send_event("log", f"📦 Loaded {len(cached)} cached results")
                send_event("results", json.dumps(cached))
                with_email = sum(1 for r in cached if r.get("email"))
                high = sum(1 for r in cached if (r.get("score") or 0) >= 70)
                send_event("log", f"📊 With email: {with_email} | Score ≥ 70: {high}")
                send_event("done", "")
                return

        send_event("log", f"🔍 Searching: {query or 'businesses'} near {place}")

        import find_businesses as fb

        old_stderr = sys.stderr
        log_buf = io.StringIO()

        class TeeStderr:
            def write(self, s):
                log_buf.write(s)
                for line in s.strip().splitlines():
                    line = line.strip()
                    if line:
                        send_event("log", line)
            def flush(self):
                pass

        sys.stderr = TeeStderr()

        # report which sources are available
        has_api = bool(os.environ.get("GOOGLE_PLACES_API_KEY", ""))
        sources = []
        if has_api:  sources.append("Google API ✓")
        sources.append("Web scraper ✓")
        sources.append("OpenStreetMap ✓")
        send_event("log", f"Sources: {' · '.join(sources)}")

        # resolve query to a built-in category (fuzzy matching)
        from difflib import SequenceMatcher
        category = None
        if query:
            q_low = query.lower().rstrip("s")
            best_ratio, best_id = 0.0, None
            for cid, cat in fb.CATEGORIES.items():
                names = [cid, cat["label"].lower()] + [t.lower() for t in cat.get("google_terms", ())]
                for n in names:
                    n_stripped = n.rstrip("s")
                    if q_low == n_stripped:
                        best_ratio, best_id = 1.0, cid
                        break
                    if len(q_low) >= 4:
                        ratio = SequenceMatcher(None, q_low, n_stripped).ratio()
                        if ratio > best_ratio:
                            best_ratio, best_id = ratio, cid
                if best_ratio == 1.0:
                    break
            if best_id and best_ratio >= 0.75:
                category = best_id
                query = None

        try:
            results = fb.search(
                place=place,
                query=query if query else None,
                category=category,
                radius_m=radius,
                limit=limit,
                mode=mode,
                find_email=find_email,
                source=source,
                scrape_depth=depth,
            )
            send_event("log", f"✅ Done — {len(results)} results")

            with_email = sum(1 for r in results if r.get("email"))
            high = sum(1 for r in results if (r.get("score") or 0) >= 70)
            send_event("log", f"📊 With email: {with_email} | Score ≥ 70: {high}")

            # save to cache
            _save_cache(ck, results)

            send_event("results", json.dumps(results))
        except Exception as exc:
            send_event("error", str(exc)[:300])
        finally:
            sys.stderr = old_stderr

        send_event("done", "")

    def log_message(self, format, *args):
        if "/api/" not in (args[0] if args else ""):
            super().log_message(format, *args)


# --- .env read/write ---

def _read_env():
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _write_env(env):
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    # update existing keys, append new ones
    written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in env:
                new_lines.append(f"{k}={env[k]}\n")
                written.add(k)
                continue
        new_lines.append(line)
    for k, v in env.items():
        if k not in written and v:
            new_lines.append(f"{k}={v}\n")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\n  Lead Finder running at http://localhost:{PORT}\n", flush=True)
    print("  Press Ctrl+C to stop.\n", flush=True)
    threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
