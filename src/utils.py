import math


def norm_cdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun approximation, ~1.5e-7 accuracy)."""
    a1, a2, a3, a4, a5 = (
        0.254829592,
        -0.284496736,
        1.421413741,
        -1.453152027,
        1.061405429,
    )
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(
        -x * x / 2
    )
    return 0.5 * (1.0 + sign * y)


def kelly_criterion(win_prob: float, payout_ratio: float) -> float:
    """Optimal Kelly bet fraction: f* = (p*b - q) / b."""
    if payout_ratio <= 0 or win_prob <= 0 or win_prob >= 1:
        return 0.0
    q = 1 - win_prob
    f = (win_prob * payout_ratio - q) / payout_ratio
    return max(0.0, f)


def price_binary_option(
    spot: float, strike: float, time_remaining_s: float, annual_vol: float = 0.70
) -> float:
    """Price a binary (digital) option using log-normal model.

    Returns probability that spot > strike at expiry.
    """
    if time_remaining_s <= 0:
        return 1.0 if spot >= strike else 0.0
    if strike <= 0:
        return 0.99

    seconds_per_year = 365.25 * 24 * 3600
    vol_per_sec = annual_vol / math.sqrt(seconds_per_year)
    vol_window = vol_per_sec * math.sqrt(time_remaining_s)

    if vol_window < 1e-10:
        return 1.0 if spot >= strike else 0.0

    d2 = math.log(spot / strike) / vol_window
    return max(0.01, min(0.99, norm_cdf(d2)))
