//! Black-Scholes-Merton price, implied volatility and analytical greeks
//! (docs/RUST_PORT.md P1b). Pure Rust, no clock (I7).
//!
//! Units follow vollib's `black_scholes_merton.greeks.analytical`: theta per
//! calendar day, vega and rho per one volatility / rate point (divided by 100).
//! Implied vol is Jaeckel's "Let's Be Rational" through the `implied-vol` crate,
//! reached the way vollib's `black_scholes_merton.implied_volatility` reaches it:
//! forward `F = S*exp((r-q)t)`, undiscounted price `price / exp(-r t)`.
//!
//! A price no volatility reaches is a refusal, never 0 and never NaN.

use implied_vol::{DefaultSpecialFn, ImpliedBlackVolatility, PriceBlackScholes, SpecialFn};
use std::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Right {
    Call,
    Put,
}

#[derive(Clone, Debug, PartialEq)]
pub enum GreeksError {
    /// An input the model cannot take (non-finite, non-positive).
    Invalid(String),
    /// The price is under the discounted intrinsic value.
    BelowIntrinsic(f64),
    /// The price is at or over the supremum any volatility approaches.
    AboveMaximum(f64),
    /// In bounds, yet the solver returned no volatility.
    NoSolution(f64),
}

impl fmt::Display for GreeksError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(m) => write!(f, "{m}"),
            Self::BelowIntrinsic(p) => write!(f, "price {p} is below its discounted intrinsic value"),
            Self::AboveMaximum(p) => write!(f, "price {p} is above what any volatility gives"),
            Self::NoSolution(p) => write!(f, "No implied volatility at price {p}"),
        }
    }
}

impl std::error::Error for GreeksError {}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Greeks {
    pub delta: f64,
    pub gamma: f64,
    /// Per calendar day.
    pub theta: f64,
    /// Per one volatility point.
    pub vega: f64,
    /// Per one rate point.
    pub rho: f64,
}

fn invalid(m: String) -> GreeksError {
    GreeksError::Invalid(m)
}

fn check_market(spot: f64, strike: f64, t: f64, rate: f64, q: f64) -> Result<(), GreeksError> {
    if !spot.is_finite() || spot <= 0.0 {
        return Err(invalid(format!("Underlying price {spot} is not positive")));
    }
    if !strike.is_finite() || strike <= 0.0 {
        return Err(invalid(format!("Strike {strike} is not positive")));
    }
    if !t.is_finite() || t <= 0.0 {
        return Err(invalid(format!("Time to expiry {t} is not positive")));
    }
    if !rate.is_finite() {
        return Err(invalid(format!("Rate {rate} is not finite")));
    }
    if !q.is_finite() {
        return Err(invalid(format!("Dividend yield {q} is not finite")));
    }
    Ok(())
}

fn check_sigma(sigma: f64) -> Result<(), GreeksError> {
    if !sigma.is_finite() || sigma <= 0.0 {
        return Err(invalid(format!("Volatility {sigma} is not positive")));
    }
    Ok(())
}

fn norm_cdf(x: f64) -> f64 {
    DefaultSpecialFn::norm_cdf(x)
}

fn norm_pdf(x: f64) -> f64 {
    (-0.5 * x * x).exp() / (2.0 * std::f64::consts::PI).sqrt()
}

fn d1_d2(spot: f64, strike: f64, t: f64, rate: f64, q: f64, sigma: f64) -> (f64, f64) {
    let d1 = ((spot / strike).ln() + ((rate - q) + sigma * sigma / 2.0) * t) / (sigma * t.sqrt());
    (d1, d1 - sigma * t.sqrt())
}

fn forward(spot: f64, rate: f64, q: f64, t: f64) -> f64 {
    spot * ((rate - q) * t).exp()
}

/// BSM premium per share at volatility `sigma`.
pub fn price(
    right: Right,
    spot: f64,
    strike: f64,
    t: f64,
    rate: f64,
    q: f64,
    sigma: f64,
) -> Result<f64, GreeksError> {
    check_market(spot, strike, t, rate, q)?;
    check_sigma(sigma)?;
    let f = forward(spot, rate, q, t);
    let black = PriceBlackScholes::builder()
        .forward(f)
        .strike(strike)
        .volatility(sigma)
        .expiry(t)
        .is_call(right == Right::Call)
        .build()
        .ok_or_else(|| invalid("inputs outside the model's domain".into()))?;
    let p = black.calculate::<DefaultSpecialFn>() * (-rate * t).exp();
    if p.is_finite() {
        Ok(p)
    } else {
        Err(invalid(format!("price is not finite for sigma {sigma}")))
    }
}

/// The volatility that prices the option at `premium`.
pub fn implied_vol(
    right: Right,
    spot: f64,
    strike: f64,
    t: f64,
    rate: f64,
    q: f64,
    premium: f64,
) -> Result<f64, GreeksError> {
    check_market(spot, strike, t, rate, q)?;
    if !premium.is_finite() || premium <= 0.0 {
        return Err(invalid(format!("Price {premium} is not positive")));
    }
    let f = forward(spot, rate, q, t);
    let undiscounted = premium / (-rate * t).exp();
    let is_call = right == Right::Call;
    // vollib's bounds: strictly under intrinsic is below; at or over the
    // supremum (F for a call, K for a put) is above.
    let intrinsic = if is_call { f - strike } else { strike - f }.max(0.0);
    if undiscounted < intrinsic {
        return Err(GreeksError::BelowIntrinsic(premium));
    }
    let max_price = if is_call { f } else { strike };
    if undiscounted >= max_price {
        return Err(GreeksError::AboveMaximum(premium));
    }
    let sigma = ImpliedBlackVolatility::builder()
        .option_price(undiscounted)
        .forward(f)
        .strike(strike)
        .expiry(t)
        .is_call(is_call)
        .build()
        .and_then(|iv| iv.calculate_with::<implied_vol::solver::Jaeckel>());
    let Some(sigma) = sigma else {
        // In bounds by our check, yet the solver found nothing: the price sits within
        // rounding of a bound. Name the nearer one, as vollib would one ulp over.
        return Err(if max_price - undiscounted < undiscounted - intrinsic {
            GreeksError::AboveMaximum(premium)
        } else {
            GreeksError::BelowIntrinsic(premium)
        });
    };
    if sigma.is_finite() && sigma > 0.0 {
        Ok(sigma)
    } else {
        Err(GreeksError::NoSolution(premium))
    }
}

/// Analytical greeks at volatility `sigma`.
pub fn greeks(
    right: Right,
    spot: f64,
    strike: f64,
    t: f64,
    rate: f64,
    q: f64,
    sigma: f64,
) -> Result<Greeks, GreeksError> {
    check_market(spot, strike, t, rate, q)?;
    check_sigma(sigma)?;
    let (d1, d2) = d1_d2(spot, strike, t, rate, q, sigma);
    let disc_q = (-q * t).exp();
    let disc_r = (-rate * t).exp();
    let first = spot * disc_q * norm_pdf(d1) * sigma / (2.0 * t.sqrt());
    let call = right == Right::Call;
    let delta = if call { disc_q * norm_cdf(d1) } else { -disc_q * norm_cdf(-d1) };
    let theta = if call {
        -(first + (-q * spot * disc_q * norm_cdf(d1)) + rate * strike * disc_r * norm_cdf(d2)) / 365.0
    } else {
        (-first + (-q * spot * disc_q * norm_cdf(-d1)) + rate * strike * disc_r * norm_cdf(-d2)) / 365.0
    };
    let rho = if call {
        t * strike * disc_r * norm_cdf(d2) * 0.01
    } else {
        -t * strike * disc_r * norm_cdf(-d2) * 0.01
    };
    Ok(Greeks {
        delta,
        gamma: disc_q * norm_pdf(d1) / (spot * sigma * t.sqrt()),
        theta,
        vega: spot * disc_q * norm_pdf(d1) * t.sqrt() * 0.01,
        rho,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: f64, b: f64, tol: f64) {
        assert!((a - b).abs() < tol, "{a} vs {b}");
    }

    #[test]
    fn haug_put_price() {
        // Haug p.4: S=100 K=95 q=.05 t=.5 r=.1 sigma=.2 put = 2.4648
        close(price(Right::Put, 100.0, 95.0, 0.5, 0.1, 0.05, 0.2).unwrap(), 2.4648, 1e-4);
    }

    #[test]
    fn vollib_doctest_call_and_iv() {
        let p = price(Right::Call, 100.0, 100.0, 0.5, 0.01, 0.0, 0.2).unwrap();
        close(p, 5.87602423383, 1e-9);
        close(implied_vol(Right::Call, 100.0, 100.0, 0.5, 0.01, 0.0, p).unwrap(), 0.2, 1e-12);
    }

    #[test]
    fn hull_theta_and_delta() {
        let g = greeks(Right::Call, 49.0, 50.0, 0.3846, 0.05, 0.0, 0.2).unwrap();
        close(g.delta, 0.521601633972, 1e-9);
        close(g.theta * 365.0, -4.30538996455, 1e-8);
    }

    #[test]
    fn put_call_relations() {
        let c = greeks(Right::Call, 100.0, 90.0, 1.0, 0.03, 0.02, 0.3).unwrap();
        let p = greeks(Right::Put, 100.0, 90.0, 1.0, 0.03, 0.02, 0.3).unwrap();
        close(c.delta - p.delta, (-0.02f64).exp(), 1e-12);
        close(c.gamma, p.gamma, 1e-15);
        close(c.vega, p.vega, 1e-15);
    }

    #[test]
    fn below_intrinsic_refuses() {
        let e = implied_vol(Right::Call, 100.0, 50.0, 1.0, 0.0, 0.0, 10.0).unwrap_err();
        assert!(matches!(e, GreeksError::BelowIntrinsic(_)), "{e}");
        assert!(e.to_string().contains("below"));
    }

    #[test]
    fn above_maximum_refuses() {
        let e = implied_vol(Right::Call, 100.0, 100.0, 1.0, 0.0, 0.0, 100.0).unwrap_err();
        assert!(matches!(e, GreeksError::AboveMaximum(_)), "{e}");
        let e = implied_vol(Right::Put, 100.0, 100.0, 1.0, 0.0, 0.0, 150.0).unwrap_err();
        assert!(matches!(e, GreeksError::AboveMaximum(_)), "{e}");
    }

    #[test]
    fn bad_inputs_refuse_not_nan() {
        assert!(price(Right::Call, 100.0, 100.0, 0.0, 0.0, 0.0, 0.2).is_err());
        assert!(price(Right::Call, f64::NAN, 100.0, 1.0, 0.0, 0.0, 0.2).is_err());
        assert!(greeks(Right::Call, 100.0, 100.0, 1.0, 0.0, 0.0, -0.2).is_err());
        assert!(implied_vol(Right::Call, 100.0, 100.0, 1.0, 0.0, 0.0, f64::NAN).is_err());
        assert!(implied_vol(Right::Call, 100.0, 100.0, 1.0, 0.0, 0.0, 0.0).is_err());
    }

    #[test]
    fn round_trip() {
        for &right in &[Right::Call, Right::Put] {
            for &k in &[60.0, 100.0, 180.0] {
                let p = price(right, 100.0, k, 0.25, 0.02, 0.01, 0.35).unwrap();
                let s = implied_vol(right, 100.0, k, 0.25, 0.02, 0.01, p).unwrap();
                close(s, 0.35, 1e-8);
            }
        }
    }
}
