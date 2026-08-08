# FlightCheck

Watches nonstop Alaska Airlines fares across a rolling 14-to-180-day window and
emails you when a price drops past a threshold. Runs entirely on GitHub Actions,
reading fares from alaskaair.com's own results page with a headless browser. No
servers, no database, no paid API, no monthly bill.

## Where the prices come from

The fare source is the public results page, loaded in a headless Chromium via
Playwright and parsed from the rendered DOM. Amadeus Self-Service, the previous
source, was decommissioned 2026-07-17, and Alaska publishes no free fare API, so
the tool reads the page a traveller would see. Each nonstop flight renders as a
`flight-card-N` with a fare cell per brand column (`SAVER`, `MAIN`, `PREMIUM`,
`FIRST`); the scraper reads the column headers to label prices rather than
assuming an order, and refuses to guess when the markup does not line up. The
reverse-engineering trail is in [spikes/alaska/](spikes/alaska/).

Because this runs on GitHub's shared datacenter IPs, bot detection is the open
risk: Alaska served real fares to a headless browser from a residential IP
during the spike, but a datacenter IP is more likely to be challenged. The
`alaska-probe` workflow is a manual + weekly canary that prices one date and
fails loudly if the grid stops rendering, so a silent block shows up as a red
run instead of empty scans.

## How it decides what to price

Pricing every date in a 14-to-180-day window every day is off the table: that is
about 166 departure dates per route, and hammering the site that hard is neither
necessary nor unobtrusive. Instead there are two tiers:

| tier | cadence | what it covers |
| --- | --- | --- |
| sweep | weekly, one route per weekday | the full window at `sweep_stride_days` intervals |
| watchlist | every run | the `watchlist_size` cheapest date pairs the last sweep found |

The sweep spots slow drift out in the far window. The watchlist catches fast
drops on the dates you would actually book. Watchlist tasks are queued first, so
if a run hits the per-run budget the sweep is what gets truncated, not the dates
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
route                          sweep   per run  per month
Seattle to Albuquerque            17        3        254
Seattle to New Orleans            17        3        254
total                                                508

2 run(s)/day. Page-load budget: 2,000/month. Projected use: 25%.
```

The 2,000/month figure is a self-imposed ceiling on page loads, not an API
allowance. If a config projects over it, widen `sweep_stride_days`, shrink
`watchlist_size`, or shorten `window_end_days`.

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

This costs nothing extra. A single results page already renders every brand
column, so the cheapest non-Saver is picked from what the page already loaded:
same page, same cost. And because Alaska labels the `SAVER` column explicitly in
the grid, the exclusion is exact here, not the loose brand-string match the old
Amadeus client had to make.

Two deliberate behaviours:

- **If a brand column is unlabelled or the grid does not line up, the scraper
  raises rather than guess.** A mislabelled column that reads a Premium price as
  a Main fare is the exact failure the auditing below exists to catch, so the
  code fails loudly instead of recording a plausible wrong number.
- **If every fare on the page is a Saver, the search reports nothing** rather
  than quietly falling back to a fare that earns no points.

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
zero redeemable and status points.

This matters because the cheapest offer on a route is very often the Saver, so
the fare this tool tracks is frequently the one that earns nothing. Alerts
therefore carry the branded fare and say so explicitly when it is a Saver. The
brand comes straight from the grid's column header, so it is exact rather than
inferred; `python -m src.doctor` still reports the brand strings it sees per
route as a sanity check.

### Floors currently configured

| route | distance | band | floor |
| --- | --- | --- | --- |
| SEA-ABQ | 1,179 mi | up to 1,400 | 7,500 |
| SEA-MSY | 2,083 mi | 1,401 to 2,100 | 10,000, **unverified** |

SEA-MSY sits 18 miles under the 2,101-mile boundary on great-circle distance,
and airlines use their own mileage figures, so it could fall either side. The
10,000 figure is a guess at the band price. Confirm both on the chart before
trusting that route's verdict.

## Round trips are one search

A round trip is priced by a single results load with both dates, not by adding
two one-way searches:

    /search/results?O=SEA&D=ABQ&OD=2026-10-17&DD=2026-10-24&A=1&RT=true&locale=en-us

`DD` is the return date. It was found by probing candidates against the live
site ([spikes/alaska/rt_probe.py](spikes/alaska/rt_probe.py)); `ID`, `RD`,
`RTD`, `OD2`, `ID1`, `returnDate` and `IDate` all produced no round-trip grid.

An earlier version summed two one-ways on the assumption that Alaska prices
each direction independently. It does not. For SEA-ABQ on 17-24 Oct the
one-way legs are Saver 179 + 179 and Main 229 + 229, while the real round trip
is Saver 297 and Main 397: a flat $61 discount across every brand. Every
observation recorded before this change is high by that offset, which is why
they sit in [data/archive/](data/archive/) rather than feeding a baseline.

Two things follow beyond correctness. One page load per date pair instead of
two halves the scrape cost. And brand availability becomes real: Alaska only
offers brands bookable on that specific pairing, so a return leg with no Saver
inventory stops yielding a Saver fare that cannot be bought.

Note that an unrecognised URL parameter is silently ignored and still renders
a plausible one-way grid. That is why the probe accepts a candidate only when
all four brand prices match a real search, never because the page loaded.

## Auditing the data

The fare grid can parse cleanly, produce plausible numbers, and still be
wrong, so `python -m src.audit` checks the recorded observations themselves:
implausible totals, a flight number appearing more often than there are legs,
leg counts above what a nonstop allows, and a fully frozen brand ladder. It
runs in CI, gating on the committed data rather than only on the code.

That last check is deliberately narrow, and the reason is worth recording.
Alaska really does price Saver a flat $50 per leg below Main on SEA-ABQ, and
$55 on SEA-MSY, holding that differential while the fare itself swings by
hundreds. A constant Saver-to-Main gap looks exactly like a column misread and
is not one. What distinguishes them is the rest of the ladder: Main-to-Premium
moves across the same reads (65, 70, 74, 95, 120), which proves the columns
are aligned. An earlier version of this check flagged the constant gap alone
and was wrong. It now fires only when *every* adjacent gap is frozen, since
that is the case a fixed pricing rule cannot explain.

Similarly, a repeated flight number is not automatically a double read: AS331
out and AS331 back is a real rotation. Only a count above the leg count is a
defect.

Every observation stores the full brand-to-price ladder. Keeping only two
numbers per search is what made the question hard to settle in the first
place; with the ladder, one look answers it.

## Setup

1. **Install the scraper's browser.** The fare source drives a headless
   Chromium, so the browser binary has to be present in addition to the Python
   package:

   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

2. **Confirm the scraper still gets fares from a cloud IP.** This is the one
   thing the whole design rests on. Run the `alaska-probe` workflow from the
   Actions tab (or push a change to `src/alaska.py`, which triggers it), and
   check it prices a date. It also runs weekly as a canary.

3. **Resend.** Get an API key. Verify a sending domain, or send from
   `onboarding@resend.dev` to yourself while testing.

4. **Repo secrets** (Settings → Secrets and variables → Actions):
   `RESEND_API_KEY`, `ALERT_EMAIL_TO`. Add repo *variables* `ALERT_EMAIL_FROM`
   and, once Pages is live, `REPORT_URL`. No fare-source credentials are needed.

5. **Edit `routes.yml`**, then run the preflight before anything else:

```bash
python -m src.doctor --config-only   # routes.yml alone, no browser, no email
python -m src.doctor --skip-live     # adds the browser and email checks
python -m src.doctor                 # adds 3 live probes per route
```

The check that matters is **service exists**. A route Alaska does not fly
nonstop returns no fares, and the scanner logs that at debug level and moves
on, so the tool would run cleanly forever and never alert. That looks exactly
like success. The doctor turns it into a failure you can see:

```
[ FAIL ] Seattle to New Orleans (SEA-MSY): service exists
         0 of 3 probes returned an offer. Either AS does not fly this route
         nonstop, or the codes are wrong. This route will never alert.
```

It also flags regional operating carriers (Horizon `QX`, SkyWest `OO`) flying
as Alaska, and reports cents per point per route so you can sanity check the
award floors.

Once it comes back clean:

```bash
python -m src.budget
python -m src.scan --sweep --dry-run     # prices everything, writes nothing
python -m src.scan --sweep               # first real run, seeds baselines
```

Alerts stay quiet for the first few days until each date pair has
`min_observations` of history. That is intended.

## CI

Four jobs run on every push and pull request:

| job | what it guards |
| --- | --- |
| `lint` | `ruff check`. Formatting is checked but advisory. |
| `test` | Full suite on Python 3.11, 3.12 and 3.13, with a coverage table in the run summary. |
| `config` | `routes.yml` validates, page-load projection is under the ceiling, report renders, committed data audits clean. |
| `workflows` | Every workflow file parses and has a `jobs` block. |

That last job exists because a malformed workflow fails *silently* on GitHub:
it simply never runs, which looks exactly like nothing having triggered it.

The `config` job runs `python -m src.doctor --config-only`. Fork pull requests
have no secrets and no browser, and their absence is not a failure of the thing
being tested, so the credential and live checks are skipped there and left to
the manual `doctor` workflow.

Dependencies carry upper bounds and Dependabot proposes monthly bumps. This
runs unattended on a cron, so a breaking major release should fail a visible
PR rather than a Tuesday morning scan.

## Usage

```bash
python -m src.scan              # what the cron does
python -m src.scan --dry-run    # no email, no writes
python -m src.scan --sweep      # force a full sweep now
python -m src.scan --limit 20   # cap page loads this run
python -m src.alaska --from SEA --to ABQ --depart 2026-10-17 --return 2026-10-24
python -m pytest tests -q
ruff check src tests
```

Everything that touches live fares needs `playwright install chromium` first.
The workflow runs twice daily, at **06:10 and 18:10 Pacific**, and commits
`data/` back to the repo. `workflow_dispatch` exposes sweep and dry-run
toggles and bypasses the DST gate below.

GitHub cron is UTC only, so holding a fixed local time across DST takes four
entries and a gate job: 13:10 and 01:10 UTC are correct in daylight time,
14:10 and 02:10 in standard time. The gate reads the current Pacific hour and
drops whichever pair is an hour off, so exactly two runs happen either way and
the discarded ones show as skipped rather than as failures. Scheduling at :10
past means the usual Actions queueing delay cannot push a run into the next
local hour and get it discarded.

Only the watchlist re-check scales with cadence; sweeps stay weekly. Twice
daily takes the projection from 328 to 508 page loads a month, a quarter of
the self-imposed ceiling.

## Storage

`data/history.jsonl` is append-only, one observation per line, committed by CI.
Chosen over Postgres so there is nothing to provision and git diffs stay
readable. If it outgrows this, reimplement `load_history` and `append` against
Supabase and nothing else changes.

Once a departure date passes, its rows move to `data/archive/departed.jsonl`
rather than being deleted. The live file stays small enough for fast median
lookups, and the archive accumulates the full price curve for every flight from
180 days out to departure. That archive is the answer to "when does this route
actually bottom out", which is worth more than the alerts.

Alongside it, `data/archive/one-way-sum-inflated.jsonl` is a separate,
quarantined capture: observations from before round trips were priced as a
single search. They are systematically high and must never feed a baseline; see
[data/archive/README.md](data/archive/README.md).

## Report and GitHub Pages

`python -m src.report` renders per-route cards for the five cheapest date
pairs, each with an inline SVG price curve, the low marked, and booking links.
No chart library and no build step, so it works from a `file://` URL.

CI publishes it to Pages after every scan, deploying it as an artifact so the
repo does not accumulate a regenerated HTML file every single day the way
committing `docs/` would.

**One manual step, once:** Settings > Pages, source set to **GitHub Actions**.
`configure-pages` has an `enablement: true` option that claims to do this for
you. It does not work here: `GITHUB_TOKEN` cannot create a Pages site and the
job fails with "Resource not accessible by integration". Verified by a real
run, not assumed.

The job checks out `main` rather than the triggering commit, because the scan
job has already pushed that run's observations and the report needs them.

Set the repo variable `REPORT_URL` to the published address, typically
`https://<owner>.github.io/<repo>/`, and every alert email links to it.

## Links in alerts

Each alert carries two links, because no single one does both jobs:

- **See these dates** goes to Google Flights for the exact departure and
  return. It is the cleanest source here that can be pinned to specific dates
  from a plain-text query.
- **Book on Alaska** goes to `alaskaair.com/en/flights-from-{city}-to-{city}`,
  a real route page with its own fare calendar. Verified to resolve. It is not
  date-specific, which is why it complements rather than replaces the first.

The scraper's own dated `/search/results` deep link is deliberately *not* used
as a click target: it lands on a live search that re-runs on load rather than a
clean booking or confirmation page. City slugs come from the route `name`, so
"Seattle to New Orleans" gives `seattle` and `new-orleans`. Override with
`origin_city` and `destination_city` when the derived slug is wrong.

## Caveats worth knowing

- **Prices are exactly what alaskaair.com shows** for a nonstop, Alaska-marketed
  itinerary at the configured `adults` count. There is no GDS or third-party
  layer to disagree with; the tradeoff is that the scraper depends on the page's
  structure.
- **Datacenter-IP bot detection is the standing risk.** If Alaska starts
  challenging the GitHub runner, the scraper raises rather than silently
  recording "no service", the run's failure guard trips, and the `alaska-probe`
  canary goes red. It should never report a price that does not exist.
- **A markup change fails loudly.** The scraper reads brand columns from the
  page's own headers and refuses to guess when the tiles and headers do not line
  up, so a redesign surfaces as an error, not as mislabelled data.
- **Regional operators show through.** An Alaska-marketed nonstop may be flown
  by Horizon (`QX`) or SkyWest (`OO`). Those are still bookable and still earn
  points; the doctor flags them and the alert email lists flight numbers.
- **Award fares are not covered.** No free feed exposes mileage pricing. The
  redeem-or-pay verdict is a best-case cents-per-point at the chart floor, not a
  quote; check award space by hand before acting on it.
