//! The account risk RULES (docs/RUST_PORT.md P1d): configuration validation, the
//! sized-intent rule evaluation and the drawdown-control decision.
//!
//! This is the pure decision core of `risk.py`. It reads no ledger and writes no
//! event: Python builds the control state (kill switch, drawdown latches, whether
//! a command id was already seen), hands it over with the intent, the context,
//! the rules and the instant as ONE JSON document, and writes back what this
//! returns. Decimals cross as strings, are `rust_decimal` here, and every
//! operation is checked: an overflow refuses, it never wraps (D6).
//!
//! The engine's core invariant: EVERY rule is evaluated and recorded. Nothing
//! below returns early on a failed rule; `evaluate` always pushes all 21 results
//! (I11). The only early exits are errors (a malformed document, an overflow),
//! which refuse the whole evaluation rather than produce a partial verdict.
//!
//! Trading hours call `crate::calendar` directly.

use std::collections::BTreeSet;

use chrono::{DateTime, Datelike, NaiveDate, NaiveTime, Utc};
use chrono_tz::Tz;
use rust_decimal::prelude::*;
use serde_json::{json, Map, Value};

use crate::calendar;

/// Why the rules refuse. `Config` maps to Python's `RiskConfigurationError`,
/// `Value` to a plain `ValueError`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RiskError {
    Config(String),
    Value(String),
}

impl RiskError {
    pub fn message(&self) -> &str {
        match self {
            Self::Config(m) | Self::Value(m) => m,
        }
    }
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Config(_) => "config",
            Self::Value(_) => "value",
        }
    }
}

impl std::fmt::Display for RiskError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message())
    }
}

type Res<T> = Result<T, RiskError>;

fn cfg<T>(msg: impl Into<String>) -> Res<T> {
    Err(RiskError::Config(msg.into()))
}
fn val<T>(msg: impl Into<String>) -> Res<T> {
    Err(RiskError::Value(msg.into()))
}

// ---------------------------------------------------------------------------
// Python-compatible decimal text
// ---------------------------------------------------------------------------

/// What `decimal.Decimal(text)` makes of a string.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Parsed {
    Finite(Decimal),
    /// NaN, sNaN or an infinity: valid text, not a usable number.
    NonFinite,
    /// Not a decimal literal at all (Python: `InvalidOperation`).
    Invalid,
}

fn digits_ok(s: &str) -> bool {
    // Python allows single underscores BETWEEN digits.
    if s.is_empty() || s.starts_with('_') || s.ends_with('_') || s.contains("__") {
        return false;
    }
    s.chars().all(|c| c.is_ascii_digit() || c == '_')
}

/// Parse the way `Decimal(str)` does for ASCII input: surrounding whitespace is
/// stripped, a sign, digits with an optional point, an optional exponent,
/// underscores between digits; `nan`/`inf` forms are `NonFinite`. A finite value
/// `rust_decimal` cannot hold (more than 28 digits of scale, or 96 bits of
/// mantissa) refuses with an error rather than rounding.
pub fn parse_py_decimal(text: &str) -> Res<Parsed> {
    let t = text.trim();
    let (neg, body) = match t.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    let lower = body.to_ascii_lowercase();
    if lower == "inf" || lower == "infinity" {
        return Ok(Parsed::NonFinite);
    }
    for p in ["snan", "nan"] {
        if let Some(rest) = lower.strip_prefix(p) {
            if rest.is_empty() || rest.chars().all(|c| c.is_ascii_digit()) {
                return Ok(Parsed::NonFinite);
            }
        }
    }
    let (mant, exp) = match lower.split_once('e') {
        Some((m, e)) => (m, Some(e)),
        None => (lower.as_str(), None),
    };
    let (int_part, frac_part) = match mant.split_once('.') {
        Some((i, f)) => (i, f),
        None => (mant, ""),
    };
    if int_part.is_empty() && frac_part.is_empty() {
        return Ok(Parsed::Invalid);
    }
    if (!int_part.is_empty() && !digits_ok(int_part)) || (!frac_part.is_empty() && !digits_ok(frac_part)) {
        return Ok(Parsed::Invalid);
    }
    let mut exp10: i64 = 0;
    if let Some(e) = exp {
        let (eneg, edigits) = match e.strip_prefix('-') {
            Some(r) => (true, r),
            None => (false, e.strip_prefix('+').unwrap_or(e)),
        };
        if !digits_ok(edigits) {
            return Ok(Parsed::Invalid);
        }
        let clean: String = edigits.chars().filter(|c| *c != '_').collect();
        let n: i64 = clean
            .parse()
            .map_err(|_| RiskError::Value(format!("decimal exponent out of range: {text:?}")))?;
        exp10 = if eneg { -n } else { n };
    }
    let digits: String = int_part.chars().chain(frac_part.chars()).filter(|c| *c != '_').collect();
    let frac_len = frac_part.chars().filter(|c| *c != '_').count() as i64;
    let trimmed = digits.trim_start_matches('0');
    let mut mantissa: i128 = if trimmed.is_empty() {
        0
    } else {
        trimmed
            .parse()
            .map_err(|_| RiskError::Value(format!("decimal too large to represent exactly: {text:?}")))?
    };
    let mut scale = frac_len - exp10;
    if scale < 0 {
        for _ in 0..(-scale) {
            mantissa = mantissa
                .checked_mul(10)
                .ok_or_else(|| RiskError::Value(format!("decimal too large to represent exactly: {text:?}")))?;
        }
        scale = 0;
    }
    if mantissa == 0 {
        // Zero keeps no more scale than rust_decimal can carry.
        scale = scale.min(28);
    }
    if scale > 28 {
        return val(format!("decimal has more than 28 fractional digits: {text:?}"));
    }
    if neg {
        mantissa = -mantissa;
    }
    Decimal::try_from_i128_with_scale(mantissa, scale as u32)
        .map(Parsed::Finite)
        .map_err(|_| RiskError::Value(format!("decimal too large to represent exactly: {text:?}")))
}

fn ck(o: Option<Decimal>) -> Res<Decimal> {
    o.ok_or_else(|| RiskError::Value("decimal arithmetic overflow".into()))
}
/// Python keeps a zero result's exponent (`0 * 5.5` is `0.0`); `rust_decimal` drops
/// it. The scale is part of the reason strings, so put it back.
fn zero_scale(r: Decimal, scale: u32) -> Decimal {
    let mut r = r;
    if r.is_zero() && r.scale() != scale.min(28) {
        r.rescale(scale.min(28));
    }
    r
}
fn mul(a: Decimal, b: Decimal) -> Res<Decimal> {
    ck(a.checked_mul(b)).map(|r| zero_scale(r, a.scale() + b.scale()))
}
fn add(a: Decimal, b: Decimal) -> Res<Decimal> {
    ck(a.checked_add(b)).map(|r| zero_scale(r, a.scale().max(b.scale())))
}
fn sub(a: Decimal, b: Decimal) -> Res<Decimal> {
    ck(a.checked_sub(b)).map(|r| zero_scale(r, a.scale().max(b.scale())))
}
fn div(a: Decimal, b: Decimal) -> Res<Decimal> {
    if b.is_zero() {
        return val("decimal division by zero");
    }
    ck(a.checked_div(b))
}
/// `Decimal(int(x.to_integral_value(ROUND_FLOOR)))`: a whole number, scale 0.
fn floor_whole(d: Decimal) -> Res<Decimal> {
    let n = ck(Some(d.floor()))?;
    let i = n.to_i128().ok_or_else(|| RiskError::Value("decimal arithmetic overflow".into()))?;
    Decimal::from_i128(i).ok_or_else(|| RiskError::Value("decimal arithmetic overflow".into()))
}
/// Python `min(a, b)`: `a` unless `b` is strictly smaller (keeps `a`'s scale on a tie).
fn pymin(a: Decimal, b: Decimal) -> Decimal {
    if b < a {
        b
    } else {
        a
    }
}
/// `x / Decimal("100")` as Python computes it for an exact quotient: the value
/// shifted two places, trailing zeros stripped down to (not past) the dividend's
/// own scale, which is Python's "ideal exponent" rule.
fn div_100(x: Decimal) -> Res<Decimal> {
    let scale = x.scale();
    if scale + 2 > 28 {
        return val("decimal has more than 28 fractional digits");
    }
    let mut m = x.mantissa();
    let mut s = scale + 2;
    while s > scale && m % 10 == 0 && m != 0 {
        m /= 10;
        s -= 1;
    }
    if m == 0 {
        s = scale;
    }
    Decimal::try_from_i128_with_scale(m, s).map_err(|_| RiskError::Value("decimal arithmetic overflow".into()))
}

// ---------------------------------------------------------------------------
// Tagged raw values (what Python sends)
// ---------------------------------------------------------------------------

enum Tag<'a> {
    Null,
    Bool(bool),
    Int(i64),
    Str(&'a str),
    /// A `decimal.Decimal`: `{"d": "<str(value)>"}`.
    Dec(&'a str),
    /// Any other Python value: `{"text": str(value), "type": type name}`.
    Other(&'a str, &'a str),
}

fn tag(v: &Value) -> Tag<'_> {
    match v {
        Value::Null => Tag::Null,
        Value::Bool(b) => Tag::Bool(*b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => Tag::Int(i),
            None => Tag::Other("", "float"),
        },
        Value::String(s) => Tag::Str(s),
        Value::Object(m) => {
            if let Some(Value::String(d)) = m.get("d") {
                Tag::Dec(d)
            } else {
                let text = m.get("text").and_then(Value::as_str).unwrap_or("");
                let ty = m.get("type").and_then(Value::as_str).unwrap_or("object");
                Tag::Other(text, ty)
            }
        }
        Value::Array(_) => Tag::Other("", "list"),
    }
}

fn field<'a>(m: &'a Map<String, Value>, k: &str) -> &'a Value {
    m.get(k).unwrap_or(&Value::Null)
}

fn obj<'a>(v: &'a Value, what: &str) -> Res<&'a Map<String, Value>> {
    v.as_object().ok_or_else(|| RiskError::Value(format!("{what} must be a JSON object")))
}

/// A `Decimal` tag, finite or not. `None` for any other tag.
fn dec_tag(v: &Value) -> Res<Option<Option<Decimal>>> {
    match tag(v) {
        Tag::Dec(s) => match parse_py_decimal(s)? {
            Parsed::Finite(d) => Ok(Some(Some(d))),
            _ => Ok(Some(None)),
        },
        _ => Ok(None),
    }
}

// ---------------------------------------------------------------------------
// Account rules
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct Rules {
    pub risk_per_trade_frac: Decimal,
    pub short_risk_per_trade_frac: Decimal,
    pub max_position_notional_frac: Decimal,
    pub max_gross_exposure_frac: Decimal,
    pub bull_chop_gross_exposure_frac: Decimal,
    pub max_portfolio_heat_frac: Decimal,
    pub max_positions: i64,
    pub max_positions_per_industry: i64,
    pub min_price: Decimal,
    pub max_adv_frac: Decimal,
    pub earnings_blackout_sessions: i64,
    pub drawdown_half_risk_frac: Decimal,
    pub drawdown_suspend_frac: Decimal,
    pub drawdown_recovery_frac: Decimal,
    pub daily_loss_block_frac: Decimal,
    pub clamp_to_position_cap: bool,
}

const POSITIVE: [&str; 12] = [
    "risk_per_trade_frac",
    "short_risk_per_trade_frac",
    "max_position_notional_frac",
    "max_gross_exposure_frac",
    "bull_chop_gross_exposure_frac",
    "max_portfolio_heat_frac",
    "min_price",
    "max_adv_frac",
    "drawdown_recovery_frac",
    "drawdown_half_risk_frac",
    "drawdown_suspend_frac",
    "daily_loss_block_frac",
];

/// (field, cap, the cap's own text as Python prints it)
const CAPS: [(&str, i64, u32); 11] = [
    ("risk_per_trade_frac", 5, 2),
    ("short_risk_per_trade_frac", 5, 2),
    ("max_position_notional_frac", 1, 0),
    ("max_gross_exposure_frac", 2, 0),
    ("bull_chop_gross_exposure_frac", 2, 0),
    ("max_portfolio_heat_frac", 25, 2),
    ("max_adv_frac", 1, 0),
    ("drawdown_recovery_frac", 50, 2),
    ("drawdown_half_risk_frac", 50, 2),
    ("drawdown_suspend_frac", 50, 2),
    ("daily_loss_block_frac", 50, 2),
];

impl Rules {
    /// `AccountRiskRules.__post_init__` over the tagged field values.
    pub fn from_raw(m: &Map<String, Value>) -> Res<Rules> {
        let mut dec: Map<String, Value> = Map::new();
        for name in POSITIVE {
            let d = match dec_tag(field(m, name))? {
                Some(Some(d)) if d > Decimal::ZERO => d,
                _ => return cfg(format!("{name} must be a positive finite Decimal")),
            };
            dec.insert(name.to_string(), json!(d.to_string()));
        }
        let get = |n: &str| -> Decimal { Decimal::from_str(dec[n].as_str().unwrap_or("0")).unwrap_or_default() };
        for (name, whole, scale) in CAPS {
            let maximum = Decimal::new(whole, scale);
            if get(name) > maximum {
                return cfg(format!("{name} exceeds its sanity cap of {maximum}"));
            }
        }
        let mut ints = [0i64; 3];
        for (i, name) in ["max_positions", "max_positions_per_industry"].iter().enumerate() {
            match tag(field(m, name)) {
                Tag::Int(v) if v > 0 => ints[i] = v,
                _ => return cfg(format!("{name} must be a positive integer")),
            }
        }
        match tag(field(m, "earnings_blackout_sessions")) {
            Tag::Int(v) if v >= 0 => ints[2] = v,
            _ => return cfg("earnings_blackout_sessions must be non-negative"),
        }
        let clamp = match tag(field(m, "clamp_to_position_cap")) {
            Tag::Bool(b) => b,
            _ => return cfg("clamp_to_position_cap must be a boolean"),
        };
        let r = Rules {
            risk_per_trade_frac: get("risk_per_trade_frac"),
            short_risk_per_trade_frac: get("short_risk_per_trade_frac"),
            max_position_notional_frac: get("max_position_notional_frac"),
            max_gross_exposure_frac: get("max_gross_exposure_frac"),
            bull_chop_gross_exposure_frac: get("bull_chop_gross_exposure_frac"),
            max_portfolio_heat_frac: get("max_portfolio_heat_frac"),
            max_positions: ints[0],
            max_positions_per_industry: ints[1],
            min_price: get("min_price"),
            max_adv_frac: get("max_adv_frac"),
            earnings_blackout_sessions: ints[2],
            drawdown_half_risk_frac: get("drawdown_half_risk_frac"),
            drawdown_suspend_frac: get("drawdown_suspend_frac"),
            drawdown_recovery_frac: get("drawdown_recovery_frac"),
            daily_loss_block_frac: get("daily_loss_block_frac"),
            clamp_to_position_cap: clamp,
        };
        if r.drawdown_recovery_frac >= r.drawdown_half_risk_frac {
            return cfg("drawdown recovery must be below the half-risk threshold");
        }
        if r.drawdown_half_risk_frac >= r.drawdown_suspend_frac {
            return cfg("drawdown half-risk threshold must precede suspension");
        }
        if r.bull_chop_gross_exposure_frac > r.max_gross_exposure_frac {
            return cfg("bull-chop gross exposure cannot exceed the maximum gross exposure");
        }
        Ok(r)
    }
}

/// `AccountRiskRules.from_mapping`. Input:
/// `{"is_mapping": bool, "keys_are_strings": bool, "values": {key: tagged}}`.
/// Returns the tagged field map `Rules::from_raw` accepts (already validated).
pub fn rules_from_mapping(doc: &Value) -> Res<Map<String, Value>> {
    let top = obj(doc, "rules mapping")?;
    if top.get("is_mapping").and_then(Value::as_bool) != Some(true) {
        return cfg("account risk rules must be loaded from a mapping");
    }
    if top.get("keys_are_strings").and_then(Value::as_bool) != Some(true) {
        return cfg("account risk rule keys must be strings");
    }
    let values = obj(field(top, "values"), "values")?;
    const CONFIG: [(&str, &str); 15] = [
        ("risk_per_trade", "risk_per_trade_frac"),
        ("short_risk_per_trade", "short_risk_per_trade_frac"),
        ("max_position_notional", "max_position_notional_frac"),
        ("max_gross_exposure", "max_gross_exposure_frac"),
        ("bull_chop_gross_exposure", "bull_chop_gross_exposure_frac"),
        ("max_portfolio_heat", "max_portfolio_heat_frac"),
        ("max_positions", "max_positions"),
        ("max_positions_per_industry", "max_positions_per_industry"),
        ("min_price", "min_price"),
        ("max_adv", "max_adv_frac"),
        ("earnings_blackout_sessions", "earnings_blackout_sessions"),
        ("drawdown_half_risk", "drawdown_half_risk_frac"),
        ("drawdown_suspend", "drawdown_suspend_frac"),
        ("drawdown_recovery", "drawdown_recovery_frac"),
        ("daily_loss_block", "daily_loss_block_frac"),
    ];
    let expected: BTreeSet<&str> = CONFIG.iter().map(|c| c.0).collect();
    let unknown: Vec<&str> = values
        .keys()
        .map(String::as_str)
        .filter(|k| !expected.contains(k) && *k != "clamp_to_position_cap")
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if !unknown.is_empty() {
        return cfg(format!("Unknown account risk rule keys: {}", unknown.join(", ")));
    }
    let missing: Vec<&str> = expected.iter().copied().filter(|k| !values.contains_key(*k)).collect();
    if !missing.is_empty() {
        return cfg(format!("Missing account risk rule keys: {}", missing.join(", ")));
    }
    const NOT_PERCENT: [&str; 4] = ["max_positions", "max_positions_per_industry", "min_price", "earnings_blackout_sessions"];
    let mut out: Map<String, Value> = Map::new();
    for (name, field_name) in CONFIG {
        let v = &values[name];
        let percent = !NOT_PERCENT.contains(&name);
        let text: String = if percent {
            match tag(v) {
                Tag::Str(s) if s.trim().ends_with('%') => {
                    let t = s.trim();
                    t[..t.len() - 1].trim().to_string()
                }
                _ => return cfg(format!("{name} must be an explicit percentage string such as '0.75%'")),
            }
        } else {
            match tag(v) {
                Tag::Bool(_) => return cfg(format!("{name} must be numeric, not boolean")),
                Tag::Null => "None".to_string(),
                Tag::Int(i) => i.to_string(),
                Tag::Str(s) | Tag::Dec(s) | Tag::Other(s, _) => s.to_string(),
            }
        };
        let parsed = match parse_py_decimal(&text) {
            Ok(Parsed::Invalid) | Err(_) => return cfg(format!("{name} must contain a valid number")),
            Ok(p) => p,
        };
        let shown = match parsed {
            Parsed::Finite(d) if percent => json!({ "d": div_100(d).map_err(|e| RiskError::Config(e.to_string()))?.to_string() }),
            Parsed::Finite(d) => json!({ "d": d.to_string() }),
            _ => json!({ "d": "NaN" }),
        };
        out.insert(field_name.to_string(), shown);
    }
    for name in ["max_positions", "max_positions_per_industry", "earnings_blackout_sessions"] {
        let n = match tag(&values[name]) {
            Tag::Str(s) if !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()) => s.parse::<i64>().ok(),
            Tag::Int(i) => Some(i),
            _ => None,
        };
        match n {
            Some(n) => out.insert(name.to_string(), json!(n)),
            None => return cfg(format!("{name} must be an integer")),
        };
    }
    let clamp = match values.get("clamp_to_position_cap") {
        None => false,
        Some(v) => match tag(v) {
            Tag::Bool(b) => b,
            Tag::Null => return cfg("clamp_to_position_cap must be a boolean (true/false), not NoneType"),
            Tag::Int(_) => return cfg("clamp_to_position_cap must be a boolean (true/false), not int"),
            Tag::Str(_) => return cfg("clamp_to_position_cap must be a boolean (true/false), not str"),
            Tag::Dec(_) => return cfg("clamp_to_position_cap must be a boolean (true/false), not Decimal"),
            Tag::Other(_, ty) => return cfg(format!("clamp_to_position_cap must be a boolean (true/false), not {ty}")),
        },
    };
    out.insert("clamp_to_position_cap".to_string(), json!(clamp));
    Rules::from_raw(&out)?;
    Ok(out)
}

// ---------------------------------------------------------------------------
// Trading hours
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct Hours {
    pub timezone: Tz,
    pub weekdays: Vec<u8>,
    pub opens: NaiveTime,
    pub closes: NaiveTime,
}

fn time_of(micros: i64) -> Option<NaiveTime> {
    if !(0..86_400_000_000).contains(&micros) {
        return None;
    }
    NaiveTime::from_num_seconds_from_midnight_opt((micros / 1_000_000) as u32, ((micros % 1_000_000) * 1000) as u32)
}

impl Hours {
    /// `TradingHours.__post_init__`. `raw`: `{"timezone", "weekdays", "opens",
    /// "closes", "exchange"}`; a time is `{"micros": n}` (naive) or
    /// `{"aware_micros": n}` (it carries a UTC offset) or anything else.
    pub fn from_raw(raw: &Value) -> Res<Hours> {
        let m = obj(raw, "trading hours")?;
        let tz_text = match tag(field(m, "timezone")) {
            Tag::Str(s) => s.to_string(),
            Tag::Other(t, _) => t.to_string(),
            _ => String::new(),
        };
        let tz: Tz = match tag(field(m, "timezone")) {
            Tag::Str(s) => s.parse().ok().unwrap_or(Tz::UTC),
            _ => Tz::UTC,
        };
        let tz_ok = matches!(tag(field(m, "timezone")), Tag::Str(s) if s.parse::<Tz>().is_ok());
        if !tz_ok {
            return cfg(format!("Unknown trading-hours timezone '{tz_text}'"));
        }
        let weekdays = match field(m, "weekdays").as_array() {
            Some(a) if !a.is_empty() => {
                let mut out = Vec::new();
                for d in a {
                    match tag(d) {
                        Tag::Int(i) if (0..7).contains(&i) => out.push(i as u8),
                        _ => return cfg("trading-hours weekdays must be integers from 0 to 6"),
                    }
                }
                out
            }
            _ => return cfg("trading-hours weekdays must be integers from 0 to 6"),
        };
        let parse_time = |v: &Value| -> Option<(Option<i64>, bool)> {
            let o = v.as_object()?;
            if let Some(n) = o.get("micros").and_then(Value::as_i64) {
                Some((Some(n), false))
            } else {
                o.get("aware_micros").and_then(Value::as_i64).map(|n| (Some(n), true))
            }
        };
        let (Some((o, o_aware)), Some((c, c_aware))) = (parse_time(field(m, "opens")), parse_time(field(m, "closes"))) else {
            return cfg("trading hours must use datetime.time boundaries");
        };
        if o_aware || c_aware {
            return cfg("trading-hours boundaries must be local wall times");
        }
        let opens = o.and_then(time_of);
        let closes = c.and_then(time_of);
        let (Some(opens), Some(closes)) = (opens, closes) else {
            return cfg("trading hours must use datetime.time boundaries");
        };
        if opens >= closes {
            return cfg("trading hours must open before they close");
        }
        match tag(field(m, "exchange")) {
            Tag::Str("XNYS") => {}
            Tag::Str(s) => return val(format!("Unknown or unsupported exchange: '{s}'")),
            Tag::Other(t, _) | Tag::Dec(t) => return val(format!("Unknown or unsupported exchange: {t}")),
            Tag::Null => return val("Unknown or unsupported exchange: None"),
            Tag::Bool(b) => return val(format!("Unknown or unsupported exchange: {}", if b { "True" } else { "False" })),
            Tag::Int(i) => return val(format!("Unknown or unsupported exchange: {i}")),
        }
        Ok(Hours { timezone: tz, weekdays, opens, closes })
    }

    /// `TradingHours.is_open`: the instant (microseconds since the epoch, UTC) is
    /// in the venue's recurring window AND inside the XNYS session, whose open and
    /// close (early closes included) are read from `crate::calendar`.
    pub fn is_open(&self, epoch_us: i64) -> Res<bool> {
        let utc: DateTime<Utc> = DateTime::from_timestamp_micros(epoch_us)
            .ok_or_else(|| RiskError::Value(format!("timestamp {epoch_us}us is out of range")))?;
        let local = utc.with_timezone(&self.timezone);
        let session_date: NaiveDate = local.date_naive();
        let weekday = local.weekday().num_days_from_monday() as u8;
        let cal = |e: calendar::CalendarError| RiskError::Value(e.to_string());
        if !self.weekdays.contains(&weekday) || !calendar::is_session(session_date).map_err(cal)? {
            return Ok(false);
        }
        let s_open = calendar::session_open(session_date).map_err(cal)?.with_timezone(&self.timezone).time();
        let s_close = calendar::session_close(session_date).map_err(cal)?.with_timezone(&self.timezone).time();
        let opens = self.opens.max(s_open);
        let closes = self.closes.min(s_close);
        let t = local.time();
        Ok(opens <= t && t < closes)
    }
}

// ---------------------------------------------------------------------------
// Venue rails
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct Rails {
    pub venue_id: String,
    /// Sorted, upper-cased, de-duplicated.
    pub allowed_symbols: Option<Vec<String>>,
    pub max_position_quantity: Option<i64>,
    pub max_orders_per_day: Option<i64>,
    pub max_daily_loss: Option<Decimal>,
    pub trading_hours: Option<Hours>,
}

impl Rails {
    /// `VenueRiskRails.__post_init__`. `raw` fields: `venue_id`, `environment`,
    /// `allowed_symbols` (null | `{"is_str":true}` | `{"items":[...]}`),
    /// `max_position_quantity`, `max_orders_per_day`, `max_daily_loss` (Decimal tag),
    /// `trading_hours` (null | `{"hours":{...}}` | other), `duplicate_protection`,
    /// `persistent_kill_switch`.
    pub fn from_raw(raw: &Value) -> Res<Rails> {
        let m = obj(raw, "venue rails")?;
        let venue_id = match tag(field(m, "venue_id")) {
            Tag::Str(s) if !s.is_empty() => s.to_string(),
            Tag::Other(t, _) if !t.is_empty() => t.to_string(),
            _ => return cfg("venue_id must be non-empty"),
        };
        let env = match tag(field(m, "environment")) {
            Tag::Str(s) if ["sim", "paper", "live"].contains(&s) => s.to_string(),
            Tag::Str(s) => return cfg(format!("Invalid venue environment '{s}'")),
            Tag::Other(t, _) => return cfg(format!("Invalid venue environment '{t}'")),
            _ => return cfg("Invalid venue environment 'None'"),
        };
        let allowed = match field(m, "allowed_symbols") {
            Value::Null => None,
            Value::Object(o) if o.get("is_str").and_then(Value::as_bool) == Some(true) => {
                return cfg("allowed_symbols must be a collection of symbols")
            }
            Value::Object(o) => {
                let items = o.get("items").and_then(Value::as_array).cloned().unwrap_or_default();
                let mut set = BTreeSet::new();
                for it in &items {
                    set.insert(it.as_str().unwrap_or("").trim().to_uppercase());
                }
                if set.is_empty() || set.iter().any(String::is_empty) {
                    return cfg("allowed_symbols must contain non-empty symbols");
                }
                Some(set.into_iter().collect::<Vec<_>>())
            }
            _ => return cfg("allowed_symbols must be a collection of symbols"),
        };
        let mut limits = [None, None];
        for (i, name) in ["max_position_quantity", "max_orders_per_day"].iter().enumerate() {
            match tag(field(m, name)) {
                Tag::Null => {}
                Tag::Int(v) if v > 0 => limits[i] = Some(v),
                _ => return cfg(format!("{name} must be a positive integer")),
            }
        }
        let max_daily_loss = match field(m, "max_daily_loss") {
            Value::Null => None,
            v => match dec_tag(v)? {
                Some(Some(d)) if d > Decimal::ZERO => Some(d),
                _ => return cfg("max_daily_loss must be a positive finite Decimal"),
            },
        };
        let trading_hours = match field(m, "trading_hours") {
            Value::Null => None,
            Value::Object(o) if o.contains_key("hours") => Some(Hours::from_raw(&o["hours"])?),
            _ => return cfg("trading_hours must be a TradingHours value"),
        };
        let mut flags = [None, None];
        for (i, name) in ["duplicate_protection", "persistent_kill_switch"].iter().enumerate() {
            match tag(field(m, name)) {
                Tag::Null => {}
                Tag::Bool(true) => flags[i] = Some(true),
                Tag::Bool(false) => return cfg(format!("{name} cannot be disabled")),
                _ => return cfg(format!("{name} must be a boolean")),
            }
        }
        if env == "paper" || env == "live" {
            let required: [(&str, bool); 7] = [
                ("allowed_symbols", allowed.is_some()),
                ("max_position_quantity", limits[0].is_some()),
                ("max_orders_per_day", limits[1].is_some()),
                ("max_daily_loss", max_daily_loss.is_some()),
                ("trading_hours", trading_hours.is_some()),
                ("duplicate_protection", flags[0].is_some()),
                ("persistent_kill_switch", flags[1].is_some()),
            ];
            let missing: Vec<&str> = required.iter().filter(|(_, ok)| !ok).map(|(n, _)| *n).collect();
            if !missing.is_empty() {
                return cfg(format!("{env} venue '{venue_id}' missing required rails: {}", missing.join(", ")));
            }
            if flags[0] != Some(true) || flags[1] != Some(true) {
                return cfg("paper/live duplicate protection and persistent kill switch must be enabled");
            }
        }
        Ok(Rails {
            venue_id,
            allowed_symbols: allowed,
            max_position_quantity: limits[0],
            max_orders_per_day: limits[1],
            max_daily_loss,
            trading_hours,
        })
    }
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Context {
    pub equity: Option<Decimal>,
    pub current_price: Option<Decimal>,
    pub gross_exposure: Option<Decimal>,
    pub portfolio_heat: Option<Decimal>,
    pub open_positions: Option<i64>,
    pub industry: Option<String>,
    pub industry_positions: Option<i64>,
    pub average_dollar_volume_20d: Option<Decimal>,
    pub sessions_until_earnings: Option<i64>,
    pub regime: Option<String>,
    pub macro_high_risk_day: Option<bool>,
    pub drawdown_from_peak_frac: Option<Decimal>,
    pub previous_session_pnl_frac: Option<Decimal>,
    pub venue_orders_today: Option<i64>,
    pub venue_daily_pnl: Option<Decimal>,
    pub current_position_quantity: Option<i64>,
}

impl Context {
    /// `RiskContext.__post_init__`; every refusal is a plain `ValueError`.
    pub fn from_raw(raw: &Value) -> Res<Context> {
        let m = obj(raw, "risk context")?;
        let mut c = Context::default();
        // Each group: Python checks the whole group in this order.
        let dec_group = |names: [&str; 3], what: &str, ok: &dyn Fn(Decimal) -> bool| -> Res<[Option<Decimal>; 3]> {
            let mut out = [None; 3];
            for (i, name) in names.iter().enumerate() {
                match field(m, name) {
                    Value::Null => {}
                    v => match dec_tag(v)? {
                        Some(Some(d)) if ok(d) => out[i] = Some(d),
                        _ => return val(format!("{name} must be {what} when provided")),
                    },
                }
            }
            Ok(out)
        };
        let [equity, price, adv] = dec_group(
            ["equity", "current_price", "average_dollar_volume_20d"],
            "a positive finite Decimal",
            &|d| d > Decimal::ZERO,
        )?;
        c.equity = equity;
        c.current_price = price;
        c.average_dollar_volume_20d = adv;
        for name in ["gross_exposure", "portfolio_heat"] {
            let v = match field(m, name) {
                Value::Null => None,
                v => match dec_tag(v)? {
                    Some(Some(d)) if d >= Decimal::ZERO => Some(d),
                    _ => return val(format!("{name} must be a non-negative finite Decimal when provided")),
                },
            };
            if name == "gross_exposure" {
                c.gross_exposure = v;
            } else {
                c.portfolio_heat = v;
            }
        }
        let [dd, pnl, vpnl] = dec_group(
            ["drawdown_from_peak_frac", "previous_session_pnl_frac", "venue_daily_pnl"],
            "a finite Decimal",
            &|_| true,
        )?;
        c.drawdown_from_peak_frac = dd;
        c.previous_session_pnl_frac = pnl;
        c.venue_daily_pnl = vpnl;
        if matches!(dd, Some(d) if d < Decimal::ZERO) {
            return val("drawdown_from_peak_frac must be non-negative");
        }
        for name in ["open_positions", "industry_positions", "sessions_until_earnings", "venue_orders_today"] {
            let v = match tag(field(m, name)) {
                Tag::Null => None,
                Tag::Int(i) if i >= 0 => Some(i),
                _ => return val(format!("{name} must be a non-negative integer when provided")),
            };
            match name {
                "open_positions" => c.open_positions = v,
                "industry_positions" => c.industry_positions = v,
                "sessions_until_earnings" => c.sessions_until_earnings = v,
                _ => c.venue_orders_today = v,
            }
        }
        c.current_position_quantity = match tag(field(m, "current_position_quantity")) {
            Tag::Null => None,
            Tag::Int(i) => Some(i),
            _ => return val("current_position_quantity must be an integer when provided"),
        };
        c.industry = match tag(field(m, "industry")) {
            Tag::Null => None,
            Tag::Str(s) if !s.is_empty() => Some(s.to_string()),
            _ => return val("industry must be non-empty when provided"),
        };
        c.regime = match tag(field(m, "regime")) {
            Tag::Null => None,
            Tag::Str(s) if ["BULL_EXPLOSIVE", "BULL_CHOPIER", "BEAR_PROTECTIVE", "UNKNOWN"].contains(&s) => Some(s.to_string()),
            Tag::Str(s) | Tag::Other(s, _) | Tag::Dec(s) => return val(format!("Unsupported market regime '{s}'")),
            Tag::Int(i) => return val(format!("Unsupported market regime '{i}'")),
            Tag::Bool(b) => return val(format!("Unsupported market regime '{}'", if b { "True" } else { "False" })),
        };
        c.macro_high_risk_day = match tag(field(m, "macro_high_risk_day")) {
            Tag::Null => None,
            Tag::Bool(b) => Some(b),
            _ => return val("macro_high_risk_day must be a boolean when provided"),
        };
        Ok(c)
    }
}

// ---------------------------------------------------------------------------
// Drawdown controls
// ---------------------------------------------------------------------------

/// One latch transition for Python to record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlChange {
    pub control_id: &'static str,
    pub enabled: bool,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrawdownDecision {
    pub brake: bool,
    pub suspension: bool,
    pub changes: Vec<ControlChange>,
}

/// The decision part of `_update_drawdown_controls`. `drawdown_text` is the
/// drawdown as Python printed it, echoed verbatim in a change's reason.
pub fn drawdown_decision(
    rules: &Rules,
    drawdown: Option<Decimal>,
    drawdown_text: &str,
    brake_active: bool,
    suspension_active: bool,
) -> DrawdownDecision {
    let mut desired_brake = brake_active;
    let mut desired_suspension = suspension_active;
    let mut changes = Vec::new();
    if let Some(dd) = drawdown {
        let recovery = dd <= rules.drawdown_recovery_frac;
        desired_brake = if recovery { false } else { brake_active || dd >= rules.drawdown_half_risk_frac };
        desired_suspension = if recovery { false } else { suspension_active || dd >= rules.drawdown_suspend_frac };
        for (control_id, active, desired) in [
            ("drawdown_brake", brake_active, desired_brake),
            ("drawdown_suspension", suspension_active, desired_suspension),
        ] {
            if desired != active {
                changes.push(ControlChange {
                    control_id,
                    enabled: desired,
                    reason: format!("Drawdown {drawdown_text} triggered account brake transition"),
                });
            }
        }
    }
    DrawdownDecision { brake: desired_brake, suspension: desired_suspension, changes }
}

// ---------------------------------------------------------------------------
// Evaluation
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Intent {
    pub is_equity: bool,
    pub symbol: String,
    pub side: Side,
    pub quantity_rule: String,
    pub entry_price: Decimal,
    pub stop_loss: Decimal,
    pub command_id: String,
}

/// What Python read from the ledger plus the instant.
#[derive(Debug, Clone, PartialEq)]
pub struct State {
    pub now_us: i64,
    /// `now.isoformat()`, the trading-hours rule's measured value.
    pub now_label: String,
    pub command_known: bool,
    pub kill_switch: bool,
    pub drawdown_brake: bool,
    pub drawdown_suspension: bool,
}

/// A measured value or threshold, typed as Python's `Any` was.
#[derive(Debug, Clone, PartialEq)]
pub enum V {
    Dec(Decimal),
    Str(String),
    Int(i128),
    Bool(bool),
    Set(Vec<String>),
    Hours,
}

impl V {
    fn s(x: &str) -> V {
        V::Str(x.to_string())
    }
    fn unknown() -> V {
        V::s("UNKNOWN")
    }
    fn to_json(&self) -> Value {
        match self {
            V::Dec(d) => json!({ "d": d.to_string() }),
            V::Str(s) => json!({ "s": s }),
            V::Int(i) => json!({ "i": i.to_string() }),
            V::Bool(b) => json!({ "b": b }),
            V::Set(v) => json!({ "set": v }),
            V::Hours => json!({ "hours": true }),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct RuleResult {
    pub rule: &'static str,
    pub passed: bool,
    pub measured: V,
    pub threshold: V,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Evaluation {
    pub results: Vec<RuleResult>,
    pub refusals: Vec<String>,
    /// The sized quantity, present only when nothing refused.
    pub approved_quantity: Option<Decimal>,
}

/// The number of rules every evaluation records.
pub const RULE_COUNT: usize = 21;

fn opt_dec(d: Option<Decimal>) -> V {
    d.map(V::Dec).unwrap_or_else(V::unknown)
}
fn opt_int(i: Option<i128>) -> V {
    i.map(V::Int).unwrap_or_else(V::unknown)
}

fn pct_text(x: &str) -> &str {
    x
}

/// Evaluate every rule. Never short-circuits: all [`RULE_COUNT`] results are built.
pub fn evaluate(rules: &Rules, rails: &Rails, ctx: &Context, intent: &Intent, st: &State) -> Res<Evaluation> {
    if !intent.is_equity {
        return val("E6 account rules support equity intents only");
    }
    let hundred = Decimal::from(100);
    let half = Decimal::new(5, 1);
    let one = Decimal::ONE;
    let sell = intent.side == Side::Sell;
    let distance = sub(intent.entry_price, intent.stop_loss)?.abs();
    let risk_frac = if sell { rules.short_risk_per_trade_frac } else { rules.risk_per_trade_frac };
    let qr = intent.quantity_rule.as_str();

    let mut fixed_quantity: Option<Decimal> = None;
    let mut notional_frac: Option<Decimal> = None;
    let mut quantity_rule_ok = false;
    let risk_sized = qr.starts_with("risk_") && qr.ends_with("pct");
    let slice = |from: usize, to_back: usize| -> &str { qr.get(from..qr.len().saturating_sub(to_back)).unwrap_or("") };
    let pct_of = |raw: &str| -> Res<Option<Decimal>> {
        match parse_py_decimal(raw) {
            Ok(Parsed::Finite(d)) => Ok(Some(div_100(d)?)),
            Ok(_) => Ok(None),
            Err(e) => Err(e),
        }
    };
    let sizing_risk_frac;
    if risk_sized {
        let requested = pct_of(slice("risk_".len(), 3))?.filter(|d| *d > Decimal::ZERO);
        match requested {
            Some(r) => {
                quantity_rule_ok = r <= risk_frac;
                sizing_risk_frac = pymin(r, risk_frac);
            }
            None => sizing_risk_frac = risk_frac,
        }
    } else if let Some(raw) = qr.strip_prefix("fixed_") {
        if !raw.is_empty() && raw.chars().all(|c| c.is_ascii_digit()) {
            let n: i128 = raw.parse().map_err(|_| RiskError::Value("fixed quantity out of range".into()))?;
            if n > 0 {
                fixed_quantity = Some(Decimal::from_i128(n).ok_or_else(|| RiskError::Value("fixed quantity out of range".into()))?);
                quantity_rule_ok = true;
            }
        }
        sizing_risk_frac = risk_frac;
    } else if qr.starts_with("notional_") && qr.ends_with("pct") {
        let requested = pct_of(slice("notional_".len(), 3))?.filter(|d| *d > Decimal::ZERO);
        if let Some(r) = requested {
            quantity_rule_ok = r <= rules.max_position_notional_frac;
            notional_frac = Some(pymin(r, rules.max_position_notional_frac));
        }
        sizing_risk_frac = risk_frac;
    } else {
        sizing_risk_frac = risk_frac;
    }
    let regime = ctx.regime.as_deref();
    let regime_scale = if regime == Some("BULL_CHOPIER") { half } else { one };
    let (drawdown_brake, drawdown_suspension) = (st.drawdown_brake, st.drawdown_suspension);
    let drawdown_scale = if drawdown_brake { half } else { one };
    let risk_budget = match ctx.equity {
        Some(e) => mul(mul(mul(e, sizing_risk_frac)?, regime_scale)?, drawdown_scale)?,
        None => Decimal::ZERO,
    };
    let max_position = match ctx.equity {
        Some(e) => Some(mul(e, rules.max_position_notional_frac)?),
        None => None,
    };
    let mut quantity = if let Some(q) = fixed_quantity {
        q
    } else if let Some(nf) = notional_frac {
        match ctx.equity {
            Some(e) => floor_whole(div(mul(e, nf)?, intent.entry_price)?)?,
            None => Decimal::ZERO,
        }
    } else {
        floor_whole(div(risk_budget, distance)?)?
    };
    let mut unclamped_quantity: Option<Decimal> = None;
    if rules.clamp_to_position_cap && risk_sized {
        if let Some(mp) = max_position {
            let cap_quantity = floor_whole(div(mp, intent.entry_price)?)?;
            if quantity > cap_quantity {
                unclamped_quantity = Some(quantity);
                quantity = cap_quantity;
            }
        }
    }
    let risk_amount = mul(quantity, distance)?;
    let notional = mul(quantity, intent.entry_price)?;
    let gross_limit_frac = if regime == Some("BULL_CHOPIER") {
        rules.bull_chop_gross_exposure_frac
    } else {
        rules.max_gross_exposure_frac
    };

    let mut results: Vec<RuleResult> = Vec::with_capacity(RULE_COUNT);
    let mut compare = |rule: &'static str, passed: bool, measured: V, threshold: V, success: String, refusal: &str| {
        results.push(RuleResult { rule, passed, measured, threshold, reason: if passed { success } else { refusal.to_string() } });
    };

    compare(
        "risk_per_trade",
        ctx.equity.is_some() && quantity > Decimal::ZERO && risk_amount <= risk_budget,
        V::Dec(risk_amount),
        V::Dec(risk_budget),
        "Sized quantity is within the adjusted risk budget".into(),
        "Risk budget/equity is unknown or cannot fund one whole share",
    );
    compare(
        "quantity_rule",
        quantity_rule_ok,
        V::s(qr),
        V::Str(format!(
            "risk_<x>pct with x <= {}; notional_<x>pct with x <= {}; or fixed_<n> with n > 0",
            mul(risk_frac, hundred)?,
            mul(rules.max_position_notional_frac, hundred)?
        )),
        "Intent sizing rule is supported and within its configured risk ceiling".into(),
        "Intent sizing rule is invalid or requests more risk than account rules allow",
    );
    compare(
        "max_position",
        matches!(max_position, Some(mp) if notional <= mp),
        V::Dec(notional),
        opt_dec(max_position),
        match unclamped_quantity {
            None => "Position notional is within the account cap".to_string(),
            Some(u) => format!("Quantity reduced from {u} to {quantity} shares to fit the position cap (clamp_to_position_cap)"),
        },
        "Position notional or equity is unknown or exceeds the account cap",
    );
    let gross_cap = match ctx.equity {
        Some(e) => Some(mul(e, gross_limit_frac)?),
        None => None,
    };
    let proposed_gross = match ctx.gross_exposure {
        Some(g) => Some(add(g, notional)?),
        None => None,
    };
    compare(
        "gross_exposure",
        matches!((gross_cap, proposed_gross), (Some(c), Some(p)) if p <= c),
        opt_dec(proposed_gross),
        opt_dec(gross_cap),
        "Gross exposure is within the regime-adjusted cap".into(),
        "Gross exposure or equity is unknown or exceeds the cap",
    );
    let heat_cap = match ctx.equity {
        Some(e) => Some(mul(e, rules.max_portfolio_heat_frac)?),
        None => None,
    };
    let heat = match ctx.portfolio_heat {
        Some(h) => Some(add(h, risk_amount)?),
        None => None,
    };
    compare(
        "portfolio_heat",
        matches!((heat_cap, heat), (Some(c), Some(h)) if h <= c),
        opt_dec(heat),
        opt_dec(heat_cap),
        "Portfolio heat is within the account cap".into(),
        "Portfolio heat or equity is unknown or exceeds the cap",
    );
    let plus_one = |n: Option<i64>| -> Option<i128> { n.map(|v| v as i128 + 1) };
    compare(
        "max_positions",
        matches!(ctx.open_positions, Some(n) if n < rules.max_positions),
        opt_int(plus_one(ctx.open_positions)),
        V::Int(rules.max_positions as i128),
        "Position count is within the account cap".into(),
        "Position count is unknown or at the account cap",
    );
    compare(
        "per_industry",
        ctx.industry.is_some() && matches!(ctx.industry_positions, Some(n) if n < rules.max_positions_per_industry),
        opt_int(plus_one(ctx.industry_positions)),
        V::Int(rules.max_positions_per_industry as i128),
        "Industry position count is within the account cap".into(),
        &format!("Industry '{}' is at its cap or unknown", ctx.industry.as_deref().unwrap_or("UNKNOWN")),
    );
    compare(
        "min_price",
        matches!(ctx.current_price, Some(p) if p >= rules.min_price),
        opt_dec(ctx.current_price),
        V::Dec(rules.min_price),
        "Current price meets the account minimum".into(),
        "Current price is unknown or below the account minimum",
    );
    let adv_limit = match ctx.average_dollar_volume_20d {
        Some(a) => Some(mul(a, rules.max_adv_frac)?),
        None => None,
    };
    compare(
        "adv_pct",
        matches!(adv_limit, Some(l) if notional <= l),
        V::Dec(notional),
        opt_dec(adv_limit),
        "Position is within the 20-day ADV cap".into(),
        "20-day ADV is unknown or position exceeds its cap",
    );
    compare(
        "earnings_blackout",
        matches!(ctx.sessions_until_earnings, Some(n) if n > rules.earnings_blackout_sessions),
        opt_int(ctx.sessions_until_earnings.map(i128::from)),
        V::Str(format!("> {} sessions", rules.earnings_blackout_sessions)),
        "Earnings are outside the blackout window".into(),
        "Earnings date is unknown or inside the blackout window",
    );
    let regime_ok = matches!(regime, Some(r) if r != "UNKNOWN") && !(regime == Some("BEAR_PROTECTIVE") && !sell);
    compare(
        "regime",
        regime_ok,
        V::s(regime.unwrap_or("UNKNOWN")),
        V::s("Known regime; no new longs in BEAR_PROTECTIVE"),
        "Regime permits this entry".into(),
        "Unknown regime or BEAR_PROTECTIVE blocks this entry",
    );
    compare(
        "macro_high_risk_day",
        ctx.macro_high_risk_day == Some(false),
        ctx.macro_high_risk_day.map(V::Bool).unwrap_or_else(V::unknown),
        V::Bool(false),
        "No high-risk macro event blocks this session".into(),
        "Macro risk is unknown or this is a high-risk day",
    );
    let scaled = mul(mul(sizing_risk_frac, regime_scale)?, drawdown_scale)?;
    compare(
        "drawdown_brake",
        ctx.drawdown_from_peak_frac.is_some() && !drawdown_suspension,
        opt_dec(ctx.drawdown_from_peak_frac),
        V::Str(format!(
            "half risk at {}; suspend at {}; recover only at {}",
            rules.drawdown_half_risk_frac, rules.drawdown_suspend_frac, rules.drawdown_recovery_frac
        )),
        format!("Risk budget scaled to {scaled} of equity"),
        "Drawdown is unknown or suspension remains latched until recovery",
    );
    let neg_daily = -rules.daily_loss_block_frac;
    compare(
        "daily_loss",
        matches!(ctx.previous_session_pnl_frac, Some(p) if p > neg_daily),
        opt_dec(ctx.previous_session_pnl_frac),
        V::Str(format!("> {neg_daily}")),
        "Previous session loss is below the block threshold".into(),
        "Previous session loss is unknown or blocks this session",
    );

    // Venue rails. `configured == false` records a pass: not required here.
    let mut rail = |name: &'static str, configured: bool, passed: Option<bool>, measured: V, limit: V| {
        if !configured {
            results.push(RuleResult {
                rule: name,
                passed: true,
                measured,
                threshold: limit,
                reason: format!("Not configured for {name}; not required for this venue"),
            });
        } else {
            let ok = passed == Some(true);
            results.push(RuleResult {
                rule: name,
                passed: ok,
                measured,
                threshold: limit,
                reason: if ok {
                    format!("{name} permits this entry")
                } else {
                    format!("{name} input is unknown or the rail blocks this entry")
                },
            });
        }
    };
    let not_required = || V::s("not required");
    let qty_i = quantity.to_i128().ok_or_else(|| RiskError::Value("quantity out of range".into()))?;
    let projected: Option<i128> = ctx
        .current_position_quantity
        .map(|c| c as i128 + if sell { -qty_i } else { qty_i });
    let venue_position_ok = match (rails.max_position_quantity, projected) {
        (Some(max), Some(p)) => Some(quantity > Decimal::ZERO && p.abs() <= max as i128),
        _ => None,
    };
    rail(
        "allowlist",
        rails.allowed_symbols.is_some(),
        rails.allowed_symbols.as_ref().map(|s| s.iter().any(|x| *x == intent.symbol)),
        V::s(&intent.symbol),
        rails.allowed_symbols.clone().map(V::Set).unwrap_or_else(not_required),
    );
    rail(
        "venue_max_position",
        rails.max_position_quantity.is_some(),
        venue_position_ok,
        opt_int(projected.map(i128::abs)),
        rails.max_position_quantity.map(|n| V::Int(n as i128)).unwrap_or_else(not_required),
    );
    rail(
        "orders_per_day",
        rails.max_orders_per_day.is_some(),
        match (rails.max_orders_per_day, ctx.venue_orders_today) {
            (Some(max), Some(n)) => Some(n < max),
            _ => None,
        },
        opt_int(plus_one(ctx.venue_orders_today)),
        rails.max_orders_per_day.map(|n| V::Int(n as i128)).unwrap_or_else(not_required),
    );
    rail(
        "venue_daily_loss",
        rails.max_daily_loss.is_some(),
        match (rails.max_daily_loss, ctx.venue_daily_pnl) {
            (Some(max), Some(p)) => Some(p > -max),
            _ => None,
        },
        opt_dec(ctx.venue_daily_pnl),
        rails.max_daily_loss.map(|m| V::Str(format!("> {}", -m))).unwrap_or_else(not_required),
    );
    let hours_open = match &rails.trading_hours {
        Some(h) => Some(h.is_open(st.now_us)?),
        None => None,
    };
    rail(
        "trading_hours",
        rails.trading_hours.is_some(),
        hours_open,
        V::s(&st.now_label),
        if rails.trading_hours.is_some() { V::Hours } else { not_required() },
    );
    rail(
        "duplicate_protection",
        true,
        Some(!st.command_known),
        V::s(&intent.command_id),
        V::s("command id not present in ledger"),
    );
    rail("persistent_kill_switch", true, Some(!st.kill_switch), V::Bool(st.kill_switch), V::Bool(false));

    let _ = pct_text("");
    let refusals: Vec<String> = results.iter().filter(|r| !r.passed).map(|r| r.reason.clone()).collect();
    let approved_quantity = if refusals.is_empty() { Some(quantity) } else { None };
    Ok(Evaluation { results, refusals, approved_quantity })
}

// ---------------------------------------------------------------------------
// JSON boundary (what `trade_engine_rs` exposes)
// ---------------------------------------------------------------------------

fn envelope(r: Res<Value>) -> String {
    match r {
        Ok(v) => json!({ "ok": v }).to_string(),
        Err(e) => json!({ "error": { "kind": e.kind(), "message": e.message() } }).to_string(),
    }
}

fn parse_doc(doc: &str) -> Res<Value> {
    serde_json::from_str(doc).map_err(|e| RiskError::Value(format!("malformed risk document: {e}")))
}

fn dec_str(v: &Value, what: &str) -> Res<Decimal> {
    match v.as_str().map(parse_py_decimal) {
        Some(Ok(Parsed::Finite(d))) => Ok(d),
        _ => val(format!("{what} must be a finite Decimal string")),
    }
}

/// `{"fields": <tagged rules>}` in, `{"ok": <tagged rules>}` out.
pub fn json_rules_from_mapping(doc: &str) -> String {
    envelope(parse_doc(doc).and_then(|d| rules_from_mapping(&d)).map(Value::Object))
}

pub fn json_validate_rules(doc: &str) -> String {
    envelope(parse_doc(doc).and_then(|d| Rules::from_raw(obj(&d, "rules")?)).map(|_| json!({})))
}

/// Returns the normalised allowed symbols (sorted, upper-case) or null.
pub fn json_validate_rails(doc: &str) -> String {
    envelope(parse_doc(doc).and_then(|d| Rails::from_raw(&d)).map(|r| json!({ "allowed_symbols": r.allowed_symbols })))
}

pub fn json_validate_hours(doc: &str) -> String {
    envelope(parse_doc(doc).and_then(|d| Hours::from_raw(&d)).map(|_| json!({})))
}

pub fn json_validate_context(doc: &str) -> String {
    envelope(parse_doc(doc).and_then(|d| Context::from_raw(&d)).map(|_| json!({})))
}

/// `{"hours": {...}, "epoch_us": n}`
pub fn json_hours_is_open(doc: &str) -> String {
    envelope(parse_doc(doc).and_then(|d| {
        let m = obj(&d, "is_open request")?;
        let h = Hours::from_raw(field(m, "hours"))?;
        let us = field(m, "epoch_us").as_i64().ok_or_else(|| RiskError::Value("epoch_us must be an integer".into()))?;
        Ok(json!(h.is_open(us)?))
    }))
}

/// `{"rules", "drawdown": str|null, "brake_active", "suspension_active", "is_equity"}`
pub fn json_drawdown_controls(doc: &str) -> String {
    envelope(parse_doc(doc).and_then(|d| {
        let m = obj(&d, "drawdown request")?;
        if field(m, "is_equity").as_bool() != Some(true) {
            return val("E6 account rules support equity intents only");
        }
        let rules = Rules::from_raw(obj(field(m, "rules"), "rules")?)?;
        let (dd, text) = match field(m, "drawdown") {
            Value::Null => (None, String::new()),
            v => (Some(dec_str(v, "drawdown")?), v.as_str().unwrap_or("").to_string()),
        };
        let flag = |k: &str| field(m, k).as_bool().unwrap_or(false);
        let r = drawdown_decision(&rules, dd, &text, flag("brake_active"), flag("suspension_active"));
        Ok(json!({
            "brake": r.brake,
            "suspension": r.suspension,
            "changes": r.changes.iter().map(|c| json!({"control_id": c.control_id, "enabled": c.enabled, "reason": c.reason})).collect::<Vec<_>>(),
        }))
    }))
}

/// `{"rules", "rails", "context", "intent", "state"}` in; the 21 results out.
pub fn json_evaluate(doc: &str) -> String {
    envelope(parse_doc(doc).and_then(|d| {
        let m = obj(&d, "evaluate request")?;
        let rules = Rules::from_raw(obj(field(m, "rules"), "rules")?)?;
        let rails = Rails::from_raw(field(m, "rails"))?;
        let ctx = Context::from_raw(field(m, "context"))?;
        let i = obj(field(m, "intent"), "intent")?;
        let side = match field(i, "side").as_str() {
            Some("BUY") => Side::Buy,
            Some("SELL") => Side::Sell,
            _ => return val("intent side must be BUY or SELL"),
        };
        let s = |k: &str| field(i, k).as_str().unwrap_or("").to_string();
        let intent = Intent {
            is_equity: field(i, "is_equity").as_bool() == Some(true),
            symbol: s("symbol"),
            side,
            quantity_rule: s("quantity_rule"),
            entry_price: dec_str(field(i, "entry_price"), "entry_price")?,
            stop_loss: dec_str(field(i, "stop_loss"), "stop_loss")?,
            command_id: s("command_id"),
        };
        let st = obj(field(m, "state"), "state")?;
        let flag = |k: &str| field(st, k).as_bool().unwrap_or(false);
        let state = State {
            now_us: field(st, "now_us").as_i64().ok_or_else(|| RiskError::Value("now_us must be an integer".into()))?,
            now_label: field(st, "now_label").as_str().unwrap_or("").to_string(),
            command_known: flag("command_known"),
            kill_switch: flag("kill_switch"),
            drawdown_brake: flag("drawdown_brake"),
            drawdown_suspension: flag("drawdown_suspension"),
        };
        let e = evaluate(&rules, &rails, &ctx, &intent, &state)?;
        Ok(json!({
            "results": e.results.iter().map(|r| json!({
                "rule": r.rule, "passed": r.passed,
                "measured": r.measured.to_json(), "threshold": r.threshold.to_json(),
                "reason": r.reason,
            })).collect::<Vec<_>>(),
            "refusals": e.refusals,
            "approved_quantity": e.approved_quantity.map(|q| q.to_string()),
        }))
    }))
}


#[cfg(test)]
mod tests {
    use super::*;

    const BASE: &str = r##"{"rules": {"risk_per_trade_frac": {"d": "0.0075"}, "short_risk_per_trade_frac": {"d": "0.005"}, "max_position_notional_frac": {"d": "0.20"}, "max_gross_exposure_frac": {"d": "1.50"}, "bull_chop_gross_exposure_frac": {"d": "1.00"}, "max_portfolio_heat_frac": {"d": "0.06"}, "max_positions": 10, "max_positions_per_industry": 3, "min_price": {"d": "5"}, "max_adv_frac": {"d": "0.01"}, "earnings_blackout_sessions": 5, "drawdown_half_risk_frac": {"d": "0.08"}, "drawdown_suspend_frac": {"d": "0.15"}, "drawdown_recovery_frac": {"d": "0.05"}, "daily_loss_block_frac": {"d": "0.03"}, "clamp_to_position_cap": false}, "rails": {"venue_id": "paper-main", "environment": "paper", "allowed_symbols": {"items": ["MSFT", "AAPL"]}, "max_position_quantity": 1000, "max_orders_per_day": 100, "max_daily_loss": {"d": "5000"}, "trading_hours": {"hours": {"timezone": "America/New_York", "weekdays": [0, 1, 2, 3, 4], "opens": {"micros": 34200000000}, "closes": {"micros": 57600000000}, "exchange": "XNYS"}}, "duplicate_protection": true, "persistent_kill_switch": true}, "context": {"equity": {"d": "50000"}, "current_price": {"d": "100"}, "gross_exposure": {"d": "0"}, "portfolio_heat": {"d": "0"}, "open_positions": 0, "industry": "Technology", "industry_positions": 0, "average_dollar_volume_20d": {"d": "1000000000"}, "sessions_until_earnings": 6, "regime": "BULL_EXPLOSIVE", "macro_high_risk_day": false, "drawdown_from_peak_frac": {"d": "0.02"}, "previous_session_pnl_frac": {"d": "0"}, "venue_orders_today": 0, "venue_daily_pnl": {"d": "0"}, "current_position_quantity": 0}, "intent": {"is_equity": true, "symbol": "AAPL", "side": "BUY", "quantity_rule": "risk_0.75pct", "entry_price": "100", "stop_loss": "95", "command_id": "command-1"}, "state": {"now_us": 1790172000000000, "now_label": "2026-09-23T14:00:00+00:00", "command_known": false, "kill_switch": false, "drawdown_brake": false, "drawdown_suspension": false}}"##;

    fn base() -> Value {
        serde_json::from_str(BASE).unwrap()
    }

    fn eval(doc: &Value) -> Value {
        serde_json::from_str(&json_evaluate(&doc.to_string())).unwrap()
    }

    fn results(reply: &Value) -> &Vec<Value> {
        reply["ok"]["results"].as_array().expect("an evaluation, not a refusal")
    }

    fn failed(reply: &Value) -> Vec<String> {
        results(reply)
            .iter()
            .filter(|r| r["passed"] == json!(false))
            .map(|r| r["rule"].as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn a_clean_intent_passes_all_twenty_one_rules() {
        let reply = eval(&base());
        assert_eq!(results(&reply).len(), 21);
        assert!(failed(&reply).is_empty(), "{:?}", failed(&reply));
        assert!(reply["ok"]["approved_quantity"].is_string());
    }

    #[test]
    fn no_short_circuit_every_rule_is_present_when_the_first_fails() {
        let mut doc = base();
        // A tiny account: the sizing rules at the head of the list fail, and so do later ones.
        doc["context"]["equity"] = json!({"d": "1"});
        doc["rules"]["min_price"] = json!({"d": "500"});
        doc["state"]["command_known"] = json!(true);
        doc["state"]["kill_switch"] = json!(true);
        let reply = eval(&doc);
        assert_eq!(results(&reply).len(), 21, "a refusal must not stop evaluation");
        let bad = failed(&reply);
        assert!(bad.contains(&"min_price".to_string()), "{bad:?}");
        assert!(bad.contains(&"duplicate_protection".to_string()), "{bad:?}");
        assert!(bad.contains(&"persistent_kill_switch".to_string()), "{bad:?}");
        assert!(bad.len() >= 3);
        assert_eq!(results(&reply)[0]["passed"], json!(false), "the FIRST rule must be the one that fails: {bad:?}");
        assert!(reply["ok"]["approved_quantity"].is_null());
        assert_eq!(reply["ok"]["refusals"].as_array().unwrap().len(), bad.len());
    }

    #[test]
    fn rule_order_is_fixed() {
        let reply = eval(&base());
        let names: Vec<&str> = results(&reply).iter().map(|r| r["rule"].as_str().unwrap()).collect();
        assert_eq!(names.first(), Some(&"risk_per_trade"));
        assert_eq!(names.last(), Some(&"persistent_kill_switch"));
        assert_eq!(names[names.len() - 2], "duplicate_protection");
    }

    #[test]
    fn the_kill_switch_refuses_and_nothing_else() {
        let mut doc = base();
        doc["state"]["kill_switch"] = json!(true);
        assert_eq!(failed(&eval(&doc)), vec!["persistent_kill_switch".to_string()]);
    }

    #[test]
    fn min_price_boundary_is_inclusive() {
        let mut doc = base();
        doc["rules"]["min_price"] = json!({"d": "100"});
        assert!(failed(&eval(&doc)).is_empty());
        doc["rules"]["min_price"] = json!({"d": "100.01"});
        assert_eq!(failed(&eval(&doc)), vec!["min_price".to_string()]);
    }

    #[test]
    fn a_closed_market_instant_fails_only_trading_hours() {
        let mut doc = base();
        doc["state"]["now_us"] = json!(1790172000000000i64 - 12 * 3_600_000_000i64);
        assert_eq!(failed(&eval(&doc)), vec!["trading_hours".to_string()]);
    }

    #[test]
    fn a_wrong_timezone_changes_the_hours_verdict() {
        let mut doc = base();
        doc["rails"]["trading_hours"]["hours"]["timezone"] = json!("Asia/Tokyo");
        assert_eq!(failed(&eval(&doc)), vec!["trading_hours".to_string()]);
    }

    #[test]
    fn an_overflowing_size_refuses_instead_of_wrapping() {
        let mut doc = base();
        doc["context"]["equity"] = json!({"d": "79228162514264337593543950335"});
        doc["context"]["current_price"] = json!({"d": "79228162514264337593543950335"});
        doc["intent"]["entry_price"] = json!("79228162514264337593543950335");
        doc["intent"]["stop_loss"] = json!("1");
        let reply = eval(&doc);
        assert!(reply.get("error").is_some() || !failed(&reply).is_empty(), "{reply}");
    }

    #[test]
    fn a_malformed_document_is_a_value_error() {
        let reply: Value = serde_json::from_str(&json_evaluate("{not json")).unwrap();
        assert_eq!(reply["error"]["kind"], json!("value"));
    }

    #[test]
    fn rules_validation_refuses_a_non_positive_threshold_as_config() {
        let mut rules = base()["rules"].clone();
        rules["risk_per_trade_frac"] = json!({"d": "0"});
        let reply: Value = serde_json::from_str(&json_validate_rules(&rules.to_string())).unwrap();
        assert_eq!(reply["error"]["kind"], json!("config"));
    }

    #[test]
    fn parser_accepts_python_decimal_text_and_flags_the_rest() {
        assert!(matches!(parse_py_decimal(" 1.50 "), Ok(Parsed::Finite(_))));
        assert!(matches!(parse_py_decimal("1e2"), Ok(Parsed::Finite(_))));
        assert!(matches!(parse_py_decimal("NaN"), Ok(Parsed::NonFinite)));
        assert!(matches!(parse_py_decimal("abc"), Ok(Parsed::Invalid)));
        assert!(matches!(parse_py_decimal(""), Ok(Parsed::Invalid)));
    }

    fn controls(dd: &str, brake: bool, susp: bool) -> Value {
        let doc = json!({
            "is_equity": true, "rules": base()["rules"], "drawdown": dd,
            "brake_active": brake, "suspension_active": susp,
        });
        serde_json::from_str::<Value>(&json_drawdown_controls(&doc.to_string())).unwrap()["ok"].clone()
    }

    #[test]
    fn drawdown_thresholds_are_inclusive_and_recovery_clears_both() {
        let at_half = controls("0.08", false, false);
        assert_eq!(at_half["brake"], json!(true));
        assert_eq!(at_half["suspension"], json!(false));
        assert_eq!(at_half["changes"].as_array().unwrap().len(), 1);
        let below = controls("0.0799", false, false);
        assert_eq!(below["brake"], json!(false));
        assert!(below["changes"].as_array().unwrap().is_empty());
        let at_suspend = controls("0.15", true, false);
        assert_eq!(at_suspend["suspension"], json!(true));
        let recovered = controls("0.05", true, true);
        assert_eq!(recovered["brake"], json!(false));
        assert_eq!(recovered["suspension"], json!(false));
        assert_eq!(recovered["changes"].as_array().unwrap().len(), 2);
        let between = controls("0.06", true, false);
        assert_eq!(between["brake"], json!(true));
        assert!(between["changes"].as_array().unwrap().is_empty());
    }

    #[test]
    fn drawdown_refuses_a_non_equity_intent() {
        let doc = json!({"is_equity": false, "rules": base()["rules"], "drawdown": null,
                         "brake_active": false, "suspension_active": false});
        let reply: Value = serde_json::from_str(&json_drawdown_controls(&doc.to_string())).unwrap();
        assert_eq!(reply["error"]["kind"], json!("value"));
    }
}
