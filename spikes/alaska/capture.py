#!/usr/bin/env python3
"""Human-in-the-loop fare capture for Alaska (Playwright).

You drive, I record. This opens a REAL Chrome window recording all network
traffic. You perform a normal flight search by hand (which trivially clears the
shadow-DOM booking widget and Cloudflare). As soon as the fare results render,
the underlying shopping request is already in the recording. When capture stops,
this script digs the fare endpoint out of the HAR for you: no DevTools hunting.

Why this beats both DevTools and scripted form-driving:
  - You don't have to identify the right XHR among the analytics noise.
  - No fragile Auro/shadow-DOM automation: a human clicks the form.
  - A real interactive session satisfies Cloudflare's JS challenge.

Setup (once):
  pip install playwright && playwright install chromium

Run:
  python spikes/alaska/capture.py

Then in the window that opens:
  1. Search ONE nonstop route+date (e.g. Seattle -> San Francisco, one way).
  2. Wait until the flight/fare list actually appears on screen.
  3. Come back to the terminal and press Enter to stop + analyze.
     (It also auto-stops a few seconds after it spots fare-shaped JSON, or
      after --max-wait seconds, whichever comes first.)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not installed. Run: pip install playwright && playwright install chromium")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Fare-specific tokens (analytics payloads deliberately excluded).
FARE_HINTS = (
    "displayPrice", "grandTotal", "fareBrand", "brandedFare", "milesPrice",
    "lowestFare", "fareCalendar", "solutionSet", "farePrice", "pricePerPax",
)
BRAND_WORDS = ("saver", "main", "first", "premium")
# A JSON body is "fare-shaped" if it mentions price AND a fare brand a few times.
PRICE_TOKEN = re.compile(r"price|amount|fare|total", re.I)


def looks_farey(body: str) -> tuple[bool, str]:
    low = body.lower()
    for h in FARE_HINTS:
        if h.lower() in low:
            return True, f"hint:{h}"
    has_price = len(PRICE_TOKEN.findall(body)) >= 3
    has_brand = any(re.search(rf"\b{w}\b", low) for w in BRAND_WORDS)
    if has_price and has_brand:
        return True, "price+brand"
    return False, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="https://www.alaskaair.com/", help="page to open")
    ap.add_argument("--max-wait", type=int, default=300, help="seconds before auto-stop")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.outdir or Path(__file__).parent / "out" / f"capture-{stamp}")
    (out / "responses").mkdir(parents=True, exist_ok=True)

    hits: list[dict] = []
    stop = threading.Event()

    # Press Enter to stop early (runs in a side thread so we can also auto-stop).
    def wait_for_enter():
        try:
            input()
        except EOFError:
            return
        stop.set()
    threading.Thread(target=wait_for_enter, daemon=True).start()

    print(f"# recording to {out}")
    print("# A Chrome window is opening. Do your search there, then press Enter here.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=0)
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            record_har_path=str(out / "session.har"),
            record_har_content="embed",
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
            ok, why = looks_farey(body)
            if not ok:
                return
            req = resp.request
            rec = {"url": resp.url, "method": req.method, "status": resp.status,
                   "why": why, "bytes": len(body),
                   "request_headers": dict(req.headers),
                   "request_post_data": req.post_data}
            hits.append(rec)
            idx = len(hits)
            fn = f"{idx:02d}_{req.method}.json"
            (out / "responses" / fn).write_text(body)
            (out / "responses" / (fn + ".meta")).write_text(json.dumps(rec, indent=2))
            host = resp.url.split("/")[2] if "//" in resp.url else resp.url
            print(f"  >> fare-shaped JSON #{idx}: {req.method} {resp.status} "
                  f"{rec['bytes']}B  {host}  ({why})")

        page.on("response", on_response)
        page.goto(args.url, wait_until="domcontentloaded")

        # Poll: stop on Enter, or ~4s after the first fare hit, or at max-wait.
        waited = 0.0
        settle_after_hit = 4.0
        while waited < args.max_wait and not stop.is_set():
            page.wait_for_timeout(500)
            waited += 0.5
            if hits and waited and not stop.is_set():
                # give trailing calls a moment, then stop automatically
                end = waited + settle_after_hit
                while waited < end and not stop.is_set():
                    page.wait_for_timeout(500)
                    waited += 0.5
                break

        try:
            page.screenshot(path=str(out / "final.png"), full_page=True)
        except Exception:
            pass
        context.close()  # flush HAR
        browser.close()

    # ---- analyze -------------------------------------------------------------
    print("\n=== SUMMARY ===")
    if hits:
        print(f"captured {len(hits)} fare-shaped JSON response(s):")
        for i, h in enumerate(hits, 1):
            print(f"  {i}. {h['method']} {h['status']} {h['bytes']}B  {h['url']}")
            if h["request_post_data"]:
                print(f"       POST body: {h['request_post_data'][:200]}")
        print("\nSaved bodies + request metadata in responses/. The .meta files "
              "have the URL, headers and POST body needed to replay each call.")
    else:
        print("No fare-shaped JSON was seen live. Don't worry: the full HAR is "
              "saved. Falling back to scanning every JSON response in it...")
        scan_har(out / "session.har")

    print(f"\nArtifacts: {out}")
    print("Paste the SUMMARY block back to me and I'll build the replay client.")
    return 0 if hits else 1


def scan_har(har_path: Path) -> None:
    """Fallback: rank every JSON response in the HAR by how fare-shaped it is."""
    try:
        har = json.loads(har_path.read_text())
    except Exception as exc:
        print(f"  could not read HAR: {exc}")
        return
    ranked = []
    for e in har.get("log", {}).get("entries", []):
        resp = e.get("response", {})
        content = resp.get("content", {})
        if "json" not in (content.get("mimeType") or "").lower():
            continue
        body = content.get("text") or ""
        ok, why = looks_farey(body)
        score = (len(body) if ok else 0) + PRICE_TOKEN.findall(body).__len__()
        if ok or PRICE_TOKEN.search(body):
            ranked.append((score, e["request"]["method"], resp.get("status"),
                           len(body), e["request"]["url"], why))
    ranked.sort(reverse=True)
    if not ranked:
        print("  no JSON responses in the HAR looked fare-shaped either.")
        return
    print("  top candidate JSON responses (most fare-shaped first):")
    for score, method, status, size, url, why in ranked[:8]:
        print(f"    - {method} {status} {size}B  {url[:110]}  {why or ''}")


if __name__ == "__main__":
    raise SystemExit(main())
