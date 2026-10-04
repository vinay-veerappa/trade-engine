//! The mirror's netting decisions (was `tos_paper/netting.py`). P5-T1 carries
//! `vertical_reason`, which the ticket mapping needs; the netting itself arrives with T6.

use super::wire::{instrument, jstr, obj, req, ComboLeg, Instrument};
use crate::ledger::json::Json;
use crate::ledger::model::{derr, R};

/// `vertical_reason`: why `legs` is not a mirrorable 2-leg 1:1 vertical, or `None` when it is.
pub fn vertical_reason(legs: &[ComboLeg]) -> R<Option<String>> {
    let as_options: Vec<_> = legs
        .iter()
        .filter_map(|leg| match &leg.contract {
            Instrument::Option(c) => Some(c),
            _ => None,
        })
        .collect();
    if legs.len() != 2 || as_options.len() != 2 {
        let symbol = Instrument::Combo(legs.to_vec()).symbol()?;
        return Ok(Some(format!("{symbol} is not a 2-leg option combo; only verticals are mirrored")));
    }
    let (first, second) = (as_options[0], as_options[1]);
    if first.underlying != second.underlying {
        return Ok(Some("legs on two underlyings are not a vertical".into()));
    }
    if first.expiry != second.expiry {
        return Ok(Some("legs on two expiries (a calendar or diagonal) are not a vertical".into()));
    }
    if first.right != second.right {
        return Ok(Some("a call leg and a put leg are not a vertical".into()));
    }
    if first.multiplier != second.multiplier {
        return Ok(Some("legs with two multipliers are not a vertical (I6)".into()));
    }
    if first.strike.eq_num(&second.strike).map_err(derr)? {
        return Ok(Some("two legs on one strike are not a vertical".into()));
    }
    if legs[0].side == legs[1].side {
        return Ok(Some("both legs on one side are not a vertical".into()));
    }
    if legs[0].ratio != legs[1].ratio {
        return Ok(Some(format!("a {}:{} ratio spread is not a 1:1 vertical", legs[0].ratio, legs[1].ratio)));
    }
    Ok(None)
}

/// The door's `vertical_reason`: `{"instrument": <combo>}` -> `{"reason": str | null}`.
pub fn vertical_reason_op(doc: &Json) -> R<Json> {
    let Instrument::Combo(legs) = instrument(req(doc, "instrument")?)? else {
        return super::wire::wire("vertical_reason takes a combo");
    };
    Ok(obj(vec![("reason", vertical_reason(&legs)?.map_or(Json::Null, jstr))]))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::json::parse;

    fn combo(second: &str, side2: &str, ratio2: i64) -> Json {
        parse(&format!(
            r#"{{"instrument": {{"kind": "combo", "legs": [
              {{"contract": {{"kind": "option", "underlying": "AAPL", "expiry": "2026-10-16", "strike": "200", "right": "P", "multiplier": 100}}, "ratio": 1, "side": "SELL"}},
              {{"contract": {second}, "ratio": {ratio2}, "side": "{side2}"}}]}}}}"#
        ))
        .unwrap()
    }

    fn reason(doc: &Json) -> Option<String> {
        match vertical_reason_op(doc).unwrap().get("reason").unwrap() {
            Json::Str(s) => Some(s.clone()),
            _ => None,
        }
    }

    const P195: &str = r#"{"kind": "option", "underlying": "AAPL", "expiry": "2026-10-16", "strike": "195", "right": "P", "multiplier": 100}"#;

    #[test]
    fn a_vertical_and_each_way_it_is_not() {
        assert_eq!(reason(&combo(P195, "BUY", 1)), None);
        assert_eq!(reason(&combo(P195, "SELL", 1)).unwrap(), "both legs on one side are not a vertical");
        assert_eq!(reason(&combo(P195, "BUY", 2)).unwrap(), "a 1:2 ratio spread is not a 1:1 vertical");
        let same = P195.replace("195", "200.0");
        assert_eq!(reason(&combo(&same, "BUY", 1)).unwrap(), "two legs on one strike are not a vertical");
        let call = P195.replace("\"P\"", "\"C\"");
        assert_eq!(reason(&combo(&call, "BUY", 1)).unwrap(), "a call leg and a put leg are not a vertical");
        let stock = r#"{"kind": "equity", "symbol": "AAPL"}"#;
        assert_eq!(
            reason(&combo(stock, "BUY", 1)).unwrap(),
            "SELL:1xAAPL  261016P00200000/BUY:1xAAPL is not a 2-leg option combo; only verticals are mirrored"
        );
    }
}
