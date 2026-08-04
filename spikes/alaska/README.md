# Alaska fare-discovery spike

Throwaway exploration, **not shipping code**. Purpose: decide whether we can
replace the decommissioned Amadeus Self-Service API by reading fares straight
from alaskaair.com (see the migration decision in the repo history).

## What it does

Drives a real Chromium browser to Alaska fares and captures the network so we
can see the shopping endpoint and its JSON shape:

- Records a full **HAR** (`session.har`) with request + response bodies embedded.
  This is the artifact you convert to "copy as cURL" for the lighter client later.
- Saves every fare-like JSON response to `out/.../responses/*.json` plus a
  `.meta` file with the request URL, method, headers, and POST body.
- Falls back to a DOM scrape for a quick "did fares even render" signal.
- Saves `final.png` / `final.html` (and `error.*` on failure) every run.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install playwright
playwright install chromium
```

## Run

```bash
# Route-page mode (default): the /en/flights-from-SEA-to-SFO page is verified
# to resolve (src/notify.py:alaska_url) and renders a fare calendar.
python spikes/alaska/probe.py --from SEA --to SFO --depart 2026-09-15

# Booking-form mode: fill the homepage widget, one-way, and submit.
python spikes/alaska/probe.py --mode form --from SEA --to SFO --depart 2026-09-15

# Watch it locally (never in CI):
python spikes/alaska/probe.py --from SEA --to SFO --depart 2026-09-15 --headed --slowmo 250
```

Exit code is `0` if any fare-like JSON was captured, else `1`.

## The GO / NO-GO test

The project runs in **GitHub Actions on shared cloud IPs**. Bot protection on
airline shopping endpoints is far more likely to challenge a datacenter IP than
your home browser. So the real question is not "does it work on my laptop" but:

> Does it still capture fare JSON when run from a cloud IP?

Run it **both** locally and from a throwaway Actions job. If the cloud run comes
back with `NO fare-like JSON captured` and a challenge page in `final.png`,
scraping-from-CI is not viable and we revisit Option B (Duffel) or move the
scraper somewhere with a residential IP.

## Reading the output

1. `run.log` / stdout — the summary lists every captured endpoint.
2. `responses/*.json` — confirm each nonstop flight has a **price** and a
   **fare brand** we can match `SAVER` against. That is the whole contract the
   rest of the app needs (`src/amadeus.py:Offer`).
3. `session.har` — find the request that produced the fare JSON; its URL,
   headers, and body become the seed for the "copy as cURL" replay client.

## Findings from the first runs (2026-08-04, local IP)

- **Route pages are marketing, not fares.** `/en/flights-from-...` is served by
  AirTRFX (`trfx-static` assets) and uses **city-name slugs, not IATA codes**
  (see `src/notify.py:alaska_url`, which passes `alert.cities`). With IATA codes
  it 404s. Real fares are not here regardless.
- **Alaska is behind Cloudflare bot management** (`cdn-cgi/challenge-platform`
  fired). It did not hard-block a local browser, but this is the thing most
  likely to challenge a cloud IP. Still the open GO/NO-GO question.
- **The booking widget is shadow-DOM web components** (Alaska "Auro" custom
  elements). Naive `get_by_label`/`get_by_placeholder` cannot pierce shadow
  roots, so scripted form-driving needs component-aware selectors (or the
  copy-as-cURL path below, which sidesteps form-driving entirely).

Net: browser-driving the form is blocked by shadow DOM + Cloudflare. The
**copy-as-cURL** path (capture the shopping XHR once from DevTools, replay it)
is the more promising route from here.

## BREAKTHROUGH findings (2026-08-04, capture via discovered deep link)

A user DevTools paste of Quantum Metric analytics beacons leaked the real
results-page URL in its `u=` param. This overturns the repo's assumption
(`src/notify.py:booking_url`) that Alaska has no dated deep link:

    https://www.alaskaair.com/search/results?O=SEA&D=SFO&OD=2026-08-19&A=1&RT=false&locale=en-us
    # O=origin  D=dest  OD=outbound date  A=adults  RT=roundtrip  locale

Loading that URL in headless Chromium (probe.py --mode url) and inspecting the
HAR + rendered DOM established the actual data architecture:

- **There is NO fare-list JSON API.** The only Alaska fare calls are:
  - `POST /search/api/shoulderDates` -> a per-date **price calendar**:
    `{date, price, awardPoints, isDiscounted, solutionId}` x31. Clean and
    cheap, but `flightSegments: []` and **no fare brand** (can't tell Saver).
  - `GET /search/api/citySearch/getAllAirports` -> airport list.
- **The per-flight fare grid is rendered client-side into the DOM** with stable
  `data-testid` hooks (verified on SEA-SFO, 10 nonstop flights):
  - `flight-card-0..9`, `flight-details-N-stops-0` (0 = nonstop)
  - fare-brand columns: `columnheader-SAVER | MAIN | PREMIUM | FIRST`
  - price nodes carrying `$96` etc., `flight-card-badge-low-fare`
  - carrier `AS`, duration like `2h 19m`
- **Saver detection becomes EXACT.** Alaska labels a SAVER column, so
  `exclude_saver` = take the MAIN column price. No more loose string match
  (`src/amadeus.py:is_saver` apologises for guessing).
- **Viability signal (positive, local IP only):** the ad beacons show the UA was
  literally `HeadlessChrome/147`, yet Alaska served real fares. The `_fs-ch-*` /
  `check-detection` / `acd` calls are F5/Shape-style detection, but it did not
  block a headless local browser. **Still unproven from a cloud IP** - that is
  the remaining GO/NO-GO for running this in GitHub Actions.

### Resulting design for `src/alaska.py`

Playwright loads the deep link, waits for `[data-testid^="flight-card-"]`, and
per nonstop card reads the SAVER/MAIN column price + flight number + duration,
returning the same `cheapest_direct(...) -> Offer | None` the planner already
calls. Downstream (planner/store/alerts/report) is untouched. Optionally use
`shoulderDates` as a cheap first pass to pick which dates are worth a full grid
load. Open question: whether Playwright-in-CI clears F5/Shape from a cloud IP.

## If it doesn't work first try

Alaska's DOM and payloads change, so expect to iterate:

- **Form fill incomplete** → open `final.html`, find the real input selectors,
  update `try_fill_form` candidates.
- **No fare JSON but the page shows prices** → widen `FARE_HINTS` in `probe.py`
  to include a token you see in the actual XHR (inspect via `--headed` DevTools).
- **Challenge / empty page** → that is the NO-GO signal above, not a bug to fix.

## Next step after a green run

Copy the working request out of the HAR and prototype `src/alaska.py` exposing
the same `cheapest_direct(...) -> Offer | None` signature as `src/amadeus.py`,
so the planner/store/alerts/report code is untouched.
