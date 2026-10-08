Health-check all discovery sources.

Usage: /lead-setup

Steps:
1. Check if `GOOGLE_PLACES_API_KEY` is set (in `.env` or environment).
2. Test the web scraper: run a minimal search to confirm Google isn't blocking.
3. Test OpenStreetMap: make a small Overpass query.
4. Report status of all three sources:
   - Google Places API: ✅ configured / ❌ no key
   - Web scraper: ✅ working / ⚠️ rate-limited
   - OpenStreetMap: ✅ available / ❌ down

If no Google key, link to https://console.cloud.google.com/apis/credentials
