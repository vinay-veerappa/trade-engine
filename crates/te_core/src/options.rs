//! Option rules (docs/RUST_PORT.md P1e): contract validation and OCC symbology
//! (was `domain/instruments.py`), the option-roots table and the settlement
//! calendar (was `domain/option_roots.py`), and what an option becomes at expiry
//! (was `domain/option_lifecycle.py`, in `lifecycle`).
//!
//! The Python dataclasses stay the data carriers; they hand this module plain values
//! (a `Decimal` crosses as sign, digits, exponent and its own `str`, so every message
//! can quote it the way Python did) and rebuild the answer. Behaviour is the pre-port
//! Python's EXACTLY, defects included: refusal type (`kind`) and message are the
//! contract, and `tests/test_option_rules_parity.py` checks both against the frozen
//! pre-port modules. Where a defect is preserved, a comment says so.
//!
//! The engine owns the calendar (D7): settlement uses `crate::calendar`.

pub mod lifecycle;
pub mod pyrules;

use std::fmt;

use crate::calendar::{self, CalendarError};
use chrono::NaiveDate;
use pyrules::{digit, is_space, strip};

/// A refusal. `Value` is a `ValueError`, `Unresolvable` an
/// `UnresolvableInstrumentError`; the other two stand in for the `decimal` module's
/// own signals, which the pre-port arithmetic could raise.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OptionError {
    Value(String),
    Unresolvable(String),
    /// `decimal.InvalidOperation`: ordering a NaN.
    InvalidOperation,
    /// `decimal.Overflow`: a strike whose thousandths exceed the Decimal exponent range.
    Overflow,
}

impl OptionError {
    pub fn kind(&self) -> &'static str {
        match self {
            OptionError::Value(_) => "value",
            OptionError::Unresolvable(_) => "unresolvable",
            OptionError::InvalidOperation => "invalid_operation",
            OptionError::Overflow => "overflow",
        }
    }
    pub fn message(&self) -> &str {
        match self {
            OptionError::Value(m) | OptionError::Unresolvable(m) => m,
            OptionError::InvalidOperation | OptionError::Overflow => "",
        }
    }
}

impl fmt::Display for OptionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.message())
    }
}

impl std::error::Error for OptionError {}

pub type Result<T> = std::result::Result<T, OptionError>;

pub(crate) fn value<T>(msg: impl Into<String>) -> Result<T> {
    Err(OptionError::Value(msg.into()))
}

impl From<CalendarError> for OptionError {
    fn from(e: CalendarError) -> Self {
        OptionError::Value(e.to_string())
    }
}

// --- Decimal wire ------------------------------------------------------------------

/// What `Decimal.as_tuple()` says about a non-finite value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Special {
    Finite,
    Inf,
    QuietNan,
    SignalingNan,
}

impl Special {
    /// The `as_tuple().exponent` code of a non-finite Decimal: "F", "n", "N"; "" is finite.
    pub fn from_code(code: &str) -> Option<Special> {
        match code {
            "" => Some(Special::Finite),
            "F" => Some(Special::Inf),
            "n" => Some(Special::QuietNan),
            "N" => Some(Special::SignalingNan),
            _ => None,
        }
    }
}

/// A `Decimal` as `as_tuple()` plus `str(d)` (quoted verbatim in refusals).
#[derive(Debug, Clone)]
pub struct DecWire {
    pub neg: bool,
    /// The coefficient digits, most significant first ("0" for zero).
    pub digits: String,
    pub exp: i64,
    pub special: Special,
    pub text: String,
}

impl DecWire {
    pub fn is_finite(&self) -> bool {
        self.special == Special::Finite
    }

    pub(crate) fn coefficient(&self) -> &str {
        let t = self.digits.trim_start_matches('0');
        if t.is_empty() {
            "0"
        } else {
            t
        }
    }

    pub(crate) fn is_zero(&self) -> bool {
        self.coefficient() == "0"
    }
}

// --- Strike rules, with the 28-digit Decimal context the Python ran under -------------

const PREC: i64 = 28;
const EMAX: i64 = 999_999;
const ETINY: i64 = -999_999 - PREC + 1;
const ETOP: i64 = EMAX - PREC + 1;
const MAX_MILLIS: u64 = 99_999_999;

/// Drop the last `drop` digits of a coefficient (no leading zeros), rounding half to even.
fn round_half_even(digits: &str, drop: usize) -> String {
    if drop == 0 {
        return digits.to_string();
    }
    let len = digits.len();
    if drop > len {
        return "0".to_string(); // below a tenth of the last place kept
    }
    let (kept, rest) = digits.split_at(len - drop);
    let rest = rest.as_bytes();
    let up = match rest[0].cmp(&b'5') {
        std::cmp::Ordering::Greater => true,
        std::cmp::Ordering::Less => false,
        std::cmp::Ordering::Equal => {
            rest[1..].iter().any(|&b| b != b'0') || kept.as_bytes().last().is_some_and(|&b| (b - b'0') % 2 == 1)
        }
    };
    let mut out: Vec<u8> = kept.bytes().collect();
    if up {
        let mut i = out.len();
        loop {
            if i == 0 {
                out.insert(0, b'1');
                break;
            }
            i -= 1;
            if out[i] == b'9' {
                out[i] = b'0';
            } else {
                out[i] += 1;
                break;
            }
        }
    }
    if out.is_empty() {
        return "0".to_string();
    }
    String::from_utf8(out).expect("ascii digits")
}

/// `strike * Decimal("1000")` under the default context (28 digits, half-even, Emax
/// 999999): the product's coefficient digits and exponent. A strike with more than 28
/// significant digits is rounded here, which is how the Python let one slip past its
/// own 3-decimal check (preserved).
fn times_1000(strike: &DecWire) -> Result<(String, i64)> {
    if strike.is_zero() {
        return Ok(("0".to_string(), strike.exp));
    }
    let mut digits = format!("{}000", strike.coefficient());
    let mut exp = strike.exp;
    let len = digits.len() as i64;
    let mut exp_min = len + exp - PREC;
    if exp_min > ETOP {
        return Err(OptionError::Overflow);
    }
    if exp_min < ETINY {
        exp_min = ETINY;
    }
    if exp < exp_min {
        digits = round_half_even(&digits, (exp_min - exp) as usize);
        exp = exp_min;
        if digits.len() as i64 > PREC {
            digits.pop(); // 999..9 rounded up to 1000..0
            exp += 1;
        }
    }
    if digits != "0" && digits.len() as i64 + exp - 1 > EMAX {
        return Err(OptionError::Overflow);
    }
    Ok((digits, exp))
}

/// `int(x.to_integral_value())` for a product `digits * 10^exp`, as a digit string.
fn integral_value(digits: &str, exp: i64) -> String {
    if exp >= 0 {
        if digits == "0" {
            return "0".to_string();
        }
        // Past 40 digits is far beyond any bound; do not build a huge string.
        let zeros = exp.min(40) as usize;
        return format!("{digits}{}", "0".repeat(zeros));
    }
    round_half_even(digits, (-exp).min(i64::from(u32::MAX)) as usize)
}

/// Whether `digits * 10^exp` has no fractional part.
fn is_integral(digits: &str, exp: i64) -> bool {
    if exp >= 0 || digits == "0" {
        return true;
    }
    let drop = -exp;
    drop <= digits.len() as i64 && digits[digits.len() - drop as usize..].bytes().all(|b| b == b'0')
}

/// The strike in thousandths, `int((strike * 1000).to_integral_value())`, refused when
/// it is not in 1..=99999999.
fn strike_millis(strike: &DecWire) -> Result<u64> {
    let (digits, exp) = times_1000(strike)?;
    let whole = integral_value(&digits, exp);
    let oob = || value(format!("Strike {} out of bounds for OCC representation", strike.text));
    if whole.len() > 18 {
        return oob();
    }
    let millis: u64 = whole.parse().expect("ascii digits");
    if millis == 0 || millis > MAX_MILLIS {
        return oob();
    }
    Ok(millis)
}

fn validate_strike(strike: &DecWire) -> Result<()> {
    let positive = strike.is_finite() && !strike.neg && !strike.is_zero();
    if !positive {
        return value(format!("Strike must be positive and finite, got {}", strike.text));
    }
    let (digits, exp) = times_1000(strike)?;
    if !is_integral(&digits, exp) {
        return value(format!(
            "Strike cannot have more than 3 decimal places (thousandths), got {} (I5)",
            strike.text
        ));
    }
    strike_millis(strike).map(|_| ())
}

// --- Rights --------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Right {
    Call,
    Put,
}

impl Right {
    pub fn code(self) -> &'static str {
        match self {
            Right::Call => "C",
            Right::Put => "P",
        }
    }

    pub fn from_code(code: &str) -> Option<Right> {
        match code {
            "C" => Some(Right::Call),
            "P" => Some(Right::Put),
            _ => None,
        }
    }
}

// --- Equity and option contract validation -----------------------------------------

/// `Equity.__init__`: the normalised symbol. `symbol` is `None` for a non-string.
pub fn equity_symbol(symbol: Option<&str>) -> Result<String> {
    let Some(raw) = symbol.filter(|s| !s.is_empty()) else {
        return value("Equity symbol must be non-empty string");
    };
    let sym = strip(raw).to_uppercase();
    if sym.is_empty() {
        return value("Equity symbol must be non-empty");
    }
    if sym.chars().count() > 10 {
        return value(format!("Equity symbol exceeds maximum length of 10 characters: '{sym}'"));
    }
    // [A-Z0-9]+(\.[A-Z]{1,2})?  -- a root, then optionally one dot and a 1-2 letter class
    let alnum = |c: char| c.is_ascii_uppercase() || c.is_ascii_digit();
    let ok = match sym.split_once('.') {
        None => sym.chars().all(alnum),
        Some((root, class)) => {
            !root.is_empty()
                && root.chars().all(alnum)
                && (1..=2).contains(&class.chars().count())
                && class.chars().all(|c| c.is_ascii_uppercase())
        }
    };
    if !ok {
        return value(format!("Equity symbol must be alphanumeric without slashes or spaces, got '{sym}'"));
    }
    Ok(sym)
}

/// The underlying part of `OptionContract.__post_init__`: the normalised root.
pub fn validate_underlying(underlying: &str) -> Result<String> {
    let und = strip(underlying).to_uppercase();
    if und.is_empty() {
        return value("Option underlying must be non-empty");
    }
    let n = und.chars().count();
    if n > 6 {
        return value(format!("Option underlying must be at most 6 characters, got '{und}' (length {n})"));
    }
    if !und.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit()) {
        return value(format!("Option underlying must be alphanumeric, got '{und}'"));
    }
    Ok(und)
}

/// The rest of `OptionContract.__post_init__`, in its order: strike, multiplier,
/// right. `multiplier` is the decimal text of an `int` (`None` for any other type, which
/// the pre-port code let through and every consumer then refused or mishandled; refused
/// here, a deliberate narrowing); `right` is the str the caller holds (`None` if it is
/// not one). Returns the coerced right.
pub fn validate_rest(
    strike: &DecWire,
    multiplier: Option<&str>,
    multiplier_text: &str,
    right: Option<&str>,
    right_text: &str,
) -> Result<Right> {
    validate_strike(strike)?;
    match multiplier {
        None => return value(format!("Multiplier must be an integer, got {multiplier_text}")),
        Some(m) if m.starts_with('-') || m == "0" => {
            return value(format!("Multiplier must be positive, got {multiplier_text}"))
        }
        Some(_) => {}
    }
    match right.map(str::to_uppercase).as_deref() {
        Some("C" | "CALL") => Ok(Right::Call),
        Some("P" | "PUT") => Ok(Right::Put),
        _ => value(format!("Invalid option right: {right_text}")),
    }
}

/// A contract as the shims send it.
#[derive(Debug, Clone)]
pub struct ContractWire {
    pub underlying: String,
    pub year: i32,
    pub month: u32,
    pub day: u32,
    pub right: Right,
    pub strike: DecWire,
}

impl ContractWire {
    pub fn expiry(&self) -> Result<NaiveDate> {
        match NaiveDate::from_ymd_opt(self.year, self.month, self.day) {
            Some(d) => Ok(d),
            None => value(format!("expiry {}-{}-{} is not a date", self.year, self.month, self.day)),
        }
    }

    /// `str(contract.expiry)` for a `datetime.date`.
    pub fn expiry_text(&self) -> String {
        format!("{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }

    /// The canonical 21-character OCC symbol: root padded to 6, YYMMDD, C or P, strike
    /// in thousandths to 8 digits.
    pub fn occ(&self) -> Result<String> {
        let millis = strike_millis(&self.strike)?;
        let pad = 6usize.saturating_sub(self.underlying.chars().count());
        Ok(format!(
            "{}{}{:02}{:02}{:02}{}{:08}",
            self.underlying,
            " ".repeat(pad),
            self.year.rem_euclid(100),
            self.month,
            self.day,
            self.right.code(),
            millis
        ))
    }
}

// --- OCC parsing -------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedOcc {
    pub root: String,
    pub year: i32,
    pub month: u32,
    pub day: u32,
    pub right: Right,
    /// The strike as `Decimal(thousandths) / Decimal(1000)` prints: 150, 1.5, 0.001.
    pub strike_text: String,
}

/// `^([A-Za-z0-9]{1,6})\s*(\d{6})([CPcp])(\d{8})$` on stripped text, as a backtracking
/// search on the root length (longest first). Returns (root_len, date_start).
fn match_occ(chars: &[char]) -> Option<(usize, usize)> {
    let max_root = chars.iter().take(6).take_while(|c| c.is_ascii_alphanumeric()).count();
    for root_len in (1..=max_root).rev() {
        let mut i = root_len;
        while i < chars.len() && is_space(chars[i]) {
            i += 1;
        }
        if chars.len() != i + 15 {
            continue;
        }
        let digits_ok = |s: &[char]| s.iter().all(|&c| digit(c).is_some());
        if digits_ok(&chars[i..i + 6]) && matches!(chars[i + 6], 'C' | 'P' | 'c' | 'p') && digits_ok(&chars[i + 7..]) {
            return Some((root_len, i));
        }
    }
    None
}

fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        4 | 6 | 9 | 11 => 30,
        2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        2 => 28,
        _ => 31,
    }
}

fn ascii_between(c: Option<char>, lo: char, hi: char) -> bool {
    c.is_some_and(|c| c >= lo && c <= hi)
}

fn ascii_val(c: char) -> u32 {
    c as u32 - '0' as u32
}

/// `datetime.strptime(s, "%y%m%d")` on six decimal-digit characters: CPython's
/// `_strptime` regex, alternatives in its order, with the first match winning even when
/// it leaves text over. Err is the `ValueError` message CPython 3.13 gives.
fn strptime_ymd(e: &[char]) -> std::result::Result<(i32, u32, u32), String> {
    let s: String = e.iter().collect();
    let val = |c: char| digit(c).expect("checked decimal digit");
    let y = (val(e[0]) * 10 + val(e[1])) as i32;
    let mut found = None;
    // (?P<m>1[0-2]|0[1-9]|[1-9])
    let m_alts: [Option<(usize, u32)>; 3] = [
        (e[2] == '1' && ascii_between(Some(e[3]), '0', '2')).then(|| (2, 10 + ascii_val(e[3]))),
        (e[2] == '0' && ascii_between(Some(e[3]), '1', '9')).then(|| (2, ascii_val(e[3]))),
        ascii_between(Some(e[2]), '1', '9').then(|| (1, ascii_val(e[2]))),
    ];
    'm: for (m_len, m) in m_alts.into_iter().flatten() {
        let p = 2 + m_len;
        let c0 = e.get(p).copied();
        let c1 = e.get(p + 1).copied();
        // (?P<d>3[0-1]|[1-2]\d|0[1-9]|[1-9]| [1-9])
        let d_alts: [Option<(usize, u32)>; 5] = [
            (c0 == Some('3') && ascii_between(c1, '0', '1')).then(|| (2, 30 + ascii_val(c1.unwrap()))),
            (ascii_between(c0, '1', '2') && c1.is_some_and(|c| digit(c).is_some()))
                .then(|| (2, ascii_val(c0.unwrap()) * 10 + val(c1.unwrap()))),
            (c0 == Some('0') && ascii_between(c1, '1', '9')).then(|| (2, ascii_val(c1.unwrap()))),
            ascii_between(c0, '1', '9').then(|| (1, ascii_val(c0.unwrap()))),
            (c0 == Some(' ') && ascii_between(c1, '1', '9')).then(|| (2, ascii_val(c1.unwrap()))),
        ];
        if let Some((d_len, d)) = d_alts.into_iter().flatten().next() {
            found = Some((m, d, p + d_len));
            break 'm;
        }
    }
    let Some((m, d, end)) = found else {
        return Err(format!("time data '{s}' does not match format '%y%m%d'"));
    };
    if end != e.len() {
        let rest: String = e[end..].iter().collect();
        return Err(format!("unconverted data remains: {rest}"));
    }
    // CPython's POSIX pivot: 00-68 are 2000-2068, 69-99 are 1969-1999 (so 69xxxx is 1969
    // and 70xxxx is 1970; preserved).
    let year = if y <= 68 { 2000 + y } else { 1900 + y };
    if d > days_in_month(year, m) {
        return Err("day is out of range for month".to_string());
    }
    Ok((year, m, d))
}

/// `OptionContract.from_occ`, up to the constructor: the parsed pieces. `occ` is
/// `None` for a non-string.
pub fn parse_occ(occ: Option<&str>) -> Result<ParsedOcc> {
    let Some(occ) = occ.filter(|s| !s.is_empty()) else {
        return value("OCC symbol must be a non-empty string");
    };
    let chars: Vec<char> = strip(occ).chars().collect();
    let Some((root_len, i)) = match_occ(&chars) else {
        return value(format!("Invalid OCC option symbol format: '{occ}'"));
    };
    let root: String = chars[..root_len].iter().collect::<String>().to_uppercase();
    let exp = &chars[i..i + 6];
    let exp_text: String = exp.iter().collect();
    let (year, month, day) = match strptime_ymd(exp) {
        Ok(ymd) => ymd,
        Err(err) => return value(format!("Invalid expiration date '{exp_text}' in OCC symbol: {err}")),
    };
    let right = if matches!(chars[i + 6], 'C' | 'c') { Right::Call } else { Right::Put };
    let n: u64 = chars[i + 7..]
        .iter()
        .fold(0, |acc, &c| acc * 10 + u64::from(digit(c).expect("checked decimal digit")));
    if n == 0 {
        return value("Strike parsed from OCC symbol must be positive, got 0");
    }
    // Decimal(n) / Decimal(1000): the ideal exponent is 0, so it prints with only the
    // decimals it needs.
    let (whole, frac) = (n / 1000, n % 1000);
    let strike_text = if frac == 0 {
        whole.to_string()
    } else {
        format!("{whole}.{}", format!("{frac:03}").trim_end_matches('0'))
    };
    Ok(ParsedOcc { root, year, month, day, right, strike_text })
}

// --- The option-roots table ----------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Exercise {
    American,
    European,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Settlement {
    Physical,
    Cash,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettleTime {
    Am,
    Pm,
}

impl Exercise {
    pub fn code(self) -> &'static str {
        match self {
            Exercise::American => "A",
            Exercise::European => "E",
        }
    }
}

impl Settlement {
    pub fn code(self) -> &'static str {
        match self {
            Settlement::Physical => "physical",
            Settlement::Cash => "cash",
        }
    }
}

impl SettleTime {
    pub fn code(self) -> &'static str {
        match self {
            SettleTime::Am => "AM",
            SettleTime::Pm => "PM",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Style {
    pub root: String,
    pub underlying: String,
    pub exercise: Exercise,
    pub settlement: Settlement,
    pub settle_time: SettleTime,
}

/// SPX is the AM-settled monthly, SPXW the PM-settled weekly; both European and cash.
const INDEX_ROOTS: [(&str, &str, SettleTime); 2] = [("SPX", "SPX", SettleTime::Am), ("SPXW", "SPX", SettleTime::Pm)];

/// Index roots whose rules are not modelled: refused, never treated as equities (I5).
const UNMODELLED_INDEX_ROOTS: [&str; 12] =
    ["NDX", "NDXP", "RUT", "RUTW", "MRUT", "XSP", "XND", "DJX", "OEX", "XEO", "VIX", "VIXW"];

fn key(root: &str) -> String {
    strip(root).to_uppercase()
}

/// The style of options listed under `root`; an unmodelled index root refuses.
pub fn option_style(root: &str) -> Result<Style> {
    let key = key(root);
    if let Some((r, u, t)) = INDEX_ROOTS.iter().find(|(r, _, _)| *r == key) {
        return Ok(Style {
            root: (*r).to_string(),
            underlying: (*u).to_string(),
            exercise: Exercise::European,
            settlement: Settlement::Cash,
            settle_time: *t,
        });
    }
    if UNMODELLED_INDEX_ROOTS.contains(&key.as_str()) {
        return Err(OptionError::Unresolvable(format!("Index option root '{key}' is not modelled yet (I5)")));
    }
    if key.is_empty() {
        return Err(OptionError::Unresolvable("Empty option root (I6)".to_string()));
    }
    Ok(Style {
        root: key.clone(),
        underlying: key,
        exercise: Exercise::American,
        settlement: Settlement::Physical,
        settle_time: SettleTime::Pm,
    })
}

/// Every root an underlying's chain lists under: `SPX` -> `["SPX", "SPXW"]`.
pub fn chain_roots(underlying: &str) -> Result<Vec<String>> {
    let key = key(underlying);
    let mut roots: Vec<String> =
        INDEX_ROOTS.iter().filter(|(_, u, _)| *u == key).map(|(r, _, _)| (*r).to_string()).collect();
    roots.sort();
    if roots.is_empty() {
        roots.push(option_style(&key)?.root);
    }
    Ok(roots)
}

// --- Settlement against the XNYS calendar -------------------------------------------

fn expiry_session(c: &ContractWire) -> Result<NaiveDate> {
    let expiry = c.expiry()?;
    if !calendar::is_session(expiry)? {
        return value(format!("{} expires on {}, which is not a session (I5)", c.occ()?, c.expiry_text()));
    }
    Ok(expiry)
}

/// When the contract's value is fixed, seconds since the Unix epoch (UTC): the expiry
/// session's open (AM roots) or close (PM). A date that is not a session refuses (I5).
pub fn settlement_instant(c: &ContractWire) -> Result<i64> {
    let expiry = expiry_session(c)?;
    let style = option_style(&c.underlying)?;
    let at = match style.settle_time {
        SettleTime::Am => calendar::session_open(expiry)?,
        SettleTime::Pm => calendar::session_close(expiry)?,
    };
    Ok(at.timestamp())
}

/// The last session the contract trades: the session before expiry for an AM root.
pub fn last_trade_date(c: &ContractWire) -> Result<NaiveDate> {
    let expiry = expiry_session(c)?;
    if option_style(&c.underlying)?.settle_time == SettleTime::Am {
        return Ok(calendar::previous_session(expiry)?);
    }
    Ok(expiry)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dec(text: &str) -> DecWire {
        let neg = text.starts_with('-');
        let t = text.trim_start_matches('-');
        let (int, frac) = t.split_once('.').unwrap_or((t, ""));
        DecWire {
            neg,
            digits: format!("{int}{frac}"),
            exp: -(frac.len() as i64),
            special: Special::Finite,
            text: text.to_string(),
        }
    }

    fn contract(root: &str, y: i32, m: u32, d: u32, right: Right, strike: &str) -> ContractWire {
        ContractWire { underlying: root.into(), year: y, month: m, day: d, right, strike: dec(strike) }
    }

    fn msg<T: std::fmt::Debug>(r: Result<T>) -> String {
        r.unwrap_err().message().to_string()
    }

    #[test]
    fn occ_roundtrips_canonical_and_compact() {
        let p = parse_occ(Some("AAPL  260918C00150000")).unwrap();
        assert_eq!((p.root.as_str(), p.year, p.month, p.day, p.right), ("AAPL", 2026, 9, 18, Right::Call));
        assert_eq!(p.strike_text, "150");
        assert_eq!(parse_occ(Some("aapl260918p00001500")).unwrap().strike_text, "1.5");
        assert_eq!(parse_occ(Some("A260918C00000001")).unwrap().strike_text, "0.001");
        let c = contract("AAPL", 2026, 9, 18, Right::Call, "150");
        assert_eq!(c.occ().unwrap(), "AAPL  260918C00150000");
    }

    #[test]
    fn occ_refusals_have_the_python_messages() {
        assert_eq!(msg(parse_occ(Some("AAPL"))), "Invalid OCC option symbol format: 'AAPL'");
        assert_eq!(
            msg(parse_occ(Some("AAPL250229C00150000"))),
            "Invalid expiration date '250229' in OCC symbol: day is out of range for month"
        );
        assert_eq!(
            msg(parse_occ(Some("AAPL261301C00150000"))),
            "Invalid expiration date '261301' in OCC symbol: unconverted data remains: 1"
        );
        assert_eq!(
            msg(parse_occ(Some("AAPL260001C00150000"))),
            "Invalid expiration date '260001' in OCC symbol: time data '260001' does not match format '%y%m%d'"
        );
        assert_eq!(
            msg(parse_occ(Some("AAPL260918C00000000"))),
            "Strike parsed from OCC symbol must be positive, got 0"
        );
    }

    #[test]
    fn two_digit_year_pivots_at_69() {
        assert_eq!(parse_occ(Some("A680101C00001000")).unwrap().year, 2068);
        assert_eq!(parse_occ(Some("A690101C00001000")).unwrap().year, 1969);
        assert_eq!(parse_occ(Some("A700101C00001000")).unwrap().year, 1970);
    }

    #[test]
    fn strike_rules() {
        let ok = |s: &str| validate_strike(&dec(s));
        assert!(ok("150").is_ok() && ok("0.001").is_ok() && ok("99999.999").is_ok());
        assert_eq!(msg(ok("0.0001")), "Strike cannot have more than 3 decimal places (thousandths), got 0.0001 (I5)");
        assert_eq!(msg(ok("100000")), "Strike 100000 out of bounds for OCC representation");
        assert_eq!(msg(ok("0")), "Strike must be positive and finite, got 0");
        assert_eq!(msg(ok("-1")), "Strike must be positive and finite, got -1");
    }

    #[test]
    fn a_strike_past_28_digits_slips_through_the_3dp_check() {
        // preserved defect: strike * 1000 rounds to 28 digits before the check
        assert!(validate_strike(&dec("1.0000000000000000000000000000001")).is_ok());
    }

    #[test]
    fn equity_symbols() {
        assert_eq!(equity_symbol(Some(" brk.b ")).unwrap(), "BRK.B");
        assert!(equity_symbol(Some("/NQ")).is_err());
        assert!(equity_symbol(Some("A.BCD")).is_err());
        assert_eq!(msg(equity_symbol(None)), "Equity symbol must be non-empty string");
        assert_eq!(msg(equity_symbol(Some("  "))), "Equity symbol must be non-empty");
    }

    #[test]
    fn roots() {
        assert_eq!(option_style("spxw").unwrap().settle_time, SettleTime::Pm);
        assert_eq!(option_style("SPX").unwrap().settle_time, SettleTime::Am);
        assert_eq!(chain_roots("SPX").unwrap(), ["SPX", "SPXW"]);
        assert_eq!(chain_roots("SPXW").unwrap(), ["SPXW"]);
        assert_eq!(option_style("NDX").unwrap_err().kind(), "unresolvable");
        assert_eq!(msg(option_style("NDX")), "Index option root 'NDX' is not modelled yet (I5)");
    }

    #[test]
    fn settlement_follows_the_calendar() {
        let third_friday = |root| contract(root, 2026, 10, 16, Right::Call, "100");
        let at = |c| settlement_instant(&c).unwrap();
        assert_eq!(at(third_friday("SPX")), 1_792_157_400); // 2026-10-16 13:30 UTC
        assert_eq!(at(third_friday("SPXW")), 1_792_180_800); // 20:00 UTC
        let xmas = contract("SPY", 2026, 12, 25, Right::Call, "100");
        assert_eq!(
            msg(settlement_instant(&xmas)),
            "SPY   261225C00100000 expires on 2026-12-25, which is not a session (I5)"
        );
        assert_eq!(last_trade_date(&third_friday("SPX")).unwrap().to_string(), "2026-10-15");
        assert_eq!(last_trade_date(&third_friday("SPY")).unwrap().to_string(), "2026-10-16");
    }
}
