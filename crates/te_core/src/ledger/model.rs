//! The ledger's domain values and event payloads, with the validation each Python
//! dataclass runs in `__post_init__`, in the same order (docs/RUST_PORT.md P2a).
//!
//! A payload is decoded into a [`Val`] tree first (the codec), and each tagged
//! dataclass is then built from its keyword fields by the constructor below, exactly
//! as `target(**kwargs)` did. Refusals carry the Python exception's kind
//! ([`LErr::kind`]); the codec wraps `value`, `payload` and `type` refusals raised by a
//! constructor as `codec`, as the Python did (`PayloadCodecError`), and lets the rest
//! (`invalid_operation`, `unresolvable`, `overflow`) through raw.
//!
//! Python is lax about field types (an `int` in a `Decimal` field is accepted and a
//! `str` right is coerced); Rust is strict, and refuses what it cannot carry
//! faithfully. Those are the documented deviations, each refused (never altered).

use chrono::{Datelike, NaiveDate};

use super::pydec::{DKind, DecErr, PyDec};
use crate::options::pyrules;
use crate::options::{self, ContractWire, DecWire, OptionError, Right, Special};

// --- errors ----------------------------------------------------------------------------

/// A refusal: the Python exception's kind and message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LErr {
    pub kind: &'static str,
    pub msg: String,
}

pub type R<T> = Result<T, LErr>;

pub fn err<T>(kind: &'static str, msg: impl Into<String>) -> R<T> {
    Err(LErr { kind, msg: msg.into() })
}

pub fn derr(e: DecErr) -> LErr {
    match e {
        DecErr::InvalidOperation => LErr { kind: "invalid_operation", msg: String::new() },
        DecErr::DivisionByZero => LErr { kind: "division_by_zero", msg: String::new() },
        DecErr::Overflow => LErr { kind: "overflow", msg: String::new() },
        DecErr::Unsupported(s) => LErr { kind: "unsupported", msg: s.to_string() },
    }
}

pub fn oerr(e: OptionError) -> LErr {
    LErr { kind: e.kind(), msg: e.message().to_string() }
}

/// Wrap a refusal raised inside a dataclass constructor the way `_decode` did.
pub fn wrap(tag: &str, e: LErr) -> LErr {
    if matches!(e.kind, "value" | "payload" | "type") {
        LErr { kind: "codec", msg: format!("Could not rebuild {tag} from stored fields: {}", e.msg) }
    } else {
        e
    }
}

// --- enums -----------------------------------------------------------------------------

macro_rules! str_enum {
    ($name:ident, $py:literal, [$(($var:ident, $val:literal)),* $(,)?]) => {
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum $name { $($var),* }
        impl $name {
            pub const PY: &'static str = $py;
            pub fn value(self) -> &'static str { match self { $($name::$var => $val),* } }
            pub fn parse(s: &str) -> Option<$name> { match s { $($val => Some($name::$var),)* _ => None } }
        }
    };
}

str_enum!(Side, "Side", [(Buy, "BUY"), (Sell, "SELL")]);
str_enum!(
    OrderType,
    "OrderType",
    [(Market, "MARKET"), (Limit, "LIMIT"), (Stop, "STOP"), (StopLimit, "STOP_LIMIT"), (Trail, "TRAIL")]
);
str_enum!(
    Tif,
    "TimeInForce",
    [(Day, "DAY"), (Gtc, "GTC"), (Gtd, "GTD"), (Opg, "OPG"), (Moc, "MOC")]
);
str_enum!(
    OrderState,
    "OrderState",
    [
        (New, "NEW"),
        (Submitted, "SUBMITTED"),
        (Accepted, "ACCEPTED"),
        (PartiallyFilled, "PARTIALLY_FILLED"),
        (Filled, "FILLED"),
        (Cancelled, "CANCELLED"),
        (Expired, "EXPIRED"),
        (Rejected, "REJECTED"),
        (PendingUnknown, "PENDING_UNKNOWN")
    ]
);
str_enum!(
    EventKind,
    "EventKind",
    [
        (SignalSeen, "SignalSeen"),
        (RiskVerdict, "RiskVerdict"),
        (OrdersCreated, "OrdersCreated"),
        (RiskControl, "RiskControl"),
        (OrderSubmitted, "OrderSubmitted"),
        (OrderUpdated, "OrderUpdated"),
        (OrderPending, "OrderPending"),
        (OrderAccepted, "OrderAccepted"),
        (OrderRejected, "OrderRejected"),
        (OrderCancelled, "OrderCancelled"),
        (OrderRefused, "OrderRefused"),
        (OrderExpired, "OrderExpired"),
        (OrderEmulationUpdated, "OrderEmulationUpdated"),
        (Fill, "Fill"),
        (Assignment, "Assignment"),
        (Exercise, "Exercise"),
        (Expiry, "Expiry"),
        (CorporateAction, "CorporateAction"),
        (CashFlow, "CashFlow"),
        (Mark, "Mark"),
        (VenueReconcile, "VenueReconcile"),
        (VenueHaltCleared, "VenueHaltCleared"),
        (EodRun, "EodRun"),
        (MirrorQueued, "MirrorQueued"),
        (MirrorRefused, "MirrorRefused"),
        (MirrorAck, "MirrorAck"),
        (MirrorFill, "MirrorFill")
    ]
);

pub const SCHEMA_VERSION: i128 = 1;
pub const CASH_FLOW_KINDS: [&str; 6] = ["interest", "borrow", "fee", "deposit", "withdrawal", "dividend"];
pub const MIRROR_ACK_STATUSES: [&str; 3] = ["ACCEPTED", "REJECTED", "PENDING"];
pub const MIRROR_ACCOUNT_PREFIX: &str = "__venue__:";

pub fn mirror_account(venue: &str) -> R<String> {
    if venue.is_empty() {
        return err("payload", "a mirror venue must be non-empty");
    }
    Ok(format!("{MIRROR_ACCOUNT_PREFIX}{venue}"))
}

// --- dates -----------------------------------------------------------------------------

/// An aware `datetime`: local fields plus the offset in seconds. (A naive datetime never
/// reaches the model: the codec refuses it, as the Python did.)
#[derive(Debug, Clone)]
pub struct DateTime {
    pub date: NaiveDate,
    pub sod: u32,
    pub us: u32,
    pub off: i32,
}

impl DateTime {
    /// The instant: seconds since 0001-01-01 UTC, and microseconds.
    pub fn instant(&self) -> (i64, u32) {
        (
            self.date.num_days_from_ce() as i64 * 86400 + self.sod as i64 - self.off as i64,
            self.us,
        )
    }

    /// Python's aware-datetime equality: the same instant.
    pub fn same(&self, other: &DateTime) -> bool {
        self.instant() == other.instant()
    }

    /// `isoformat()`.
    pub fn iso(&self) -> String {
        let mut s = format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
            self.date.year(),
            self.date.month(),
            self.date.day(),
            self.sod / 3600,
            (self.sod % 3600) / 60,
            self.sod % 60
        );
        if self.us != 0 {
            s.push_str(&format!(".{:06}", self.us));
        }
        let a = self.off.unsigned_abs();
        s.push(if self.off < 0 { '-' } else { '+' });
        s.push_str(&format!("{:02}:{:02}", a / 3600, (a % 3600) / 60));
        if a % 60 != 0 {
            s.push_str(&format!(":{:02}", a % 60));
        }
        s
    }

    /// `astimezone(timezone.utc)`.
    pub fn to_utc(&self) -> R<DateTime> {
        let (total, us) = self.instant();
        let days = total.div_euclid(86400);
        let sod = total.rem_euclid(86400) as u32;
        let date = i32::try_from(days)
            .ok()
            .and_then(NaiveDate::from_num_days_from_ce_opt)
            .filter(|d| (1..=9999).contains(&d.year()));
        match date {
            Some(date) => Ok(DateTime { date, sod, us, off: 0 }),
            None => err("overflow", "date value out of range"),
        }
    }
}

pub fn date_iso(d: &NaiveDate) -> String {
    format!("{:04}-{:02}-{:02}", d.year(), d.month(), d.day())
}

fn ascii_digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

fn num(s: &str) -> u32 {
    s.parse().unwrap_or(0)
}

fn bad_iso(s: &str) -> LErr {
    LErr { kind: "value", msg: format!("Invalid isoformat string: {s:?}") }
}

fn make_date(y: u32, m: u32, d: u32) -> R<NaiveDate> {
    if y == 0 {
        return err("value", "year 0 is out of range");
    }
    if !(1..=12).contains(&m) {
        return err("value", "month must be in 1..12");
    }
    NaiveDate::from_ymd_opt(y as i32, m, d).map_or_else(|| err("value", "day is out of range for month"), Ok)
}

/// `(date, rest)` of an ISO date prefix: `YYYY-MM-DD` or `YYYYMMDD`.
fn date_prefix(s: &str) -> Option<(u32, u32, u32, &str)> {
    let b = s.as_bytes();
    if b.len() >= 10 && b[4] == b'-' && b[7] == b'-' && ascii_digits(&s[0..4]) && ascii_digits(&s[5..7]) && ascii_digits(&s[8..10]) {
        return Some((num(&s[0..4]), num(&s[5..7]), num(&s[8..10]), &s[10..]));
    }
    if b.len() >= 8 && ascii_digits(&s[0..8]) {
        return Some((num(&s[0..4]), num(&s[4..6]), num(&s[6..8]), &s[8..]));
    }
    None
}

/// `date.fromisoformat` (extended and basic forms; week dates are not supported).
pub fn parse_date(s: &str) -> R<NaiveDate> {
    if !s.is_ascii() {
        return Err(bad_iso(s));
    }
    if s.contains('W') {
        return err("unsupported", "ISO week dates");
    }
    match date_prefix(s) {
        Some((y, m, d, "")) => make_date(y, m, d),
        _ => Err(bad_iso(s)),
    }
}

/// `datetime.fromisoformat`: the datetime and whether it carried an offset.
pub fn parse_datetime(s: &str) -> R<(DateTime, bool)> {
    if !s.is_ascii() {
        return Err(bad_iso(s));
    }
    if s.contains('W') {
        return err("unsupported", "ISO week dates");
    }
    let Some((y, mo, d, rest)) = date_prefix(s) else {
        return Err(bad_iso(s));
    };
    let date = make_date(y, mo, d)?;
    if rest.is_empty() {
        return Ok((DateTime { date, sod: 0, us: 0, off: 0 }, false));
    }
    let time = &rest[1..];
    if time.is_empty() {
        return Err(bad_iso(s));
    }
    // split the offset off the time
    let (tpart, tz): (&str, Option<&str>) = match time.find(['+', '-']) {
        Some(i) => (&time[..i], Some(&time[i..])),
        None => match time.strip_suffix('Z') {
            Some(t) => (t, Some("Z")),
            None => (time, None),
        },
    };
    let tpart = tpart.trim_end_matches(' ');
    let (sod, us) = parse_time(tpart).ok_or_else(|| bad_iso(s))??;
    let (off, aware) = match tz {
        None => (0, false),
        Some("Z") => (0, true),
        Some(z) => (parse_offset(z).ok_or_else(|| bad_iso(s))??, true),
    };
    Ok((DateTime { date, sod, us, off }, aware))
}

/// `HH[:MM[:SS[.f]]]` or `HH[MM[SS[.f]]]`: (seconds of day, microseconds).
fn parse_time(t: &str) -> Option<R<(u32, u32)>> {
    let (main, frac) = match t.find(['.', ',']) {
        Some(i) => (&t[..i], Some(&t[i + 1..])),
        None => (t, None),
    };
    let parts: Vec<&str> = if main.contains(':') { main.split(':').collect() } else {
        match main.len() {
            2 => vec![main],
            4 => vec![&main[0..2], &main[2..4]],
            6 => vec![&main[0..2], &main[2..4], &main[4..6]],
            _ => return None,
        }
    };
    if parts.is_empty() || parts.len() > 3 || !parts.iter().all(|p| p.len() == 2 && ascii_digits(p)) {
        return None;
    }
    if frac.is_some() && parts.len() != 3 {
        return None;
    }
    let h = num(parts[0]);
    let mi = parts.get(1).map_or(0, |p| num(p));
    let se = parts.get(2).map_or(0, |p| num(p));
    let us = match frac {
        None => 0,
        Some(f) => {
            if !ascii_digits(f) {
                return None;
            }
            let mut f6: String = f.chars().take(6).collect();
            while f6.len() < 6 {
                f6.push('0');
            }
            num(&f6)
        }
    };
    if h > 23 {
        return Some(err("value", "hour must be in 0..23"));
    }
    if mi > 59 {
        return Some(err("value", "minute must be in 0..59"));
    }
    if se > 59 {
        return Some(err("value", "second must be in 0..59"));
    }
    Some(Ok((h * 3600 + mi * 60 + se, us)))
}

/// `±HH[:MM[:SS]]` or `±HH[MM[SS]]`, in seconds.
fn parse_offset(z: &str) -> Option<R<i32>> {
    let sign = if z.starts_with('-') { -1 } else { 1 };
    let body = &z[1..];
    if body.contains('.') {
        return Some(err("unsupported", "a sub-second UTC offset"));
    }
    let parts: Vec<&str> = if body.contains(':') { body.split(':').collect() } else {
        match body.len() {
            2 => vec![body],
            4 => vec![&body[0..2], &body[2..4]],
            6 => vec![&body[0..2], &body[2..4], &body[4..6]],
            _ => return None,
        }
    };
    if parts.is_empty() || parts.len() > 3 || !parts.iter().all(|p| p.len() == 2 && ascii_digits(p)) {
        return None;
    }
    let h = num(parts[0]) as i32;
    let mi = parts.get(1).map_or(0, |p| num(p)) as i32;
    let se = parts.get(2).map_or(0, |p| num(p)) as i32;
    if mi > 59 || se > 59 {
        return Some(err("value", "minute must be in 0..59"));
    }
    let off = sign * (h * 3600 + mi * 60 + se);
    if off.abs() >= 86400 {
        return Some(err("value", "offset must be a timedelta strictly between -timedelta(hours=24) and timedelta(hours=24)"));
    }
    Some(Ok(off))
}

// --- values ----------------------------------------------------------------------------

/// A decoded JSON node: what `_decode` returned.
#[derive(Debug, Clone)]
pub enum Val {
    None,
    Bool(bool),
    Int(i128),
    Str(String),
    Dec(PyDec),
    DateTime(DateTime),
    Date(NaiveDate),
    Side(Side),
    Right(Right),
    OrderType(OrderType),
    OrderState(OrderState),
    Tif(Tif),
    Kind(EventKind),
    Tuple(Vec<Val>),
    /// A mapping: a repeated key keeps its first position and its last value.
    Map(Vec<(String, Val)>),
    Obj(Box<Obj>),
}

impl Val {
    pub fn type_name(&self) -> &'static str {
        match self {
            Val::None => "NoneType",
            Val::Bool(_) => "bool",
            Val::Int(_) => "int",
            Val::Str(_) => "str",
            Val::Dec(_) => "Decimal",
            Val::DateTime(_) => "datetime",
            Val::Date(_) => "date",
            Val::Side(_) => "Side",
            Val::Right(_) => "OptionRight",
            Val::OrderType(_) => "OrderType",
            Val::OrderState(_) => "OrderState",
            Val::Tif(_) => "TimeInForce",
            Val::Kind(_) => "EventKind",
            Val::Tuple(_) => "tuple",
            Val::Map(_) => "mappingproxy",
            Val::Obj(o) => o.tag(),
        }
    }
}

// --- instruments -----------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct OptionContract {
    pub underlying: String,
    pub expiry: NaiveDate,
    pub strike: PyDec,
    pub right: Right,
    pub multiplier: i128,
}

#[derive(Debug, Clone)]
pub struct ComboLeg {
    /// An `OptionContract` or an `Equity`.
    pub contract: Instrument,
    pub ratio: i128,
    pub side: Side,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ContractMonth {
    pub year: i32,
    pub month: u32,
}

impl ContractMonth {
    pub fn new(year: i32, month: u32) -> R<Self> {
        if !(1..=12).contains(&month) {
            return err("value", format!("Month must be 1..=12, got {month}"));
        }
        if !(1900..=2100).contains(&year) {
            return err("value", format!("Year {year} out of range (1900..=2100)"));
        }
        Ok(Self { year, month })
    }

    pub fn month_code(&self) -> char {
        match self.month {
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
}

#[derive(Debug, Clone)]
pub struct FutureContract {
    pub root: String,
    pub contract_month: Option<ContractMonth>,
    pub tick_size: PyDec,
    pub point_value: PyDec,
}

impl FutureContract {
    pub fn same(&self, o: &FutureContract) -> bool {
        self.root == o.root
            && self.contract_month == o.contract_month
            && dec_eq(&self.tick_size, &o.tick_size)
            && dec_eq(&self.point_value, &o.point_value)
    }

    pub fn symbol(&self) -> String {
        match &self.contract_month {
            None => self.root.clone(),
            Some(cm) => format!("{}{}{:02}", self.root, cm.month_code(), cm.year.rem_euclid(100)),
        }
    }
}

#[derive(Debug, Clone)]
pub enum Instrument {
    Equity(String),
    Option(OptionContract),
    Combo(Vec<ComboLeg>),
    Future(FutureContract),
}

pub fn dec_eq(a: &PyDec, b: &PyDec) -> bool {
    a.eq_num(b).unwrap_or(false)
}

pub fn opt_dec_eq(a: &Option<PyDec>, b: &Option<PyDec>) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(x), Some(y)) => dec_eq(x, y),
        _ => false,
    }
}

pub fn decwire(d: &PyDec) -> DecWire {
    let special = match d.kind() {
        DKind::Finite => Special::Finite,
        DKind::Inf => Special::Inf,
        DKind::QNan => Special::QuietNan,
        DKind::SNan => Special::SignalingNan,
    };
    DecWire {
        neg: d.is_negative(),
        digits: d.coefficient().to_string(),
        exp: if d.is_finite() { d.exponent() } else { 0 },
        special,
        text: d.to_py_string(),
    }
}

impl OptionContract {
    pub fn wire(&self) -> ContractWire {
        ContractWire {
            underlying: self.underlying.clone(),
            year: self.expiry.year(),
            month: self.expiry.month(),
            day: self.expiry.day(),
            right: self.right,
            strike: decwire(&self.strike),
        }
    }

    pub fn occ(&self) -> R<String> {
        self.wire().occ().map_err(oerr)
    }

    pub fn same(&self, o: &OptionContract) -> bool {
        self.underlying == o.underlying
            && self.expiry == o.expiry
            && dec_eq(&self.strike, &o.strike)
            && self.right == o.right
            && self.multiplier == o.multiplier
    }
}

impl Instrument {
    /// `==` as the frozen dataclasses define it.
    pub fn same(&self, o: &Instrument) -> bool {
        match (self, o) {
            (Instrument::Equity(a), Instrument::Equity(b)) => a == b,
            (Instrument::Option(a), Instrument::Option(b)) => a.same(b),
            (Instrument::Combo(a), Instrument::Combo(b)) => {
                a.len() == b.len() && a.iter().zip(b).all(|(x, y)| x.ratio == y.ratio && x.side == y.side && x.contract.same(&y.contract))
            }
            (Instrument::Future(a), Instrument::Future(b)) => a.same(b),
            _ => false,
        }
    }

    /// A string two instruments share exactly when `==` (and so `hash`) says they are one.
    pub fn hk(&self) -> String {
        match self {
            Instrument::Equity(s) => format!("E\u{1}{s}"),
            Instrument::Option(c) => format!(
                "O\u{1}{}\u{1}{}\u{1}{}\u{1}{}\u{1}{}",
                c.underlying,
                date_iso(&c.expiry),
                c.strike.num_key(),
                c.right.code(),
                c.multiplier
            ),
            Instrument::Combo(legs) => {
                let mut s = String::from("C");
                for l in legs {
                    s.push_str(&format!("\u{2}{}\u{1}{}\u{1}{}", l.ratio, l.side.value(), l.contract.hk()));
                }
                s
            }
            Instrument::Future(f) => {
                let m_str = match &f.contract_month {
                    Some(m) => format!("{:04}-{:02}", m.year, m.month),
                    None => String::new(),
                };
                format!(
                    "F\u{1}{}\u{1}{}\u{1}{}\u{1}{}",
                    f.root,
                    m_str,
                    f.tick_size.num_key(),
                    f.point_value.num_key()
                )
            }
        }
    }

    /// `.symbol`.
    pub fn symbol(&self) -> R<String> {
        match self {
            Instrument::Equity(s) => Ok(s.clone()),
            Instrument::Option(c) => c.occ(),
            Instrument::Combo(legs) => {
                let mut parts = Vec::new();
                for l in legs {
                    parts.push(format!("{}:{}x{}", l.side.value(), l.ratio, pyrules::strip(&l.contract.symbol()?)));
                }
                Ok(parts.join("/"))
            }
            Instrument::Future(f) => Ok(f.symbol()),
        }
    }

    pub fn tag(&self) -> &'static str {
        match self {
            Instrument::Equity(_) => "Equity",
            Instrument::Option(_) => "OptionContract",
            Instrument::Combo(_) => "Combo",
            Instrument::Future(_) => "Future",
        }
    }
}

// --- the dataclasses -------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct Order {
    pub order_id: String,
    pub account_id: String,
    pub instrument: Instrument,
    pub order_type: OrderType,
    pub side: Side,
    pub quantity: PyDec,
    pub command_id: String,
    pub created_at: DateTime,
    pub limit_price: Option<PyDec>,
    pub stop_price: Option<PyDec>,
    pub trail_amount: Option<PyDec>,
    pub tif: Tif,
    pub state: OrderState,
    pub parent_order_id: Option<String>,
    pub oco_group: Option<String>,
}

impl Order {
    /// Dataclass `==`.
    pub fn same(&self, o: &Order) -> bool {
        self.order_id == o.order_id
            && self.account_id == o.account_id
            && self.instrument.same(&o.instrument)
            && self.order_type == o.order_type
            && self.side == o.side
            && dec_eq(&self.quantity, &o.quantity)
            && self.command_id == o.command_id
            && self.created_at.same(&o.created_at)
            && opt_dec_eq(&self.limit_price, &o.limit_price)
            && opt_dec_eq(&self.stop_price, &o.stop_price)
            && opt_dec_eq(&self.trail_amount, &o.trail_amount)
            && self.tif == o.tif
            && self.state == o.state
            && self.parent_order_id == o.parent_order_id
            && self.oco_group == o.oco_group
    }
}

#[derive(Debug, Clone)]
pub struct Fill {
    pub fill_id: String,
    pub order_id: String,
    pub account_id: String,
    pub instrument: Instrument,
    pub quantity: PyDec,
    pub price: PyDec,
    pub venue_env: String,
    pub filled_at: DateTime,
    pub side: Side,
    pub fee: PyDec,
    pub leg_id: Option<String>,
    pub venue_order_id: Option<String>,
    pub venue_execution_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Lot {
    pub lot_id: String,
    pub quantity: PyDec,
    pub cost_basis: PyDec,
    pub acquired_at: DateTime,
    pub side: Side,
}

#[derive(Debug, Clone)]
pub struct RiskRuleResult {
    pub rule_name: String,
    pub passed: bool,
    pub measured_value: Val,
    pub threshold: Val,
    pub reason: String,
}

#[derive(Debug, Clone)]
pub struct RiskVerdict {
    pub order_intent_id: String,
    pub accepted: bool,
    pub evaluations: Vec<RiskRuleResult>,
    pub refusal_reasons: Vec<String>,
    pub approved_quantity: Option<PyDec>,
}

#[derive(Debug, Clone)]
pub struct RiskControlChange {
    pub control_id: String,
    pub enabled: bool,
    pub reason: String,
    pub changed_at: DateTime,
}

#[derive(Debug, Clone)]
pub struct Signal {
    pub signal_id: String,
    pub scan_id: String,
    pub symbol: String,
    pub session_date: NaiveDate,
    pub direction: String,
    pub metrics: Vec<(String, Val)>,
    pub next_earnings_date: Option<NaiveDate>,
    pub created_at: Option<DateTime>,
}

#[derive(Debug, Clone)]
pub struct CorporateAction {
    pub symbol: String,
    pub action_type: String,
    pub effective_date: NaiveDate,
    pub as_of: DateTime,
    pub details: Vec<(String, Val)>,
}

#[derive(Debug, Clone)]
pub struct OrderStateChange {
    pub order_id: String,
    pub reason: Option<String>,
    pub venue_order_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct OrdersCreated {
    pub orders: Vec<Order>,
    pub fingerprint: String,
    pub reason: String,
}

#[derive(Debug, Clone)]
pub struct OrderUpdated {
    pub order: Order,
    pub reason: String,
    pub venue_order_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct EmulatedOrderState {
    pub order_id: String,
    pub observed_price: Option<PyDec>,
    pub extreme: Option<PyDec>,
    pub stop_price: Option<PyDec>,
    pub triggered: bool,
    pub reason: String,
}

#[derive(Debug, Clone)]
pub struct CashFlow {
    pub amount: PyDec,
    pub kind: String,
    pub as_of: DateTime,
    pub note: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Mark {
    pub instrument: Instrument,
    pub price: PyDec,
    pub as_of: DateTime,
    pub source: Option<String>,
}

#[derive(Debug, Clone)]
pub struct VenueReconcile {
    pub venue: String,
    pub as_of: DateTime,
    pub reconciled: bool,
    pub drift: Vec<String>,
    pub note: Option<String>,
}

#[derive(Debug, Clone)]
pub struct VenueHaltCleared {
    pub venue: String,
    pub at: DateTime,
    pub reason: String,
    pub reconcile_seq: i128,
}

#[derive(Debug, Clone)]
pub struct OptionLifecycle {
    pub account_id: String,
    pub contract: OptionContract,
    pub quantity: PyDec,
    pub held: Side,
    pub underlying_price: PyDec,
    pub price_source: String,
    pub as_of: DateTime,
    pub reason: String,
    pub early: bool,
}

#[derive(Debug, Clone)]
pub struct EodRun {
    pub session: NaiveDate,
    pub job: String,
    pub account_id: String,
    pub bars_processed: i128,
    pub at_close: DateTime,
}

#[derive(Debug, Clone)]
pub struct MirrorAllocation {
    pub strategy_order_id: String,
    pub strategy_account: String,
    pub quantity: PyDec,
}

#[derive(Debug, Clone)]
pub struct MirrorQueued {
    pub venue: String,
    pub ticket_key: String,
    pub instrument: Instrument,
    pub side: Side,
    pub quantity: PyDec,
    pub order_type: OrderType,
    pub limit_price: Option<PyDec>,
    pub tif: Tif,
    pub allocations: Vec<MirrorAllocation>,
    pub at: DateTime,
}

impl MirrorQueued {
    /// Dataclass `==`.
    pub fn same(&self, o: &MirrorQueued) -> bool {
        self.venue == o.venue
            && self.ticket_key == o.ticket_key
            && self.instrument.same(&o.instrument)
            && self.side == o.side
            && dec_eq(&self.quantity, &o.quantity)
            && self.order_type == o.order_type
            && opt_dec_eq(&self.limit_price, &o.limit_price)
            && self.tif == o.tif
            && self.allocations.len() == o.allocations.len()
            && self.allocations.iter().zip(&o.allocations).all(|(a, b)| {
                a.strategy_order_id == b.strategy_order_id
                    && a.strategy_account == b.strategy_account
                    && dec_eq(&a.quantity, &b.quantity)
            })
            && self.at.same(&o.at)
    }
}

#[derive(Debug, Clone)]
pub struct MirrorRefused {
    pub venue: String,
    pub strategy_order_id: String,
    pub strategy_account: String,
    pub reason: String,
    pub at: DateTime,
}

#[derive(Debug, Clone)]
pub struct MirrorAck {
    pub venue: String,
    pub ticket_key: String,
    pub status: String,
    pub message: String,
    pub at: DateTime,
    pub venue_order_id: Option<String>,
    pub book_status: Option<OrderState>,
}

#[derive(Debug, Clone)]
pub struct MirrorFill {
    pub venue: String,
    pub ticket_key: String,
    pub venue_order_id: String,
    pub filled: PyDec,
    pub avg_price: PyDec,
    pub at: DateTime,
}

/// Every tagged dataclass the codec knows.
#[derive(Debug, Clone)]
pub enum Obj {
    Instr(Instrument),
    ComboLeg(ComboLeg),
    Fill(Fill),
    Lot(Lot),
    Signal(Signal),
    RiskVerdict(RiskVerdict),
    RiskControl(RiskControlChange),
    RiskRule(RiskRuleResult),
    Order(Order),
    StateChange(OrderStateChange),
    OrdersCreated(OrdersCreated),
    OrderUpdated(OrderUpdated),
    Emulated(EmulatedOrderState),
    CashFlow(CashFlow),
    Mark(Mark),
    Reconcile(VenueReconcile),
    HaltCleared(VenueHaltCleared),
    EodRun(EodRun),
    Lifecycle(OptionLifecycle),
    Corp(CorporateAction),
    MAlloc(MirrorAllocation),
    MQueued(MirrorQueued),
    MRefused(MirrorRefused),
    MAck(MirrorAck),
    MFill(MirrorFill),
}

impl Obj {
    /// The class name (and codec tag).
    pub fn tag(&self) -> &'static str {
        match self {
            Obj::Instr(i) => i.tag(),
            Obj::ComboLeg(_) => "ComboLeg",
            Obj::Fill(_) => "Fill",
            Obj::Lot(_) => "Lot",
            Obj::Signal(_) => "Signal",
            Obj::RiskVerdict(_) => "RiskVerdict",
            Obj::RiskControl(_) => "RiskControlChange",
            Obj::RiskRule(_) => "RiskRuleResult",
            Obj::Order(_) => "Order",
            Obj::StateChange(_) => "OrderStateChange",
            Obj::OrdersCreated(_) => "OrdersCreated",
            Obj::OrderUpdated(_) => "OrderUpdated",
            Obj::Emulated(_) => "EmulatedOrderState",
            Obj::CashFlow(_) => "CashFlow",
            Obj::Mark(_) => "Mark",
            Obj::Reconcile(_) => "VenueReconcile",
            Obj::HaltCleared(_) => "VenueHaltCleared",
            Obj::EodRun(_) => "EodRun",
            Obj::Lifecycle(_) => "OptionLifecycle",
            Obj::Corp(_) => "CorporateAction",
            Obj::MAlloc(_) => "MirrorAllocation",
            Obj::MQueued(_) => "MirrorQueued",
            Obj::MRefused(_) => "MirrorRefused",
            Obj::MAck(_) => "MirrorAck",
            Obj::MFill(_) => "MirrorFill",
        }
    }
}

// --- keyword binding and field extraction --------------------------------------------

/// The keyword fields of a dataclass being rebuilt (`target(**kwargs)`).
pub struct Kw {
    tag: &'static str,
    f: Vec<(String, Val)>,
}

impl Kw {
    /// Bind like Python: an unexpected or missing keyword is a `TypeError` before the
    /// body (and so the validation) runs.
    pub fn bind(tag: &'static str, f: Vec<(String, Val)>, spec: &[(&str, bool)]) -> R<Kw> {
        for (k, _) in &f {
            if !spec.iter().any(|(n, _)| n == k) {
                return err("type", format!("{tag}() got an unexpected keyword argument '{k}'"));
            }
        }
        for (n, required) in spec {
            if *required && !f.iter().any(|(k, _)| k == n) {
                return err("type", format!("{tag}() missing required argument: '{n}'"));
            }
        }
        Ok(Kw { tag, f })
    }

    fn take(&mut self, name: &str) -> Option<Val> {
        self.f.iter().position(|(k, _)| k == name).map(|i| self.f.remove(i).1)
    }

    fn req(&mut self, name: &str) -> Val {
        self.take(name).unwrap_or(Val::None)
    }
}

/// A field of a type the Rust model does not carry. Python is lax here (an int in a
/// Decimal field, a str for a right) and its outcome varies by field, so this is its own
/// kind, `strict`: never wrapped, and the parity tests count it as the one sanctioned
/// asymmetry (Rust refuses; it never accepts what Python refuses).
fn bad_type<T>(what: &str, name: &str, v: &Val) -> R<T> {
    err("strict", format!("{name} must be {what}, got {}", v.type_name()))
}

fn x_str(v: Val, n: &str) -> R<String> {
    match v {
        Val::Str(s) => Ok(s),
        o => bad_type("a str", n, &o),
    }
}
fn x_ostr(v: Val, n: &str) -> R<Option<String>> {
    match v {
        Val::None => Ok(None),
        Val::Str(s) => Ok(Some(s)),
        o => bad_type("a str or None", n, &o),
    }
}
fn x_bool(v: Val, n: &str) -> R<bool> {
    match v {
        Val::Bool(b) => Ok(b),
        o => bad_type("a bool", n, &o),
    }
}
fn x_int(v: Val, n: &str) -> R<i128> {
    match v {
        Val::Int(i) => Ok(i),
        o => bad_type("an int", n, &o),
    }
}
fn x_dec(v: Val, n: &str) -> R<PyDec> {
    match v {
        Val::Dec(d) => Ok(d),
        o => bad_type("a Decimal", n, &o),
    }
}
fn x_odec(v: Val, n: &str) -> R<Option<PyDec>> {
    match v {
        Val::None => Ok(None),
        Val::Dec(d) => Ok(Some(d)),
        o => bad_type("a Decimal or None", n, &o),
    }
}
fn x_dt(v: Val, n: &str) -> R<DateTime> {
    match v {
        Val::DateTime(d) => Ok(d),
        o => bad_type("a datetime", n, &o),
    }
}
fn x_odt(v: Val, n: &str) -> R<Option<DateTime>> {
    match v {
        Val::None => Ok(None),
        Val::DateTime(d) => Ok(Some(d)),
        o => bad_type("a datetime or None", n, &o),
    }
}
fn x_date(v: Val, n: &str) -> R<NaiveDate> {
    match v {
        Val::Date(d) => Ok(d),
        o => bad_type("a date", n, &o),
    }
}
fn x_odate(v: Val, n: &str) -> R<Option<NaiveDate>> {
    match v {
        Val::None => Ok(None),
        Val::Date(d) => Ok(Some(d)),
        o => bad_type("a date or None", n, &o),
    }
}
fn x_side(v: Val, n: &str) -> R<Side> {
    match v {
        Val::Side(s) => Ok(s),
        o => bad_type("a Side", n, &o),
    }
}
fn x_instr(v: Val, n: &str) -> R<Instrument> {
    match v {
        Val::Obj(o) => match *o {
            Obj::Instr(i) => Ok(i),
            other => bad_type("an Instrument", n, &Val::Obj(Box::new(other))),
        },
        o => bad_type("an Instrument", n, &o),
    }
}
fn x_tuple(v: Val, n: &str) -> R<Vec<Val>> {
    match v {
        Val::Tuple(t) => Ok(t),
        o => bad_type("a tuple", n, &o),
    }
}
fn x_strs(v: Val, n: &str) -> R<Vec<String>> {
    x_tuple(v, n)?.into_iter().map(|e| x_str(e, n)).collect()
}
fn x_map(v: Val, n: &str) -> R<Vec<(String, Val)>> {
    match v {
        Val::None => Ok(Vec::new()),
        Val::Map(m) => Ok(m),
        o => bad_type("a mapping", n, &o),
    }
}

/// `_as_decimal`: `Decimal(str(value))` unless it already is one; non-finite refuses.
pub fn as_decimal(v: Val, name: &str) -> R<PyDec> {
    let d = match v {
        Val::Dec(d) => d,
        Val::Int(i) => PyDec::from_i128(i),
        Val::Str(s) => match PyDec::parse(&s) {
            Some(d) => d,
            None => return err("invalid_operation", ""),
        },
        _ => return err("invalid_operation", ""),
    };
    if !d.is_finite() {
        return err("payload", format!("{name} must be finite, got {}", d.to_py_string()));
    }
    Ok(d)
}

fn as_odecimal(v: Val, name: &str) -> R<Option<PyDec>> {
    match v {
        Val::None => Ok(None),
        o => as_decimal(o, name).map(Some),
    }
}

fn nonempty(s: &str, msg: &str) -> R<()> {
    if s.is_empty() {
        return err("value", msg);
    }
    Ok(())
}

fn pnonempty(s: &str, msg: &str) -> R<()> {
    if s.is_empty() {
        return err("payload", msg);
    }
    Ok(())
}

/// `x <= 0`, `x < 0`, `x > 0` against zero (a NaN raises `invalid_operation`).
pub fn cmp0(d: &PyDec) -> R<std::cmp::Ordering> {
    d.cmp_int(0).map_err(derr)
}
fn le0(d: &PyDec) -> R<bool> {
    Ok(cmp0(d)? != std::cmp::Ordering::Greater)
}
fn lt0(d: &PyDec) -> R<bool> {
    Ok(cmp0(d)? == std::cmp::Ordering::Less)
}

/// `str.isdigit()` over the decimal digits (ASCII and the other Nd scripts).
pub fn py_isdigit(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_digit() || pyrules::digit(c).is_some())
}

// --- constructors ----------------------------------------------------------------------

pub fn make_equity(symbol: &str) -> R<Instrument> {
    options::equity_symbol(Some(symbol)).map(Instrument::Equity).map_err(oerr)
}

pub fn make_future(symbol: &str) -> R<Instrument> {
    crate::sim::tick::parse_future(symbol)
}

pub fn make_option(
    underlying: &str,
    expiry: NaiveDate,
    strike: Val,
    multiplier: i128,
    right: Val,
) -> R<OptionContract> {
    let und = options::validate_underlying(underlying).map_err(oerr)?;
    // `Decimal(str(strike))` for anything that is not already a Decimal
    let strike = match strike {
        Val::Dec(d) => d,
        Val::Int(i) => PyDec::from_i128(i),
        Val::Str(s) => PyDec::parse(&s).map_or_else(|| err("invalid_operation", ""), Ok)?,
        _ => return err("invalid_operation", ""),
    };
    let wire = decwire(&strike);
    let mtext = multiplier.to_string();
    let (rstr, rtext): (Option<String>, String) = match &right {
        Val::Right(r) => (Some(r.code().to_string()), r.code().to_string()),
        Val::Str(s) => (Some(s.clone()), s.clone()),
        other => (None, format!("{other:?}")),
    };
    let r = options::validate_rest(&wire, Some(&mtext), &mtext, rstr.as_deref(), &rtext).map_err(oerr)?;
    Ok(OptionContract { underlying: und, expiry, strike, right: r, multiplier })
}

pub fn build_option(f: Vec<(String, Val)>) -> R<Instrument> {
    let mut k = Kw::bind(
        "OptionContract",
        f,
        &[("underlying", true), ("expiry", true), ("strike", true), ("right", true), ("multiplier", false)],
    )?;
    let underlying = x_str(k.req("underlying"), "underlying")?;
    let expiry = x_date(k.req("expiry"), "expiry")?;
    let strike = k.req("strike");
    let right = k.req("right");
    let multiplier = match k.take("multiplier") {
        None => 100,
        Some(v) => x_int(v, "multiplier")?,
    };
    make_option(&underlying, expiry, strike, multiplier, right).map(Instrument::Option)
}

pub fn build_equity(f: Vec<(String, Val)>) -> R<Instrument> {
    let mut k = Kw::bind("Equity", f, &[("symbol", true)])?;
    let s = k.req("symbol");
    match s {
        Val::Str(s) => make_equity(&s),
        // a non-str (a StrEnum member is one in Python) is not carried
        other => bad_type("a str", "symbol", &other),
    }
}

pub fn build_combo(f: Vec<(String, Val)>) -> R<Instrument> {
    let mut k = Kw::bind("Combo", f, &[("legs", true)])?;
    let legs = x_tuple(k.req("legs"), "legs")?;
    if legs.is_empty() {
        return err("value", "Combo must have at least one leg");
    }
    let mut out = Vec::new();
    for l in legs {
        match l {
            Val::Obj(o) => match *o {
                Obj::ComboLeg(l) => out.push(l),
                other => return bad_type("a ComboLeg", "legs", &Val::Obj(Box::new(other))),
            },
            o => return bad_type("a ComboLeg", "legs", &o),
        }
    }
    Ok(Instrument::Combo(out))
}

pub fn build_combo_leg(f: Vec<(String, Val)>) -> R<ComboLeg> {
    let mut k = Kw::bind("ComboLeg", f, &[("contract", true), ("ratio", true), ("side", true)])?;
    let contract = x_instr(k.req("contract"), "contract")?;
    if matches!(contract, Instrument::Combo(_)) {
        return err("value", "Combo leg contract must be OptionContract or Equity, got <class 'Combo'>");
    }
    let ratio = x_int(k.req("ratio"), "ratio")?;
    if ratio <= 0 {
        return err("value", format!("Combo leg ratio must be positive, got {ratio}"));
    }
    let side = x_side(k.req("side"), "side")?;
    Ok(ComboLeg { contract, ratio, side })
}

/// `validate_order_prices`.
pub fn validate_order_prices(
    ot: OrderType,
    limit: &Option<PyDec>,
    stop: &Option<PyDec>,
    trail: &Option<PyDec>,
) -> R<()> {
    let v = |m: &str| err("value", m);
    match ot {
        OrderType::Market => {
            if limit.is_some() {
                return v("MARKET order cannot have a limit_price");
            }
            if stop.is_some() {
                return v("MARKET order cannot have a stop_price");
            }
            if trail.is_some() {
                return v("MARKET order cannot have a trail_amount");
            }
        }
        OrderType::Limit => {
            if limit.is_none() {
                return v("LIMIT order must have a limit_price");
            }
            if stop.is_some() {
                return v("LIMIT order cannot have a stop_price");
            }
            if trail.is_some() {
                return v("LIMIT order cannot have a trail_amount");
            }
        }
        OrderType::Stop => {
            if stop.is_none() {
                return v("STOP order must have a stop_price");
            }
            if limit.is_some() {
                return v("STOP order cannot have a limit_price");
            }
            if trail.is_some() {
                return v("STOP order cannot have a trail_amount");
            }
        }
        OrderType::StopLimit => {
            if limit.is_none() || stop.is_none() {
                return v("STOP_LIMIT order must have both limit_price and stop_price");
            }
            if trail.is_some() {
                return v("STOP_LIMIT order cannot have a trail_amount");
            }
        }
        OrderType::Trail => {
            let bad = match trail {
                None => true,
                Some(t) => le0(t)?,
            };
            if bad {
                return v("TRAIL order must have a positive trail_amount (I5)");
            }
            if limit.is_some() {
                return v("TRAIL order cannot have a limit_price");
            }
            if stop.is_some() {
                return v("TRAIL order cannot have a stop_price");
            }
        }
    }
    if let Some(l) = limit {
        if le0(l)? {
            return err("value", format!("limit_price must be positive, got {}", l.to_py_string()));
        }
    }
    if let Some(s) = stop {
        if le0(s)? {
            return err("value", format!("stop_price must be positive, got {}", s.to_py_string()));
        }
    }
    Ok(())
}

pub fn build_order(f: Vec<(String, Val)>) -> R<Order> {
    let mut k = Kw::bind(
        "Order",
        f,
        &[
            ("order_id", true),
            ("account_id", true),
            ("instrument", true),
            ("order_type", true),
            ("side", true),
            ("quantity", true),
            ("command_id", true),
            ("created_at", true),
            ("limit_price", false),
            ("stop_price", false),
            ("trail_amount", false),
            ("tif", false),
            ("state", false),
            ("parent_order_id", false),
            ("oco_group", false),
        ],
    )?;
    let order_id = x_str(k.req("order_id"), "order_id")?;
    let account_id = x_str(k.req("account_id"), "account_id")?;
    let instrument = x_instr(k.req("instrument"), "instrument")?;
    let order_type = match k.req("order_type") {
        Val::OrderType(t) => t,
        o => return bad_type("an OrderType", "order_type", &o),
    };
    let side = x_side(k.req("side"), "side")?;
    let quantity = x_dec(k.req("quantity"), "quantity")?;
    let command_id = x_str(k.req("command_id"), "command_id")?;
    let created_at = x_dt(k.req("created_at"), "created_at")?;
    let limit_price = k.take("limit_price").map_or(Ok(None), |v| x_odec(v, "limit_price"))?;
    let stop_price = k.take("stop_price").map_or(Ok(None), |v| x_odec(v, "stop_price"))?;
    let trail_amount = k.take("trail_amount").map_or(Ok(None), |v| x_odec(v, "trail_amount"))?;
    let tif = match k.take("tif") {
        None => Tif::Day,
        Some(Val::Tif(t)) => t,
        Some(o) => return bad_type("a TimeInForce", "tif", &o),
    };
    let state = match k.take("state") {
        None => OrderState::New,
        Some(Val::OrderState(s)) => s,
        Some(o) => return bad_type("an OrderState", "state", &o),
    };
    let parent_order_id = k.take("parent_order_id").map_or(Ok(None), |v| x_ostr(v, "parent_order_id"))?;
    let oco_group = k.take("oco_group").map_or(Ok(None), |v| x_ostr(v, "oco_group"))?;
    // __post_init__
    nonempty(&order_id, "order_id must be non-empty")?;
    nonempty(&account_id, "account_id must be non-empty")?;
    nonempty(&command_id, "command_id must be non-empty (I3)")?;
    if le0(&quantity)? {
        return err("value", format!("Order quantity must be positive, got {} (I5)", quantity.to_py_string()));
    }
    validate_order_prices(order_type, &limit_price, &stop_price, &trail_amount)?;
    Ok(Order {
        order_id,
        account_id,
        instrument,
        order_type,
        side,
        quantity,
        command_id,
        created_at,
        limit_price,
        stop_price,
        trail_amount,
        tif,
        state,
        parent_order_id,
        oco_group,
    })
}

pub fn build_fill(f: Vec<(String, Val)>) -> R<Fill> {
    let mut k = Kw::bind(
        "Fill",
        f,
        &[
            ("fill_id", true),
            ("order_id", true),
            ("account_id", true),
            ("instrument", true),
            ("quantity", true),
            ("price", true),
            ("venue_env", true),
            ("filled_at", true),
            ("side", true),
            ("fee", false),
            ("leg_id", false),
            ("venue_order_id", false),
            ("venue_execution_id", false),
        ],
    )?;
    let fill_id = x_str(k.req("fill_id"), "fill_id")?;
    let order_id = x_str(k.req("order_id"), "order_id")?;
    let account_id = x_str(k.req("account_id"), "account_id")?;
    let instrument = x_instr(k.req("instrument"), "instrument")?;
    let quantity = x_dec(k.req("quantity"), "quantity")?;
    let price = x_dec(k.req("price"), "price")?;
    let venue_env = x_str(k.req("venue_env"), "venue_env")?;
    let filled_at = x_dt(k.req("filled_at"), "filled_at")?;
    let side = x_side(k.req("side"), "side")?;
    let fee = match k.take("fee") {
        None => PyDec::zero(),
        Some(v) => x_dec(v, "fee")?,
    };
    let leg_id = k.take("leg_id").map_or(Ok(None), |v| x_ostr(v, "leg_id"))?;
    let venue_order_id = k.take("venue_order_id").map_or(Ok(None), |v| x_ostr(v, "venue_order_id"))?;
    let venue_execution_id = k.take("venue_execution_id").map_or(Ok(None), |v| x_ostr(v, "venue_execution_id"))?;
    nonempty(&fill_id, "fill_id must be non-empty")?;
    nonempty(&order_id, "order_id must be non-empty")?;
    nonempty(&account_id, "account_id must be non-empty")?;
    if le0(&quantity)? {
        return err("value", format!("Fill quantity must be strictly positive, got {}", quantity.to_py_string()));
    }
    if le0(&price)? {
        return err("value", format!("Fill price must be strictly positive, got {} (I5)", price.to_py_string()));
    }
    if !matches!(venue_env.as_str(), "sim" | "paper" | "live") {
        return err("value", format!("Invalid venue_env '{venue_env}'"));
    }
    Ok(Fill {
        fill_id,
        order_id,
        account_id,
        instrument,
        quantity,
        price,
        venue_env,
        filled_at,
        side,
        fee,
        leg_id,
        venue_order_id,
        venue_execution_id,
    })
}

/// `Lot.__post_init__`, shared by the decoder and the fold (which builds lots too).
pub fn make_lot(lot_id: String, quantity: PyDec, cost_basis: PyDec, acquired_at: DateTime, side: Side) -> R<Lot> {
    nonempty(&lot_id, "lot_id must be non-empty")?;
    if le0(&quantity)? {
        return err("value", format!("Lot quantity must be positive, got {}", quantity.to_py_string()));
    }
    if le0(&cost_basis)? {
        return err("value", format!("Lot cost_basis must be positive, got {}", cost_basis.to_py_string()));
    }
    Ok(Lot { lot_id, quantity, cost_basis, acquired_at, side })
}

pub fn build_lot(f: Vec<(String, Val)>) -> R<Lot> {
    let mut k = Kw::bind(
        "Lot",
        f,
        &[("lot_id", true), ("quantity", true), ("cost_basis", true), ("acquired_at", true), ("side", true)],
    )?;
    let lot_id = x_str(k.req("lot_id"), "lot_id")?;
    let quantity = x_dec(k.req("quantity"), "quantity")?;
    let cost_basis = x_dec(k.req("cost_basis"), "cost_basis")?;
    let acquired_at = x_dt(k.req("acquired_at"), "acquired_at")?;
    let side = x_side(k.req("side"), "side")?;
    make_lot(lot_id, quantity, cost_basis, acquired_at, side)
}

pub fn build_risk_rule(f: Vec<(String, Val)>) -> R<RiskRuleResult> {
    let mut k = Kw::bind(
        "RiskRuleResult",
        f,
        &[("rule_name", true), ("passed", true), ("measured_value", true), ("threshold", true), ("reason", true)],
    )?;
    let rule_name = x_str(k.req("rule_name"), "rule_name")?;
    let passed = x_bool(k.req("passed"), "passed")?;
    let measured_value = k.req("measured_value");
    let threshold = k.req("threshold");
    let reason = x_str(k.req("reason"), "reason")?;
    nonempty(&rule_name, "rule_name must be non-empty")?;
    Ok(RiskRuleResult { rule_name, passed, measured_value, threshold, reason })
}

pub fn build_risk_verdict(f: Vec<(String, Val)>) -> R<RiskVerdict> {
    let mut k = Kw::bind(
        "RiskVerdict",
        f,
        &[
            ("order_intent_id", true),
            ("accepted", false),
            ("evaluations", false),
            ("refusal_reasons", false),
            ("approved_quantity", false),
        ],
    )?;
    let order_intent_id = x_str(k.req("order_intent_id"), "order_intent_id")?;
    let accepted = match k.take("accepted") {
        None | Some(Val::None) => None,
        Some(Val::Bool(b)) => Some(b),
        Some(o) => return bad_type("a bool or None", "accepted", &o),
    };
    let evaluations = match k.take("evaluations") {
        None => Vec::new(),
        Some(v) => {
            let mut out = Vec::new();
            for e in x_tuple(v, "evaluations")? {
                match e {
                    Val::Obj(o) => match *o {
                        Obj::RiskRule(r) => out.push(r),
                        other => return bad_type("a RiskRuleResult", "evaluations", &Val::Obj(Box::new(other))),
                    },
                    o => return bad_type("a RiskRuleResult", "evaluations", &o),
                }
            }
            out
        }
    };
    let refusal_reasons = match k.take("refusal_reasons") {
        None => Vec::new(),
        Some(v) => x_strs(v, "refusal_reasons")?,
    };
    let approved_quantity = k.take("approved_quantity").map_or(Ok(None), |v| x_odec(v, "approved_quantity"))?;
    nonempty(&order_intent_id, "order_intent_id must be non-empty")?;
    if let Some(q) = &approved_quantity {
        if !q.is_finite() || le0(q)? {
            return err("value", "approved_quantity must be a positive finite Decimal");
        }
    }
    if evaluations.is_empty() {
        return err("value", "RiskVerdict must contain at least one evaluation (I11)");
    }
    let has_failed = evaluations.iter().any(|e| !e.passed);
    let derived = !has_failed && refusal_reasons.is_empty();
    if let Some(a) = accepted {
        if a != derived {
            return err(
                "value",
                format!(
                    "Contradictory RiskVerdict: accepted={} but evaluations (failed_rules={}, refusal_reasons={}) evaluates to accepted={} (I11)",
                    py_bool(a),
                    py_bool(has_failed),
                    refusal_reasons.len(),
                    py_bool(derived)
                ),
            );
        }
    }
    if !derived && refusal_reasons.is_empty() {
        return err("value", "A refused RiskVerdict must include at least one refusal reason");
    }
    if !derived && approved_quantity.is_some() {
        return err("value", "A refused RiskVerdict cannot include an approved quantity");
    }
    Ok(RiskVerdict { order_intent_id, accepted: derived, evaluations, refusal_reasons, approved_quantity })
}

fn py_bool(b: bool) -> &'static str {
    if b {
        "True"
    } else {
        "False"
    }
}

pub fn build_risk_control(f: Vec<(String, Val)>) -> R<RiskControlChange> {
    let mut k = Kw::bind(
        "RiskControlChange",
        f,
        &[("control_id", true), ("enabled", true), ("reason", true), ("changed_at", true)],
    )?;
    let control_id = x_str(k.req("control_id"), "control_id")?;
    let enabled = x_bool(k.req("enabled"), "enabled")?;
    let reason = x_str(k.req("reason"), "reason")?;
    let changed_at = x_dt(k.req("changed_at"), "changed_at")?;
    nonempty(&control_id, "control_id must be non-empty")?;
    nonempty(&reason, "reason must be non-empty")?;
    Ok(RiskControlChange { control_id, enabled, reason, changed_at })
}

pub fn build_signal(f: Vec<(String, Val)>) -> R<Signal> {
    let mut k = Kw::bind(
        "Signal",
        f,
        &[
            ("signal_id", true),
            ("scan_id", true),
            ("symbol", true),
            ("session_date", true),
            ("direction", true),
            ("metrics", false),
            ("next_earnings_date", false),
            ("created_at", false),
        ],
    )?;
    let signal_id = x_str(k.req("signal_id"), "signal_id")?;
    let scan_id = x_str(k.req("scan_id"), "scan_id")?;
    let symbol = x_str(k.req("symbol"), "symbol")?;
    let session_date = x_date(k.req("session_date"), "session_date")?;
    let direction = x_str(k.req("direction"), "direction")?;
    let metrics = k.take("metrics").map_or(Ok(Vec::new()), |v| x_map(v, "metrics"))?;
    let next_earnings_date = k.take("next_earnings_date").map_or(Ok(None), |v| x_odate(v, "next_earnings_date"))?;
    let created_at = k.take("created_at").map_or(Ok(None), |v| x_odt(v, "created_at"))?;
    nonempty(&signal_id, "signal_id must be non-empty")?;
    nonempty(&scan_id, "scan_id must be non-empty")?;
    nonempty(&symbol, "symbol must be non-empty")?;
    if direction != "long" && direction != "short" {
        return err("value", format!("direction must be 'long' or 'short', got '{direction}'"));
    }
    Ok(Signal { signal_id, scan_id, symbol, session_date, direction, metrics, next_earnings_date, created_at })
}

pub fn build_corporate_action(f: Vec<(String, Val)>) -> R<CorporateAction> {
    let mut k = Kw::bind(
        "CorporateAction",
        f,
        &[("symbol", true), ("action_type", true), ("effective_date", true), ("as_of", true), ("details", false)],
    )?;
    let symbol = x_str(k.req("symbol"), "symbol")?;
    let action_type = x_str(k.req("action_type"), "action_type")?;
    let effective_date = x_date(k.req("effective_date"), "effective_date")?;
    let as_of = x_dt(k.req("as_of"), "as_of")?;
    let details = k.take("details").map_or(Ok(Vec::new()), |v| x_map(v, "details"))?;
    Ok(CorporateAction { symbol, action_type, effective_date, as_of, details })
}

pub fn build_state_change(f: Vec<(String, Val)>) -> R<OrderStateChange> {
    let mut k = Kw::bind(
        "OrderStateChange",
        f,
        &[("order_id", true), ("reason", false), ("venue_order_id", false)],
    )?;
    let order_id = x_str(k.req("order_id"), "order_id")?;
    let reason = k.take("reason").map_or(Ok(None), |v| x_ostr(v, "reason"))?;
    let venue_order_id = k.take("venue_order_id").map_or(Ok(None), |v| x_ostr(v, "venue_order_id"))?;
    pnonempty(&order_id, "OrderStateChange.order_id must be non-empty")?;
    if let Some(r) = &reason {
        pnonempty(r, "OrderStateChange.reason must be non-empty when provided")?;
    }
    Ok(OrderStateChange { order_id, reason, venue_order_id })
}

fn x_order(v: Val, n: &str) -> R<Order> {
    match v {
        Val::Obj(o) => match *o {
            Obj::Order(x) => Ok(x),
            other => bad_type("an Order", n, &Val::Obj(Box::new(other))),
        },
        o => bad_type("an Order", n, &o),
    }
}

pub fn build_orders_created(f: Vec<(String, Val)>) -> R<OrdersCreated> {
    let mut k = Kw::bind("OrdersCreated", f, &[("orders", true), ("fingerprint", true), ("reason", true)])?;
    let orders: Vec<Order> = x_tuple(k.req("orders"), "orders")?.into_iter().map(|v| x_order(v, "orders")).collect::<R<_>>()?;
    let fingerprint = x_str(k.req("fingerprint"), "fingerprint")?;
    let reason = x_str(k.req("reason"), "reason")?;
    if orders.is_empty() {
        return err("payload", "OrdersCreated.orders must not be empty");
    }
    pnonempty(&fingerprint, "OrdersCreated.fingerprint must be non-empty")?;
    pnonempty(&reason, "OrdersCreated.reason must be non-empty")?;
    let mut ids: Vec<&str> = orders.iter().map(|o| o.order_id.as_str()).collect();
    ids.sort_unstable();
    ids.dedup();
    if ids.len() != orders.len() {
        return err("payload", "OrdersCreated.order_id values must be unique");
    }
    let mut accounts: Vec<&str> = orders.iter().map(|o| o.account_id.as_str()).collect();
    accounts.sort_unstable();
    accounts.dedup();
    if accounts.len() != 1 {
        return err("payload", "OrdersCreated orders must belong to one account");
    }
    Ok(OrdersCreated { orders, fingerprint, reason })
}

pub fn build_order_updated(f: Vec<(String, Val)>) -> R<OrderUpdated> {
    let mut k = Kw::bind("OrderUpdated", f, &[("order", true), ("reason", true), ("venue_order_id", false)])?;
    let order = x_order(k.req("order"), "order")?;
    let reason = x_str(k.req("reason"), "reason")?;
    let venue_order_id = k.take("venue_order_id").map_or(Ok(None), |v| x_ostr(v, "venue_order_id"))?;
    pnonempty(&reason, "OrderUpdated.reason must be non-empty")?;
    Ok(OrderUpdated { order, reason, venue_order_id })
}

fn positive_opt(v: Val, name: &str, label: &str) -> R<Option<PyDec>> {
    let d = as_odecimal(v, name)?;
    if let Some(x) = &d {
        if le0(x)? {
            return err("payload", format!("EmulatedOrderState.{label} must be positive"));
        }
    }
    Ok(d)
}

pub fn build_emulated(f: Vec<(String, Val)>) -> R<EmulatedOrderState> {
    let mut k = Kw::bind(
        "EmulatedOrderState",
        f,
        &[
            ("order_id", true),
            ("observed_price", true),
            ("extreme", true),
            ("stop_price", true),
            ("triggered", true),
            ("reason", true),
        ],
    )?;
    let order_id = x_str(k.req("order_id"), "order_id")?;
    let observed = k.req("observed_price");
    let extreme = k.req("extreme");
    let stop = k.req("stop_price");
    let triggered = x_bool(k.req("triggered"), "triggered")?;
    let reason = x_str(k.req("reason"), "reason")?;
    pnonempty(&order_id, "EmulatedOrderState.order_id must be non-empty")?;
    let observed_price = positive_opt(observed, "observed_price", "observed_price")?;
    let extreme = positive_opt(extreme, "extreme", "extreme")?;
    let stop_price = positive_opt(stop, "stop_price", "stop_price")?;
    pnonempty(&reason, "EmulatedOrderState.reason must be non-empty")?;
    Ok(EmulatedOrderState { order_id, observed_price, extreme, stop_price, triggered, reason })
}

pub fn build_cash_flow(f: Vec<(String, Val)>) -> R<CashFlow> {
    let mut k = Kw::bind("CashFlow", f, &[("amount", true), ("kind", true), ("as_of", true), ("note", false)])?;
    let amount = k.req("amount");
    let kind = x_str(k.req("kind"), "kind")?;
    let as_of = x_dt(k.req("as_of"), "as_of")?;
    let note = k.take("note").map_or(Ok(None), |v| x_ostr(v, "note"))?;
    let amount = as_decimal(amount, "CashFlow.amount")?;
    if !CASH_FLOW_KINDS.contains(&kind.as_str()) {
        return err(
            "payload",
            format!(
                "CashFlow.kind must be one of ('interest', 'borrow', 'fee', 'deposit', 'withdrawal', 'dividend'), got '{kind}'"
            ),
        );
    }
    Ok(CashFlow { amount, kind, as_of, note })
}

pub fn build_mark(f: Vec<(String, Val)>) -> R<Mark> {
    let mut k = Kw::bind("Mark", f, &[("instrument", true), ("price", true), ("as_of", true), ("source", false)])?;
    let instrument = match k.req("instrument") {
        Val::Obj(o) => match *o {
            Obj::Instr(i) => i,
            _ => return err("payload", "Mark.instrument must be an Instrument (I6)"),
        },
        _ => return err("payload", "Mark.instrument must be an Instrument (I6)"),
    };
    let price = k.req("price");
    let as_of = x_dt(k.req("as_of"), "as_of")?;
    let source = k.take("source").map_or(Ok(None), |v| x_ostr(v, "source"))?;
    let price = as_decimal(price, "Mark.price")?;
    if le0(&price)? {
        return err("payload", format!("Mark.price must be positive, got {} (I5)", price.to_py_string()));
    }
    Ok(Mark { instrument, price, as_of, source })
}

pub fn build_reconcile(f: Vec<(String, Val)>) -> R<VenueReconcile> {
    let mut k = Kw::bind(
        "VenueReconcile",
        f,
        &[("venue", true), ("as_of", true), ("reconciled", true), ("drift", false), ("note", false)],
    )?;
    let venue = x_str(k.req("venue"), "venue")?;
    let as_of = x_dt(k.req("as_of"), "as_of")?;
    let reconciled = x_bool(k.req("reconciled"), "reconciled")?;
    let drift = k.take("drift").map_or(Ok(Vec::new()), |v| x_strs(v, "drift"))?;
    let note = k.take("note").map_or(Ok(None), |v| x_ostr(v, "note"))?;
    pnonempty(&venue, "VenueReconcile.venue must be non-empty")?;
    if reconciled && !drift.is_empty() {
        return err("payload", "VenueReconcile cannot be reconciled while listing drift instruments");
    }
    if !reconciled && drift.is_empty() {
        return err("payload", "VenueReconcile that is not reconciled must name the drifting instruments (I11)");
    }
    Ok(VenueReconcile { venue, as_of, reconciled, drift, note })
}

pub fn build_halt_cleared(f: Vec<(String, Val)>) -> R<VenueHaltCleared> {
    let mut k = Kw::bind(
        "VenueHaltCleared",
        f,
        &[("venue", true), ("at", true), ("reason", true), ("reconcile_seq", true)],
    )?;
    let venue = x_str(k.req("venue"), "venue")?;
    let at = x_dt(k.req("at"), "at")?;
    let reason = x_str(k.req("reason"), "reason")?;
    let seq = k.req("reconcile_seq");
    pnonempty(&venue, "VenueHaltCleared.venue must be non-empty")?;
    if pyrules::strip(&reason).is_empty() {
        return err("payload", "VenueHaltCleared.reason must be non-empty (I11)");
    }
    let reconcile_seq = match seq {
        Val::Int(i) if i >= 1 => i,
        Val::Int(i) => return err("payload", format!("VenueHaltCleared.reconcile_seq must be a ledger seq, got {i}")),
        o => return err("payload", format!("VenueHaltCleared.reconcile_seq must be a ledger seq, got {}", o.type_name())),
    };
    Ok(VenueHaltCleared { venue, at, reason, reconcile_seq })
}

pub fn build_lifecycle(f: Vec<(String, Val)>) -> R<OptionLifecycle> {
    let mut k = Kw::bind(
        "OptionLifecycle",
        f,
        &[
            ("account_id", true),
            ("contract", true),
            ("quantity", true),
            ("held", true),
            ("underlying_price", true),
            ("price_source", true),
            ("as_of", true),
            ("reason", true),
            ("early", false),
        ],
    )?;
    let account_id = x_str(k.req("account_id"), "account_id")?;
    let contract = match k.req("contract") {
        Val::Obj(o) => match *o {
            Obj::Instr(Instrument::Option(c)) => Some(c),
            _ => None,
        },
        _ => None,
    };
    let quantity = k.req("quantity");
    let held = k.req("held");
    let price = k.req("underlying_price");
    let price_source = x_str(k.req("price_source"), "price_source")?;
    let as_of = x_dt(k.req("as_of"), "as_of")?;
    let reason = x_str(k.req("reason"), "reason")?;
    let early = k.take("early").map_or(Ok(false), |v| match v {
        Val::Bool(b) => Ok(b),
        _ => err("payload", "OptionLifecycle.early must be a bool"),
    });
    pnonempty(&account_id, "OptionLifecycle.account_id must be non-empty")?;
    let Some(contract) = contract else {
        return err("payload", "OptionLifecycle.contract must be an OptionContract (I6)");
    };
    let quantity = as_decimal(quantity, "OptionLifecycle.quantity")?;
    if le0(&quantity)? || !quantity.is_integral().map_err(derr)? {
        return err(
            "payload",
            format!("OptionLifecycle.quantity must be a positive whole number of contracts, got {}", quantity.to_py_string()),
        );
    }
    let held = match held {
        Val::Side(s) => s,
        _ => return err("payload", "OptionLifecycle.held must be a Side (I5)"),
    };
    let underlying_price = as_decimal(price, "OptionLifecycle.underlying_price")?;
    if le0(&underlying_price)? {
        return err(
            "payload",
            format!("OptionLifecycle.underlying_price must be positive, got {} (I5)", underlying_price.to_py_string()),
        );
    }
    pnonempty(&price_source, "OptionLifecycle.price_source must be non-empty (I11)")?;
    pnonempty(&reason, "OptionLifecycle.reason must be non-empty (I11)")?;
    let early = early?;
    Ok(OptionLifecycle { account_id, contract, quantity, held, underlying_price, price_source, as_of, reason, early })
}

pub fn build_eod_run(f: Vec<(String, Val)>) -> R<EodRun> {
    let mut k = Kw::bind(
        "EodRun",
        f,
        &[("session", true), ("job", true), ("account_id", true), ("bars_processed", true), ("at_close", true)],
    )?;
    let session = x_date(k.req("session"), "session")?;
    let job = x_str(k.req("job"), "job")?;
    let account_id = x_str(k.req("account_id"), "account_id")?;
    let bars_processed = x_int(k.req("bars_processed"), "bars_processed")?;
    let at_close = x_dt(k.req("at_close"), "at_close")?;
    pnonempty(&job, "EodRun.job must be non-empty")?;
    pnonempty(&account_id, "EodRun.account_id must be non-empty")?;
    if bars_processed < 0 {
        return err("payload", "EodRun.bars_processed must be non-negative");
    }
    Ok(EodRun { session, job, account_id, bars_processed, at_close })
}

/// `_require_whole`.
fn require_whole(v: PyDec, name: &str, positive: bool) -> R<PyDec> {
    let value = v;
    let whole = value.is_integral().map_err(derr)?;
    let neg = lt0(&value)?;
    let zero = positive && cmp0(&value)? == std::cmp::Ordering::Equal;
    if !whole || neg || zero {
        let kind = if positive { "a positive" } else { "a non-negative" };
        return err(
            "payload",
            format!("{name} must be {kind} whole number of contracts, got {} (I5)", value.to_py_string()),
        );
    }
    Ok(value)
}

fn require_whole_val(v: Val, name: &str, positive: bool) -> R<PyDec> {
    let d = as_decimal(v, name)?;
    require_whole(d, name, positive)
}

pub fn build_mirror_alloc(f: Vec<(String, Val)>) -> R<MirrorAllocation> {
    let mut k = Kw::bind(
        "MirrorAllocation",
        f,
        &[("strategy_order_id", true), ("strategy_account", true), ("quantity", true)],
    )?;
    let strategy_order_id = x_str(k.req("strategy_order_id"), "strategy_order_id")?;
    let strategy_account = x_str(k.req("strategy_account"), "strategy_account")?;
    let q = k.req("quantity");
    pnonempty(&strategy_order_id, "MirrorAllocation.strategy_order_id must be non-empty")?;
    pnonempty(&strategy_account, "MirrorAllocation.strategy_account must be non-empty")?;
    let quantity = require_whole_val(q, "MirrorAllocation.quantity", true)?;
    Ok(MirrorAllocation { strategy_order_id, strategy_account, quantity })
}

/// `_require_vertical`: only a 2-leg 1:1 option vertical is mirrored as a combo.
///
/// The one place a venue ticket's instrument kind is gated for `Combo`; the other gate is
/// [`check_mirror_instrument`].
fn require_vertical(legs: &[ComboLeg], name: &str) -> R<()> {
    let symbol = || Instrument::Combo(legs.to_vec()).symbol();
    let opts: Vec<&OptionContract> = legs
        .iter()
        .filter_map(|l| if let Instrument::Option(c) = &l.contract { Some(c) } else { None })
        .collect();
    if legs.len() != 2 || opts.len() != 2 {
        return err("payload", format!("{name}: a mirrored combo is a 2-leg option vertical, got {}", symbol()?));
    }
    let (a, b) = (opts[0], opts[1]);
    if a.underlying != b.underlying
        || a.expiry != b.expiry
        || a.right != b.right
        || a.multiplier != b.multiplier
        || dec_eq(&a.strike, &b.strike)
        || legs[0].side == legs[1].side
        || legs[0].ratio != legs[1].ratio
    {
        return err("payload", format!("{name}: {} is not a 1:1 vertical", symbol()?));
    }
    Ok(())
}

/// `MirrorQueued.__post_init__`'s instrument gate: a vertical `Combo` or an option
/// contract. This is the single line to relax when a mirror ticket may carry an `Equity`
/// (branch `te/T2-follow`): add `Instrument::Equity(_)` to the accepted arms here, and
/// make the matching one-line change in `events.py` `MirrorQueued.__post_init__`.
pub(crate) fn check_mirror_instrument(instrument: &Instrument) -> R<()> {
    match instrument {
        Instrument::Combo(legs) => require_vertical(legs, "MirrorQueued.instrument"),
        // every decodable instrument is legal now: Python's refusal reaches only non-instruments
        Instrument::Option(_) | Instrument::Equity(_) => Ok(()),
        Instrument::Future(_) => err("payload", "MirrorQueued.instrument: futures not supported in mirror"),
    }
}

pub fn build_mirror_queued(f: Vec<(String, Val)>) -> R<MirrorQueued> {
    let mut k = Kw::bind(
        "MirrorQueued",
        f,
        &[
            ("venue", true),
            ("ticket_key", true),
            ("instrument", true),
            ("side", true),
            ("quantity", true),
            ("order_type", true),
            ("limit_price", true),
            ("tif", true),
            ("allocations", true),
            ("at", true),
        ],
    )?;
    let venue = x_str(k.req("venue"), "venue")?;
    let ticket_key = x_str(k.req("ticket_key"), "ticket_key")?;
    let instrument = x_instr(k.req("instrument"), "instrument")?;
    let side = k.req("side");
    let quantity = k.req("quantity");
    let order_type = k.req("order_type");
    let limit_price = k.req("limit_price");
    let tif = k.req("tif");
    let allocations = k.req("allocations");
    let at = x_dt(k.req("at"), "at")?;
    pnonempty(&venue, "MirrorQueued.venue must be non-empty")?;
    pnonempty(&ticket_key, "MirrorQueued.ticket_key must be non-empty (I3)")?;
    check_mirror_instrument(&instrument)?;
    let side = match side {
        Val::Side(s) => s,
        _ => return err("payload", "MirrorQueued.side must be a Side"),
    };
    let quantity = require_whole_val(quantity, "MirrorQueued.quantity", true)?;
    let order_type = match order_type {
        Val::OrderType(t @ (OrderType::Market | OrderType::Limit)) => t,
        _ => return err("payload", "MirrorQueued.order_type must be MARKET or LIMIT"),
    };
    let tif = match tif {
        Val::Tif(t @ (Tif::Day | Tif::Gtc)) => t,
        _ => return err("payload", "MirrorQueued.tif must be DAY or GTC"),
    };
    let limit_price = if order_type == OrderType::Limit {
        if matches!(limit_price, Val::None) {
            return err("payload", "MirrorQueued: a LIMIT ticket needs a limit_price (I5)");
        }
        let lp = as_decimal(limit_price, "MirrorQueued.limit_price")?;
        if le0(&lp)? {
            return err("payload", "MirrorQueued.limit_price must be positive (I5)");
        }
        Some(lp)
    } else {
        if !matches!(limit_price, Val::None) {
            return err("payload", "MirrorQueued: a MARKET ticket cannot carry a limit_price");
        }
        None
    };
    let mut allocs = Vec::new();
    for a in x_tuple(allocations, "allocations")? {
        match a {
            Val::Obj(o) => match *o {
                Obj::MAlloc(a) => allocs.push(a),
                _ => return err("payload", "MirrorQueued.allocations must be MirrorAllocations, at least one (§4.4)"),
            },
            _ => return err("payload", "MirrorQueued.allocations must be MirrorAllocations, at least one (§4.4)"),
        }
    }
    if allocs.is_empty() {
        return err("payload", "MirrorQueued.allocations must be MirrorAllocations, at least one (§4.4)");
    }
    let mut ids: Vec<&str> = allocs.iter().map(|a| a.strategy_order_id.as_str()).collect();
    ids.sort_unstable();
    ids.dedup();
    if ids.len() != allocs.len() {
        return err("payload", "MirrorQueued allocates one strategy order twice (I3)");
    }
    let mut total = PyDec::zero();
    for a in &allocs {
        total = total.add(&a.quantity).map_err(derr)?;
    }
    if !dec_eq(&total, &quantity) {
        return err(
            "payload",
            format!(
                "MirrorQueued allocations total {} but the ticket is {} (I11)",
                total.to_py_string(),
                quantity.to_py_string()
            ),
        );
    }
    Ok(MirrorQueued { venue, ticket_key, instrument, side, quantity, order_type, limit_price, tif, allocations: allocs, at })
}

pub fn build_mirror_refused(f: Vec<(String, Val)>) -> R<MirrorRefused> {
    let mut k = Kw::bind(
        "MirrorRefused",
        f,
        &[("venue", true), ("strategy_order_id", true), ("strategy_account", true), ("reason", true), ("at", true)],
    )?;
    let venue = x_str(k.req("venue"), "venue")?;
    let strategy_order_id = x_str(k.req("strategy_order_id"), "strategy_order_id")?;
    let strategy_account = x_str(k.req("strategy_account"), "strategy_account")?;
    let reason = x_str(k.req("reason"), "reason")?;
    let at = x_dt(k.req("at"), "at")?;
    pnonempty(&venue, "MirrorRefused.venue must be non-empty")?;
    pnonempty(&strategy_order_id, "MirrorRefused.strategy_order_id must be non-empty")?;
    pnonempty(&strategy_account, "MirrorRefused.strategy_account must be non-empty")?;
    pnonempty(&reason, "MirrorRefused.reason must be non-empty (I11)")?;
    Ok(MirrorRefused { venue, strategy_order_id, strategy_account, reason, at })
}

pub fn build_mirror_ack(f: Vec<(String, Val)>) -> R<MirrorAck> {
    let mut k = Kw::bind(
        "MirrorAck",
        f,
        &[
            ("venue", true),
            ("ticket_key", true),
            ("status", true),
            ("message", true),
            ("at", true),
            ("venue_order_id", false),
            ("book_status", false),
        ],
    )?;
    let venue = x_str(k.req("venue"), "venue")?;
    let ticket_key = x_str(k.req("ticket_key"), "ticket_key")?;
    let status = x_str(k.req("status"), "status")?;
    let message = x_str(k.req("message"), "message")?;
    let at = x_dt(k.req("at"), "at")?;
    let venue_order_id = k.take("venue_order_id").map_or(Ok(None), |v| x_ostr(v, "venue_order_id"))?;
    let book_status = match k.take("book_status") {
        None | Some(Val::None) => None,
        Some(Val::OrderState(s)) => Some(s),
        Some(_) => return err("payload", "MirrorAck.book_status must be an OrderState"),
    };
    pnonempty(&venue, "MirrorAck.venue must be non-empty")?;
    pnonempty(&ticket_key, "MirrorAck.ticket_key must be non-empty")?;
    if !MIRROR_ACK_STATUSES.contains(&status.as_str()) {
        return err("payload", format!("MirrorAck.status must be one of ('ACCEPTED', 'REJECTED', 'PENDING'), got '{status}'"));
    }
    pnonempty(&message, "MirrorAck.message must be non-empty (I11)")?;
    if let Some(v) = &venue_order_id {
        if !py_isdigit(v) {
            return err("payload", format!("MirrorAck.venue_order_id must be an all-digit id, got '{v}'"));
        }
    }
    Ok(MirrorAck { venue, ticket_key, status, message, at, venue_order_id, book_status })
}

pub fn build_mirror_fill(f: Vec<(String, Val)>) -> R<MirrorFill> {
    let mut k = Kw::bind(
        "MirrorFill",
        f,
        &[
            ("venue", true),
            ("ticket_key", true),
            ("venue_order_id", true),
            ("filled", true),
            ("avg_price", true),
            ("at", true),
        ],
    )?;
    let venue = x_str(k.req("venue"), "venue")?;
    let ticket_key = x_str(k.req("ticket_key"), "ticket_key")?;
    let voi = k.req("venue_order_id");
    let filled = k.req("filled");
    let avg = k.req("avg_price");
    let at = x_dt(k.req("at"), "at")?;
    pnonempty(&venue, "MirrorFill.venue must be non-empty")?;
    pnonempty(&ticket_key, "MirrorFill.ticket_key must be non-empty")?;
    let venue_order_id = match voi {
        Val::Str(s) if py_isdigit(&s) => s,
        _ => return err("payload", "MirrorFill.venue_order_id must be an all-digit id"),
    };
    let filled = require_whole_val(filled, "MirrorFill.filled", true)?;
    let avg_price = as_decimal(avg, "MirrorFill.avg_price")?;
    if le0(&avg_price)? {
        return err("payload", format!("MirrorFill.avg_price must be positive, got {} (I5)", avg_price.to_py_string()));
    }
    Ok(MirrorFill { venue, ticket_key, venue_order_id, filled, avg_price, at })
}

/// `isinstance(payload, expected)` for an event kind: the dataclass the kind must carry.
pub fn expected_payload(kind: EventKind) -> &'static str {
    use EventKind::*;
    match kind {
        SignalSeen => "Signal",
        RiskVerdict => "RiskVerdict",
        OrdersCreated => "OrdersCreated",
        RiskControl => "RiskControlChange",
        OrderSubmitted => "Order",
        OrderUpdated => "OrderUpdated",
        OrderPending | OrderAccepted | OrderRejected | OrderCancelled | OrderRefused | OrderExpired => {
            "OrderStateChange"
        }
        OrderEmulationUpdated => "EmulatedOrderState",
        Fill => "Fill",
        Assignment | Exercise | Expiry => "OptionLifecycle",
        CorporateAction => "CorporateAction",
        CashFlow => "CashFlow",
        Mark => "Mark",
        VenueReconcile => "VenueReconcile",
        VenueHaltCleared => "VenueHaltCleared",
        EodRun => "EodRun",
        MirrorQueued => "MirrorQueued",
        MirrorRefused => "MirrorRefused",
        MirrorAck => "MirrorAck",
        MirrorFill => "MirrorFill",
    }
}

/// An event: `Event.__post_init__` has already run.
#[derive(Debug, Clone)]
pub struct Event {
    pub account: String,
    pub kind: EventKind,
    pub payload: Obj,
    pub ts_utc: DateTime,
    pub command_id: Option<String>,
    pub schema_version: i128,
    pub seq: Option<i128>,
}

/// The `Event.__post_init__` checks that follow the field conversions.
pub fn check_event(e: &Event) -> R<()> {
    pnonempty(&e.account, "Event.account must be non-empty")?;
    if let Some(c) = &e.command_id {
        pnonempty(c, "Event.command_id must be non-empty when provided (I3)")?;
    }
    if e.schema_version <= 0 {
        return err("payload", format!("Event.schema_version must be a positive int, got {}", e.schema_version));
    }
    if e.schema_version > SCHEMA_VERSION {
        return err(
            "payload",
            format!(
                "Event.schema_version {} is newer than this engine understands ({SCHEMA_VERSION}); refusing rather than mis-reading it (I5)",
                e.schema_version
            ),
        );
    }
    if let Some(s) = e.seq {
        if s <= 0 {
            return err("payload", format!("Event.seq must be a positive int, got {s}"));
        }
    }
    let expected = expected_payload(e.kind);
    if e.payload.tag() != expected {
        return err(
            "payload",
            format!("{} payload must be {expected}, got {}", e.kind.value(), e.payload.tag()),
        );
    }
    let payload_account: Option<&str> = match &e.payload {
        Obj::Order(o) => Some(&o.account_id),
        Obj::Fill(f) => Some(&f.account_id),
        Obj::Lifecycle(l) => Some(&l.account_id),
        Obj::EodRun(r) => Some(&r.account_id),
        Obj::OrderUpdated(u) => Some(&u.order.account_id),
        _ => None,
    };
    if let Some(a) = payload_account {
        if a != e.account {
            return err(
                "payload",
                format!(
                    "{} payload belongs to account '{a}' but the event is filed under '{}' (I8)",
                    e.kind.value(),
                    e.account
                ),
            );
        }
    }
    let venue: Option<&str> = match &e.payload {
        Obj::MQueued(m) => Some(&m.venue),
        Obj::MRefused(m) => Some(&m.venue),
        Obj::MAck(m) => Some(&m.venue),
        Obj::MFill(m) => Some(&m.venue),
        _ => None,
    };
    if let Some(v) = venue {
        let want = mirror_account(v)?;
        if e.account != want {
            return err(
                "payload",
                format!(
                    "{} for venue '{v}' must be filed under '{want}', not '{}' (I8)",
                    e.kind.value(),
                    e.account
                ),
            );
        }
    }
    if let Obj::OrdersCreated(c) = &e.payload {
        if c.orders.iter().any(|o| o.account_id != e.account) {
            return err("payload", format!("{} contains orders for a different account (I8)", e.kind.value()));
        }
    }
    Ok(())
}
