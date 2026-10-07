//! The trailing-stop path rule (was `oms/trailing.py`): advance one observed price.

use super::{dk, dmax, dmin};
use crate::ledger::model::{err, Side, R};
use crate::money::Money;

/// The emulator's persisted state.
#[derive(Debug, Clone)]
pub struct Trail {
    pub side: Side,
    pub trail_amount: Money,
    pub extreme: Option<Money>,
    pub stop_price: Option<Money>,
    pub triggered: bool,
}

/// `TrailingStopEmulator.__init__`'s check.
pub fn check_trail_amount(trail_amount: &Money) -> R<()> {
    if trail_amount.cmp_int(0).map_err(dk)?.is_le() {
        return err("value", "trail_amount must be positive");
    }
    Ok(())
}

/// `update(price)`: whether the stop triggered. A refusal after the state moved leaves
/// it moved, as the Python did (the stop of a SELL is written before it is checked).
pub fn update(t: &mut Trail, price: &Money) -> R<bool> {
    if !price.is_finite() || price.cmp_int(0).map_err(dk)?.is_le() {
        return err("value", format!("price must be finite and positive, got {}", price.canon()));
    }
    if t.triggered {
        return Ok(true);
    }
    if t.side == Side::Sell {
        let extreme = match &t.extreme {
            None => price.clone(),
            Some(e) => dmax(e, price)?,
        };
        t.extreme = Some(extreme.clone());
        let stop = extreme.sub(&t.trail_amount).map_err(dk)?;
        t.stop_price = Some(stop.clone());
        if stop.cmp_int(0).map_err(dk)?.is_le() {
            return err("value", "trail amount produces a non-positive protective stop");
        }
        t.triggered = price.le(&stop).map_err(dk)?;
    } else {
        let extreme = match &t.extreme {
            None => price.clone(),
            Some(e) => dmin(e, price)?,
        };
        t.extreme = Some(extreme.clone());
        let stop = extreme.add(&t.trail_amount).map_err(dk)?;
        t.stop_price = Some(stop.clone());
        t.triggered = price.ge(&stop).map_err(dk)?;
    }
    Ok(t.triggered)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sim::dec;

    #[test]
    fn sell_trails_up_and_triggers() {
        let mut t = Trail { side: Side::Sell, trail_amount: dec("2"), extreme: None, stop_price: None, triggered: false };
        assert!(!update(&mut t, &dec("100")).unwrap());
        assert!(!update(&mut t, &dec("105")).unwrap());
        assert_eq!(t.stop_price.as_ref().unwrap().canon(), "103");
        assert!(update(&mut t, &dec("103")).unwrap());
    }

    #[test]
    fn buy_trails_down() {
        let mut t = Trail { side: Side::Buy, trail_amount: dec("1.5"), extreme: None, stop_price: None, triggered: false };
        assert!(!update(&mut t, &dec("10")).unwrap());
        assert!(!update(&mut t, &dec("9")).unwrap());
        assert!(update(&mut t, &dec("10.5")).unwrap());
    }
}
