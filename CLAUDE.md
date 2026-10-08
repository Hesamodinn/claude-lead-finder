# claude-lead-finder

Local lead-generation tool. Three discovery sources (Google Places API, browser scrape, OpenStreetMap), email extraction, deterministic scoring, interactive map UI.

## Running

- **Web UI**: `python server.py` → http://localhost:9100
- **CLI**: `python scripts/find_businesses.py "City, ST" category`
- **Web scraper**: built-in, no setup needed (scrapes Google directly)

## Commands

The `.claude/commands/` directory has slash commands for Claude Code:

| Command | What it does |
|---------|-------------|
| `/lead-find` | Find and qualify businesses by trade and location |
| `/lead-batch` | Run multiple searches from a keywords file |
| `/lead-setup` | Health-check all discovery sources |
| `/lead-jobs` | List cached search results |

Plain language works too — "find electricians near Denver with email" triggers the skill.

## Key files

- `scripts/find_businesses.py` — main search (all 3 sources, dedup, scoring, email)
- `scripts/_scraper.py` — direct Google web/Maps scraper (stdlib only)
- `scripts/_common.py` — shared helpers (HTTP, geocoding, email extraction)
- `scripts/check_presence.py` — social media presence checker
- `server.py` — local web server with SSE streaming + settings API
- `map.html` — interactive map UI (Leaflet + results table + export)

## No AI calls

This tool makes zero LLM/AI calls. All logic is deterministic Python using stdlib only.
