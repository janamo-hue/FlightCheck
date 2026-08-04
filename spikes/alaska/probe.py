#!/usr/bin/env python3
"""Alaska Airlines fare-discovery spike (Playwright, browser-driving).

Goal of this spike (NOT shipping code):
  1. Confirm we can reach Alaska fares from a driven browser without tripping
     bot protection.
  2. Discover the shopping XHR/fetch endpoint the site calls (URL, method,
     headers, request body, response shape) so a later, lighter client can
     replay it directly ("copy as cURL" path).
  3. Confirm the response carries what src.amadeus.Offer needs: a per-flight
     price plus a fare brand string we can match "SAVER" against.

Why browser-driving and not a guessed URL:
  The repo already established (see src/notify.py:booking_url) that Alaska
  publishes no dated deep-link format, so a hand-built /search/results URL
  lands on an empty search. So we drive the real flow and let the page build
  the request, then capture it from the network.

Everything is dumped to an output dir so the FIRST run is useful even if a
selector or the on-screen parse is wrong: the HAR contains the real request
regardless of whether this script correctly recognises it.

Setup:
  python -m venv .venv && . .venv/bin/activate
  pip install playwright
  playwright install chromium

Usage:
  # Route-page mode (default): verified-to-resolve page with a fare calendar.
  python spikes/alaska/probe.py --from SEA --to SFO --depart 2026-09-15

  # Booking-form mode: fill the homepage widget and submit (nonstop, one-way).
  python spikes/alaska/probe.py --mode form --from SEA --to SFO --depart 2026-09-15

  # Watch it happen (local debugging; do NOT use in CI):
  python spikes/alaska/probe.py --from SEA --to SFO --depart 2026-09-15 --headed --slowmo 250

The GO/NO-GO signal for this whole migration is whether this returns fare
JSON when run from a cloud IP (e.g. a throwaway GitHub Actions run), not just
from your laptop. Run it in both places.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(
        "playwright is not installed. Run:\n"
        "  pip install playwright && playwright install chromium"
    )

# A recent, real desktop Chrome UA. Bot protection scores obviously-scripted
# UAs harshly, so we do not advertise headless.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Substrings that mark a JSON payload as probably fare/shopping data. These are
# deliberately fare-SPECIFIC: an earlier looser set (which included "cabin" and
# bare "main"/"saver") matched Optimizely feature-flag and Qualtrics survey
# payloads. If a real run shows fares on screen but captures nothing, widen this
# with a token you actually see in the shopping XHR via --headed DevTools.
FARE_HINTS = (
    "displayPrice", "grandTotal", "fareBrand", "brandedFare", "milesPrice",
    "lowestFare", "fareCalendar", "solutionSet", "farePrice", "pricePerPax",
)

# On-screen tokens for the DOM-scrape fallback.
BRAND_WORDS = ("saver", "main", "first", "premium")


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:120]


def route_url(origin: str, dest: str) -> str:
    # Verified to resolve (see src/notify.py:alaska_url). Renders a fare
    # calendar client-side, so a shopping XHR fires without a form submit.
    return f"https://www.alaskaair.com/en/flights-from-{origin}-to-{dest}"


def try_fill_form(page, origin, dest, depart, ret, log):
    """Best-effort fill of the homepage booking widget.

    Alaska's DOM changes; we try several candidate locators and log which one
    worked so the next iteration can be pinned. If nothing matches we bail and
    let the caller fall back to route mode. The HAR is still captured.
    """
    page.goto("https://www.alaskaair.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)  # let the widget hydrate

    def first_hit(candidates, action):
        for how, val in candidates:
            try:
                loc = how(val)
                if loc.count() > 0:
                    action(loc.first, val)
                    log(f"  matched {val!r}")
                    return True
            except Exception as exc:  # noqa: BLE001 - discovery, log and move on
                log(f"  {val!r} failed: {exc}")
        return False

    by_ph = page.get_by_placeholder
    by_label = page.get_by_label

    # One-way keeps the fare grid simple; flip via role radio if present.
    try:
        page.get_by_role("radio", name=re.compile("one.?way", re.I)).first.check(timeout=3000)
        log("  selected one-way")
    except Exception:
        log("  one-way radio not found (continuing, may default round-trip)")

    ok_from = first_hit(
        [(by_ph, re.compile("from", re.I)), (by_label, re.compile("depart.*from|from", re.I))],
        lambda loc, _: (loc.click(), loc.fill(origin), page.wait_for_timeout(1200),
                        loc.press("Enter")),
    )
    ok_to = first_hit(
        [(by_ph, re.compile("^to$|to city|destination", re.I)),
         (by_label, re.compile("going to|to", re.I))],
        lambda loc, _: (loc.click(), loc.fill(dest), page.wait_for_timeout(1200),
                        loc.press("Enter")),
    )
    ok_date = first_hit(
        [(by_ph, re.compile("depart", re.I)), (by_label, re.compile("depart", re.I))],
        lambda loc, _: (loc.click(), loc.fill(depart)),
    )

    if not (ok_from and ok_to and ok_date):
        log("  form fill incomplete; selectors need updating from the saved HTML")
        return False

    try:
        page.get_by_role("button", name=re.compile("find flights|search", re.I)).first.click()
        log("  submitted search")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"  submit button not found: {exc}")
        return False


def scrape_prices(page, log):
    """Fallback DOM scrape: cheapest visible $amount and any brand words seen.

    Not authoritative. The HAR/JSON is the real deliverable; this just gives a
    quick human-readable 'did fares render at all' signal.
    """
    try:
        text = page.inner_text("body")
    except Exception:
        return None
    amounts = sorted(
        float(m.replace(",", ""))
        for m in re.findall(r"\$\s?([0-9][0-9,]{1,6})", text)
    )
    brands = sorted({w for w in BRAND_WORDS if re.search(rf"\b{w}\b", text, re.I)})
    log(f"  on-screen $ amounts (low..): {amounts[:8]}")
    log(f"  on-screen brand words: {brands or 'none'}")
    return {"cheapest_visible": amounts[0] if amounts else None, "brands": brands}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="origin", required=True, help="origin IATA, e.g. SEA")
    ap.add_argument("--to", dest="dest", required=True, help="destination IATA, e.g. SFO")
    ap.add_argument("--depart", required=True, help="YYYY-MM-DD")
    ap.add_argument("--return", dest="ret", default=None, help="YYYY-MM-DD (omit for one-way)")
    ap.add_argument("--mode", choices=["route", "form", "url"], default="route")
    ap.add_argument("--url", default=None, help="explicit URL to load (mode=url)")
    ap.add_argument("--headed", action="store_true", help="show the browser (local only)")
    ap.add_argument("--slowmo", type=int, default=0, help="ms delay between actions")
    ap.add_argument("--settle", type=int, default=8000, help="ms to wait for XHRs after load")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.outdir or Path(__file__).parent / "out" / f"{args.origin}-{args.dest}-{stamp}")
    (out / "responses").mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    def log(msg: str) -> None:
        print(msg)
        lines.append(msg)

    log(f"# Alaska fare spike  {args.origin}->{args.dest} depart={args.depart} "
        f"ret={args.ret or '(one-way)'} mode={args.mode}")
    log(f"# output: {out}")

    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, slow_mo=args.slowmo)
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            record_har_path=str(out / "session.har"),
            record_har_content="embed",  # bodies inline -> full request/response for cURL later
        )
        page = context.new_page()

        def on_response(resp):
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                return
            try:
                body = resp.text()
            except Exception:
                return
            hint = next((h for h in FARE_HINTS if h.lower() in body.lower()), None)
            if not hint:
                return
            req = resp.request
            rec = {
                "url": resp.url,
                "method": req.method,
                "status": resp.status,
                "matched_hint": hint,
                "bytes": len(body),
                "request_headers": dict(req.headers),
                "request_post_data": req.post_data,
            }
            captured.append(rec)
            fname = f"{len(captured):02d}_{req.method}_{slug(resp.url.split('?')[0].split('/')[-1] or 'root')}.json"
            (out / "responses" / fname).write_text(body)
            (out / "responses" / (fname + ".meta")).write_text(json.dumps(rec, indent=2))
            log(f"  [fare-json] {req.method} {resp.status} hint={hint!r} {len(body)}B -> responses/{fname}")

        page.on("response", on_response)

        try:
            if args.mode == "url":
                target = args.url or route_url(args.origin, args.dest)
                log(f"loading url: {target}")
                page.goto(target, wait_until="domcontentloaded", timeout=45000)
            elif args.mode == "form":
                log("driving booking form...")
                if not try_fill_form(page, args.origin, args.dest, args.depart, args.ret, log):
                    log("form mode failed; retrying via route page")
                    page.goto(route_url(args.origin, args.dest), wait_until="domcontentloaded", timeout=45000)
            else:  # route
                target = route_url(args.origin, args.dest)
                log(f"loading route page: {target}")
                page.goto(target, wait_until="domcontentloaded", timeout=45000)

            # Give client-side shopping calls time to fire and settle.
            try:
                page.wait_for_load_state("networkidle", timeout=args.settle)
            except PWTimeout:
                log("  networkidle timeout (ok, some sites poll forever)")
            page.wait_for_timeout(args.settle)

            scrape_prices(page, log)

            page.screenshot(path=str(out / "final.png"), full_page=True)
            (out / "final.html").write_text(page.content())

        except Exception as exc:  # noqa: BLE001 - always flush artifacts
            log(f"!! run error: {exc}")
            try:
                page.screenshot(path=str(out / "error.png"))
                (out / "error.html").write_text(page.content())
            except Exception:
                pass
        finally:
            context.close()   # flushes the HAR
            browser.close()

    # ---- verdict ---------------------------------------------------------
    log("")
    log("=== SUMMARY ===")
    if captured:
        log(f"captured {len(captured)} fare-like JSON response(s):")
        for c in captured:
            log(f"  - {c['method']} {c['status']} {c['bytes']}B  {c['url'][:110]}")
        log("")
        log("NEXT: inspect responses/*.json for per-flight price + brand (SAVER/MAIN).")
        log("The matching request in session.har is your 'copy as cURL' seed.")
    else:
        log("NO fare-like JSON captured. Possible causes:")
        log("  - bot protection challenged the request (check final.png/error.png)")
        log("  - fares load from a differently-shaped payload (widen FARE_HINTS)")
        log("  - form/route did not actually trigger a search (check final.html)")
    log(f"\nArtifacts in: {out}")
    (out / "run.log").write_text("\n".join(lines))
    return 0 if captured else 1


if __name__ == "__main__":
    raise SystemExit(main())
