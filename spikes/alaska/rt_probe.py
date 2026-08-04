#!/usr/bin/env python3
"""Find Alaska's round-trip results deep link by trying candidate parameters.

The one-way link is known and shipping:

    /search/results?O=SEA&D=ABQ&OD=2026-10-17&A=1&RT=false&locale=en-us

What is not known is the return-date parameter. The scraper works around that
by loading two one-way searches and adding them, which the docstring asserted
was equivalent. It is not: on SEA-ABQ for 17-24 Oct the sum overstates every
brand by exactly $61 (Saver 358 vs 297, Main 458 vs 397, Premium 528 vs 467).
Alaska's own results page states "All fares are round-trip per passenger".

This tries candidate names, loads each in headless Chromium, and reports which
yields a fare grid. Ground truth comes from a screenshot of the real search, so
a candidate is accepted only if its prices match, not merely if the page
renders. An unrecognised parameter is silently ignored and still produces a
plausible one-way grid, which is exactly the trap that produced the $61.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urlencode

RESULTS = "https://www.alaskaair.com/search/results"
PRICE = re.compile(r"\$\s?([0-9][0-9,]*)")
BRANDS = ("SAVER", "MAIN", "PREMIUM", "FIRST")

# OD is the outbound date, so an inbound or return counterpart is the shape.
CANDIDATES = ["ID", "RD", "DD", "RTD", "OD2", "ID1", "returnDate", "IDate"]


def build(origin, dest, depart, ret, param, adults=1, rt=True):
    q = {"O": origin, "D": dest, "OD": depart, "A": adults,
         "RT": "true" if rt else "false", "locale": "en-us"}
    if param and ret:
        q[param] = ret
    return RESULTS + "?" + urlencode(q)


def read_grid(page, timeout_ms):
    """{brand: price} from the first card, or None if no grid appeared."""
    try:
        page.wait_for_selector('[data-testid^="flight-card-"]', timeout=timeout_ms)
    except Exception:
        return None

    brands = []
    for h in page.query_selector_all('[data-testid^="columnheader-"]'):
        name = (h.get_attribute("data-testid") or "").split("columnheader-")[-1]
        if name in BRANDS:
            brands.append(name)

    tiles = {}
    for tile in page.query_selector_all('[data-testid^="valuetile-0-"]'):
        m = re.search(r"valuetile-0-(\d+)$", tile.get_attribute("data-testid") or "")
        p = PRICE.search(tile.inner_text() or "")
        if m and p:
            tiles[int(m.group(1))] = float(p.group(1).replace(",", ""))

    if not brands or len(brands) != len(tiles):
        return {"_brands": brands, "_tiles": tiles, "_mismatch": True}
    return dict(zip(brands, [tiles[k] for k in sorted(tiles)], strict=True))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="origin", default="SEA")
    ap.add_argument("--to", dest="dest", default="ABQ")
    ap.add_argument("--depart", default="2026-10-17")
    ap.add_argument("--ret", default="2026-10-24")
    ap.add_argument("--expect-saver", type=float, default=297)
    ap.add_argument("--expect-main", type=float, default=397)
    ap.add_argument("--timeout", type=int, default=45000)
    args = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()

        # Baseline on the known-good one-way link. Without it, every candidate
        # failing is ambiguous between "wrong parameter" and "IP blocked".
        base_url = build(args.origin, args.dest, args.depart, None, None, rt=False)
        print(f"baseline one-way\n  {base_url}")
        page.goto(base_url, wait_until="domcontentloaded")
        baseline = read_grid(page, args.timeout)
        print(f"  -> {baseline}\n")
        if baseline is None:
            print("BLOCKED: the known-good one-way link produced no grid, so "
                  "candidate results would be meaningless.")
            browser.close()
            return 3

        for param in CANDIDATES:
            u = build(args.origin, args.dest, args.depart, args.ret, param)
            print(f"{param}\n  {u}")
            try:
                page.goto(u, wait_until="domcontentloaded")
                grid = read_grid(page, args.timeout)
            except Exception as exc:
                grid = {"_error": str(exc)[:120]}

            verdict = "no grid"
            if isinstance(grid, dict) and not grid.get("_error"):
                saver, main_ = grid.get("SAVER"), grid.get("MAIN")
                if main_ == args.expect_main and (
                        saver is None or saver == args.expect_saver):
                    verdict = "MATCH: round-trip totals"
                elif grid == baseline:
                    verdict = "ignored (identical to one-way baseline)"
                elif main_:
                    verdict = f"rendered but MAIN={main_}, expected {args.expect_main}"
                elif grid.get("_mismatch"):
                    verdict = f"column mismatch {grid}"
            results[param] = {"url": u, "grid": grid, "verdict": verdict}
            print(f"  -> {grid}\n  -> {verdict}\n")

        browser.close()

    print("=" * 70)
    winners = [p for p, r in results.items() if r["verdict"].startswith("MATCH")]
    if winners:
        print(f"ROUND-TRIP PARAMETER: {winners[0]}")
        print(f"  {results[winners[0]]['url']}")
    else:
        print("No candidate produced round-trip totals. Either the name is not "
              "in the list, or the return date is not in the URL at all and the "
              "round-trip flow is stateful.")
    print(json.dumps(results, indent=2, default=str))
    return 0 if winners else 1


if __name__ == "__main__":
    raise SystemExit(main())
