"""GTA VI launch-day Twitch forecast (make.fun bounty gta6-twitch-ccu-forecast-01).

Comparables-driven forecaster, Python 3.13 standard library only.

Idea: past big launches' release days share a shape -- Twitch viewers in the
category start low in hour 1 (only New Zealand / Australia are awake at 00:00
UTC) and climb steeply as Europe and then the US unlock at local midnight, so a
flat persistence forecast misses by ~80%.  For each comparable launch we take
its release-day 24-hour curve, express every hour as a log-ratio to its own
hour 1, L_h = ln(v_h / v_1), weight the launch by how close its hour-1 size is
to this case's hour-1 print p1, and read the weighted empirical quantiles of
L_h per hour.  The median is smoothed over a 3-hour window in log space and
the spread is floored at the house baseline's spread (0.15 * sqrt(h - 1)) and
capped at 1.2 in log-sigma terms, so the intervals stay honest without
collapsing or exploding.  With no usable comparable we fall back to the house
baseline (persistence) formula.

Run: MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# The grader's -P leaves /bot off sys.path: put it back first, so a module
# shipped beside this file would import. (This entry is a single file.)
sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0, 0.674, 1.645]
BASE_SIGMA = 0.15  # house baseline spread, log units per sqrt-hour

# Tunable parameters (defaults chosen by leave-one-out validation on the
# 38 comparables of practice case p00; see eval.py next to the repo).
# LOO (37 usable targets): model loss 0.404, typical miss 127%, 90%
# coverage 0.85, vs house baseline 0.930 / 805% / 0.62 on the same targets.
# The overall LOO is dragged up by tiny hour-1 launches (e.g. Silksong,
# v1 = 164) whose release days ramp 1000x; on the 27 targets with v1 >=
# 20,000 -- the regime GTA VI's hour 1 will almost surely fall in -- the
# model scores 0.260 vs the baseline's 0.321.  Variants tried and rejected
# as noise-level (within ~0.005): bandwidth 0.7 / 1.5, smoothing 0 / 2,
# spread floor 0.05 / 0.20 / 0.25, sigma cap 0.8 / 2.0, recency weighting.
# Defaults below are the specified, principled values, not a fitted peak.
BANDWIDTH = 1.0       # log-size similarity bandwidth for comparable weights
SMOOTH_HALF = 1       # median smoothing: window is 2 * SMOOTH_HALF + 1 hours
FLOOR_FACTOR = 0.15   # spread floor, in the house baseline's units
SIGMA_CAP = 1.2       # spread cap, in log-sigma units


def round2(x: float) -> float | int:
    """x rounded to 2 decimals, half up, as JavaScript's Math.round does.

    A whole number comes back as an int, so json.dumps writes 1240 where
    JavaScript writes 1240 (not 1240.0). Same helper as the kit starter.
    """
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def day_curve(comparable: dict) -> dict[int, float]:
    """Release-day hourly viewers of one comparable, keyed by hour 1..24.

    Hour h is the point whose `from` lies in [day_start + (h-1)h,
    day_start + h h).  Points outside the day, and non-positive prints, are
    dropped; a missing hour simply stays absent.
    """
    try:
        start = _parse_time(comparable["day_start"])
    except (KeyError, TypeError, ValueError):
        return {}
    curve: dict[int, float] = {}
    for point in comparable.get("hourly") or []:
        try:
            viewers = point["viewers"]
            at = _parse_time(point["from"])
        except (KeyError, TypeError, ValueError):
            continue
        if viewers is None or viewers <= 0:
            continue
        hour = int((at - start).total_seconds() // 3600) + 1
        if 1 <= hour <= 24:
            curve[hour] = float(viewers)
    return curve


def baseline_quantiles(p1: float, h: int) -> list[float | int]:
    """House baseline (starter) forecast for hour h: flat median, widening spread."""
    return [round2(p1 * math.exp(z * BASE_SIGMA * math.sqrt(h - 1))) for z in Z]


def _weighted_quantile(pairs: list[tuple[float, float]], tau: float) -> float:
    """Weighted empirical quantile of log-ratios [(value, weight), ...]."""
    ordered = sorted(pairs, key=lambda pair: pair[0])
    total = sum(w for _, w in ordered)
    threshold = tau * total
    acc = 0.0
    for value, weight in ordered:
        acc += weight
        if acc >= threshold:
            return value
    return ordered[-1][0]


def forecast_quantiles(
    p1: float,
    comparables: list[dict],
    bandwidth: float = BANDWIDTH,
    smooth_half: int = SMOOTH_HALF,
    floor_factor: float = FLOOR_FACTOR,
    sigma_cap: float = SIGMA_CAP,
) -> dict[int, list[float | int]]:
    """Forecast hours 2..24 as {hour: [q05, q25, q50, q75, q95]}.

    p1 is hour 1's print (already floored at 1 by the caller).  comparables
    are the case's comparables in file order; each contributes its release-day
    log-ratio curve, weighted by size similarity to p1.
    """
    p1 = max(1.0, float(p1))
    log_p1 = math.log(p1)

    # Donors: (weight, day curve). The Grand Theft Auto VI pre-release
    # comparable has no hour-1 print on its "day", so it drops out here.
    donors: list[tuple[float, dict[int, float]]] = []
    for comp in comparables or []:
        curve = day_curve(comp)
        v1 = curve.get(1)
        if not v1:
            continue
        diff = math.log(v1) - log_p1
        weight = math.exp(-(diff * diff) / (2.0 * bandwidth * bandwidth))
        donors.append((weight, curve))

    raw: dict[int, list[float]] = {}
    for h in range(2, 25):
        pairs = [
            (math.log(curve[h] / curve[1]), w)
            for w, curve in donors
            if h in curve and 1 in curve
        ]
        if not pairs:
            raw[h] = [math.log(q) for q in baseline_quantiles(p1, h)]
            continue
        quantiles = [_weighted_quantile(pairs, tau) for tau in LEVELS]
        raw[h] = [log_p1 + q for q in quantiles]

    # Smooth the median in log space over a sliding window.
    medians = {h: raw[h][2] for h in range(2, 25)}
    smoothed: dict[int, float] = {}
    for h in range(2, 25):
        window = [medians[k] for k in range(h - smooth_half, h + smooth_half + 1)
                  if k in medians]
        smoothed[h] = sum(window) / len(window)

    out: dict[int, list[float | int]] = {}
    for h in range(2, 25):
        log_q = raw[h]
        med_raw = log_q[2]
        med = smoothed[h]
        sigma_raw = (log_q[4] - log_q[0]) / (2.0 * 1.645) if log_q[4] > log_q[0] else 0.0
        floor = floor_factor * math.sqrt(h - 1)
        if sigma_raw <= 1e-12:
            # Degenerate spread: rebuild a normal band around the median.
            target = min(max(floor, 1e-9), sigma_cap)
            adjusted = [med + z * target for z in Z]
        else:
            target = min(max(sigma_raw, floor), sigma_cap)
            factor = target / sigma_raw
            adjusted = [med + (q - med_raw) * factor for q in log_q]
        # Round, keep > 0, <= 1e9, and non-decreasing.
        row: list[float | int] = []
        prev = 0.0
        for value in adjusted:
            q = round2(math.exp(value))
            q = max(q, 0.01)
            q = min(q, 1_000_000_000)
            if q < prev:
                q = prev
            row.append(q)
            prev = float(q)
        out[h] = row
    return out


def forecast_case(case: dict, name: str) -> dict:
    if case["levels"] != LEVELS:
        raise ValueError(f"{name} asks for levels {case['levels']}, not {LEVELS}")
    first = next((k for k in case["known"] if k["hour"] == 1), None)
    if first is None:
        raise ValueError(f"{name} has no hour-1 print")
    p1 = max(1, first["viewers"])
    quantiles = forecast_quantiles(p1, case.get("comparables") or [])
    return {"hours": [{"hour": row["hour"], "q": quantiles[row["hour"]]}
                     for row in case["forecast"]]}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Every case file, in name order, so two runs write the same bytes.
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
