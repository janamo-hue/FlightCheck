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

## Points: redeem or pay

Atmos Rewards prices Alaska- and Hawaiian-operated awards from a **distance-band
floor with dynamic pricing above it**. The floor is published; what an award
actually costs on a given date is not, and no free API exposes it. seats.aero's
partner API is the realistic source and it needs a paid Pro subscription.

So this tool does not try to quote award prices. It computes the best case
instead: `cash_price / award_floor_points x 100`, in cents per point. That
answers "if saver space exists at the chart floor, does redeeming beat paying?"
which is enough to tell you **when checking award space is worth the trouble**.
You still do the actual award search by hand on alaskaair.com, but only on the
dates the tool flags.

Set `award_floor_points` per route from Alaska's North America award chart, and
`point_value_cents` to your own valuation. Published third-party valuations of
an Atmos point cluster around 1.5 cents, with Alaska-operated saver economy
typically landing between 1.3 and 1.6. Leave `award_floor_points` unset to turn
the analysis off for a route, which is right for connecting itineraries where a
single distance band does not apply.

Two things follow:

- **Every alert carries a verdict**, redeem or pay cash, from the cents per
  point. This is a property of the fare, not of why the alert fired: a cheap
  fare on a route with a low floor is genuinely both a good cash deal and good
  point value.
- **`spike_pct` adds an upward trigger.** For cash, a fare rising is not news.
  For points it is the whole signal, because an expensive cash fare is exactly
  when burning points pays. Spikes only alert when redeeming would also clear
  `redeem_above_cents`, so an unaffordable flight with bad point value stays
  quiet.

### Excluding Saver fares

`exclude_saver: true` (the default) makes the scanner track the cheapest
**non-Saver** fare instead of the cheapest fare overall.

This costs nothing extra. Amadeus has no branded-fare filter on the Flight
Offers Search endpoint, and the fare-rule flags that come closest live on the
POST variant and filter on restrictions rather than on the brand. But a single
call already returns many offers, so `max_offers` pulls a pool and the cheapest
non-Saver is picked from what we already paid for. Same call count.

Two deliberate behaviours:

- **Offers with no brand label are kept.** Dropping them would silently empty
  out any route where Amadeus omits `brandedFare`, which is precisely the
  invisible failure the doctor exists to catch.
- **If every offer is a Saver, the search reports nothing** rather than
  quietly falling back to a fare that earns no points.

The Saver price is still recorded for comparison, so alerts can show what the
exclusion costs: `USD 59 above the Saver at USD 209, 2.8c per point earned`.
That last figure is the real decision. Saver earns nothing, so the premium buys
the entire distance-based accrual. Below your `point_value_cents` it is cheaper
to buy up than to acquire the points any other way; above it, take the Saver
and buy points elsewhere. Set `distance_miles` per route to get it.

Turn exclusion off per route with `exclude_saver: false` if you only care about
the cheapest cash fare and not about earning.

### Earning: Saver fares are the trap

Atmos earns one point and one status point per mile flown, **excluding Saver
fares**. Saver tickets issued from mid-2026 for travel from August 2026 earn
zero redeemable and status points, down from a reduced rate before that.

This matters because the cheapest offer on a route is very often the Saver, so
the fare this tool tracks is frequently the one that earns nothing. Alerts
therefore carry the branded fare and say so explicitly when it is a Saver. The
cheap-long-flight-earns-well intuition from the old distance-based program no
longer holds at the bottom of the fare ladder.

Detection reads `brandedFare` from the Amadeus response and matches loosely on
"SAVER". The exact string Alaska returns through the GDS is unverified, so
`python -m src.doctor` reports the brand strings it actually sees per route.
Check that line on the first real run and tighten the match if needed.

### Floors currently configured

| route | distance | band | floor |
| --- | --- | --- | --- |
| SEA-ABQ | 1,179 mi | up to 1,400 | 7,500 |
| SEA-MSY | 2,083 mi | 1,401 to 2,100 | 10,000, **unverified** |
| SEA-MEX | 2,334 mi | connecting | unset |

SEA-MSY sits 18 miles under the 2,101-mile boundary on great-circle distance,
and airlines use their own mileage figures, so it could fall either side. The
10,000 figure is a guess at the band price. Confirm both on the chart before
trusting that route's verdict.

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

4. **Edit `routes.yml`**, then run the preflight before anything else:

```bash
pip install -r requirements.txt
python -m src.doctor --skip-live   # config and credentials, no API calls
python -m src.doctor               # adds 3 live probes per route
```

There is also a manual `doctor` workflow in the Actions tab, so you can run it
against the repo secrets without a local checkout.

The check that matters is **service exists**. A route Alaska does not fly
nonstop returns no offers, and the scanner logs that at debug level and moves
on, so the tool would run cleanly forever and never alert. That looks exactly
like success. The doctor turns it into a failure you can see:

```
[ FAIL ] Seattle to New Orleans (SEA-MSY): service exists
         0 of 3 probes returned an offer. Either AS does not fly this route
         nonstop, or the codes are wrong. This route will never alert.
```

It also flags codeshares, since `includedAirlineCodes` filters on the
marketing carrier, and reports cents per point per route so you can sanity
check the award floors.

Once it comes back clean:

```bash
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

## Report and GitHub Pages

`python -m src.report` renders per-route cards for the five cheapest date
pairs, each with an inline SVG price curve, the low marked, and booking links.
No chart library and no build step, so it works from a `file://` URL.

CI publishes it to Pages automatically. The `publish` job rebuilds the page
after every scan and deploys it as a Pages artifact, so the repo does not
accumulate a regenerated HTML file every single day the way committing
`docs/` would. `actions/configure-pages` runs with `enablement: true`, which
switches Pages on the first time the workflow runs, so there is no manual
setup in repo settings.

The job checks out `main` rather than the triggering commit, because the scan
job has already pushed that run's observations and the report needs them.

Set the repo variable `REPORT_URL` to the published address, typically
`https://<owner>.github.io/<repo>/`, and every alert email links to it.

## Links in alerts

Each alert carries two links, because no single one does both jobs:

- **See these dates** goes to Google Flights for the exact departure and
  return. It is the only source here that can be pinned to specific dates.
- **Book on Alaska** goes to `alaskaair.com/en/flights-from-{city}-to-{city}`,
  a real route page with its own fare calendar. Verified to resolve. It is not
  date-specific, which is why it complements rather than replaces the first.

City slugs come from the route `name`, so "Seattle to New Orleans" gives
`seattle` and `new-orleans`. Override with `origin_city` and
`destination_city` when the derived slug is wrong.

Alaska publishes no dated deep-link format, and an earlier version of this
tool shipped guessed query parameters for one. That was removed rather than
risk a link landing on an empty search.

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
