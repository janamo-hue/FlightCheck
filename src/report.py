"""Render price history to a static HTML page.

Writes docs/index.html so GitHub Pages can serve it straight from the repo.
Inline SVG, no chart library, no build step: the page has to survive being
opened from a file:// URL or committed to Pages without a toolchain.

Run: python -m src.report [--out docs/index.html] [--days 60]
"""

from __future__ import annotations

import argparse
import html
import os
from collections import defaultdict
from datetime import timedelta

from . import alerts, config, store

W, H, PAD = 640, 120, 8


def sparkline(points: list[tuple[str, float]]) -> str:
    """Price over observation time, with the low marked."""
    if len(points) < 2:
        return '<p class="thin">Not enough history yet.</p>'

    values = [p[1] for p in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    def xy(i: int, v: float) -> tuple[float, float]:
        x = PAD + i * (W - 2 * PAD) / (len(points) - 1)
        y = PAD + (hi - v) * (H - 2 * PAD) / span
        return x, y

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, v) for i, v in enumerate(values)))
    area = f"{PAD},{H - PAD} {line} {W - PAD},{H - PAD}"
    li = values.index(lo)
    lx, ly = xy(li, lo)

    return f"""<svg viewBox="0 0 {W} {H}" class="spark" preserveAspectRatio="none">
      <polygon points="{area}" fill="rgba(37,99,235,.10)"/>
      <polyline points="{line}" fill="none" stroke="#2563eb" stroke-width="2"
                vector-effect="non-scaling-stroke"/>
      <circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="#15803d"/>
    </svg>
    <div class="axis"><span>{hi:,.0f}</span><span>low {lo:,.0f}</span></div>"""


def build(days: int = 60) -> str:
    cfg = config.load()
    names = {r.key: r.name for r in cfg.routes}
    state = store.load_state()
    history = store.load_history()

    cutoff = (store.now() - timedelta(days=days)).isoformat()
    recent = [o for o in history if o.observed_at >= cutoff]

    by_route: dict[str, list[store.Observation]] = defaultdict(list)
    for obs in recent:
        by_route[obs.route].append(obs)

    q = state.get("quota", {})
    used, allowed = q.get("calls", 0), cfg.monthly_call_quota
    generated = store.now().strftime("%Y-%m-%d %H:%M UTC")

    sections = []
    for key, name in names.items():
        rows = by_route.get(key, [])
        if not rows:
            sections.append(f"<section><h2>{html.escape(name)}</h2>"
                            f'<p class="thin">No observations in the last {days} days.</p></section>')
            continue

        # Cheapest date pair currently tracked, then its own price curve.
        best: dict[str, list[store.Observation]] = defaultdict(list)
        for obs in rows:
            best[obs.pair].append(obs)

        ranked = sorted(best.items(), key=lambda kv: min(o.price for o in kv[1]))[:5]
        cards = []
        for pair, obs_list in ranked:
            obs_list.sort(key=lambda o: o.observed_at)
            latest = obs_list[-1]
            lo = min(o.price for o in obs_list)
            depart, _, ret = pair.partition("|")
            when = f"{depart} to {ret}" if ret else f"{depart} one way"
            delta = latest.price - lo
            trend = "at the low" if delta <= 0.5 else f"{delta:,.0f} above the low"

            route_cfg = next((r for r in cfg.routes if r.key == key), None)
            cpp = alerts.cents_per_point(latest.price, route_cfg) if route_cfg else None
            if cpp is None:
                points = ""
            else:
                call = ("redeem" if cpp >= route_cfg.redeem_above_cents else "pay cash")
                points = (f'<div class="thin">{cpp:.1f}c per point at the '
                          f'{route_cfg.award_floor_points:,} floor, so {call}</div>')
            cards.append(f"""<div class="card">
              <div class="row"><strong>{html.escape(when)}</strong>
                <span class="price">{latest.currency} {latest.price:,.0f}</span></div>
              <div class="thin">{len(obs_list)} observations, {trend}</div>
              {points}
              {sparkline([(o.observed_at, o.price) for o in obs_list])}
            </div>""")

        sections.append(f"<section><h2>{html.escape(name)} "
                        f'<span class="thin">{key}</span></h2>{"".join(cards)}</section>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fare watch</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 -apple-system, Segoe UI, sans-serif; margin: 0 auto;
         max-width: 760px; padding: 24px 16px; }}
  h1 {{ margin: 0 0 2px; font-size: 22px; }}
  h2 {{ font-size: 16px; margin: 28px 0 10px; }}
  .thin {{ color: #71717a; font-size: 12px; font-weight: 400; }}
  .card {{ border: 1px solid #e4e4e7; border-radius: 10px; padding: 12px;
           margin-bottom: 10px; }}
  .row {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .price {{ font-size: 19px; font-weight: 700; }}
  .spark {{ width: 100%; height: 120px; display: block; margin-top: 8px; }}
  .axis {{ display: flex; justify-content: space-between; }}
  .axis span {{ color: #71717a; font-size: 11px; }}
  @media (prefers-color-scheme: dark) {{ .card {{ border-color: #3f3f46; }} }}
</style></head><body>
<h1>Fare watch</h1>
<p class="thin">Last {days} days. Generated {generated}.
   Amadeus quota {used:,} of {allowed:,} this month.</p>
{"".join(sections)}
<p class="thin">Prices are GDS totals from Amadeus and may differ from the
   airline site. Green dot marks the lowest price observed for that itinerary.
   Cents per point assumes saver space exists at the route's chart floor, which
   is the best case rather than a quote.</p>
</body></html>"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/index.html")
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args(argv)

    page = build(args.days)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(page)
    print(f"wrote {args.out} ({len(page):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
