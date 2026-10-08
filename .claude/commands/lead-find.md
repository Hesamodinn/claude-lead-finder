Find and qualify local businesses.

Usage: /lead-find <business type> in <city, state/province>

Examples:
- /lead-find electricians in Folsom, CA
- /lead-find dentists in Hamilton, Ontario
- /lead-find coffee shops in Brooklyn, NY --limit 50

Steps:
1. Run `python scripts/find_businesses.py "<city>" <category>` with the right flags.
   Use `--list-categories` to check if the trade is a built-in category.
   For trades not in the list, use `--query "<trade>"` (needs Google API or Docker scraper).
2. Add `--review` to generate the interactive results browser.
3. Present a summary: total found, how many have email, how many score ≥ 70.
4. For the top leads (score ≥ 70), explain *why* they scored well (the `reasons` field).
5. Offer to check social media presence of specific businesses.

Key flags: `--mode no_website|has_website|both`, `--source auto|api|scrape|osm`,
`--radius N` (metres), `--limit N`, `--depth N` (scraper), `--no-email`.
