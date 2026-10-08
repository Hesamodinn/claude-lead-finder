---
name: lead-finder
description: >
  Find and qualify local businesses by trade and location. Three discovery sources
  (Google Places API, web scraper, OpenStreetMap), multi-signal deduplication,
  email extraction, deterministic scoring, and an interactive results browser.
  Triggers on requests like "find dentists in Denver" or "build a lead list of
  electricians near Folsom, CA".
  Works with Claude Code, Cursor, Codex CLI, Windsurf, Cline, Aider, or any
  assistant that can run Python scripts and read JSON output.
user-invocable: true
argument-hint: "[trade] in [city]"
license: MIT
metadata:
  author: Hesamodinn
  version: "2.2.0"
  category: marketing
---

# Lead Finder

Tested Python scripts in `scripts/` do the actual work — run them and present their
output. Do not try to find businesses by reasoning or web search alone.

This skill is portable: copy the `skills/lead-finder/` folder into any project's
`.claude/skills/` directory and it will auto-trigger on lead-finding requests.

## Commands

| Command | Action |
|---------|--------|
| `/lead-find <trade> in <city>` | Find and qualify businesses, return a scored table and results browser |
| `/lead-batch <file> [--city "City"]` | Run many searches as one job |
| `/lead-setup` | Health-check all discovery sources |
| `/lead-jobs` | List cached search results |

Plain language works too. The skill triggers on requests like:
- "find electricians near Folsom, CA"
- "build me a lead list of dentists in Denver, CO"
- "pull Google Maps listings for plumbers in Phoenix"
- "search for coffee shops in Hamilton, Ontario with email"

## Discovery sources (best available wins)

| Priority | Source | Needs | Speed | Coverage | ToS |
|----------|--------|-------|-------|----------|-----|
| 1 | Google Places API | `GOOGLE_PLACES_API_KEY` | ~2 s | Excellent | ✅ Compliant |
| 2 | Web scraper (Playwright / stdlib) | Nothing (built-in) | 5–15 s | Good | ⚠️ Violates Google ToS |
| 3 | OpenStreetMap | Nothing | 5–30 s | Sparse | ✅ Free / open |

`--source auto` (default) tries them in order. Force one with `--source api|scrape|osm`.

## Finding businesses

```
python scripts/find_businesses.py "<place>" <category> [options]
python scripts/find_businesses.py --query "free text" "<place>" [options]
python scripts/find_businesses.py --list-categories
```

17 built-in categories: restaurant, salon, gym, dentist, clinic, electrician, plumber,
trades, auto, lawyer, realestate, accountant, insurance, cleaner, petcare, photographer,
tutor. Free-text `--query` works for anything else (needs Google API or browser scraper).

Key flags:
- `--mode no_website` (default) | `has_website` | `both`
- `--source auto` | `api` | `scrape` | `osm`
- `--depth N` — browser scrape depth (default 5, more = slower + more results)
- `--limit N` — max results (default 20)
- `--radius N` — metres (default 8000)
- `--no-email` — skip email extraction (faster)
- `--review` — open interactive results browser
- `--include-chains` — include franchise branches

Output is JSON to stdout. Every result carries `score` (0–100) and `reasons`.

## Checking presence

```
python scripts/check_presence.py "<business name>" [website]
```

Scans the business's own website for social/directory links. Returns `{platform: url}`.

## Presenting results

1. Summarise: total found, with email, score ≥ 70.
2. For top leads, explain *why* (the `reasons` field).
3. Offer to check social presence of specific businesses.
4. Offer to export as JSON or CSV.

## Responsible use

The web scraper fetches Google search and Maps pages directly when `--source scrape`.
- Start at depth 5 and raise only when needed.
- Watch for empty results — it means Google is rate-limiting. Wait a few minutes.
- Suggest `GOOGLE_PLACES_API_KEY` for production use.
- For large requests, flag the risk, then proceed.
- Refuse only clearly abusive use (mass scraping, reselling raw data).
