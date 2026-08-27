# CHANGES

2026-08-04 [scope] Amadeus Self-Service API decommissioned 2026-07-17 (keys disabled); FlightCheck's data source is dead and must be replaced.
2026-08-04 [decision] Replace Amadeus with direct Alaska scraping (Option A) over Duffel/Enterprise, to keep the free/no-bill design.
2026-08-04 [note] Spike (spikes/alaska/) found: real dated deep link /search/results?O=&D=&OD=&A=&RT=&locale=; no fare-list JSON API; fares render into DOM (data-testid flight-card-*, SAVER/MAIN columns); shoulderDates API gives per-date price calendar (no brand). Headless local IP served real fares; cloud-IP viability still unproven.

2026-08-07 [code] Finished Amadeus->Alaska migration: doctor.py now probes fares via the scraper (headless-browser check replaces Amadeus auth); deleted src/amadeus.py + test_amadeus.py; dropped AMADEUS_* from doctor.yml and .env.example.
2026-08-07 [code] Fixed bugs: alert color keyed on a nonexistent 'redeem' kind; doctor projection ignored runs_per_day; removed dead QuotaExceeded, seats_left, fare_basis, is_saver.
2026-08-07 [doc] Rewrote README for the Alaska-scraping architecture (was Amadeus-centric) and purged stale Amadeus references from comments and config.
2026-08-07 [code] Scraper resilience: bounded retry/reload with jittered backoff, a politeness delay between page loads, browser-crash recovery, and an HTTP-status blocked-vs-unserved classifier in src/alaska.py.
2026-08-07 [code] Ops/CI hardening: debit page loads on guard-tripped runs; fix rt-probe/doctor workflow script injection via env vars; scan.yml rebase+retry push and least-privilege permissions; .gitignore.
2026-08-07 [code] DRY: shared page-load projection (planner) and booking-link builders (src/links.py); ARCHIVE_PATH moved under data/archive/. Added parser/resilience/notify/report/doctor/links unit tests.
2026-08-07 [code] doctor: a Resend "Sending access" key 401s on GET /domains but sends fine; check_email now treats a restricted_api_key 401 as valid instead of a false FAIL.

2026-08-27 [feat] alerts: added absolute price targets (target_price, saver_target_price per route). They compare against a configured number rather than a fare's own past, so they fire on first sighting and ignore min_observations.
2026-08-27 [note] Motivation: 0 alerts in 28 runs / 311 observations. SEA-ABQ median == min (397): a fare at its floor has baseline == current, so drop is always 0% and it can never set a new low. The relative triggers were structurally unable to fire for the fares watched most.
2026-08-27 [feat] alerts: Saver is now alerted on via its own target, while staying excluded from the priced fare. Saver runs ~100 USD under MAIN (297 vs 397 on ABQ, 377 vs 487 on MSY).
2026-08-27 [code] alerts: target kind takes precedence over drop/low/spike; MAIN and Saver debounce independently; _debounced and record_alert now key off the rung that triggered rather than the MAIN price.
2026-08-27 [code] Added src/targets.py, a read-only tuner that replays history.jsonl against configured and candidate targets.
2026-08-27 [decision] Targets set ~5% under each observed floor (ABQ 377/282, MSY 463/358), so they are silent against all stored history by design. Fares sit in discrete buckets: any target at the floor would fire on 60% of observations.
