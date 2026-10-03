"""P1b oracle (docs/RUST_PORT.md D4): the Rust greeks / implied vol against vollib.

vollib is a test-only dependency here; src/ no longer imports it. The grid is dense
and the tolerances are tight, so a wrong formula or unit (theta divisor, dropped
dividend, flipped sign) cannot pass.

Tolerances
- price, delta, gamma, theta, vega, rho: ``rel 1e-9, abs 1e-12``. Both sides evaluate
  the same closed forms in double precision; the only difference is the normal CDF
  (Cody erf in vollib, the implied-vol crate's erfc in Rust), good to ~1e-15.
- implied vol vs vollib: ``rel 1e-9, abs 1e-12``. Both run Jaeckel's algorithm.
- round trip price -> IV -> price: ``rel 1e-8`` of price or ``1e-12`` absolute. Deep
  out-of-the-money prices carry few significant digits, so IV is the thing compared
  only where the price keeps vega (checked below by comparing the repriced option).
"""

from __future__ import annotations

import itertools
import math

import pytest
import trade_engine_rs as rs
from vollib.black_scholes_merton import black_scholes_merton
from vollib.black_scholes_merton.greeks import analytical
from vollib.black_scholes_merton.implied_volatility import implied_volatility

SPOT = 100.0
STRIKES = [SPOT * m for m in (0.5, 0.75, 0.9, 1.0, 1.1, 1.5, 2.0)]
HOUR = 1.0 / (365.0 * 24.0)
TIMES = [HOUR, 1.0 / 365.0, 7.0 / 365.0, 0.25, 0.5, 1.0, 3.0]
SIGMAS = [0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3.0]
RATES = [0.0, -0.005, 0.02, 0.05]
DIVS = [0.0, 0.01, 0.05]
FLAGS = ["c", "p"]
GRID = list(itertools.product(FLAGS, STRIKES, TIMES, SIGMAS, RATES, DIVS))

REL, ABS = 1e-9, 1e-12


def close(a: float, b: float, rel: float = REL, abs_: float = ABS) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)


def test_grid_is_dense():
    assert len(GRID) == 2 * 7 * 7 * 7 * 4 * 3


def test_price_matches_vollib():
    bad = []
    for flag, k, t, s, r, q in GRID:
        want = float(black_scholes_merton(flag, SPOT, k, t, r, s, q))
        got = rs.greeks_price(flag, SPOT, k, t, r, q, s)
        if not close(got, want):
            bad.append((flag, k, t, s, r, q, got, want))
    assert not bad, bad[:5]


def test_greeks_match_vollib():
    bad = []
    for flag, k, t, s, r, q in GRID:
        got = rs.greeks_greeks(flag, SPOT, k, t, r, q, s)
        want = tuple(
            float(getattr(analytical, name)(flag, SPOT, k, t, r, s, q))
            for name in ("delta", "gamma", "theta", "vega", "rho")
        )
        for name, g, w in zip(("delta", "gamma", "theta", "vega", "rho"), got, want):
            if not close(g, w):
                bad.append((name, flag, k, t, s, r, q, g, w))
    assert not bad, bad[:5]


def test_implied_vol_agrees_with_vollib_and_round_trips():
    answered = refused = 0
    bad = []
    for flag, k, t, s, r, q in GRID:
        price = float(black_scholes_merton(flag, SPOT, k, t, r, s, q))
        if price <= 0.0:
            continue
        try:
            want = float(implied_volatility(price, SPOT, k, t, r, q, flag))
        except Exception:
            want = None
        if want is None or not want > 0.0:
            # vollib gave no answer, or 0.0 (a price at intrinsic, which no positive
            # volatility reaches); Rust must refuse too, never answer 0 or a number.
            with pytest.raises(ValueError):
                rs.greeks_implied_vol(flag, SPOT, k, t, r, q, price)
            refused += 1
            continue
        got = rs.greeks_implied_vol(flag, SPOT, k, t, r, q, price)
        answered += 1
        if not close(got, want):
            bad.append((flag, k, t, s, r, q, got, want))
        # Round trip: the repriced option equals the input price.
        again = rs.greeks_price(flag, SPOT, k, t, r, q, got)
        if not close(again, price, rel=1e-8, abs_=1e-12):
            bad.append(("round-trip", flag, k, t, s, r, q, again, price))
    assert not bad, bad[:5]
    assert answered > 0.6 * len(GRID) and refused > 0, (answered, refused)


def _refusal_prices(flag, k, t, r, q):
    f = SPOT * math.exp((r - q) * t)
    disc = math.exp(-r * t)
    intrinsic = max(f - k, 0.0) if flag == "c" else max(k - f, 0.0)
    ceiling = f if flag == "c" else k
    below = [disc * intrinsic * m for m in (0.999, 0.9, 0.5)] if intrinsic > 0 else []
    above = [disc * ceiling * m for m in (1.0, 1.0001, 1.5, 3.0)]
    return below, above


def test_every_price_vollib_refuses_rust_refuses_and_names_the_bound():
    below_seen = above_seen = 0
    for flag, k, t, r, q in itertools.product(FLAGS, STRIKES, TIMES, RATES, DIVS):
        below, above = _refusal_prices(flag, k, t, r, q)
        for price in below + above:
            if price <= 0.0:
                continue
            try:
                implied_volatility(price, SPOT, k, t, r, q, flag)
                vollib_refuses = False
            except Exception:
                vollib_refuses = True
            if vollib_refuses:
                with pytest.raises(ValueError) as err:
                    rs.greeks_implied_vol(flag, SPOT, k, t, r, q, price)
                text = str(err.value)
                if price in below:
                    below_seen += 1
                    assert "below" in text, text
                else:
                    above_seen += 1
                    assert "above" in text, text
            else:
                # Where vollib answers Rust answers with the same number, except at the
                # exact ceiling: there one ulp of exp() decides which side of the bound a
                # price falls on and sigma is unbounded, so only require a finite answer.
                want = float(implied_volatility(price, SPOT, k, t, r, q, flag))
                try:
                    got = rs.greeks_implied_vol(flag, SPOT, k, t, r, q, price)
                except ValueError:
                    assert not want > 0.0 or price in above[:1], (flag, k, t, r, q, price, want)
                    continue
                if price in above[:1]:
                    assert math.isfinite(got) and got > 0.0
                else:
                    assert close(got, want), (flag, k, t, r, q, price, got, want)
    assert below_seen > 100 and above_seen > 100, (below_seen, above_seen)


@pytest.mark.parametrize("flag", FLAGS)
def test_refusal_is_never_zero_or_nan(flag):
    for price in (1e-320, 1e308, 150.0, 1e-12):
        try:
            got = rs.greeks_implied_vol(flag, SPOT, 100.0, 1.0, 0.0, 0.0, price)
        except ValueError:
            continue
        assert math.isfinite(got) and got > 0.0, (flag, price, got)
