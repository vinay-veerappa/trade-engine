//! Futures contract specifications, symbol parsing, and tick arithmetic (docs/RUST_PORT.md P6B-T1).
//!
//! # Spec Table
//!
//! Specs come from one table in Rust:
//! - NQ: tick_size = 0.25, point_value = 20
//! - MNQ: tick_size = 0.25, point_value = 2
//! - ES: tick_size = 0.25, point_value = 50
//! - MES: tick_size = 0.25, point_value = 5
//! - YM: 1.0 / 5, MYM: 1.0 / 0.5, RTY: 0.1 / 50, M2K: 0.1 / 5 (P6C)
//! - CL: 0.01 / 1000, MCL: 0.01 / 100, GC: 0.1 / 100, MGC: 0.1 / 10 (P6C)
//!
//! Every root here has a Globex calendar (`calendar::globex::GlobexCalendar::for_root`); a test keeps the two lists equal.
//! Unknown roots (such as ZN, SPY) are explicitly refused.
//!
//! Values match `tvDownloadOHLC/web/lib/contract-specs.ts` (L48-57) exactly; checked against CME contract specs in P6C.
//!
//! # Rounding and Slippage
//!
//! `round_to_tick(price, tick, mode)` rounds a price to the discrete tick grid.
//! Supported modes:
//! - `RoundMode::Floor`: rounds towards negative infinity (-∞).
//! - `RoundMode::Ceil`: rounds towards positive infinity (+∞).
//! - `RoundMode::Nearest`: rounds to nearest tick with tie breaking towards positive infinity (+∞),
//!   matching JavaScript's `Math.round` as used in `tvDownloadOHLC/web/lib/contract-specs.ts::snapTick`.
//!   A tie at exact halfway 0.5 rounds up towards +∞ (e.g. 100.125 -> 100.25, -100.125 -> -100.00).
//!
//! `slip_ticks(price, side, n, tick)` applies `n` adverse ticks:
//! - Buy: `price + n * tick`
//! - Sell: `price - n * tick`
//! matching `replay-sim.ts::applySlippage` (plan §0.2). Slipped prices snap to the tick grid.

use std::cmp::Ordering;

use crate::ledger::model::{err, ContractMonth, FutureContract, Instrument, LErr, Side, R};
use crate::ledger::pydec::{DecErr, PyDec, Round};

/// Static contract specification definitions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StaticFutureSpec {
    pub root: &'static str,
    pub tick_size_str: &'static str,
    pub point_value_str: &'static str,
}

/// System-of-record specifications for supported CME futures roots (equity index, energy, metals).
/// Refuses unknown roots.
pub const FUTURE_SPECS: &[StaticFutureSpec] = &[
    StaticFutureSpec { root: "NQ", tick_size_str: "0.25", point_value_str: "20" },
    StaticFutureSpec { root: "MNQ", tick_size_str: "0.25", point_value_str: "2" },
    StaticFutureSpec { root: "ES", tick_size_str: "0.25", point_value_str: "50" },
    StaticFutureSpec { root: "MES", tick_size_str: "0.25", point_value_str: "5" },
    StaticFutureSpec { root: "YM", tick_size_str: "1.0", point_value_str: "5" },
    StaticFutureSpec { root: "MYM", tick_size_str: "1.0", point_value_str: "0.5" },
    StaticFutureSpec { root: "RTY", tick_size_str: "0.1", point_value_str: "50" },
    StaticFutureSpec { root: "M2K", tick_size_str: "0.1", point_value_str: "5" },
    StaticFutureSpec { root: "CL", tick_size_str: "0.01", point_value_str: "1000" },
    StaticFutureSpec { root: "MCL", tick_size_str: "0.01", point_value_str: "100" },
    StaticFutureSpec { root: "GC", tick_size_str: "0.1", point_value_str: "100" },
    StaticFutureSpec { root: "MGC", tick_size_str: "0.1", point_value_str: "10" },
];

/// A resolved futures contract specification with parsed decimal fields.
#[derive(Debug, Clone)]
pub struct FutureSpec {
    pub root: &'static str,
    pub tick_size: PyDec,
    pub point_value: PyDec,
}

impl FutureSpec {
    pub fn same(&self, o: &FutureSpec) -> bool {
        self.root == o.root
            && crate::ledger::model::dec_eq(&self.tick_size, &o.tick_size)
            && crate::ledger::model::dec_eq(&self.point_value, &o.point_value)
    }
}

fn lerr(kind: &'static str, msg: impl Into<String>) -> LErr {
    LErr { kind, msg: msg.into() }
}

fn derr(e: DecErr) -> LErr {
    LErr {
        kind: match e {
            DecErr::InvalidOperation => "value",
            DecErr::DivisionByZero => "division_by_zero",
            DecErr::Overflow => "overflow",
            DecErr::Unsupported(_) => "unsupported",
        },
        msg: format!("{e:?}"),
    }
}

fn d(r: Result<PyDec, DecErr>) -> R<PyDec> {
    r.map_err(derr)
}

/// Lookup contract specification for a futures root.
/// Refuses unknown roots.
pub fn spec_for_root(root: &str) -> R<FutureSpec> {
    let r_upper = root.trim().to_ascii_uppercase();
    for s in FUTURE_SPECS {
        if s.root == r_upper {
            let tick_size = PyDec::parse(s.tick_size_str)
                .ok_or_else(|| lerr("value", "internal error: invalid tick_size"))?;
            let point_value = PyDec::parse(s.point_value_str)
                .ok_or_else(|| lerr("value", "internal error: invalid point_value"))?;
            return Ok(FutureSpec {
                root: s.root,
                tick_size,
                point_value,
            });
        }
    }
    err("unsupported", format!("Unknown futures root: '{root}'"))
}

/// Standard CME month codes:
/// F (Jan), G (Feb), H (Mar), J (Apr), K (May), M (Jun),
/// N (Jul), Q (Aug), U (Sep), V (Oct), X (Nov), Z (Dec).
pub fn month_from_code(code: char) -> Option<u32> {
    match code.to_ascii_uppercase() {
        'F' => Some(1),
        'G' => Some(2),
        'H' => Some(3),
        'J' => Some(4),
        'K' => Some(5),
        'M' => Some(6),
        'N' => Some(7),
        'Q' => Some(8),
        'U' => Some(9),
        'V' => Some(10),
        'X' => Some(11),
        'Z' => Some(12),
        _ => None,
    }
}

pub fn month_to_code(month: u32) -> char {
    match month {
        1 => 'F',
        2 => 'G',
        3 => 'H',
        4 => 'J',
        5 => 'K',
        6 => 'M',
        7 => 'N',
        8 => 'Q',
        9 => 'U',
        10 => 'V',
        11 => 'X',
        12 => 'Z',
        _ => '?',
    }
}

/// Parse a futures symbol into a `FutureContract`.
///
/// Supported shapes:
/// - Continuous roots: `"NQ"`, `"MNQ"`, `"ES"`, `"MES"`
/// - Leading slash: `"/ES"`, `"/NQ"`, `"/MNQ"`, `"/MES"`
/// - Month-coded contracts: `"NQZ26"`, `"MNQZ26"`, `"/ESZ26"`, `"MESH25"`, `"NQZ6"`, `"NQZ2026"`
///
/// Malformed symbols refuse with clear messages.
/// Unknown roots refuse with `Unknown futures root: '...'`.
pub fn parse_future_symbol(symbol: &str) -> R<FutureContract> {
    let trimmed = symbol.trim();
    if trimmed.is_empty() {
        return err("value", "Futures symbol must be a non-empty string");
    }

    let bare = if let Some(stripped) = trimmed.strip_prefix('/') {
        if stripped.is_empty() {
            return err("value", "Futures symbol must not be empty after '/'");
        }
        if stripped.starts_with('/') {
            return err("value", format!("Futures symbol contains multiple leading slashes: '{symbol}'"));
        }
        stripped
    } else {
        trimmed
    };

    if bare.contains('/') || bare.contains(' ') || bare.contains('\t') {
        return err("value", format!("Futures symbol must not contain slashes or whitespace: '{symbol}'"));
    }

    let upper = bare.to_ascii_uppercase();

    // Check supported roots ordered by length descending so MNQ is matched before NQ, MES before ES.
    let mut matched_root: Option<&'static str> = None;
    let mut remainder = "";

    let mut candidates: Vec<&'static str> = FUTURE_SPECS.iter().map(|s| s.root).collect();
    candidates.sort_by_key(|c| std::cmp::Reverse(c.len()));
    for candidate in candidates {
        if upper.starts_with(candidate) {
            matched_root = Some(candidate);
            remainder = &upper[candidate.len()..];
            break;
        }
    }

    let Some(root) = matched_root else {
        let chars: Vec<char> = upper.chars().collect();
        let mut i = chars.len();
        while i > 0 && chars[i - 1].is_ascii_digit() {
            i -= 1;
        }
        let mut r_end = upper.len();
        if i > 0 && i < chars.len() && month_from_code(chars[i - 1]).is_some() {
            r_end = i - 1;
        }
        let root_guess = if r_end > 0 { &upper[..r_end] } else { &upper };
        return err("unsupported", format!("Unknown futures root: '{root_guess}'"));
    };

    let spec = spec_for_root(root)?;

    let contract_month = if remainder.is_empty() {
        None
    } else {
        let first_char = remainder.chars().next().unwrap();
        let month = month_from_code(first_char).ok_or_else(|| {
            lerr("value", format!("Invalid contract month code '{first_char}' in futures symbol '{symbol}'"))
        })?;
        let year_str = &remainder[first_char.len_utf8()..];
        if year_str.is_empty() {
            return err("value", format!("Missing contract year in futures symbol '{symbol}'"));
        }
        if !year_str.chars().all(|c| c.is_ascii_digit()) {
            return err("value", format!("Invalid contract year '{year_str}' in futures symbol '{symbol}': expected digits"));
        }

        let year: i32 = match year_str.len() {
            1 => {
                let y: i32 = year_str.parse().map_err(|_| lerr("value", "invalid year digit"))?;
                2020 + y
            }
            2 => {
                let y: i32 = year_str.parse().map_err(|_| lerr("value", "invalid year digits"))?;
                if y <= 68 { 2000 + y } else { 1900 + y }
            }
            4 => year_str.parse().map_err(|_| lerr("value", "invalid year digits"))?,
            _ => return err("value", format!("Invalid contract year '{year_str}' in futures symbol '{symbol}': expected 1, 2, or 4 digits")),
        };

        Some(ContractMonth::new(year, month)?)
    };

    Ok(FutureContract {
        root: root.to_string(),
        contract_month,
        tick_size: spec.tick_size,
        point_value: spec.point_value,
    })
}

/// Convenience parser returning `Instrument::Future`.
pub fn parse_future(symbol: &str) -> R<Instrument> {
    parse_future_symbol(symbol).map(Instrument::Future)
}

/// Rounding mode for tick grid snapping.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RoundMode {
    /// Round to nearest tick grid point.
    /// Tie rule: ties at 0.5 round towards positive infinity (+∞),
    /// matching `tvDownloadOHLC/web/lib/contract-specs.ts::snapTick` (JS `Math.round`).
    Nearest,
    /// Round towards negative infinity (-∞).
    Floor,
    /// Round towards positive infinity (+∞).
    Ceil,
}

/// Snap `price` to multiples of `tick` in checked `PyDec` according to `mode`.
///
/// For `RoundMode::Nearest`:
/// Ties (exact 0.5 fractions of a tick) break towards positive infinity (+∞),
/// matching JavaScript's `Math.round` in `tvDownloadOHLC/web/lib/contract-specs.ts::snapTick`.
///
/// For `RoundMode::Floor`:
/// Rounds towards -∞ (`to_integral(Round::Floor)`).
///
/// For `RoundMode::Ceil`:
/// Rounds towards +∞ (`-Floor(-x)`).
pub fn round_to_tick(price: &PyDec, tick: &PyDec, mode: RoundMode) -> R<PyDec> {
    if !price.is_finite() {
        return err("value", "price must be finite");
    }
    if !tick.is_finite() || tick.cmp_int(0).map_err(derr)? != Ordering::Greater {
        return err("value", "tick size must be positive and finite");
    }

    let units = d(price.div(tick))?;
    let rounded_units = match mode {
        RoundMode::Floor => d(units.to_integral(Round::Floor))?,
        RoundMode::Ceil => {
            let neg_units = d(units.neg())?;
            let floor_neg = d(neg_units.to_integral(Round::Floor))?;
            d(floor_neg.neg())?
        }
        RoundMode::Nearest => {
            // JS Math.round(x) is floor(x + 0.5), which breaks ties towards +∞.
            let half = PyDec::parse("0.5").ok_or_else(|| lerr("value", "internal error: 0.5"))?;
            let shifted = d(units.add(&half))?;
            d(shifted.to_integral(Round::Floor))?
        }
    };

    let snapped = d(rounded_units.mul(tick))?;
    if tick.exponent() <= 0 {
        d(snapped.quantize(tick.exponent()))
    } else {
        Ok(snapped)
    }
}

/// Slip a price by `n` whole ticks adversely:
/// - A buy moves up `n * tick`.
/// - A sell down `n * tick`.
///
/// Slipped prices are snapped to the tick grid (matching `replay-sim.ts::applySlippage`).
/// `n == 0` returns the price untouched, unsnapped, as `applySlippage`'s `if (!ticks) return price`.
pub fn slip_ticks(price: &PyDec, side: Side, n: u32, tick: &PyDec) -> R<PyDec> {
    if n == 0 {
        return Ok(price.clone());
    }
    let delta = d(tick.mul_i128(n as i128))?;
    let raw = match side {
        Side::Buy => d(price.add(&delta))?,
        Side::Sell => d(price.sub(&delta))?,
    };
    round_to_tick(&raw, tick, RoundMode::Nearest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::model::{dec_eq, make_future, Instrument, OrderType, Side, Tif};
    use crate::sim::broker::{Alloc, Bar, Book, VOrder};
    use crate::sim::Ts;

    fn p(s: &str) -> PyDec {
        PyDec::parse(s).unwrap_or_else(|| panic!("failed to parse PyDec: {s}"))
    }

    fn ts(iso: &str) -> Ts {
        Ts::aware(iso, "ts").unwrap()
    }

    #[test]
    fn test_spec_table() {
        // 1. Cross-check supported roots: NQ, MNQ, ES, MES
        let nq = spec_for_root("NQ").unwrap();
        assert_eq!(nq.root, "NQ");
        assert!(dec_eq(&nq.tick_size, &p("0.25")));
        assert!(dec_eq(&nq.point_value, &p("20")));

        let mnq = spec_for_root("MNQ").unwrap();
        assert_eq!(mnq.root, "MNQ");
        assert!(dec_eq(&mnq.tick_size, &p("0.25")));
        assert!(dec_eq(&mnq.point_value, &p("2")));

        let es = spec_for_root("ES").unwrap();
        assert_eq!(es.root, "ES");
        assert!(dec_eq(&es.tick_size, &p("0.25")));
        assert!(dec_eq(&es.point_value, &p("50")));

        let mes = spec_for_root("MES").unwrap();
        assert_eq!(mes.root, "MES");
        assert!(dec_eq(&mes.tick_size, &p("0.25")));
        assert!(dec_eq(&mes.point_value, &p("5")));

        // Case insensitivity
        assert!(spec_for_root("nq").is_ok());
        assert!(spec_for_root("mnq").is_ok());
        assert!(spec_for_root("es").is_ok());
        assert!(spec_for_root("mes").is_ok());

        // 2. Refuse unknown roots (P6C: CL, MCL, RTY, YM, GC are supported now)
        assert_eq!(spec_for_root("ZN").unwrap_err().kind, "unsupported");
        assert_eq!(spec_for_root("6E").unwrap_err().kind, "unsupported");
        assert_eq!(spec_for_root("SPY").unwrap_err().kind, "unsupported");
        assert_eq!(spec_for_root("").unwrap_err().kind, "unsupported");
    }

    /// P6C: the eight new roots, value for value against web/lib/contract-specs.ts L48-57 and CME.
    #[test]
    fn test_spec_table_p6c_roots() {
        let want = [
            ("YM", "1.0", "5"),
            ("MYM", "1.0", "0.5"),
            ("RTY", "0.1", "50"),
            ("M2K", "0.1", "5"),
            ("CL", "0.01", "1000"),
            ("MCL", "0.01", "100"),
            ("GC", "0.1", "100"),
            ("MGC", "0.1", "10"),
        ];
        for (root, tick, pv) in want {
            let s = spec_for_root(root).unwrap();
            assert_eq!(s.root, root);
            assert!(dec_eq(&s.tick_size, &p(tick)), "{root} tick");
            assert!(dec_eq(&s.point_value, &p(pv)), "{root} point value");
            let fc = parse_future_symbol(&format!("{root}Z26")).unwrap();
            assert_eq!(fc.root, root);
            assert_eq!(fc.contract_month, Some(ContractMonth { year: 2026, month: 12 }));
            assert_eq!(parse_future_symbol(&format!("/{}", root.to_lowercase())).unwrap().root, root);
        }
        // M-prefixed micros are not swallowed by their minis, and vice versa.
        assert_eq!(parse_future_symbol("MCLH27").unwrap().root, "MCL");
        assert_eq!(parse_future_symbol("CLH27").unwrap().root, "CL");
        assert_eq!(parse_future_symbol("MGCG27").unwrap().root, "MGC");
        assert_eq!(parse_future_symbol("GCG27").unwrap().root, "GC");
        assert_eq!(parse_future_symbol("MYMM27").unwrap().root, "MYM");
        assert_eq!(parse_future_symbol("YMM27").unwrap().root, "YM");
    }

    /// Every spec root has a Globex calendar, and every calendar root has a spec.
    #[test]
    fn test_spec_roots_equal_calendar_roots() {
        let mut a: Vec<&str> = FUTURE_SPECS.iter().map(|s| s.root).collect();
        let mut b = crate::calendar::globex::supported_roots();
        a.sort();
        b.sort();
        assert_eq!(a, b);
    }

    #[test]
    fn test_symbol_parsing_valid() {
        // Continuous roots
        let nq = parse_future_symbol("NQ").unwrap();
        assert_eq!(nq.root, "NQ");
        assert_eq!(nq.contract_month, None);
        assert_eq!(nq.symbol(), "NQ");

        let mnq = parse_future_symbol("MNQ").unwrap();
        assert_eq!(mnq.root, "MNQ");
        assert_eq!(mnq.contract_month, None);
        assert_eq!(mnq.symbol(), "MNQ");

        // Leading slash
        let es = parse_future_symbol("/ES").unwrap();
        assert_eq!(es.root, "ES");
        assert_eq!(es.contract_month, None);
        assert_eq!(es.symbol(), "ES");

        let mes = parse_future_symbol("/MES").unwrap();
        assert_eq!(mes.root, "MES");
        assert_eq!(mes.contract_month, None);
        assert_eq!(mes.symbol(), "MES");

        // Lowercase and whitespace
        let es_ws = parse_future_symbol("  /es  ").unwrap();
        assert_eq!(es_ws.root, "ES");
        assert_eq!(es_ws.symbol(), "ES");

        // Month-coded symbols: NQZ26
        let nqz26 = parse_future_symbol("NQZ26").unwrap();
        assert_eq!(nqz26.root, "NQ");
        assert_eq!(nqz26.contract_month, Some(ContractMonth { year: 2026, month: 12 }));
        assert_eq!(nqz26.symbol(), "NQZ26");

        let mnqz26 = parse_future_symbol("MNQZ26").unwrap();
        assert_eq!(mnqz26.root, "MNQ");
        assert_eq!(mnqz26.contract_month, Some(ContractMonth { year: 2026, month: 12 }));
        assert_eq!(mnqz26.symbol(), "MNQZ26");

        let es_slash = parse_future_symbol("/ESZ26").unwrap();
        assert_eq!(es_slash.root, "ES");
        assert_eq!(es_slash.contract_month, Some(ContractMonth { year: 2026, month: 12 }));

        // All 12 month codes (F G H J K M N Q U V X Z)
        let codes = [
            ('F', 1), ('G', 2), ('H', 3), ('J', 4), ('K', 5), ('M', 6),
            ('N', 7), ('Q', 8), ('U', 9), ('V', 10), ('X', 11), ('Z', 12)
        ];
        for (code, m) in codes {
            let sym = format!("NQ{code}26");
            let c = parse_future_symbol(&sym).unwrap();
            assert_eq!(c.contract_month, Some(ContractMonth { year: 2026, month: m }));
            assert_eq!(c.symbol(), sym);
        }

        // 1-digit, 2-digit, 4-digit years
        let nqz6 = parse_future_symbol("NQZ6").unwrap();
        assert_eq!(nqz6.contract_month, Some(ContractMonth { year: 2026, month: 12 }));

        let nqz2026 = parse_future_symbol("NQZ2026").unwrap();
        assert_eq!(nqz2026.contract_month, Some(ContractMonth { year: 2026, month: 12 }));

        // POSIX 2-digit year pivot: 00..=68 -> 2000..=2068, 69..=99 -> 1969..=1999
        let nqz68 = parse_future_symbol("NQZ68").unwrap();
        assert_eq!(nqz68.contract_month, Some(ContractMonth { year: 2068, month: 12 }));

        let nqz69 = parse_future_symbol("NQZ69").unwrap();
        assert_eq!(nqz69.contract_month, Some(ContractMonth { year: 1969, month: 12 }));

        // Via make_future and parse_future
        let instr = make_future("NQZ26").unwrap();
        assert_eq!(instr.symbol().unwrap(), "NQZ26");
        assert_eq!(instr.tag(), "Future");
    }

    #[test]
    fn test_symbol_parsing_malformed() {
        // Empty / whitespace
        assert_eq!(parse_future_symbol("").unwrap_err().msg, "Futures symbol must be a non-empty string");
        assert_eq!(parse_future_symbol("   ").unwrap_err().msg, "Futures symbol must be a non-empty string");

        // Just slash / double slash
        assert!(parse_future_symbol("/").is_err());
        assert!(parse_future_symbol("//ES").is_err());

        // Internal spaces or slashes
        assert!(parse_future_symbol("NQ Z26").is_err());
        assert!(parse_future_symbol("NQ/Z26").is_err());

        // Unknown roots (P6C: CL and MCL are supported now; ZN stands in)
        let zn_err = parse_future_symbol("ZN").unwrap_err();
        assert_eq!(zn_err.kind, "unsupported");
        assert!(zn_err.msg.contains("Unknown futures root: 'ZN'"));

        let zb_err = parse_future_symbol("ZBH27").unwrap_err();
        assert_eq!(zb_err.kind, "unsupported");
        assert!(zb_err.msg.contains("Unknown futures root: 'ZB'"));

        let znz_err = parse_future_symbol("ZNZ26").unwrap_err();
        assert_eq!(znz_err.kind, "unsupported");
        assert!(znz_err.msg.contains("Unknown futures root: 'ZN'"));

        let spy_err = parse_future_symbol("SPY").unwrap_err();
        assert_eq!(spy_err.kind, "unsupported");
        assert!(spy_err.msg.contains("Unknown futures root: 'SPY'"));

        // Invalid month code
        let bad_month = parse_future_symbol("NQA26").unwrap_err();
        assert!(bad_month.msg.contains("Invalid contract month code 'A'"));

        // Missing year
        let no_year = parse_future_symbol("NQZ").unwrap_err();
        assert!(no_year.msg.contains("Missing contract year"));

        // Malformed year
        let non_digit = parse_future_symbol("NQZ26X").unwrap_err();
        assert!(non_digit.msg.contains("expected digits"));

        // Invalid year length (3 digits)
        let len_3 = parse_future_symbol("NQZ202").unwrap_err();
        assert!(len_3.msg.contains("expected 1, 2, or 4 digits"));
    }

    #[test]
    fn test_round_to_tick_multiples() {
        let tick = p("0.25");
        let multiples = ["100.00", "100.25", "100.50", "100.75", "-100.00", "-100.25", "-100.50", "-100.75"];

        for s in multiples {
            let price = p(s);
            let nearest = round_to_tick(&price, &tick, RoundMode::Nearest).unwrap();
            let floor = round_to_tick(&price, &tick, RoundMode::Floor).unwrap();
            let ceil = round_to_tick(&price, &tick, RoundMode::Ceil).unwrap();

            assert_eq!(nearest.to_py_string(), s, "Nearest for exact multiple {s}");
            assert_eq!(floor.to_py_string(), s, "Floor for exact multiple {s}");
            assert_eq!(ceil.to_py_string(), s, "Ceil for exact multiple {s}");
        }
    }

    #[test]
    fn test_round_to_tick_non_multiples_positive() {
        let tick = p("0.25");

        // 100.10: 400.4 ticks
        let p1 = p("100.10");
        assert_eq!(round_to_tick(&p1, &tick, RoundMode::Floor).unwrap().to_py_string(), "100.00");
        assert_eq!(round_to_tick(&p1, &tick, RoundMode::Ceil).unwrap().to_py_string(), "100.25");
        assert_eq!(round_to_tick(&p1, &tick, RoundMode::Nearest).unwrap().to_py_string(), "100.00");

        // 100.20: 400.8 ticks
        let p2 = p("100.20");
        assert_eq!(round_to_tick(&p2, &tick, RoundMode::Floor).unwrap().to_py_string(), "100.00");
        assert_eq!(round_to_tick(&p2, &tick, RoundMode::Ceil).unwrap().to_py_string(), "100.25");
        assert_eq!(round_to_tick(&p2, &tick, RoundMode::Nearest).unwrap().to_py_string(), "100.25");
    }

    #[test]
    fn test_round_to_tick_ties() {
        let tick = p("0.25");

        // 100.125: exact tie at 400.5 ticks
        // Nearest tie rule: round half towards +∞ (matching JS Math.round in snapTick)
        let tie1 = p("100.125");
        assert_eq!(round_to_tick(&tie1, &tick, RoundMode::Floor).unwrap().to_py_string(), "100.00");
        assert_eq!(round_to_tick(&tie1, &tick, RoundMode::Ceil).unwrap().to_py_string(), "100.25");
        assert_eq!(round_to_tick(&tie1, &tick, RoundMode::Nearest).unwrap().to_py_string(), "100.25");

        // 100.375: exact tie at 401.5 ticks
        let tie2 = p("100.375");
        assert_eq!(round_to_tick(&tie2, &tick, RoundMode::Floor).unwrap().to_py_string(), "100.25");
        assert_eq!(round_to_tick(&tie2, &tick, RoundMode::Ceil).unwrap().to_py_string(), "100.50");
        assert_eq!(round_to_tick(&tie2, &tick, RoundMode::Nearest).unwrap().to_py_string(), "100.50");
    }

    #[test]
    fn test_round_to_tick_negative() {
        let tick = p("0.25");

        // -100.10: -400.4 ticks
        let n1 = p("-100.10");
        assert_eq!(round_to_tick(&n1, &tick, RoundMode::Floor).unwrap().to_py_string(), "-100.25");
        assert_eq!(round_to_tick(&n1, &tick, RoundMode::Ceil).unwrap().to_py_string(), "-100.00");
        assert_eq!(round_to_tick(&n1, &tick, RoundMode::Nearest).unwrap().to_py_string(), "-100.00");

        // -100.20: -400.8 ticks
        let n2 = p("-100.20");
        assert_eq!(round_to_tick(&n2, &tick, RoundMode::Floor).unwrap().to_py_string(), "-100.25");
        assert_eq!(round_to_tick(&n2, &tick, RoundMode::Ceil).unwrap().to_py_string(), "-100.00");
        assert_eq!(round_to_tick(&n2, &tick, RoundMode::Nearest).unwrap().to_py_string(), "-100.25");

        // -100.125: exact tie at -400.5 ticks
        // Math.round(-400.5) in JS is -400 (half towards +∞), so -400 / 4 = -100.00
        let tie_neg = p("-100.125");
        assert_eq!(round_to_tick(&tie_neg, &tick, RoundMode::Floor).unwrap().to_py_string(), "-100.25");
        assert_eq!(round_to_tick(&tie_neg, &tick, RoundMode::Ceil).unwrap().to_py_string(), "-100.00");
        assert_eq!(round_to_tick(&tie_neg, &tick, RoundMode::Nearest).unwrap().to_py_string(), "-100.00");

        // -100.375: exact tie at -401.5 ticks
        // Math.round(-401.5) in JS is -401, so -401 / 4 = -100.25
        let tie_neg2 = p("-100.375");
        assert_eq!(round_to_tick(&tie_neg2, &tick, RoundMode::Floor).unwrap().to_py_string(), "-100.50");
        assert_eq!(round_to_tick(&tie_neg2, &tick, RoundMode::Ceil).unwrap().to_py_string(), "-100.25");
        assert_eq!(round_to_tick(&tie_neg2, &tick, RoundMode::Nearest).unwrap().to_py_string(), "-100.25");
    }

    #[test]
    fn test_slip_ticks() {
        let tick = p("0.25");
        let base = p("20000.25");

        // Buy slips up (adverse), Sell slips down (adverse)
        assert_eq!(slip_ticks(&base, Side::Buy, 1, &tick).unwrap().to_py_string(), "20000.50");
        assert_eq!(slip_ticks(&base, Side::Sell, 1, &tick).unwrap().to_py_string(), "20000.00");

        assert_eq!(slip_ticks(&base, Side::Buy, 4, &tick).unwrap().to_py_string(), "20001.25");
        assert_eq!(slip_ticks(&base, Side::Sell, 4, &tick).unwrap().to_py_string(), "19999.25");

        // n = 0 keeps price unchanged
        assert_eq!(slip_ticks(&base, Side::Buy, 0, &tick).unwrap().to_py_string(), "20000.25");
        assert_eq!(slip_ticks(&base, Side::Sell, 0, &tick).unwrap().to_py_string(), "20000.25");

        // Unaligned price gets snapped on slippage, but not when n = 0 (applySlippage returns it as is)
        let unaligned = p("20000.10");
        assert_eq!(slip_ticks(&unaligned, Side::Buy, 0, &tick).unwrap().to_py_string(), "20000.10");
        assert_eq!(slip_ticks(&unaligned, Side::Buy, 1, &tick).unwrap().to_py_string(), "20000.25");
        assert_eq!(slip_ticks(&unaligned, Side::Sell, 1, &tick).unwrap().to_py_string(), "19999.75");
    }

    #[test]
    fn test_refusals_at_each_site() {
        let future_contract = parse_future_symbol("NQZ26").unwrap();
        let future_instr = Instrument::Future(future_contract.clone());

        // 1. Ledger codec bridge: enc_instrument refuses Future
        let codec_err = crate::ledger::codec::enc_instrument(&future_instr).unwrap_err();
        assert_eq!(codec_err.kind, "unsupported");
        assert!(codec_err.msg.contains("futures not supported in ledger codec"));

        // 2. Canon: canon_contracts refuses Future via enc_instrument
        let mut pos_map = crate::ledger::ops::OMap::new();
        pos_map.insert(future_instr.hk(), future_instr.clone(), p("1"));
        let canon_err = crate::ledger::canon::canon_contracts(&pos_map).unwrap_err();
        assert_eq!(canon_err.kind, "unsupported");
        assert!(canon_err.msg.contains("futures not supported in ledger codec"));

        // 3. Ledger fold: multiplier refuses Future
        let fold_mult_err = crate::ledger::fold::multiplier(&future_instr).unwrap_err();
        assert_eq!(fold_mult_err.kind, "fold");
        assert_eq!(fold_mult_err.msg, "futures not supported in ledger fold (I6)");

        // 4. Mirror: check_mirror_instrument refuses Future
        let mirror_err = crate::ledger::model::check_mirror_instrument(&future_instr).unwrap_err();
        assert_eq!(mirror_err.kind, "payload");
        assert_eq!(mirror_err.msg, "MirrorQueued.instrument: futures not supported in mirror");

        // 5. Margin: risk_options margin preparation refuses Future
        let mut st_margin = crate::ledger::fold::AccountState::new("A1");
        let pos = crate::ledger::fold::make_position(
            "A1",
            future_instr.clone(),
            p("1"),
            p("20000"),
            p("0"),
            Vec::new(),
        ).unwrap();
        st_margin.positions.insert(future_instr.hk(), future_instr.clone(), pos);
        let margin_err = crate::risk_options::margin_of(&st_margin).unwrap_err();
        assert_eq!(margin_err.kind, "unsupported");
        assert_eq!(margin_err.msg, "futures not supported in margin calculation");

        // 6. OMS: multiplier and validate_quantity refuse Future
        let oms_mult_err = crate::oms::structures::multiplier(&future_instr).unwrap_err();
        assert_eq!(oms_mult_err.kind, "unsupported");
        assert_eq!(oms_mult_err.msg, "futures not supported in OMS multiplier");

        let oms_val_err = crate::oms::manager::validate_quantity(&future_instr, &p("1")).unwrap_err();
        assert_eq!(oms_val_err.kind, "unsupported");
        assert_eq!(oms_val_err.msg, "futures not supported in OMS");

        let oms_alloc_err = crate::oms::manager::allocate_quantity(&p("10"), &[p("0.5"), p("0.5")], &future_instr).unwrap_err();
        assert_eq!(oms_alloc_err.kind, "unsupported");
        assert_eq!(oms_alloc_err.msg, "futures not supported in OMS");

        // 7. sim::broker::Book: Book::validate and Book::process_bar refuse non-equity with existing message
        let mut book = Book::new("A1", true, p("0")).unwrap();
        let mut clock = || Ok("2026-03-02T14:00:00+00:00".to_string());
        book.connect(&mut clock).unwrap();

        let vorder = VOrder {
            id: "o1".into(),
            instr: future_instr.clone(),
            otype: OrderType::Market,
            side: Side::Buy,
            quantity: p("1"),
            submitted_at: ts("2026-03-02T14:00:00+00:00"),
            tif: Tif::Day,
            limit: None,
            stop: None,
            trail: None,
            allocs: vec![Alloc { soid: "o1-s".into(), account: "A1".into(), qty: p("1") }],
            parent: None,
            oco: None,
        };
        let book_order_err = book.submit(vorder, &mut clock).unwrap_err();
        assert_eq!(book_order_err.kind, "value");
        assert_eq!(book_order_err.msg, "SimBroker accepts equity orders only");

        let bar = Bar {
            instr: future_instr.clone(),
            ts: ts("2026-03-02T14:30:00+00:00"),
            open: p("20000"),
            high: p("20010"),
            low: p("19990"),
            close: p("20005"),
            volume: p("100"),
            as_of: ts("2026-03-02T14:30:00+00:00"),
        };
        let book_bar_err = book.process_bar(Some(bar)).unwrap_err();
        assert_eq!(book_bar_err.kind, "value");
        assert_eq!(book_bar_err.msg, "SimBroker supports equities only");

        // 8. sim::snapshot: underlying_of and model_price refuse Future
        let snap_und_err = crate::sim::snapshot::underlying_of(&future_instr).unwrap_err();
        assert_eq!(snap_und_err.kind, "unsupported");
        assert_eq!(snap_und_err.msg, "futures not supported in snapshot venue");

        let snap_venue = crate::sim::snapshot::Venue::new(
            "A1",
            Some(p("0")),
            Some(p("0")),
            Some(p("0")),
            Some(60.0),
        ).unwrap();
        let mut get_quote = |_occ: &str| Ok(None);
        let mut snap = crate::sim::snapshot::Snap {
            as_of: ts("2026-03-02T14:30:00+00:00"),
            underlying: "NQ".into(),
            underlying_price: p("20000"),
            quote: &mut get_quote,
        };
        let snap_model_err = snap_venue.model_price(&future_instr, Side::Buy, &mut snap).unwrap_err();
        assert_eq!(snap_model_err.kind, "unsupported");
        assert_eq!(snap_model_err.msg, "futures not supported in snapshot venue");
    }
}
