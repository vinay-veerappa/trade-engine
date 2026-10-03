//! What an option becomes at expiry, on exercise and on assignment (was
//! `domain/option_lifecycle.py`). Money is `rust_decimal`, crossing as text (D6): a result
//! keeps the scale Python's `Decimal` arithmetic gave it (`100.00 - 100` is `0.00`).
//!
//! Not identical to the pre-port Python, by design: an infinite money value, a strike or
//! price of more than 28 significant digits, and an exponent-form `Decimal` (`1E+2`) are
//! outside `rust_decimal`'s range; they are refused here, where Python rounded or kept
//! the exponent. No caller produces one (prices are quoted to the cent or the tick).

use std::str::FromStr;

use rust_decimal::Decimal;

use super::{equity_symbol, option_style, value, ContractWire, Exercise, OptionError, Result, Right, Settlement};

const THRESHOLD: &str = "0.01";

/// The OCC's exercise-by-exception threshold, as text.
pub fn exercise_threshold() -> &'static str {
    THRESHOLD
}

fn threshold() -> Decimal {
    Decimal::from_str(THRESHOLD).expect("constant")
}

/// A money value: `None` is a NaN, which raises `decimal.InvalidOperation` the moment it
/// is compared (Python's ordering of a NaN signals).
struct Money(Option<Decimal>);

impl Money {
    fn parse(text: &str) -> Result<Money> {
        let t = text.trim();
        if t.eq_ignore_ascii_case("nan") || t.eq_ignore_ascii_case("snan") || t.eq_ignore_ascii_case("-nan") {
            return Ok(Money(None));
        }
        if let Ok(d) = Decimal::from_str_exact(t) {
            return Ok(Money(Some(d)));
        }
        match Decimal::from_scientific(t) {
            Ok(d) => Ok(Money(Some(d))),
            Err(_) => value(format!("Unsupported money value {text}")),
        }
    }

    fn get(&self) -> Result<Decimal> {
        self.0.ok_or(OptionError::InvalidOperation)
    }
}

fn strike(c: &ContractWire) -> Result<Decimal> {
    Money::parse(&c.strike.text)?.get()
}

/// Python's `Decimal` add and subtract keep the larger scale (`100.00 - 100` is `0.00`,
/// `100 + 0.00` is `100.00`); `rust_decimal` drops it when an operand is zero.
fn with_scale_of(r: Decimal, a: Decimal, b: Decimal) -> Decimal {
    let mut r = r;
    let want = a.scale().max(b.scale());
    if r.scale() < want {
        r.rescale(want);
    }
    r
}

fn sub(a: Decimal, b: Decimal) -> Result<Decimal> {
    a.checked_sub(b).map(|r| with_scale_of(r, a, b)).ok_or(OptionError::Overflow)
}

fn add(a: Decimal, b: Decimal) -> Result<Decimal> {
    a.checked_add(b).map(|r| with_scale_of(r, a, b)).ok_or(OptionError::Overflow)
}

/// Per-share intrinsic value at the price; never negative.
fn intrinsic_value(c: &ContractWire, price_text: &str) -> Result<Decimal> {
    let price = Money::parse(price_text)?.get()?;
    if price <= Decimal::ZERO {
        return value(format!("Underlying price must be positive, got {price_text} (I5)"));
    }
    let k = strike(c)?;
    let raw = if c.right == Right::Call { sub(price, k)? } else { sub(k, price)? };
    // Python's max(raw, ZERO): ZERO only when it is strictly greater, so a tie keeps raw
    // (and its scale).
    Ok(if Decimal::ZERO > raw { Decimal::ZERO } else { raw })
}

pub fn intrinsic(c: &ContractWire, price_text: &str) -> Result<String> {
    intrinsic_value(c, price_text).map(|d| d.to_string())
}

/// "expire", "exercise" or "assign".
pub fn expiry_outcome(c: &ContractWire, held_buy: bool, price_text: &str) -> Result<&'static str> {
    if intrinsic_value(c, price_text)? < threshold() {
        return Ok("expire");
    }
    Ok(if held_buy { "exercise" } else { "assign" })
}

pub fn is_cash_settled(c: &ContractWire) -> Result<bool> {
    Ok(option_style(&c.underlying)?.settlement == Settlement::Cash)
}

pub fn can_exercise_early(c: &ContractWire) -> Result<bool> {
    Ok(option_style(&c.underlying)?.exercise == Exercise::American)
}

/// The deliverable's equity symbol; a cash-settled contract refuses.
pub fn deliverable(c: &ContractWire) -> Result<String> {
    if is_cash_settled(c)? {
        return value(format!("{} settles in cash; it delivers no shares", c.occ()?));
    }
    equity_symbol(Some(&option_style(&c.underlying)?.underlying))
}

/// The share trade one exercised or assigned lot makes: (buys, per-share price).
pub fn delivery(c: &ContractWire, held_buy: bool, premium_text: &str) -> Result<(bool, String)> {
    let premium = Money::parse(premium_text)?.get()?;
    if premium < Decimal::ZERO {
        return value(format!("Premium must not be negative, got {premium_text}"));
    }
    let call = c.right == Right::Call;
    let buys = call == held_buy;
    let k = strike(c)?;
    if call {
        return Ok((buys, add(k, premium)?.to_string()));
    }
    let price = sub(k, premium)?;
    if price <= Decimal::ZERO {
        return value(format!("{}: premium {premium_text} is not below the strike (I5)", c.occ()?));
    }
    Ok((buys, price.to_string()))
}

/// Whether the holder of an American call exercises it before the ex-dividend date.
pub fn exercised_for_dividend(c: &ContractWire, close_text: &str, bid_text: &str, dividend_text: &str) -> Result<bool> {
    if c.right != Right::Call {
        return value(format!("{} is a put; the dividend rule is for calls", c.occ()?));
    }
    if !can_exercise_early(c)? {
        return Ok(false);
    }
    let dividend = Money::parse(dividend_text)?.get()?;
    if dividend <= Decimal::ZERO {
        return value(format!("Dividend must be positive, got {dividend_text} (I5)"));
    }
    let bid = Money::parse(bid_text)?.get()?;
    if bid < Decimal::ZERO {
        return value(format!("Bid must not be negative, got {bid_text}"));
    }
    let intrinsic = intrinsic_value(c, close_text)?;
    if intrinsic < threshold() {
        return Ok(false);
    }
    Ok(sub(bid, intrinsic)? < dividend)
}

#[cfg(test)]
mod tests {
    use super::super::{DecWire, Special};
    use super::*;

    fn contract(root: &str, right: Right, strike: &str) -> ContractWire {
        let t = strike.to_string();
        let (int, frac) = t.split_once('.').unwrap_or((&t, ""));
        ContractWire {
            underlying: root.into(),
            year: 2026,
            month: 10,
            day: 16,
            right,
            strike: DecWire {
                neg: false,
                digits: format!("{int}{frac}"),
                exp: -(frac.len() as i64),
                special: Special::Finite,
                text: t,
            },
        }
    }

    #[test]
    fn intrinsic_is_never_negative_and_keeps_scale() {
        let call = contract("AAPL", Right::Call, "100");
        assert_eq!(intrinsic(&call, "105.50").unwrap(), "5.50");
        assert_eq!(intrinsic(&call, "95").unwrap(), "0");
        assert_eq!(intrinsic(&call, "100.00").unwrap(), "0.00");
        let put = contract("AAPL", Right::Put, "100");
        assert_eq!(intrinsic(&put, "95").unwrap(), "5");
        assert_eq!(intrinsic(&put, "105").unwrap(), "0");
        assert_eq!(intrinsic(&put, "0").unwrap_err().message(), "Underlying price must be positive, got 0 (I5)");
    }

    #[test]
    fn outcome_follows_the_threshold_and_side() {
        let call = contract("AAPL", Right::Call, "100");
        assert_eq!(expiry_outcome(&call, true, "100.01").unwrap(), "exercise");
        assert_eq!(expiry_outcome(&call, false, "100.01").unwrap(), "assign");
        assert_eq!(expiry_outcome(&call, true, "100.009").unwrap(), "expire");
    }

    #[test]
    fn delivery_puts_the_premium_in_the_price() {
        let put = contract("AAPL", Right::Put, "100");
        assert_eq!(delivery(&put, false, "2.50").unwrap(), (true, "97.50".to_string()));
        assert_eq!(
            delivery(&put, false, "100").unwrap_err().message(),
            "AAPL  261016P00100000: premium 100 is not below the strike (I5)"
        );
        let call = contract("AAPL", Right::Call, "100");
        assert_eq!(delivery(&call, true, "2.5").unwrap(), (true, "102.5".to_string()));
        assert_eq!(delivery(&call, false, "0").unwrap(), (false, "100".to_string()));
    }

    #[test]
    fn cash_settled_delivers_nothing_and_the_dividend_rule_is_for_calls() {
        let spx = contract("SPX", Right::Call, "5000");
        assert!(is_cash_settled(&spx).unwrap() && !can_exercise_early(&spx).unwrap());
        assert_eq!(deliverable(&spx).unwrap_err().message(), "SPX   261016C05000000 settles in cash; it delivers no shares");
        assert_eq!(deliverable(&contract("aapl", Right::Call, "1")).unwrap(), "AAPL");
        let put = contract("AAPL", Right::Put, "100");
        assert!(exercised_for_dividend(&put, "100", "1", "1").is_err());
        let call = contract("AAPL", Right::Call, "100");
        // intrinsic 10, bid 10.20: extrinsic 0.20 < dividend 0.50 -> exercise
        assert!(exercised_for_dividend(&call, "110", "10.20", "0.50").unwrap());
        assert!(!exercised_for_dividend(&call, "110", "10.50", "0.50").unwrap());
        assert!(!exercised_for_dividend(&spx, "5100", "100", "1").unwrap());
    }
}
