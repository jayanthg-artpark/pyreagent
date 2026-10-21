"""Ct estimation from raw amplification curves.

A plain baseline/threshold method so agents get a usable first answer from
any instrument. Use the instrument vendor's analysis software for reportable results.
"""

from __future__ import annotations

import statistics

BASELINE = (3, 15)
CT_METHOD = (
    "baseline-subtracted threshold crossing: baseline = mean of cycles 3-15, threshold = max(10 x baseline SD, "
    "10% of the curve's rise), linear interpolation; no amplification if the rise is under 20 x baseline SD"
)


def estimate_ct(values: list[float], *, baseline: tuple[int, int] = BASELINE, min_signal_to_noise: float = 20.0) -> float | None:
    """Fractional cycle (1-based) where the curve crosses threshold, or None if it never amplifies."""
    first, last = baseline
    if len(values) < last + 2:
        return None
    window = values[first - 1 : last]
    mean = statistics.fmean(window)
    sd = statistics.pstdev(window)
    delta = [v - mean for v in values]
    rise = max(delta[last:])
    noise = max(sd, 1e-9 * max(1.0, abs(mean)))
    if rise < min_signal_to_noise * noise:
        return None
    threshold = max(10 * sd, 0.1 * rise)
    for i in range(last, len(delta)):
        if delta[i] >= threshold:
            previous = delta[i - 1]
            fraction = (threshold - previous) / (delta[i] - previous) if delta[i] != previous else 0.0
            return round(i + fraction, 2)
    return None
