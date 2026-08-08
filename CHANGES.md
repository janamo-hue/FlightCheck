# CHANGES

2026-08-04 [scope] Amadeus Self-Service API decommissioned 2026-07-17 (keys disabled); FlightCheck's data source is dead and must be replaced.
2026-08-04 [decision] Replace Amadeus with direct Alaska scraping (Option A) over Duffel/Enterprise, to keep the free/no-bill design.
2026-08-04 [note] Spike (spikes/alaska/) found: real dated deep link /search/results?O=&D=&OD=&A=&RT=&locale=; no fare-list JSON API; fares render into DOM (data-testid flight-card-*, SAVER/MAIN columns); shoulderDates API gives per-date price calendar (no brand). Headless local IP served real fares; cloud-IP viability still unproven.

2026-08-07 [code] Finished Amadeus->Alaska migration: doctor.py now probes fares via the scraper (headless-browser check replaces Amadeus auth); deleted src/amadeus.py + test_amadeus.py; dropped AMADEUS_* from doctor.yml and .env.example.
2026-08-07 [code] Fixed bugs: alert color keyed on a nonexistent 'redeem' kind; doctor projection ignored runs_per_day; removed dead QuotaExceeded, seats_left, fare_basis, is_saver.
2026-08-07 [doc] Rewrote README for the Alaska-scraping architecture (was Amadeus-centric) and purged stale Amadeus references from comments and config.
