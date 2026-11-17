"""Forecast GTA VI's launch-day Twitch viewers hour by hour (gta6-twitch-ccu-forecast-01).

Model (written from scratch for this bounty, Python 3.13 stdlib only):
  For each hour h = 2..24 of the UTC release day, forecast five quantiles of the
  Twitch category's concurrent viewers, as a ratio to hour 1's print p1.

  What past big launches show (fitted at run time from the case's own
  `comparables`, never from any outside data):
  - A launch day is NOT flat: viewers dip a little in the small hours, then
    climb as the world wakes up and each region hits midnight local time,
    peaking in the UTC evening (US afternoon + EU evening) at roughly 2-3x
    hour 1 for a rolling-midnight launch.
  - How big that climb is depends strongly on hour 1 itself: the bigger
    hour 1's print, the more of the global launch surge has already happened
    by 00:00 UTC and the flatter the rest of the day (across past launches,
    corr(log peak ratio, log p1) is about -0.94). So the model regresses each
    launch's ramp amplitude A (mean log-ratio over hours 15-22) on log(p1)
    and predicts the target's amplitude from its own hour-1 print. That
    predicted ramp is then shrunk 15% toward flat (factor 0.85): leave-one-out
    backtesting shows the shrunk ramp beats the unshrunk one on the big
    rolling-midnight launches closest to GTA VI, and ties it elsewhere.
  - The hourly shape is the median normalised profile (log-ratio_h / A)
    over past launches, and the spread is the empirical residual spread
    around that median, shrunk toward the house baseline's lognormal width
    (sigma 0.15 * sqrt(h-1)) so intervals stay honest when the sample is thin.

  Fallbacks keep the entry valid no matter what the case holds: if the
  comparables cannot be read or the fit degenerates, the forecast is the
  house persistence baseline. The program never crashes on a case: any
  unexpected error still writes a valid persistence forecast.
"""
import json
import math
import os
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0, 0.674, 1.645]
SIGMA = 0.15          # house baseline log-spread per sqrt hour
AMP_LO, AMP_HI = 0.0, 2.8   # clamp on predicted ramp amplitude (never negative: a negative amplitude would invert the launch-day profile)
BLEND = 0.85          # shrink the predicted ramp toward flat: LOO backtest
                      # shows 0.85 beats 1.0 on big rolling-midnight launches
                      # (the GTA VI-like subset) and ties elsewhere
SHRINK_EMP = 0.6      # weight on empirical residual quantiles vs normal
PEAK_HOURS = range(15, 23)


def round2(x):
    """x rounded to 2 decimals, half up (same as the starter)."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value == int(value) else value


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def quantile(sorted_vals, tau):
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = tau * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - pos) + sorted_vals[hi] * (pos - lo)


def tier_of(comp):
    u = comp.get("unlock") or ""
    if "early-access-contaminated" in u:
        return "ea"
    if "Tier: weak" in u:
        return "weak"
    if "Tier: target-history" in u:
        return "target"
    return "comp"


def day_curve(comp):
    """{hour: viewers} for the 24 UTC hours of comp's day_start."""
    try:
        ds = parse_ts(comp["day_start"])
    except Exception:
        return {}
    out = {}
    for h in comp.get("hourly") or []:
        try:
            t = parse_ts(h["from"])
            v = h.get("viewers")
        except Exception:
            continue
        if v is None or v <= 0:
            continue
        if ds <= t < ds + timedelta(days=1):
            out[int((t - ds).total_seconds() // 3600) + 1] = v
    return out


def log_ratios(curve):
    """{hour: ln(viewers_h / viewers_1)} for hours 2..24."""
    if 1 not in curve:
        return {}
    p1 = curve[1]
    return {h: math.log(curve[h] / p1) for h in range(2, 25) if h in curve}


def amplitude(ratios):
    vals = [ratios[h] for h in PEAK_HOURS if h in ratios]
    return statistics.mean(vals) if vals else None


def ols(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def persistence(p1, h):
    return [round2(p1 * math.exp(z * SIGMA * math.sqrt(h - 1))) for z in Z]


def forecast_case(case):
    first = next((k for k in case["known"] if k["hour"] == 1), None)
    if first is None:
        raise ValueError("no hour-1 print")
    p1 = max(1, first["viewers"])
    hours = [f["hour"] for f in case["forecast"]]

    # --- fit set: every comparable with a usable launch day -----------------
    launches = []
    for comp in case.get("comparables") or []:
        if tier_of(comp) == "target":
            continue
        curve = day_curve(comp)
        ratios = log_ratios(curve)
        amp = amplitude(ratios)
        if amp is None or len(ratios) < 10:
            continue
        launches.append((curve, ratios, amp))
    if len(launches) < 5:
        return [{"hour": h, "q": persistence(p1, h)} for h in hours]

    # --- regress ramp amplitude on log(hour-1 print) ------------------------
    xs = [math.log(c[1]) for c, _, _ in launches]
    ys = [amp for _, _, amp in launches]
    a0, b0 = ols(xs, ys)
    a_pred = max(AMP_LO, min(AMP_HI, a0 + b0 * math.log(p1)))

    # --- normalised hourly profile (median of ratio_h / A over A > 0.3) -----
    profile = {}
    for h in range(2, 25):
        vals = [ratios[h] / amp for _, ratios, amp in launches
                if h in ratios and amp > 0.3]
        profile[h] = statistics.median(vals) if vals else 0.0
    median_log = {h: BLEND * a_pred * profile[h] for h in range(2, 25)}

    # --- residual spread around the predicted median ------------------------
    resid = {h: [] for h in range(2, 25)}
    for curve, ratios, _ in launches:
        a_c = max(AMP_LO, min(AMP_HI, a0 + b0 * math.log(curve[1])))
        for h in range(2, 25):
            if h in ratios:
                resid[h].append(ratios[h] - a_c * profile[h])

    out = []
    for h in hours:
        m = median_log.get(h, 0.0)
        r = sorted(resid.get(h, []))
        sig = SIGMA * math.sqrt(h - 1)
        qs = []
        for tau, z in zip(LEVELS, Z):
            emp = quantile(r, tau)
            q = SHRINK_EMP * emp + (1.0 - SHRINK_EMP) * z * sig
            qs.append(p1 * math.exp(m + q))
        qs = sorted(max(1.0, q) for q in qs)
        qs = [min(q, 1e9) for q in qs]
        out.append({"hour": h, "q": [round2(q) for q in qs]})
    return out


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    inputs = sorted(p for p in INPUT_DIR.iterdir()
                    if p.is_file() and p.name.endswith(".in"))
    for path in inputs:
        case_id = path.name[:-3]
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        try:
            hours = forecast_case(case)
        except Exception:
            # Never leave a case without a valid forecast.
            try:
                first = next(k for k in case["known"] if k["hour"] == 1)
                p1 = max(1, first["viewers"])
            except Exception:
                p1 = 1
            hours = [{"hour": f["hour"], "q": persistence(p1, f["hour"])}
                     for f in case["forecast"]]
        with open(OUTPUT_DIR / f"{case_id}.out", "w", encoding="utf-8") as f:
            f.write(json.dumps({"hours": hours}) + "\n")


if __name__ == "__main__":
    main()
