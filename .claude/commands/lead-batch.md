Run multiple lead searches as one batch.

Usage: /lead-batch <keywords-file> [--city "City, ST"]

The keywords file has one trade per line (e.g. "electrician", "plumber", "dentist").
Each line becomes a separate search in the given city.

Steps:
1. Read the keywords file.
2. For each keyword, run `python scripts/find_businesses.py "<city>" <keyword>` or
   `--query "<keyword>"` if it's not a built-in category.
3. Collect all results, deduplicate across searches.
4. Present a combined summary with per-trade breakdowns.
5. Offer to export the full batch as JSON or CSV.

If no city is given, ask for one.
