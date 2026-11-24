//! Runtime decisions only. All observations and effects belong to the host.
pub mod eod;
pub mod eod_flow;
pub mod intraday;
pub mod intraday_flow;
pub mod routing;
pub mod lifecycle;
pub mod journal;

use crate::ledger::model::{err, R};

/// Python's `str.strip()` with no argument: Rust's Unicode whitespace plus U+001C..U+001F.
pub(crate) fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

#[derive(Default)]
pub struct Plan {
    pub text: Vec<String>,
    pub numbers: Vec<i64>,
    pub flags: Vec<bool>,
}
pub fn decide(op: &str, s: &[String], n: &[i64], b: &[bool], f: &[f64]) -> R<Plan> {
    match op.split_once(':') {
        Some(("eod", op)) => eod::decide(op, s, n, b, f),
        Some(("routing", op)) => routing::decide(op, s, n, b),
        Some(("intraday", op)) => intraday::decide(op, s, n, b, f),
        Some(("lifecycle", op)) => lifecycle::decide(op, s, n, b, f),
        _ => err("value", format!("Unknown runtime decision {op}")),
    }
}
