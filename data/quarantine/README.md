# Quarantined observations

`2026-08-04-mislabelled-columns.jsonl` holds every observation recorded before
the fare-grid column mapping was fixed. The prices in it are wrong, and wrong
in a specific way: the value stored as `price` is one brand column too high,
and the value stored as `saver_price` is really the Main cabin fare.

The signature is a per-route constant: exactly $100 between the two figures on
all 17 SEA-ABQ rows and exactly $110 on all 16 SEA-MSY rows, holding steady
while the underlying fares ranged over $235 and $590 respectively.

It is kept rather than deleted because it is a real capture of the grid and is
useful for testing the parser, but it must never feed a baseline: a median
built from it would set every alert threshold about $100 too high.

`python -m src.audit --history data/quarantine/2026-08-04-mislabelled-columns.jsonl`
reproduces the finding.
