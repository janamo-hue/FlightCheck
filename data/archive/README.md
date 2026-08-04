# Archived observations

`one-way-sum-inflated.jsonl` holds every observation recorded while round trips
were priced as two one-way searches added together.

Those totals are systematically high. Alaska discounts a round trip against two
one-ways by a flat amount per brand: on SEA-ABQ for 17-24 Oct the one-way legs
are Saver 179 + 179 and Main 229 + 229, while the real round trip is Saver 297
and Main 397. Every brand overstated by exactly $61.

The offset is per-route and was never measured on SEA-MSY, so these cannot be
corrected by subtraction. They are kept as a genuine capture of the one-way
grid, and must not feed a baseline: medians built from them would set every
threshold too high by roughly the offset.

Superseded by single round-trip loads using `DD` as the return date.
