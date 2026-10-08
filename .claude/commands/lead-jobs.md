List cached search results.

Usage: /lead-jobs

Steps:
1. Check the `data/cache/` directory for cached search results.
2. For each cache file, show: the search parameters (decoded from filename), result count, and when it was saved.
3. Offer to clear old cache entries if requested.

Cache files are JSON in `data/cache/`. They're loaded automatically when the same search is run again.
