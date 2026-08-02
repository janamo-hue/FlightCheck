# alaska-watch

Watches nonstop Alaska Airlines fares across a rolling 14-to-180-day window and
emails you when a price drops past a threshold. Runs entirely on GitHub Actions
and the Amadeus free tier. No servers, no database, no monthly bill.

## How it decides what to price

Amadeus gives 2,000 free Flight Offers Search calls per month. A 14-to-180-day
window is about 166 departure dates per route, so pricing every date every day
is off the table by two orders of magnitude. Instead:

| tier | cadence | what it covers |
| --- | --- | --- |
| sweep | weekly, one route per weekday | the full window at `sweep_stride_days` intervals |
| watchlist | every run | the `watchlist_size` cheapest date pairs the last sweep found |

The sweep spots slow drift out in the far window. The watchlist catches fast
drops on the dates you would actually book. Watchlist tasks are queued first, so
if a run hits the call budget the sweep is what gets truncated, not the dates
you care about.

A sweep that does not fit in one run is **resumed, not dropped**. The pairs
still owed live in `state.json` under `sweep_cycles`, and `last_sweep` is only
stamped once the cycle actually finishes. Backlogs from several routes are
interleaved round-robin so a long route cannot starve a short one. This matters
most on the very first run, when every route wants a full sweep at once.

When `depart_weekdays` is set, `sweep_stride_days` applies to the matching days,
so `[4]` with a stride of 2 means every second Friday.

Run `python -m src.budget` before adding routes:

```
route                          sweep   daily  per month
Seattle to San Diego              84       8        605
Seattle to Boston                 42       8        423
Seattle to Barcelona              56       8        484
total                                             1,512

Amadeus free tier: 2,000/month. Projected use: 76% of quota.
```

Three or four routes fit in the free tier. Past that, widen
`sweep_stride_days`, shrink `watchlist_size`, or shorten `window_end_days`.

## How it decides what is a drop

Baseline is the **median** of the trailing `baseline_days` of observations for
that exact route and date pair. Median rather than mean, so one fluke fare does
not drag the reference down and mask a genuine drop later.

Two triggers:

1. Price falls `drop_pct` below the baseline.
2. Price sets a new low within the last `atl_days`, by at least
   `all_time_low_margin_pct`. Without the margin a flat fare that ticks down a
   dollar alerts on every run; without the horizon a single cheap fare from
   eight months ago mutes the trigger permanently.

Both are gated by `min_observations`, so a brand new route stays quiet until it
has history. After an alert fires, that date pair is silent for
`debounce_hours` unless the price falls a further `realert_pct`.

## Setup

1. **Amadeus.** Sign up at developers.amadeus.com and create an app. You get
   test keys immediately, but the test environment serves cached and incomplete
   inventory, so it is only good for wiring things up. Move the app to
   production for real fares. The free 2,000 calls/month applies in production;
   I'm not certain whether the current flow requires a card on file before the
   free quota unlocks, so check that when you sign up. Set `AMADEUS_ENV=test`
   while developing.

2. **Resend.** Get an API key. Verify a sending domain, or send from
   `onboarding@resend.dev` to yourself while testing.

3. **Repo secrets** (Settings → Secrets and variables → Actions):
   `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`, `RESEND_API_KEY`,
   `ALERT_EMAIL_TO`. Add repo *variables* `AMADEUS_ENV` and `ALERT_EMAIL_FROM`.

4. **Edit `routes.yml`**, then check the budget and seed the history:

```bash
pip install -r requirements.txt
python -m src.budget
python -m src.scan --sweep --dry-run     # prices everything, writes nothing
python -m src.scan --sweep               # first real run, seeds baselines
```

Alerts stay quiet for the first few days until each date pair has
`min_observations` of history. That is intended.

## Usage

```bash
python -m src.scan              # what the cron does
python -m src.scan --dry-run    # no email, no writes
python -m src.scan --sweep      # force a full sweep now
python -m src.scan --limit 20   # cap API calls
python -m pytest tests -q
```

The workflow runs daily at 14:10 UTC and commits `data/` back to the repo.
`workflow_dispatch` exposes sweep and dry-run toggles.

## Storage

`data/history.jsonl` is append-only, one observation per line, committed by CI.
Chosen over Postgres so there is nothing to provision and git diffs stay
readable. If it outgrows this, reimplement `load_history` and `append` against
Supabase and nothing else changes.

Once a departure date passes, its rows move to `data/archive.jsonl` rather than
being deleted. The live file stays small enough for fast median lookups, and
the archive accumulates the full price curve for every flight from 180 days out
to departure. That archive is the answer to "when does this route actually
bottom out", which is worth more than the alerts.

## Report

`python -m src.report` writes `docs/index.html`: per-route cards for the five
cheapest date pairs, each with an inline SVG price curve and the low marked.
No chart library and no build step, so it works from a `file://` URL or from
GitHub Pages. CI regenerates and commits it on every run. To publish, set
Settings > Pages to serve from `main` and the `/docs` folder.

## Quota

`state.json` carries a rolling monthly counter. The scanner stops once it hits
`quota_reserve_pct` of `monthly_call_quota`, so a heavy month degrades to
"nothing new today" instead of Amadeus rejecting calls and the tool going
quiet without explanation. `python -m src.budget` projects usage before you
add routes.

## Caveats worth knowing

- **These are GDS fares.** Alaska's web-only and Saver promos sometimes
  undercut what Amadeus returns, so the tool will occasionally miss a drop. It
  should not report a price that does not exist, which is the failure mode that
  actually matters.
- **`includedAirlineCodes=AS` means Alaska-marketed**, which can include a
  codeshare. `nonStop=true` keeps it to direct flights, and the alert email
  lists flight numbers so you can eyeball it.
- **Award fares are not covered.** Amadeus does not expose mileage pricing. If
  you want Mileage Plan award alerts that is a separate data source entirely.
- **Prices are per the configured `adults` count** and include taxes
  (`grandTotal`).
- **Alert links go to Google Flights**, not Alaska. Alaska publishes no
  deep-link format, so rather than ship a guessed URL that might land on an
  empty search, the email links to a Google Flights query for the same
  nonstop itinerary.
