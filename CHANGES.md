# CHANGES

2026-08-04 [scope] Amadeus Self-Service API decommissioned 2026-07-17 (keys disabled); FlightCheck's data source is dead and must be replaced.
2026-08-04 [decision] Replace Amadeus with direct Alaska scraping (Option A) over Duffel/Enterprise, to keep the free/no-bill design.
2026-08-04 [note] Spike (spikes/alaska/) found: real dated deep link /search/results?O=&D=&OD=&A=&RT=&locale=; no fare-list JSON API; fares render into DOM (data-testid flight-card-*, SAVER/MAIN columns); shoulderDates API gives per-date price calendar (no brand). Headless local IP served real fares; cloud-IP viability still unproven.
