//! The RiskGuard pre-check's pure decision rules (W5 P1), ported from
//! `src/trade_engine/oms/precheck.py`. The Python module keeps the I/O and the
//! orchestration: `PrecheckConfig`, `RiskGuardClient`'s HTTP, `Precheck`'s send callbacks,
//! the first-refusal loop and the `_working_orders` ledger scan.
//!
//! Money crosses as `Money`. A refusal of its arithmetic is an `R` error, never a rounded
//! or saturated answer. No I/O here.

use std::cmp::{max, min};
use std::collections::HashMap;

use crate::ledger::json::{parse, Json};
use crate::ledger::model::{derr, err, Instrument, Side, R};
use crate::money::Money;

/// The instrument root RiskGuard's schema takes (`common.schema.json` `instrumentRoot`).
/// `root` checks by hand against this shape; the parity test compares it to the schema.
pub const ROOT_PATTERN: &str = "^[A-Z][A-Z0-9]{0,3}$";

/// `common.schema.json` `ReasonCode`.
pub const REASON_CODES: &[&str] = &[
    "OK",
    "OK_REDUCING",
    "VERDICT_STALE",
    "ACCOUNT_BLOCKED",
    "BLOCK_ADD",
    "INSTRUMENT_BLOCKED",
    "MAX_QTY",
    "COPY_OPENING_REFUSED",
    "ATW_INITIAL_CAP",
    "ATW_RISK_GROWS",
    "ATW_TOO_CLOSE",
    "FEED_QUIET",
];

/// `check.schema.json` `singleResponse`: every key it may carry, and the ones it must.
pub const RESPONSE_KEYS: &[&str] =
    &["allowed", "isReducing", "reasonCode", "detail", "ruleId", "verdictRevision", "checkedAt"];
pub const REQUIRED_RESPONSE_KEYS: &[&str] = &["allowed", "isReducing", "reasonCode"];

/// The service-owned refusal `text` (REFUSAL-TEXT-1, `verdict.schema.json` `refusalText`).
/// The service sends it on `/v1/check` although `check.schema.json` does not list it yet, so
/// it is accepted beside `RESPONSE_KEYS`, strictly shaped, and never read.
pub const TEXT_KEY: &str = "text";
pub const TEXT_SCOPES: &[&str] = &["account", "fleet", "service", "surface"];

/// `refusalText`: exactly `headline` (non-empty), `scope` (one of `TEXT_SCOPES`) and `clears`.
fn valid_refusal_text(value: &Json) -> bool {
    let Json::Obj(pairs) = value else { return false };
    let mut seen: Vec<&str> = Vec::new();
    for (key, _) in pairs {
        if !["headline", "scope", "clears"].contains(&key.as_str()) || seen.contains(&key.as_str()) {
            return false;
        }
        seen.push(key);
    }
    seen.len() == 3
        && matches!(value.get("headline"), Some(Json::Str(text)) if !text.is_empty())
        && matches!(value.get("scope"), Some(Json::Str(scope)) if TEXT_SCOPES.contains(&scope.as_str()))
        && matches!(value.get("clears"), Some(Json::Str(_)))
}

const NOT_LOOPBACK: &str = "riskguard.precheck.baseUrl must be a loopback http URL";
const NO_PORT: &str = "riskguard.precheck.baseUrl needs an explicit port and no path";
const BAD_PORT: &str = "riskguard.precheck.baseUrl is not a URL: bad port";

/// The opening and reducing parts of one venue order, split per allocation.
///
/// Each `usize` is the index into the `allocs` the split was given. `openings` holds only
/// the accounts with an opening part, in first-appearance order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AllocSplit {
    pub reducing: Vec<(usize, Money)>,
    pub opening: Vec<(usize, Money)>,
    pub openings: Vec<(String, Money)>,
}

/// `(reducing, opening)`. A BUY closes a short first; a SELL closes a long first.
pub fn split_quantity(q: &Money, side: Side, position: &Money) -> R<(Money, Money)> {
    let base = if side == Side::Buy { position.neg().map_err(derr)? } else { *position };
    let opposite = max(base, Money::ZERO);
    let reducing = min(*q, opposite);
    let opening = q.sub(&reducing).map_err(derr)?;
    Ok((reducing, opening))
}

/// Split a venue order's allocations into the reducing and the opening parts, per account.
///
/// Each account's quantity is the sum of its allocations (all share the order's side),
/// classified against that account's own position (an account missing from `position_of`
/// counts as flat). Inside one account the reducing quantity goes to its allocations in
/// order, first come first served, cutting one in two where the boundary falls.
pub fn split_allocations(
    side: Side,
    allocs: &[(String, Money)],
    position_of: &HashMap<String, Money>,
) -> R<AllocSplit> {
    let mut accounts: Vec<&String> = Vec::new();
    let mut totals: HashMap<&String, Money> = HashMap::new();
    for (account, quantity) in allocs {
        match totals.get_mut(account) {
            Some(total) => *total = total.add(quantity).map_err(derr)?,
            None => {
                accounts.push(account);
                totals.insert(account, *quantity);
            }
        }
    }

    let mut reducing_left: HashMap<&String, Money> = HashMap::new();
    let mut openings: Vec<(String, Money)> = Vec::new();
    for account in accounts {
        let position = position_of.get(account).copied().unwrap_or(Money::ZERO);
        let (reducing, opening) = split_quantity(&totals[account], side, &position)?;
        reducing_left.insert(account, reducing);
        if opening > Money::ZERO {
            openings.push((account.clone(), opening));
        }
    }

    let mut out = AllocSplit { reducing: Vec::new(), opening: Vec::new(), openings };
    for (index, (account, quantity)) in allocs.iter().enumerate() {
        let left = reducing_left[account];
        let take = min(*quantity, left);
        reducing_left.insert(account, left.sub(&take).map_err(derr)?);
        if take > Money::ZERO {
            out.reducing.push((index, take));
        }
        if *quantity > take {
            out.opening.push((index, quantity.sub(&take).map_err(derr)?));
        }
    }
    Ok(out)
}

/// The instrument text RiskGuard's schema takes, or `None` when it has none: a futures
/// ROOT (`NQ`), an equity's symbol, an option's underlying (F12: the OCC symbol always
/// failed the pattern). A combo has no root. The result must match [`ROOT_PATTERN`].
pub fn root(instr: &Instrument) -> Option<String> {
    let text = match instr {
        Instrument::Future(contract) => contract.root.clone(),
        Instrument::Equity(symbol) => symbol.clone(),
        Instrument::Option(contract) => contract.underlying.clone(),
        Instrument::Combo(_) => return None,
    };
    is_root(&text).then_some(text)
}

/// `^[A-Z][A-Z0-9]{0,3}$`, checked by hand. Every byte is ASCII, so a multi-byte character
/// fails the byte test.
fn is_root(text: &str) -> bool {
    let mut chars = text.chars();
    matches!(chars.next(), Some('A'..='Z'))
        && chars.as_str().len() <= 3
        && chars.as_str().bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
}

/// The hand-written `singleResponse` check (`check.schema.json`): exact keys and types.
/// A body that does not parse, a repeated key (stricter than Python, deliberately), or a
/// `verdictRevision` that is not a non-negative integer, gives `false`.
pub fn valid_response(body: &str) -> bool {
    let Ok(doc) = parse(body) else { return false };
    let Json::Obj(pairs) = &doc else { return false };
    if repeats_top_level_key(body) {
        return false;
    }
    let has = |key: &str| pairs.iter().any(|(k, _)| k == key);
    if !REQUIRED_RESPONSE_KEYS.iter().all(|key| has(key))
        || !pairs.iter().all(|(key, _)| RESPONSE_KEYS.contains(&key.as_str()) || key == TEXT_KEY)
    {
        return false;
    }
    let get = |key: &str| doc.get(key);
    matches!(get("allowed"), Some(Json::Bool(_)))
        && matches!(get("isReducing"), Some(Json::Bool(_)))
        && matches!(get("reasonCode"), Some(Json::Str(code)) if REASON_CODES.contains(&code.as_str()))
        && ["detail", "ruleId", "checkedAt"]
            .iter()
            .all(|key| get(key).is_none_or(|value| matches!(value, Json::Str(_))))
        && match get("verdictRevision") {
            None => true,
            Some(Json::Int(n)) => *n >= 0,
            Some(Json::BigInt(text)) => !text.starts_with('-'),
            Some(_) => false,
        }
        && get(TEXT_KEY).is_none_or(valid_refusal_text)
}

/// True when the top-level object repeats a key. `json::parse` keeps the last value of a
/// repeated key (Python's rule), so the repeat is found here, in the text it read. Called
/// only on text `parse` accepted; the scan never panics on any input.
fn repeats_top_level_key(text: &str) -> bool {
    let b = text.as_bytes();
    let mut i = skip_ws(b, 0);
    if b.get(i) != Some(&b'{') {
        return false;
    }
    i += 1;
    let mut seen: Vec<String> = Vec::new();
    loop {
        i = skip_ws(b, i);
        if b.get(i) != Some(&b'"') {
            return false;
        }
        let end = string_end(b, i);
        let Ok(Json::Str(key)) = parse(&text[i..end]) else { return false };
        if seen.contains(&key) {
            return true;
        }
        seen.push(key);
        i = skip_ws(b, end);
        if b.get(i) != Some(&b':') {
            return false;
        }
        i = skip_ws(b, value_end(b, skip_ws(b, i + 1)));
        match b.get(i) {
            Some(b',') => i += 1,
            _ => return false,
        }
    }
}

fn skip_ws(b: &[u8], mut i: usize) -> usize {
    while matches!(b.get(i), Some(b' ' | b'\t' | b'\n' | b'\r')) {
        i += 1;
    }
    i
}

/// The index just past the string that opens at `i`.
fn string_end(b: &[u8], mut i: usize) -> usize {
    i += 1;
    while let Some(&c) = b.get(i) {
        match c {
            b'\\' => i += 2,
            b'"' => return i + 1,
            _ => i += 1,
        }
    }
    b.len()
}

/// The index just past the value that starts at `i`, at the `,` or `}` that ends it.
fn value_end(b: &[u8], mut i: usize) -> usize {
    let mut depth = 0usize;
    while let Some(&c) = b.get(i) {
        match c {
            b'"' => {
                i = string_end(b, i);
                continue;
            }
            b'{' | b'[' => depth += 1,
            b'}' | b']' if depth == 0 => return i,
            b'}' | b']' => depth -= 1,
            b',' if depth == 0 => return i,
            _ => {}
        }
        i += 1;
    }
    i
}

/// The `riskguard.precheck.baseUrl` the engine may call: `http://127.0.0.1:PORT` or
/// `http://localhost:PORT`, with an explicit port and no path, userinfo, query or fragment.
/// Returns the normalised `http://{host}:{port}` (the scheme and host lowercased).
pub fn loopback_base(url: &str) -> R<String> {
    let Some((scheme, rest)) = url.split_once("://") else { return err("value", NOT_LOOPBACK) };
    if !scheme.eq_ignore_ascii_case("http") || rest.chars().any(|c| matches!(c, '@' | '?' | '#')) {
        return err("value", NOT_LOOPBACK);
    }
    let (authority, path) = match rest.find('/') {
        Some(at) => (&rest[..at], &rest[at..]),
        None => (rest, ""),
    };
    let (host, port) = match authority.split_once(':') {
        Some((host, port)) => (host.to_ascii_lowercase(), port),
        None => (authority.to_ascii_lowercase(), ""),
    };
    if host != "127.0.0.1" && host != "localhost" {
        return err("value", NOT_LOOPBACK);
    }
    if port.is_empty() {
        return err("value", NO_PORT);
    }
    let number = match port.bytes().all(|b| b.is_ascii_digit()).then(|| port.parse::<u32>()) {
        Some(Ok(n)) if (1..=65535).contains(&n) => n,
        _ => return err("value", BAD_PORT),
    };
    if !(path.is_empty() || path == "/") {
        return err("value", NO_PORT);
    }
    Ok(format!("http://{host}:{number}"))
}

/// The opening part of a replace, or `None` when it is sent without a check. `new_qty` is
/// the replace's new quantity (`None`: unchanged). A replace is checked when it grows the
/// order or changes its price, and only then for the part that opens.
pub fn replace_opening(
    order_qty: &Money,
    new_qty: Option<Money>,
    side: Side,
    position: &Money,
    price_changed: bool,
) -> R<Option<Money>> {
    let new = new_qty.unwrap_or(*order_qty);
    let grows = new > *order_qty;
    let (_, opening) = split_quantity(&new, side, position)?;
    Ok((grows || price_changed).then_some(opening).filter(|opening| *opening > Money::ZERO))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::model::{ComboLeg, FutureContract, OptionContract};
    use crate::options::Right;
    use chrono::NaiveDate;

    fn m(text: &str) -> Money {
        Money::parse(text).unwrap_or_else(|| panic!("not a Money: {text}"))
    }

    fn map(pairs: &[(&str, &str)]) -> HashMap<String, Money> {
        pairs.iter().map(|(account, p)| (account.to_string(), m(p))).collect()
    }

    fn allocs(pairs: &[(&str, &str)]) -> Vec<(String, Money)> {
        pairs.iter().map(|(account, q)| (account.to_string(), m(q))).collect()
    }

    fn option(underlying: &str) -> Instrument {
        Instrument::Option(OptionContract {
            underlying: underlying.to_string(),
            expiry: NaiveDate::from_ymd_opt(2026, 12, 18).unwrap(),
            strike: m("500"),
            right: Right::Call,
            multiplier: 100,
        })
    }

    #[test]
    fn split_quantity_closes_the_opposite_side_first() {
        let cases = [
            ("5", Side::Sell, "1", ("1", "4")),
            ("2", Side::Buy, "0", ("0", "2")),
            ("1", Side::Sell, "3", ("1", "0")),
            ("3", Side::Buy, "-5", ("3", "0")),
            ("4", Side::Sell, "-2", ("0", "4")),
            ("4", Side::Buy, "-2", ("2", "2")),
            ("2.5", Side::Sell, "0.5", ("0.5", "2")),
        ];
        for (q, side, position, (reducing, opening)) in cases {
            assert_eq!(
                split_quantity(&m(q), side, &m(position)).unwrap(),
                (m(reducing), m(opening)),
                "q={q} side={side:?} position={position}"
            );
        }
    }

    #[test]
    fn split_allocations_sums_per_account_and_cuts_at_the_boundary() {
        // A: long 3, sells 5 -> 3 reducing, 2 opening. B: flat, buys 2 -> all opening.
        let split = split_allocations(
            Side::Sell,
            &allocs(&[("A", "2"), ("B", "1"), ("A", "3")]),
            &map(&[("A", "3")]),
        )
        .unwrap();
        // A's reducing (3) goes first-come: the first allocation takes 2, the third takes 1.
        assert_eq!(split.reducing, vec![(0, m("2")), (2, m("1"))]);
        // Opening parts follow allocation order: B's whole 1 (flat, sells open), then A's 2.
        assert_eq!(split.opening, vec![(1, m("1")), (2, m("2"))]);
        // Openings are per account, first appearance: A (2) then B (1).
        assert_eq!(split.openings, vec![("A".to_string(), m("2")), ("B".to_string(), m("1"))]);
    }

    #[test]
    fn split_allocations_lists_openings_in_first_appearance_order_and_treats_missing_as_flat() {
        let split = split_allocations(
            Side::Buy,
            &allocs(&[("B", "1"), ("A", "2"), ("B", "1")]),
            &map(&[("A", "-1")]),
        )
        .unwrap();
        assert_eq!(split.reducing, vec![(1, m("1"))]);
        assert_eq!(
            split.openings,
            vec![("B".to_string(), m("2")), ("A".to_string(), m("1"))]
        );
        assert_eq!(split.opening, vec![(0, m("1")), (1, m("1")), (2, m("1"))]);
    }

    #[test]
    fn split_allocations_of_nothing_is_empty() {
        let split = split_allocations(Side::Buy, &[], &HashMap::new()).unwrap();
        assert_eq!(split, AllocSplit { reducing: vec![], opening: vec![], openings: vec![] });
    }

    #[test]
    fn root_of_each_instrument_kind() {
        assert_eq!(root(&Instrument::Equity("AAPL".into())), Some("AAPL".into()));
        assert_eq!(root(&option("SPY")), Some("SPY".into()));
        let future = Instrument::Future(FutureContract {
            root: "NQ".into(),
            contract_month: None,
            tick_size: m("0.25"),
            point_value: m("20"),
        });
        assert_eq!(root(&future), Some("NQ".into()));
        let combo = Instrument::Combo(vec![ComboLeg { contract: option("SPY"), ratio: 1, side: Side::Buy }]);
        assert_eq!(root(&combo), None);
    }

    #[test]
    fn root_refuses_what_the_pattern_refuses() {
        for text in ["spy", "TOOLONG", "ABCDE", "1AB", "", "S-Y", "SÉ", "A\n"] {
            assert_eq!(root(&Instrument::Equity(text.into())), None, "{text:?}");
        }
        assert_eq!(root(&Instrument::Equity("ABCD".into())), Some("ABCD".into()));
        assert_eq!(root(&Instrument::Equity("A9".into())), Some("A9".into()));
    }

    const ALLOWED: &str = r#"{"allowed":true,"isReducing":false,"reasonCode":"OK"}"#;

    #[test]
    fn valid_response_accepts_the_required_and_optional_keys() {
        assert!(valid_response(ALLOWED));
        assert!(valid_response(
            r#"{"allowed":false,"isReducing":true,"reasonCode":"MAX_QTY","detail":"x","ruleId":"r1","verdictRevision":7,"checkedAt":"2026-11-17T15:00:01.000Z"}"#
        ));
    }

    #[test]
    fn valid_response_verdict_revision_rules() {
        let with = |revision: &str| format!(r#"{{"allowed":true,"isReducing":false,"reasonCode":"OK","verdictRevision":{revision}}}"#);
        assert!(valid_response(&with("0")));
        assert!(valid_response(&with("123456789012345678901234567890123456789012")));
        assert!(!valid_response(&with("1.0")));
        assert!(!valid_response(&with("true")));
        assert!(!valid_response(&with("-1")));
        assert!(!valid_response(&with("-123456789012345678901234567890123456789012")));
        assert!(!valid_response(&with("null")));
        assert!(!valid_response(&with(r#""7""#)));
    }

    #[test]
    fn valid_response_refuses_unknown_and_missing_keys() {
        assert!(!valid_response(r#"{"allowed":true,"isReducing":false,"reasonCode":"OK","extra":1}"#));
        assert!(!valid_response(r#"{"allowed":true,"isReducing":false}"#));
        assert!(!valid_response(r#"{"isReducing":false,"reasonCode":"OK"}"#));
        assert!(!valid_response(r#"{"allowed":true,"isReducing":false,"reasonCode":"MADE_UP"}"#));
    }

    #[test]
    fn valid_response_refuses_wrong_types() {
        assert!(!valid_response(r#"{"allowed":"yes","isReducing":false,"reasonCode":"OK"}"#));
        assert!(!valid_response(r#"{"allowed":true,"isReducing":0,"reasonCode":"OK"}"#));
        assert!(!valid_response(r#"{"allowed":true,"isReducing":false,"reasonCode":"OK","detail":5}"#));
        assert!(!valid_response(r#"{"allowed":true,"isReducing":false,"reasonCode":"OK","checkedAt":null}"#));
        assert!(!valid_response(r#"{"allowed":true,"isReducing":false,"reasonCode":"OK","ruleId":[]}"#));
    }

    #[test]
    fn valid_response_accepts_a_well_shaped_refusal_text_and_nothing_else() {
        let with = |text: &str| {
            format!(r#"{{"allowed":false,"isReducing":false,"reasonCode":"ACCOUNT_BLOCKED","text":{text}}}"#)
        };
        assert!(valid_response(&with(r#"{"headline":"Account not registered.","scope":"account","clears":"an operator clear"}"#)));
        assert!(valid_response(&with(r#"{"clears":"","scope":"fleet","headline":"h"}"#)));
        assert!(!valid_response(&with(r#"{"headline":"","scope":"account","clears":"x"}"#)));
        assert!(!valid_response(&with(r#"{"headline":"h","scope":"desk","clears":"x"}"#)));
        assert!(!valid_response(&with(r#"{"headline":"h","scope":"account"}"#)));
        assert!(!valid_response(&with(r#"{"headline":"h","scope":"account","clears":"x","extra":1}"#)));
        assert!(!valid_response(&with(r#"{"headline":"h","scope":"account","clears":5}"#)));
        assert!(!valid_response(&with(r#""Account not registered.""#)));
        assert!(!valid_response(&with("null")));
    }

    #[test]
    fn valid_response_refuses_a_repeated_key_even_when_both_values_are_fine() {
        assert!(!valid_response(
            r#"{"allowed":true,"allowed":true,"isReducing":false,"reasonCode":"OK"}"#
        ));
        // An escaped spelling of a key is the same key.
        assert!(!valid_response(
            r#"{"allowed":true,"allowed":true,"isReducing":false,"reasonCode":"OK"}"#
        ));
        // A repeat inside a nested value is not a top-level repeat.
        assert!(valid_response(
            r#"{"allowed":true,"isReducing":false,"reasonCode":"OK","detail":"a,\"b\":1"}"#
        ));
    }

    #[test]
    fn valid_response_refuses_what_does_not_parse_or_is_not_an_object() {
        assert!(!valid_response(""));
        assert!(!valid_response("{"));
        assert!(!valid_response("[]"));
        assert!(!valid_response("null"));
        assert!(!valid_response(&format!("{ALLOWED} trailing")));
        assert!(!valid_response(r#"{"allowed":NaN,"isReducing":false,"reasonCode":"OK"}"#));
    }

    #[test]
    fn loopback_base_accepts_and_normalises_a_loopback_url() {
        assert_eq!(loopback_base("http://LOCALHOST:8080/").unwrap(), "http://localhost:8080");
        assert_eq!(loopback_base("HTTP://127.0.0.1:80").unwrap(), "http://127.0.0.1:80");
        assert_eq!(loopback_base("http://127.0.0.1:0080").unwrap(), "http://127.0.0.1:80");
        assert_eq!(loopback_base("http://127.0.0.1:65535").unwrap(), "http://127.0.0.1:65535");
    }

    #[test]
    fn loopback_base_refusals_and_their_messages() {
        let not_loopback = [
            "https://127.0.0.1:9",
            "http://10.0.0.1:1",
            "http://example.com:9",
            "127.0.0.1:9",
            "",
            "http://u@127.0.0.1:1",
            "http://127.0.0.1:1?x",
            "http://127.0.0.1:1#x",
            "http://[::1]:1",
        ];
        for url in not_loopback {
            let e = loopback_base(url).unwrap_err();
            assert_eq!((e.kind, e.msg.as_str()), ("value", NOT_LOOPBACK), "{url}");
        }
        for url in ["http://127.0.0.1", "http://127.0.0.1:", "http://localhost/"] {
            let e = loopback_base(url).unwrap_err();
            assert_eq!((e.kind, e.msg.as_str()), ("value", NO_PORT), "{url}");
        }
        for url in ["http://127.0.0.1:1/x", "http://127.0.0.1:1/x/"] {
            let e = loopback_base(url).unwrap_err();
            assert_eq!((e.kind, e.msg.as_str()), ("value", NO_PORT), "{url}");
        }
        for url in [
            "http://127.0.0.1:99999",
            "http://127.0.0.1:0",
            "http://127.0.0.1:-1",
            "http://127.0.0.1:x",
            "http://127.0.0.1:1:2",
            "http://127.0.0.1:99999999999999999999999",
        ] {
            let e = loopback_base(url).unwrap_err();
            assert_eq!((e.kind, e.msg.as_str()), ("value", BAD_PORT), "{url}");
        }
    }

    #[test]
    fn replace_opening_is_checked_only_when_it_grows_or_reprices_and_opens() {
        let flat = m("0");
        // Grows while flat: all of the new quantity opens.
        assert_eq!(replace_opening(&m("2"), Some(m("5")), Side::Buy, &flat, false).unwrap(), Some(m("5")));
        // Same size, price changed: checked for the whole opening part.
        assert_eq!(replace_opening(&m("2"), None, Side::Buy, &flat, true).unwrap(), Some(m("2")));
        // Shrinks without a price change: sent without a check.
        assert_eq!(replace_opening(&m("5"), Some(m("2")), Side::Buy, &flat, false).unwrap(), None);
        // Grows, but the growth only reduces a short: nothing opens.
        assert_eq!(replace_opening(&m("2"), Some(m("3")), Side::Buy, &m("-3"), false).unwrap(), None);
        // Grows, and the part beyond the short opens.
        assert_eq!(replace_opening(&m("2"), Some(m("5")), Side::Buy, &m("-3"), false).unwrap(), Some(m("2")));
        // Price changed but nothing opens: sent without a check.
        assert_eq!(replace_opening(&m("2"), None, Side::Sell, &m("2"), true).unwrap(), None);
        // A sell against a long of 3 with a new quantity of 1 opens nothing.
        assert_eq!(replace_opening(&m("1"), None, Side::Sell, &m("3"), true).unwrap(), None);
    }

    #[test]
    fn split_quantity_matches_the_replace_opening_on_the_same_inputs() {
        let (_, opening) = split_quantity(&m("5"), Side::Buy, &m("-3")).unwrap();
        assert_eq!(opening, m("2"));
        assert_eq!(
            replace_opening(&m("1"), Some(m("5")), Side::Buy, &m("-3"), false).unwrap(),
            Some(opening)
        );
    }
}

/// The wire constants against the contract schemas. Runs when `RISKGUARD_REPO` names a
/// checkout that holds `contracts/schemas`; otherwise it returns without asserting.
#[cfg(test)]
mod schema_parity {
    use super::*;
    use crate::ledger::json::Json;

    fn schema(name: &str) -> Option<Json> {
        let repo = std::env::var("RISKGUARD_REPO").ok().filter(|r| !r.is_empty())?;
        let path = std::path::Path::new(&repo).join("contracts").join("schemas").join(name);
        parse(&std::fs::read_to_string(path).ok()?).ok()
    }

    fn text_of(node: Option<&Json>) -> Option<String> {
        match node {
            Some(Json::Str(text)) => Some(text.clone()),
            _ => None,
        }
    }

    fn strings(node: Option<&Json>) -> Vec<String> {
        match node {
            Some(Json::Arr(items)) => items.iter().filter_map(|item| text_of(Some(item))).collect(),
            _ => Vec::new(),
        }
    }

    fn sorted(mut values: Vec<String>) -> Vec<String> {
        values.sort();
        values
    }

    fn owned(values: &[&str]) -> Vec<String> {
        sorted(values.iter().map(|value| value.to_string()).collect())
    }

    #[test]
    fn wire_constants_match_the_contract_schemas() {
        let (Some(check), Some(common)) = (schema("check.schema.json"), schema("common.schema.json")) else {
            return;
        };
        let check_defs = check.get("$defs").expect("check.schema.json has $defs");
        let common_defs = common.get("$defs").expect("common.schema.json has $defs");
        let single = check_defs.get("singleResponse").expect("check.schema.json has singleResponse");

        let reason_codes = strings(common_defs.get("ReasonCode").and_then(|d| d.get("enum")));
        assert_eq!(sorted(reason_codes), owned(REASON_CODES));

        let properties = match single.get("properties") {
            // `text` is accepted beside the schema (see `TEXT_KEY`), listed there or not.
            Some(Json::Obj(pairs)) => {
                sorted(pairs.iter().filter(|(key, _)| key != TEXT_KEY).map(|(key, _)| key.clone()).collect())
            }
            _ => Vec::new(),
        };
        assert_eq!(properties, owned(RESPONSE_KEYS));
        assert_eq!(sorted(strings(single.get("required"))), owned(REQUIRED_RESPONSE_KEYS));

        let pattern = common_defs.get("instrumentRoot").and_then(|d| d.get("pattern"));
        assert_eq!(text_of(pattern).as_deref(), Some(ROOT_PATTERN));
    }
}
