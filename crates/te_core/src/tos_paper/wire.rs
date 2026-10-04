//! Reading the door's JSON arguments, and the instrument wire shared by the modules.
//!
//! An instrument crosses as `{"kind": "equity", "symbol"}`, `{"kind": "option",
//! "underlying", "expiry": "YYYY-MM-DD", "strike": "<Decimal text>", "right", "multiplier"}`,
//! `{"kind": "combo", "legs": [{"contract", "ratio", "side"}]}` or `{"kind": "other",
//! "repr"}` (an instrument the mirror does not know). The host builds these from its
//! domain objects; `symbol`, equality and OCC text are computed here, once.

use super::{pytext, OVERFLOW_ERROR, VALUE, WIRE};
use crate::ledger::json::Json;
use crate::ledger::model::{decwire, derr, err, LErr, R};
use crate::ledger::pydec::PyDec;
use crate::options::pyrules::{is_space, strip};
use crate::options::{ContractWire, Right};

pub fn wire<T>(msg: impl Into<String>) -> R<T> {
    err(WIRE, msg)
}

pub fn obj(pairs: Vec<(&str, Json)>) -> Json {
    Json::Obj(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

pub fn jstr(s: impl Into<String>) -> Json {
    Json::Str(s.into())
}

pub fn jopt_dec(d: &Option<PyDec>) -> Json {
    match d {
        Some(d) => jstr(d.to_py_string()),
        None => Json::Null,
    }
}

pub fn req<'a>(j: &'a Json, key: &str) -> R<&'a Json> {
    j.get(key).map_or_else(|| wire(format!("door document lacks {key:?}")), Ok)
}

pub fn req_str<'a>(j: &'a Json, key: &str) -> R<&'a str> {
    match req(j, key)? {
        Json::Str(s) => Ok(s),
        other => wire(format!("{key:?} must be a string, got {}", other.type_name())),
    }
}

pub fn req_int(j: &Json, key: &str) -> R<i128> {
    match req(j, key)? {
        Json::Int(n) => Ok(*n),
        other => wire(format!("{key:?} must be an int, got {}", other.type_name())),
    }
}

pub fn dec_of(text: &str) -> R<PyDec> {
    PyDec::parse(text).map_or_else(|| wire(format!("not a Decimal: {text:?}")), Ok)
}

pub fn req_dec(j: &Json, key: &str) -> R<PyDec> {
    dec_of(req_str(j, key)?)
}

/// A Decimal that may be `null` (or absent).
pub fn opt_dec(j: &Json, key: &str) -> R<Option<PyDec>> {
    match j.get(key) {
        None | Some(Json::Null) => Ok(None),
        Some(Json::Str(s)) => dec_of(s).map(Some),
        Some(other) => wire(format!("{key:?} must be a Decimal string or null, got {}", other.type_name())),
    }
}

pub fn req_arr<'a>(j: &'a Json, key: &str) -> R<&'a [Json]> {
    match req(j, key)? {
        Json::Arr(a) => Ok(a),
        other => wire(format!("{key:?} must be a list, got {}", other.type_name())),
    }
}

/// An instrument as the mirror's decisions read it.
#[derive(Debug, Clone)]
pub enum Instrument {
    Equity { symbol: String },
    Option(OptionC),
    Combo(Vec<ComboLeg>),
    Other { repr: String },
}

#[derive(Debug, Clone)]
pub struct OptionC {
    pub underlying: String,
    pub expiry: String,
    pub strike: PyDec,
    pub right: Right,
    pub multiplier: i128,
}

#[derive(Debug, Clone)]
pub struct ComboLeg {
    pub contract: Instrument,
    pub ratio: i128,
    pub side: String,
}

impl OptionC {
    /// `contract.to_occ()`.
    pub fn occ(&self) -> R<String> {
        let ymd: Vec<&str> = self.expiry.split('-').collect();
        let num = |i: usize| ymd.get(i).and_then(|t| t.parse::<i64>().ok());
        let (Some(y), Some(m), Some(d)) = (num(0), num(1), num(2)) else {
            return wire(format!("bad expiry {:?}", self.expiry));
        };
        let c = ContractWire {
            underlying: self.underlying.clone(),
            year: y as i32,
            month: m as u32,
            day: d as u32,
            right: self.right,
            strike: decwire(&self.strike),
        };
        c.occ().map_err(|e| LErr { kind: VALUE, msg: e.message().to_string() })
    }
}

pub fn instrument(j: &Json) -> R<Instrument> {
    match req_str(j, "kind")? {
        "equity" => Ok(Instrument::Equity { symbol: req_str(j, "symbol")?.to_string() }),
        "option" => {
            let right = match req_str(j, "right")? {
                "C" => Right::Call,
                "P" => Right::Put,
                other => return wire(format!("bad right {other:?}")),
            };
            Ok(Instrument::Option(OptionC {
                underlying: req_str(j, "underlying")?.to_string(),
                expiry: req_str(j, "expiry")?.to_string(),
                strike: req_dec(j, "strike")?,
                right,
                multiplier: req_int(j, "multiplier")?,
            }))
        }
        "combo" => {
            let mut legs = Vec::new();
            for leg in req_arr(j, "legs")? {
                legs.push(ComboLeg {
                    contract: instrument(req(leg, "contract")?)?,
                    ratio: req_int(leg, "ratio")?,
                    side: req_str(leg, "side")?.to_string(),
                });
            }
            Ok(Instrument::Combo(legs))
        }
        "other" => Ok(Instrument::Other { repr: req_str(j, "repr")?.to_string() }),
        other => wire(format!("unknown instrument kind {other:?}")),
    }
}

impl Instrument {
    /// `instrument.symbol`.
    pub fn symbol(&self) -> R<String> {
        match self {
            Instrument::Equity { symbol } => Ok(symbol.clone()),
            Instrument::Option(c) => c.occ(),
            Instrument::Combo(legs) => {
                let mut parts = Vec::new();
                for leg in legs {
                    parts.push(format!("{}:{}x{}", leg.side, leg.ratio, strip(&leg.contract.symbol()?)));
                }
                Ok(parts.join("/"))
            }
            Instrument::Other { repr } => Ok(repr.clone()),
        }
    }

    /// `a == b` for instruments: same class and fields (a strike by value, `200 == 200.0`).
    pub fn same(&self, other: &Instrument) -> R<bool> {
        match (self, other) {
            (Instrument::Equity { symbol: a }, Instrument::Equity { symbol: b }) => Ok(a == b),
            (Instrument::Option(a), Instrument::Option(b)) => Ok(a.underlying == b.underlying
                && a.expiry == b.expiry
                && a.right == b.right
                && a.multiplier == b.multiplier
                && a.strike.eq_num(&b.strike).map_err(derr)?),
            (Instrument::Combo(a), Instrument::Combo(b)) => {
                if a.len() != b.len() {
                    return Ok(false);
                }
                for (x, y) in a.iter().zip(b) {
                    if x.ratio != y.ratio || x.side != y.side || !x.contract.same(&y.contract)? {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            (Instrument::Other { repr: a }, Instrument::Other { repr: b }) => Ok(a == b),
            _ => Ok(false),
        }
    }
}

/// `int(d)` as a JSON int of any size: a Decimal's integral part, truncated toward zero.
pub fn int_json(d: &PyDec) -> R<Json> {
    if d.is_nan() {
        return err(VALUE, "cannot convert NaN to integer");
    }
    if !d.is_finite() {
        return err(OVERFLOW_ERROR, "cannot convert Infinity to integer");
    }
    // Truncate toward zero on the exact digits (int() never rounds to the context).
    let mut digits = d.coefficient().to_str_radix(10);
    let exp = d.exponent();
    if exp >= 0 {
        if digits != "0" {
            digits.push_str(&"0".repeat(exp as usize));
        }
    } else {
        let drop = (-exp) as usize;
        digits = if drop >= digits.len() { "0".into() } else { digits[..digits.len() - drop].to_string() };
    }
    let text = if d.is_negative() && digits != "0" { format!("-{digits}") } else { digits };
    Ok(match text.parse::<i128>() {
        Ok(n) => Json::Int(n),
        Err(_) => Json::BigInt(text),
    })
}

/// A probe of the Python text facts the tables hold, for the parity tests: for each
/// character of `chars`, `[isdigit, isprintable, isspace]`.
pub fn text_probe(j: &Json) -> R<Json> {
    let chars = req_str(j, "chars")?;
    Ok(Json::Arr(
        chars
            .chars()
            .map(|c| {
                Json::Arr(vec![
                    Json::Bool(pytext::is_digit(c)),
                    Json::Bool(pytext::is_printable(c)),
                    Json::Bool(is_space(c)),
                ])
            })
            .collect(),
    ))
}

// -- the ledger model's instruments, for the decisions that fold with the ledger -------------------

use crate::ledger::model as lm;

/// An instrument document read as the ledger model's `Instrument` (an equity, an option
/// or a combo: `kind: "other"` is not one, and refuses as a host bug).
pub fn linstr(j: &Json) -> R<lm::Instrument> {
    match req_str(j, "kind")? {
        "equity" => Ok(lm::Instrument::Equity(req_str(j, "symbol")?.to_string())),
        "option" => {
            let right = match req_str(j, "right")? {
                "C" => Right::Call,
                "P" => Right::Put,
                other => return wire(format!("bad right {other:?}")),
            };
            Ok(lm::Instrument::Option(lm::OptionContract {
                underlying: req_str(j, "underlying")?.to_string(),
                expiry: lm::parse_date(req_str(j, "expiry")?)?,
                strike: req_dec(j, "strike")?,
                right,
                multiplier: req_int(j, "multiplier")?,
            }))
        }
        "combo" => {
            let mut legs = Vec::new();
            for leg in req_arr(j, "legs")? {
                legs.push(lm::ComboLeg {
                    contract: linstr(req(leg, "contract")?)?,
                    ratio: req_int(leg, "ratio")?,
                    side: side_of(req_str(leg, "side")?)?,
                });
            }
            Ok(lm::Instrument::Combo(legs))
        }
        other => wire(format!("instrument kind {other:?} has no ledger form")),
    }
}

/// The wire document of a model instrument (the strike keeps its spelling).
pub fn linstr_json(i: &lm::Instrument) -> Json {
    match i {
        lm::Instrument::Equity(s) => obj(vec![("kind", jstr("equity")), ("symbol", jstr(s.clone()))]),
        lm::Instrument::Option(c) => obj(vec![
            ("kind", jstr("option")),
            ("underlying", jstr(c.underlying.clone())),
            ("expiry", jstr(lm::date_iso(&c.expiry))),
            ("strike", jstr(c.strike.to_py_string())),
            ("right", jstr(if c.right == Right::Call { "C" } else { "P" })),
            ("multiplier", Json::Int(c.multiplier)),
        ]),
        lm::Instrument::Combo(legs) => obj(vec![
            ("kind", jstr("combo")),
            (
                "legs",
                Json::Arr(
                    legs.iter()
                        .map(|l| {
                            obj(vec![
                                ("contract", linstr_json(&l.contract)),
                                ("ratio", Json::Int(l.ratio)),
                                ("side", jstr(l.side.value())),
                            ])
                        })
                        .collect(),
                ),
            ),
        ]),
    }
}

pub fn side_of(text: &str) -> R<lm::Side> {
    lm::Side::parse(text).map_or_else(|| wire(format!("bad side {text:?}")), Ok)
}

pub fn order_type_of(text: &str) -> R<lm::OrderType> {
    lm::OrderType::parse(text).map_or_else(|| wire(format!("bad order type {text:?}")), Ok)
}

pub fn state_of(text: &str) -> R<lm::OrderState> {
    lm::OrderState::parse(text).map_or_else(|| wire(format!("bad order state {text:?}")), Ok)
}

pub fn tif_of(text: &str) -> R<lm::Tif> {
    lm::Tif::parse(text).map_or_else(|| wire(format!("bad tif {text:?}")), Ok)
}
