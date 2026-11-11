//! Canonical futures instrument, the edge symbol map, and the dated expiry/roll table
//! (docs/briefs/BP-1-FUTURES.md, design docs/design/BROKER-PLATFORM.md §6.1).
//!
//! One table, no second reader: the specifications themselves live in
//! [`crate::sim::tick::FUTURE_SPECS`] (re-exported as `te_core::futures`), and this module
//! reads it. Nothing here reads a clock (I7): every date that is not part of a rule is an
//! argument.
//!
//! # Canonical form
//!
//! `NQZ6`: root, one letter month code, one-digit year. The one-digit year is resolved
//! only through [`resolve_year_digit`], which takes a `reference_date` and picks the
//! nearest year that is at most one year in the past and at most ten years ahead. A
//! symbol that carries a two- or four-digit year is read directly.
//!
//! # Platforms
//!
//! | Platform | Spelling of NQ Dec 2026 | Continuous |
//! |---|---|---|
//! | NT8 | `NQ 12-26` | `NQ` |
//! | Tradovate | `NQZ6` | `NQ` |
//! | TradingView | `NQZ2026` | `NQ1!` |
//! | Schwab | `/NQZ26` | `/NQ` |
//! | IBKR | `localSymbol NQZ6` + `lastTradeDateOrContractMonth 202612` | `NQ` |
//!
//! A bare root (or TradingView's `NQ1!`) is a *continuous* symbol. It never names a
//! contract by itself: it resolves only through [`front_month`], which takes the date
//! and the roll table as arguments.
//!
//! # Expiry and roll table
//!
//! Dated rows for 2024-2030, computed from the cited rules and bounded by
//! [`TABLE_FIRST_YEAR`]/[`TABLE_LAST_YEAR`]. A contract month outside the table is
//! refused, never extrapolated. Holiday shifts are not modeled (the Globex calendar's
//! holiday table ends 2027-12-31, so using it here would refuse 2028-2030); every
//! business-day rule below therefore counts weekdays only.
//!
//! | Cycle | Expiry rule | Roll |
//! |---|---|---|
//! | Index (NQ, MNQ, ES, MES, YM, MYM, RTY, M2K), H/M/U/Z only | Third Friday of the contract month (CME equity-index last trade day, 9:30 a.m. ET) | 8 calendar days before expiry |
//! | Energy (CL, MCL), all 12 months | Three business days before the 25th of the month preceding delivery (NYMEX Rule 200.C) | 5 calendar days before expiry |
//! | Metals (GC, MGC), all 12 months | Third-to-last business day of the contract month (COMEX gold last trade day) | 5 calendar days before expiry |
//!
//! # Wire shape
//!
//! [`instruments_json`] is the read API's body (`GET /v1/instruments/futures`): a JSON
//! array, one row per root, every number as a string (D6).

use chrono::{Datelike, Duration, NaiveDate, Weekday};

use crate::ledger::model::{err, ContractMonth, FutureContract, LErr, R};
use crate::sim::tick::{month_from_code, spec_for_root, FutureSpec, FUTURE_SPECS};

fn lerr(kind: &'static str, msg: impl Into<String>) -> LErr {
    LErr { kind, msg: msg.into() }
}

// --- platforms ---------------------------------------------------------------

/// The platforms whose spellings are converted at the edge (B2).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    Nt8,
    Tradovate,
    TradingView,
    Schwab,
    Ibkr,
}

impl Platform {
    /// Parse a platform name. Case-insensitive; `tv` is accepted for TradingView.
    /// An unknown platform is refused with kind `unsupported`.
    pub fn from_name(name: &str) -> R<Self> {
        match name.trim().to_ascii_lowercase().as_str() {
            "nt8" => Ok(Platform::Nt8),
            "tradovate" => Ok(Platform::Tradovate),
            "tradingview" | "tv" => Ok(Platform::TradingView),
            "schwab" => Ok(Platform::Schwab),
            "ibkr" => Ok(Platform::Ibkr),
            _ => err("unsupported", format!("Unknown platform: '{name}'")),
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Platform::Nt8 => "nt8",
            Platform::Tradovate => "tradovate",
            Platform::TradingView => "tradingview",
            Platform::Schwab => "schwab",
            Platform::Ibkr => "ibkr",
        }
    }
}

/// What a platform spelling names: a dated contract, or a continuous root.
#[derive(Debug, Clone)]
pub enum SymbolRef {
    Contract(FutureContract),
    /// A bare root or `NQ1!`. Resolve it with [`front_month`], never here.
    Continuous(String),
}

impl SymbolRef {
    /// The dated contract, or a `continuous` refusal.
    pub fn into_contract(self) -> R<FutureContract> {
        match self {
            SymbolRef::Contract(c) => Ok(c),
            SymbolRef::Continuous(root) => err(
                "continuous",
                format!("'{root}' is a continuous symbol; resolve it with front_month(root, as_of)"),
            ),
        }
    }

    pub fn contract(&self) -> R<&FutureContract> {
        match self {
            SymbolRef::Contract(c) => Ok(c),
            SymbolRef::Continuous(root) => err(
                "continuous",
                format!("'{root}' is a continuous symbol; resolve it with front_month(root, as_of)"),
            ),
        }
    }
}

// --- contract construction ---------------------------------------------------

/// The dated contract for `root` in `month`, with the spec from the one table.
/// An unknown root is refused with kind `unsupported`.
pub fn contract_for(root: &str, month: ContractMonth) -> R<FutureContract> {
    let spec = spec_for_root(root)?;
    Ok(FutureContract {
        root: spec.root.to_string(),
        contract_month: Some(month),
        tick_size: spec.tick_size,
        point_value: spec.point_value,
    })
}

fn require_month(contract: &FutureContract) -> R<ContractMonth> {
    match contract.contract_month {
        Some(month) => Ok(month),
        None => err(
            "continuous",
            format!(
                "'{}' has no contract month; resolve the front month with front_month(root, as_of)",
                contract.root
            ),
        ),
    }
}

/// The canonical text of a dated contract: `NQZ6` (root + month code + one-digit year).
pub fn canonical_text(contract: &FutureContract) -> R<String> {
    let month = require_month(contract)?;
    let spec = spec_for_root(&contract.root)?;
    Ok(format!("{}{}{}", spec.root, month.month_code(), month.year.rem_euclid(10)))
}

/// The canonical text of a root and contract month: `NQZ6`.
pub fn canonical_text_for(root: &str, month: ContractMonth) -> R<String> {
    canonical_text(&contract_for(root, month)?)
}

/// Resolve a one-digit year against a reference date: the nearest year ending in `digit`
/// that is at most one year in the past (`ref_year - 1`) and at most ten years ahead
/// (`ref_year + 10`). Ties go to the earlier year. Never reads a clock; the caller
/// passes the date.
pub fn resolve_year_digit(digit: u32, reference: NaiveDate) -> R<i32> {
    if digit > 9 {
        return err("value", format!("One-digit contract year must be 0..=9, got {digit}"));
    }
    let ref_year = reference.year();
    let lo = ref_year - 1;
    let hi = ref_year + 10;
    let mut best: Option<i32> = None;
    for year in lo..=hi {
        if year.rem_euclid(10) != digit as i32 {
            continue;
        }
        best = Some(match best {
            None => year,
            Some(current) => {
                let (new_gap, cur_gap) = ((year - ref_year).abs(), (current - ref_year).abs());
                if new_gap < cur_gap {
                    year
                } else {
                    current
                }
            }
        });
    }
    match best {
        Some(year) => Ok(year),
        None => err(
            "value",
            format!("No year ending in {digit} within {lo}..={hi} of reference date {reference}"),
        ),
    }
}

// --- parsing platform spellings ----------------------------------------------

/// Refuse whitespace that is not part of the grammar: a leading/trailing space, a tab,
/// or any inner space other than the single NT8 separator.
fn refuse_side_whitespace(label: &str, text: &str) -> R<()> {
    if text.is_empty() {
        return err("value", format!("{label} symbol must be a non-empty string"));
    }
    if text.trim() != text {
        return err("value", format!("{label} symbol '{text}' has surrounding whitespace"));
    }
    if text.chars().any(|c| c == '\t' || c == '\n' || c == '\r') {
        return err("value", format!("{label} symbol '{text}' contains whitespace"));
    }
    Ok(())
}

/// These spellings are uppercase. Lowercase is refused, never folded (I5).
fn require_uppercase(label: &str, text: &str) -> R<()> {
    if text != text.to_ascii_uppercase() {
        return err("value", format!("{label} symbol '{text}' must be uppercase"));
    }
    Ok(())
}

/// The longest matching root at the start of `text`, plus the remainder.
fn split_root<'a>(label: &str, text: &'a str) -> R<(&'static str, &'a str)> {
    let mut roots: Vec<&'static str> = FUTURE_SPECS.iter().map(|s| s.root).collect();
    roots.sort_by_key(|r| std::cmp::Reverse(r.len()));
    for root in roots {
        if let Some(rest) = text.strip_prefix(root) {
            return Ok((root, rest));
        }
    }
    err("unsupported", format!("Unknown futures root in {label} symbol '{text}'"))
}

/// Parse `ROOT[MONTH][YEAR]` in the canonical style, with the year length that platform
/// uses (`One` = `NQZ6`, `Two` = `NQZ26`, `Four` = `NQZ2026`). An empty year tail means a
/// continuous symbol.
fn parse_coded(label: &str, text: &str, digits: usize, reference: NaiveDate) -> R<SymbolRef> {
    let (root, rest) = split_root(label, text)?;
    let spec = spec_for_root(root)?;
    if rest.is_empty() {
        return Ok(SymbolRef::Continuous(spec.root.to_string()));
    }
    let mut chars = rest.char_indices();
    let Some((_, code)) = chars.next() else {
        return err("unsupported", format!("Unknown futures root in {label} symbol '{text}'"));
    };
    let month = match month_from_code(code) {
        Some(month) if code.is_ascii_uppercase() => month,
        Some(_) => {
            return err("value", format!("{label} symbol '{text}' must be uppercase"));
        }
        None => {
            return err(
                "value",
                format!("Invalid contract month code '{code}' in {label} symbol '{text}'"),
            )
        }
    };
    let year_text = &rest[code.len_utf8()..];
    if year_text.is_empty() {
        return err("value", format!("Missing contract year in {label} symbol '{text}'"));
    }
    if !year_text.chars().all(|c| c.is_ascii_digit()) {
        return err(
            "value",
            format!("Invalid contract year '{year_text}' in {label} symbol '{text}': expected digits"),
        );
    }
    if year_text.len() != digits {
        return err(
            "value",
            format!(
                "Invalid contract year '{year_text}' in {label} symbol '{text}': expected {digits} digit(s)"
            ),
        );
    }
    let year = match digits {
        1 => {
            let digit: u32 = year_text
                .parse()
                .map_err(|_| lerr("value", format!("Invalid contract year '{year_text}'")))?;
            resolve_year_digit(digit, reference)?
        }
        2 => 2000 + year_text.parse::<i32>().map_err(|_| lerr("value", "invalid year digits"))?,
        4 => year_text.parse::<i32>().map_err(|_| lerr("value", "invalid year digits"))?,
        other => return err("value", format!("internal error: unsupported year length {other}")),
    };
    Ok(SymbolRef::Contract(contract_for(
        spec.root,
        ContractMonth::new(year, month)?,
    )?))
}

/// Parse a platform spelling into either a dated contract or a continuous root.
///
/// Refusals, each with a `kind`:
/// - `unsupported`: unknown platform, unknown root;
/// - `value`: malformed spelling, whitespace, lowercase, unknown month code, missing or
///   wrong-length year.
pub fn from_platform(platform: Platform, text: &str, reference: NaiveDate) -> R<SymbolRef> {
    refuse_side_whitespace(platform.name(), text)?;
    require_uppercase(platform.name(), text)?;
    match platform {
        Platform::Tradovate => parse_coded("tradovate", text, 1, reference),
        Platform::TradingView => {
            if let Some(root) = text.strip_suffix("1!") {
                let spec = spec_for_root(root)?;
                return Ok(SymbolRef::Continuous(spec.root.to_string()));
            }
            parse_coded("tradingview", text, 4, reference)
        }
        Platform::Schwab => {
            let bare = text.strip_prefix('/').ok_or_else(|| {
                lerr("value", format!("schwab symbol '{text}' must start with '/'"))
            })?;
            if bare.is_empty() || bare.starts_with('/') {
                return err("value", format!("schwab symbol '{text}' is malformed"));
            }
            parse_coded("schwab", bare, 2, reference)
        }
        Platform::Nt8 => {
            let mut parts = text.split(' ');
            let root = parts.next().unwrap_or_default();
            let tail = parts.next();
            if parts.next().is_some() {
                return err("value", format!("nt8 symbol '{text}' has more than one space"));
            }
            let Some(tail) = tail else {
                // Bare root: continuous.
                let spec = spec_for_root(root)?;
                return Ok(SymbolRef::Continuous(spec.root.to_string()));
            };
            let (month_text, year_text) = tail.split_once('-').ok_or_else(|| {
                lerr("value", format!("nt8 symbol '{text}' must be 'ROOT MM-YY'"))
            })?;
            if month_text.len() != 2
                || year_text.len() != 2
                || !month_text.chars().all(|c| c.is_ascii_digit())
                || !year_text.chars().all(|c| c.is_ascii_digit())
            {
                return err("value", format!("nt8 symbol '{text}' must be 'ROOT MM-YY'"));
            }
            let month: u32 = month_text
                .parse()
                .map_err(|_| lerr("value", format!("invalid month in nt8 symbol '{text}'")))?;
            let year = 2000 + year_text
                .parse::<i32>()
                .map_err(|_| lerr("value", format!("invalid year in nt8 symbol '{text}'")))?;
            let spec = spec_for_root(root)?;
            Ok(SymbolRef::Contract(contract_for(
                spec.root,
                ContractMonth::new(year, month)?,
            )?))
        }
        Platform::Ibkr => parse_coded("ibkr", text, 1, reference),
    }
}

/// Parse the dated contract of a platform spelling. A continuous spelling (`NQ`, `NQ1!`,
/// `/NQ`) is refused with kind `continuous`: use [`front_month`] for it.
pub fn from_platform_contract(
    platform: Platform,
    text: &str,
    reference: NaiveDate,
) -> R<FutureContract> {
    from_platform(platform, text, reference)?.into_contract()
}

/// IBKR's `lastTradeDateOrContractMonth` (`202612`) checked against its `localSymbol`
/// (`NQZ6`). Both must name the same month; the dated field supplies the full year and the
/// local symbol's one-digit year must agree, otherwise the pair is refused with kind
/// `value`.
pub fn from_ibkr(local: &str, last_trade_date: Option<&str>, reference: NaiveDate) -> R<SymbolRef> {
    let parsed = from_platform(Platform::Ibkr, local, reference)?;
    let Some(last) = last_trade_date else {
        return Ok(parsed);
    };
    if last.len() != 6 || !last.chars().all(|c| c.is_ascii_digit()) {
        return err(
            "value",
            format!("ibkr lastTradeDateOrContractMonth '{last}' must be YYYYMM"),
        );
    }
    let year: i32 = last[..4]
        .parse()
        .map_err(|_| lerr("value", format!("invalid year in ibkr date '{last}'")))?;
    let month: u32 = last[4..]
        .parse()
        .map_err(|_| lerr("value", format!("invalid month in ibkr date '{last}'")))?;
    let contract = match parsed {
        SymbolRef::Contract(c) => c,
        SymbolRef::Continuous(root) => {
            return err(
                "value",
                format!("ibkr localSymbol '{root}' is continuous but '{last}' names a dated contract"),
            )
        }
    };
    let month_of_contract = require_month(&contract)?;
    if month_of_contract.month != month {
        return err(
            "value",
            format!(
                "ibkr localSymbol '{local}' is month {} but lastTradeDateOrContractMonth '{last}' is month {month}",
                month_of_contract.month
            ),
        );
    }
    if month_of_contract.year.rem_euclid(10) != year.rem_euclid(10) {
        return err(
            "value",
            format!(
                "ibkr localSymbol '{local}' year {} disagrees with lastTradeDateOrContractMonth '{last}'",
                month_of_contract.year
            ),
        );
    }
    Ok(SymbolRef::Contract(contract_for(
        &contract.root,
        ContractMonth::new(year, month)?,
    )?))
}

// --- emitting platform spellings ---------------------------------------------

/// The dated contract as this platform spells it.
pub fn to_platform(platform: Platform, contract: &FutureContract) -> R<String> {
    let month = require_month(contract)?;
    let spec = spec_for_root(&contract.root)?;
    let code = month.month_code();
    Ok(match platform {
        Platform::Nt8 => format!("{} {:02}-{:02}", spec.root, month.month, month.year.rem_euclid(100)),
        Platform::Tradovate => format!("{}{}{}", spec.root, code, month.year.rem_euclid(10)),
        Platform::TradingView => format!("{}{}{:04}", spec.root, code, month.year),
        Platform::Schwab => format!("/{}{}{:02}", spec.root, code, month.year.rem_euclid(100)),
        Platform::Ibkr => format!("{}{}{}", spec.root, code, month.year.rem_euclid(10)),
    })
}

/// IBKR's `lastTradeDateOrContractMonth` for a dated contract: `202612`.
pub fn ibkr_last_trade_date(contract: &FutureContract) -> R<String> {
    let month = require_month(contract)?;
    Ok(format!("{:04}{:02}", month.year, month.month))
}

/// The continuous spelling of a root: TradingView `NQ1!`, Schwab `/NQ`, everywhere else
/// the bare root.
pub fn continuous_text(platform: Platform, root: &str) -> R<String> {
    let spec = spec_for_root(root)?;
    Ok(match platform {
        Platform::TradingView => format!("{}1!", spec.root),
        Platform::Schwab => format!("/{}", spec.root),
        Platform::Nt8 | Platform::Tradovate | Platform::Ibkr => spec.root.to_string(),
    })
}

// --- expiry and roll table ---------------------------------------------------

/// First contract year in the dated table.
pub const TABLE_FIRST_YEAR: i32 = 2024;
/// Last contract year in the dated table.
pub const TABLE_LAST_YEAR: i32 = 2030;

/// One dated row: a contract month with its expiry and its roll date.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ContractSchedule {
    pub root: &'static str,
    pub month: ContractMonth,
    pub expiry: NaiveDate,
    pub roll_date: NaiveDate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Cycle {
    /// Quarterly (H/M/U/Z) equity index: third Friday, roll 8 days before.
    Index,
    /// Monthly energy: 3 business days before the 25th of the prior month, roll 5 days
    /// before.
    Energy,
    /// Monthly metals: third-to-last business day of the contract month, roll 5 days
    /// before.
    Metals,
}

fn cycle_for_root(root: &'static str) -> Cycle {
    match root {
        "NQ" | "MNQ" | "ES" | "MES" | "YM" | "MYM" | "RTY" | "M2K" => Cycle::Index,
        "CL" | "MCL" => Cycle::Energy,
        "GC" | "MGC" => Cycle::Metals,
        other => unreachable!("every root in FUTURE_SPECS has a cycle; got {other}"),
    }
}

fn is_business_day(d: NaiveDate) -> bool {
    !matches!(d.weekday(), Weekday::Sat | Weekday::Sun)
}

fn days(n: i64) -> Duration {
    Duration::days(n)
}

fn nth_weekday(year: i32, month: u32, weekday: Weekday, n: u32) -> R<NaiveDate> {
    let first = NaiveDate::from_ymd_opt(year, month, 1)
        .ok_or_else(|| lerr("value", format!("no such month: {year}-{month}")))?;
    let offset = (weekday.num_days_from_monday() as i64
        - first.weekday().num_days_from_monday() as i64)
        .rem_euclid(7);
    let day = first + days(offset + 7 * (n as i64 - 1));
    if day.month() != month {
        return err("value", format!("{year}-{month} has no weekday {weekday:?} #{n}"));
    }
    Ok(day)
}

fn last_day_of_month(year: i32, month: u32) -> R<NaiveDate> {
    let (ny, nm) = if month == 12 { (year + 1, 1) } else { (year, month + 1) };
    let first_next = NaiveDate::from_ymd_opt(ny, nm, 1)
        .ok_or_else(|| lerr("value", format!("no such month: {ny}-{nm}")))?;
    first_next
        .pred_opt()
        .ok_or_else(|| lerr("value", "date out of range"))
}

fn nth_business_day_before(date: NaiveDate, n: u32) -> R<NaiveDate> {
    let mut d = date;
    let mut seen = 0;
    while seen < n {
        d = d
            .pred_opt()
            .ok_or_else(|| lerr("value", "date out of range"))?;
        if is_business_day(d) {
            seen += 1;
        }
    }
    Ok(d)
}

fn nth_last_business_day(year: i32, month: u32, n: u32) -> R<NaiveDate> {
    let mut d = last_day_of_month(year, month)?;
    let mut seen = 0;
    loop {
        if is_business_day(d) {
            seen += 1;
            if seen == n {
                return Ok(d);
            }
        }
        d = d
            .pred_opt()
            .ok_or_else(|| lerr("value", "date out of range"))?;
    }
}

fn refuse_outside_table(root: &str, month: ContractMonth) -> LErr {
    lerr(
        "unsupported",
        format!(
            "{root}{} ({} {}) is outside the roll table ({TABLE_FIRST_YEAR}-{TABLE_LAST_YEAR}); it is refused, never extrapolated",
            month.month_code(),
            month.year,
            month.month
        ),
    )
}

/// The dated schedule for one root and contract month.
///
/// Refusals, all with kind `unsupported`: unknown root, a year outside
/// [`TABLE_FIRST_YEAR`]..=[`TABLE_LAST_YEAR`], a month outside the root's listed cycle
/// (the index roots list H/M/U/Z only).
pub fn contract_schedule(root: &str, month: ContractMonth) -> R<ContractSchedule> {
    let spec = spec_for_root(root)?;
    if month.year < TABLE_FIRST_YEAR || month.year > TABLE_LAST_YEAR {
        return Err(refuse_outside_table(spec.root, month));
    }
    let cycle = cycle_for_root(spec.root);
    if cycle == Cycle::Index && ![3, 6, 9, 12].contains(&month.month) {
        return err(
            "unsupported",
            format!(
                "{} lists only the quarterly H/M/U/Z contracts; {} is not in the roll table",
                spec.root,
                month.month_code()
            ),
        );
    }
    let (expiry, roll_date) = match cycle {
        // Equity index: last trade day is the third Friday of the contract month; the
        // volume roll happens 8 calendar days earlier.
        Cycle::Index => {
            let expiry = nth_weekday(month.year, month.month, Weekday::Fri, 3)?;
            (expiry, expiry - days(8))
        }
        // NYMEX Rule 200.C (CL, and MCL on the same rule): last trade day is three
        // business days before the 25th of the month preceding the delivery month.
        Cycle::Energy => {
            let (py, pm) = if month.month == 1 {
                (month.year - 1, 12)
            } else {
                (month.year, month.month - 1)
            };
            let the_25th = NaiveDate::from_ymd_opt(py, pm, 25)
                .ok_or_else(|| lerr("value", "date out of range"))?;
            let expiry = nth_business_day_before(the_25th, 3)?;
            (expiry, expiry - days(5))
        }
        // COMEX gold: last trade day is the third-to-last business day of the contract
        // month.
        Cycle::Metals => {
            let expiry = nth_last_business_day(month.year, month.month, 3)?;
            (expiry, expiry - days(5))
        }
    };
    Ok(ContractSchedule {
        root: spec.root,
        month,
        expiry,
        roll_date,
    })
}

/// The whole dated table for one root, in delivery order, 2024-2030.
pub fn roll_table(root: &str) -> R<Vec<ContractSchedule>> {
    let spec = spec_for_root(root)?;
    let months: Vec<u32> = match cycle_for_root(spec.root) {
        Cycle::Index => vec![3, 6, 9, 12],
        Cycle::Energy | Cycle::Metals => (1..=12).collect(),
    };
    let mut out = Vec::with_capacity(months.len() * (TABLE_LAST_YEAR - TABLE_FIRST_YEAR + 1) as usize);
    for year in TABLE_FIRST_YEAR..=TABLE_LAST_YEAR {
        for month in &months {
            out.push(contract_schedule(spec.root, ContractMonth::new(year, *month)?)?);
        }
    }
    Ok(out)
}

/// The front month on `as_of`: the earliest contract in `table` whose roll date is after
/// `as_of`. The day before a roll date still gives the old contract; on the roll date it
/// gives the next one. The date is an argument (I7).
///
/// Refused with kind `unsupported` when no contract in the table is still rolling after
/// `as_of` (the date is beyond the table), and with kind `value` when the table does not
/// belong to `root`.
pub fn front_month(root: &str, as_of: NaiveDate, table: &[ContractSchedule]) -> R<ContractSchedule> {
    let spec = spec_for_root(root)?;
    if table.is_empty() {
        return err("unsupported", format!("empty roll table for {}", spec.root));
    }
    if let Some(row) = table.iter().find(|row| row.root != spec.root) {
        return err(
            "value",
            format!("roll table for {} contains a {} row", spec.root, row.root),
        );
    }
    table
        .iter()
        .filter(|row| row.roll_date > as_of)
        .min_by_key(|row| row.roll_date)
        .copied()
        .ok_or_else(|| {
            lerr(
                "unsupported",
                format!(
                    "front month for {} is not in the roll table on {as_of}: the table ends {TABLE_LAST_YEAR}-12-31",
                    spec.root
                ),
            )
        })
}

/// The front month using the root's own table.
pub fn front_month_for(root: &str, as_of: NaiveDate) -> R<ContractSchedule> {
    let table = roll_table(root)?;
    front_month(root, as_of, &table)
}

/// The front-month contract of `root` on `as_of`.
pub fn front_month_contract(root: &str, as_of: NaiveDate) -> R<FutureContract> {
    let row = front_month_for(root, as_of)?;
    contract_for(row.root, row.month)
}

// --- wire --------------------------------------------------------------------

fn session_name(root: &str) -> &'static str {
    crate::calendar::globex::GlobexCalendar::for_root(root)
        .map(|cal| cal.group().name())
        .unwrap_or("globex")
}

/// The instrument table as JSON: one row per root, every number a string (D6).
///
/// Shape (stable for the read API): `[{"root","exchange","currency","tick_size",
/// "point_value","tick_value","session"}, ...]` in table order.
pub fn instruments_json() -> String {
    let rows: Vec<serde_json::Value> = FUTURE_SPECS
        .iter()
        .map(|s| {
            serde_json::json!({
                "root": s.root,
                "exchange": s.exchange,
                "currency": s.currency,
                "tick_size": s.tick_size_str,
                "point_value": s.point_value_str,
                "tick_value": s.tick_value_str,
                "session": session_name(s.root),
            })
        })
        .collect();
    serde_json::to_string(&rows).unwrap_or_else(|_| "[]".to_string())
}

/// One row of the table for one root, or a refusal.
pub fn spec_json(root: &str) -> R<String> {
    let spec: FutureSpec = spec_for_root(root)?;
    Ok(serde_json::json!({
        "root": spec.root,
        "exchange": spec.exchange,
        "currency": spec.currency,
        "tick_size": spec.tick_size.canon(),
        "point_value": spec.point_value.canon(),
        "tick_value": spec.tick_value.canon(),
        "session": session_name(spec.root),
    })
    .to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::model::dec_eq;
    use crate::money::Money;
    use proptest::prelude::*;

    fn day(y: i32, m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, d).unwrap()
    }

    fn ctext(platform: Platform, text: &str, reference: NaiveDate) -> String {
        canonical_text(&from_platform_contract(platform, text, reference).unwrap()).unwrap()
    }

    const PLATFORMS: [Platform; 5] = [
        Platform::Nt8,
        Platform::Tradovate,
        Platform::TradingView,
        Platform::Schwab,
        Platform::Ibkr,
    ];

    #[test]
    fn platform_names_round_trip_and_refuse_unknown() {
        for platform in PLATFORMS {
            assert_eq!(Platform::from_name(platform.name()).unwrap(), platform);
            assert_eq!(
                Platform::from_name(&platform.name().to_ascii_uppercase()).unwrap(),
                platform
            );
        }
        assert_eq!(Platform::from_name("tv").unwrap(), Platform::TradingView);
        let unknown = Platform::from_name("vela").unwrap_err();
        assert_eq!(unknown.kind, "unsupported");
        assert!(unknown.msg.contains("Unknown platform"));
    }

    /// The design's worked example, one platform spelling at a time (B2).
    #[test]
    fn every_platform_spelling_of_nq_dec_2026_is_one_contract() {
        let reference = day(2026, 11, 11);
        let want = "NQZ6";
        assert_eq!(ctext(Platform::Nt8, "NQ 12-26", reference), want);
        assert_eq!(ctext(Platform::Tradovate, "NQZ6", reference), want);
        assert_eq!(ctext(Platform::TradingView, "NQZ2026", reference), want);
        assert_eq!(ctext(Platform::Schwab, "/NQZ26", reference), want);
        assert_eq!(ctext(Platform::Ibkr, "NQZ6", reference), want);

        let ibkr = from_ibkr("NQZ6", Some("202612"), reference)
            .unwrap()
            .into_contract()
            .unwrap();
        assert_eq!(canonical_text(&ibkr).unwrap(), want);
        assert_eq!(ibkr_last_trade_date(&ibkr).unwrap(), "202612");
        // The exchange's dated field disambiguates the one-digit year.
        assert_eq!(
            canonical_text(&from_ibkr("NQZ6", Some("203612"), day(2030, 1, 2)).unwrap().into_contract().unwrap()).unwrap(),
            "NQZ6"
        );

        // Back out to every platform spelling.
        let contract = contract_for("NQ", ContractMonth::new(2026, 12).unwrap()).unwrap();
        assert_eq!(to_platform(Platform::Nt8, &contract).unwrap(), "NQ 12-26");
        assert_eq!(to_platform(Platform::Tradovate, &contract).unwrap(), "NQZ6");
        assert_eq!(to_platform(Platform::TradingView, &contract).unwrap(), "NQZ2026");
        assert_eq!(to_platform(Platform::Schwab, &contract).unwrap(), "/NQZ26");
        assert_eq!(to_platform(Platform::Ibkr, &contract).unwrap(), "NQZ6");

        // Micros are not swallowed by their minis.
        assert_eq!(ctext(Platform::Tradovate, "MNQZ6", reference), "MNQZ6");
        assert_eq!(ctext(Platform::Tradovate, "MCLZ6", reference), "MCLZ6");
        assert_eq!(ctext(Platform::Tradovate, "MGCZ6", reference), "MGCZ6");
    }

    // Every root, every month of 2024-2030, every platform: canonical -> spelling ->
    // canonical is the identity. The reference date sits in the contract's own year so
    // the one-digit year is unambiguous.
    proptest! {
        #[test]
        fn spellings_round_trip_for_every_root_and_month(
            root_index in 0usize..12,
            year in TABLE_FIRST_YEAR..=TABLE_LAST_YEAR,
            month in 1u32..=12,
        ) {
            let root = FUTURE_SPECS[root_index].root;
            let reference = NaiveDate::from_ymd_opt(year, month, 15).unwrap();
            let contract = contract_for(root, ContractMonth::new(year, month).unwrap()).unwrap();
            let canonical = canonical_text(&contract).unwrap();
            let again = canonical_text_for(root, ContractMonth::new(year, month).unwrap()).unwrap();
            prop_assert_eq!(again.as_str(), canonical.as_str());
            for platform in PLATFORMS {
                let spelled = to_platform(platform, &contract).unwrap();
                let back = from_platform_contract(platform, &spelled, reference).unwrap();
                let back_text = canonical_text(&back).unwrap();
                prop_assert_eq!(back_text.as_str(), canonical.as_str(), "{} {:?}", root, platform);
                let text = from_platform(platform, &spelled, reference).unwrap().into_contract().unwrap();
                let text_text = canonical_text(&text).unwrap();
                prop_assert_eq!(text_text.as_str(), canonical.as_str());
            }
            // IBKR pairs the local symbol with the dated field.
            let local = to_platform(Platform::Ibkr, &contract).unwrap();
            let dated = ibkr_last_trade_date(&contract).unwrap();
            let back = from_ibkr(&local, Some(&dated), reference).unwrap().into_contract().unwrap();
            let back_text = canonical_text(&back).unwrap();
            prop_assert_eq!(back_text.as_str(), canonical.as_str());
        }
    }

    /// Continuous spellings: `NQ`, `NQ1!`, `/NQ` name the root, never a contract.
    #[test]
    fn continuous_spellings_never_name_a_contract() {
        let reference = day(2026, 6, 1);
        let cases = [
            (Platform::Nt8, "NQ"),
            (Platform::Tradovate, "NQ"),
            (Platform::TradingView, "NQ1!"),
            (Platform::Schwab, "/NQ"),
            (Platform::Ibkr, "NQ"),
        ];
        for (platform, text) in cases {
            match from_platform(platform, text, reference).unwrap() {
                SymbolRef::Continuous(root) => assert_eq!(root, "NQ"),
                other => panic!("{text} should be continuous, got {other:?}"),
            }
            let err = from_platform_contract(platform, text, reference).unwrap_err();
            assert_eq!(err.kind, "continuous", "{text}");
            assert!(err.msg.contains("front_month"));
            assert_eq!(continuous_text(platform, "NQ").unwrap(), text);
        }
        // A bare non-root is still an unknown root.
        assert_eq!(from_platform(Platform::Tradovate, "ZN", reference).unwrap_err().kind, "unsupported");
        assert_eq!(from_platform(Platform::TradingView, "ZN1!", reference).unwrap_err().kind, "unsupported");
    }

    #[test]
    fn refusals_carry_a_kind() {
        let reference = day(2026, 11, 11);
        // Unknown root.
        for text in ["ZN", "ZN1!", "/ZNZ26", "NQ 12-26 "] {
            let e = from_platform(Platform::Tradovate, text, reference).unwrap_err();
            assert!(e.kind == "unsupported" || e.kind == "value", "{text}: {:?}", e.kind);
        }
        assert_eq!(from_platform(Platform::Tradovate, "ZN", reference).unwrap_err().kind, "unsupported");
        assert_eq!(from_platform(Platform::Nt8, "ZB 03-27", reference).unwrap_err().kind, "unsupported");
        // Unknown month code (and a digit in the month slot).
        assert_eq!(from_platform(Platform::Tradovate, "NQA6", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Tradovate, "NQ16", reference).unwrap_err().kind, "value");
        // Malformed spelling.
        assert_eq!(from_platform(Platform::Tradovate, "NQZ2026", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::TradingView, "NQZ6", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Schwab, "NQZ26", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Schwab, "//NQZ26", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Nt8, "NQ 12/26", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Nt8, "NQ 12-2026", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Nt8, "NQ 1-26", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Nt8, "NQ  12-26", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Tradovate, "NQZ", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Tradovate, "NQZX", reference).unwrap_err().kind, "value");
        assert_eq!(from_platform(Platform::Ibkr, "NQZ6", reference).unwrap().into_contract().unwrap().root, "NQ");
        // IBKR date disagreement.
        assert_eq!(from_ibkr("NQZ6", Some("202506"), reference).unwrap_err().kind, "value");
        assert_eq!(from_ibkr("NQZ6", Some("202512"), reference).unwrap_err().kind, "value");
        assert_eq!(from_ibkr("NQZ6", Some("20261"), reference).unwrap_err().kind, "value");
        assert_eq!(from_ibkr("NQZ6", Some("202612"), reference).unwrap().into_contract().unwrap().contract_month, Some(ContractMonth { year: 2026, month: 12 }));
        // Whitespace and wrong case, every platform.
        for platform in PLATFORMS {
            let text = to_platform(platform, &contract_for("NQ", ContractMonth::new(2026, 12).unwrap()).unwrap()).unwrap();
            assert_eq!(from_platform(platform, &format!(" {text}"), reference).unwrap_err().kind, "value", "{platform:?} leading space");
            assert_eq!(from_platform(platform, &format!("{text} "), reference).unwrap_err().kind, "value", "{platform:?} trailing space");
            assert_eq!(from_platform(platform, &format!("\t{text}"), reference).unwrap_err().kind, "value", "{platform:?} tab");
            assert_eq!(from_platform(platform, &text.to_ascii_lowercase(), reference).unwrap_err().kind, "value", "{platform:?} lowercase");
            assert_eq!(from_platform(platform, "", reference).unwrap_err().kind, "value");
        }
    }

    /// One-digit years: nearest year at most one year in the past, at most ten ahead.
    /// Both sides of a decade boundary: `NQZ0` is 2030 while 2030 is reachable, and 2040
    /// once 2030 is two years behind.
    #[test]
    fn one_digit_year_resolves_by_the_decade_rule() {
        assert_eq!(resolve_year_digit(6, day(2026, 6, 1)).unwrap(), 2026);
        assert_eq!(resolve_year_digit(6, day(2036, 6, 1)).unwrap(), 2036);
        assert_eq!(resolve_year_digit(4, day(2024, 1, 15)).unwrap(), 2024);
        // 2030 is one year in the past here: allowed, and nearer than 2040.
        assert_eq!(resolve_year_digit(0, day(2031, 6, 15)).unwrap(), 2030);
        // 2030 is now two years in the past: refused by the window, so 2040 is chosen.
        assert_eq!(resolve_year_digit(0, day(2032, 6, 15)).unwrap(), 2040);
        // Ahead of the reference, nearest wins.
        assert_eq!(resolve_year_digit(0, day(2029, 12, 15)).unwrap(), 2030);
        // Through a platform spelling, the same rule.
        assert_eq!(ctext(Platform::Tradovate, "NQZ0", day(2031, 6, 15)), "NQZ0");
        let contract = from_platform_contract(Platform::Tradovate, "NQZ0", day(2031, 6, 15)).unwrap();
        assert_eq!(contract.contract_month, Some(ContractMonth { year: 2030, month: 12 }));
        let contract = from_platform_contract(Platform::Tradovate, "NQZ0", day(2032, 6, 15)).unwrap();
        assert_eq!(contract.contract_month, Some(ContractMonth { year: 2040, month: 12 }));
        // The canonical text keeps one digit, so NQZ6 and NQZ26 are the same month.
        let c = contract_for("NQ", ContractMonth::new(2036, 12).unwrap()).unwrap();
        assert_eq!(canonical_text(&c).unwrap(), "NQZ6");
        assert_eq!(canonical_text(&contract_for("NQ", ContractMonth::new(2026, 12).unwrap()).unwrap()).unwrap(), "NQZ6");
    }

    /// The cited rules, on known dates.
    #[test]
    fn expiry_and_roll_rows_follow_the_cited_rules() {
        // Equity index: third Friday, roll 8 calendar days earlier.
        let nq = contract_schedule("NQ", ContractMonth::new(2026, 3).unwrap()).unwrap();
        assert_eq!(nq.expiry, day(2026, 3, 20));
        assert_eq!(nq.roll_date, day(2026, 3, 12));
        assert_eq!(nq.root, "NQ");
        let es = contract_schedule("ES", ContractMonth::new(2024, 12).unwrap()).unwrap();
        assert_eq!(es.expiry, day(2024, 12, 20));
        assert_eq!(es.roll_date, day(2024, 12, 12));
        // Energy: three business days before the 25th of the prior month.
        let cl = contract_schedule("CL", ContractMonth::new(2026, 3).unwrap()).unwrap();
        assert_eq!(cl.expiry, day(2026, 2, 20));
        assert_eq!(cl.roll_date, day(2026, 2, 15));
        // January rolls into the prior December.
        let cljan = contract_schedule("CL", ContractMonth::new(2026, 1).unwrap()).unwrap();
        assert_eq!(cljan.expiry, day(2025, 12, 22));
        // Metals: third-to-last business day of the contract month.
        let gc = contract_schedule("GC", ContractMonth::new(2026, 2).unwrap()).unwrap();
        assert_eq!(gc.expiry, day(2026, 2, 25));
        assert_eq!(gc.roll_date, day(2026, 2, 20));
    }

    /// The dated table is bounded: 2024-2030 only, listed months only.
    #[test]
    fn the_table_is_dated_and_refuses_outside_it() {
        let nq = roll_table("NQ").unwrap();
        assert_eq!(nq.len(), 4 * 7);
        assert_eq!(nq.first().unwrap().month.year, TABLE_FIRST_YEAR);
        assert_eq!(nq.last().unwrap().month.year, TABLE_LAST_YEAR);
        for row in &nq {
            assert_eq!(row.month.month % 3, 0, "index roots list H/M/U/Z only");
            assert!(row.roll_date < row.expiry, "roll precedes expiry");
        }
        let cl = roll_table("CL").unwrap();
        assert_eq!(cl.len(), 12 * 7);
        let gc = roll_table("MGC").unwrap();
        assert_eq!(gc.len(), 12 * 7);
        for row in cl.iter().chain(gc.iter()) {
            assert!(row.roll_date <= row.expiry);
        }

        // An index root's non-quarterly month is refused, not extrapolated.
        let wrong_cycle = contract_schedule("NQ", ContractMonth::new(2026, 7).unwrap()).unwrap_err();
        assert_eq!(wrong_cycle.kind, "unsupported");
        assert!(wrong_cycle.msg.contains("quarterly"));
        // A year outside the table is refused.
        for year in [2023, 2031, 2040] {
            let e = contract_schedule("CL", ContractMonth::new(year, 1).unwrap()).unwrap_err();
            assert_eq!(e.kind, "unsupported");
            assert!(e.msg.contains("outside the roll table"));
        }
        // An unknown root is refused.
        assert_eq!(roll_table("ZN").unwrap_err().kind, "unsupported");
        assert_eq!(contract_schedule("SPY", ContractMonth::new(2026, 3).unwrap()).unwrap_err().kind, "unsupported");
    }

    /// `NQ1!` on both sides of a roll date, and a date beyond the table refused.
    #[test]
    fn front_month_crosses_the_roll_date_and_refuses_beyond_the_table() {
        // NQH6 rolls 2026-03-12.
        assert_eq!(front_month_contract("NQ", day(2026, 3, 11)).unwrap().contract_month, Some(ContractMonth { year: 2026, month: 3 }));
        assert_eq!(front_month_contract("NQ", day(2026, 3, 12)).unwrap().contract_month, Some(ContractMonth { year: 2026, month: 6 }));
        // TradingView's continuous spelling resolves only through front_month.
        assert_eq!(
            canonical_text(&front_month_contract("NQ", day(2026, 3, 11)).unwrap()).unwrap(),
            "NQH6"
        );
        assert_eq!(
            canonical_text(&front_month_contract("NQ", day(2026, 3, 12)).unwrap()).unwrap(),
            "NQM6"
        );
        // A monthly root rolls every month.
        assert_eq!(front_month_contract("CL", day(2026, 2, 14)).unwrap().contract_month, Some(ContractMonth { year: 2026, month: 3 }));
        assert_eq!(front_month_contract("CL", day(2026, 2, 15)).unwrap().contract_month, Some(ContractMonth { year: 2026, month: 4 }));
        // Beyond the table: refused.
        let beyond = front_month_contract("NQ", day(2031, 1, 1)).unwrap_err();
        assert_eq!(beyond.kind, "unsupported");
        assert!(beyond.msg.contains("not in the roll table"));
        // A table for another root is refused.
        let es = roll_table("ES").unwrap();
        assert_eq!(front_month("NQ", day(2026, 3, 11), &es).unwrap_err().kind, "value");
        // An unknown root is refused.
        assert_eq!(front_month_for("ZN", day(2026, 3, 11)).unwrap_err().kind, "unsupported");
    }

    /// The wire table is strings only (D6) and carries every root once.
    #[test]
    fn instrument_table_is_json_strings_from_the_one_table() {
        let text = instruments_json();
        let rows: serde_json::Value = serde_json::from_str(&text).unwrap();
        let rows = rows.as_array().unwrap();
        assert_eq!(rows.len(), FUTURE_SPECS.len());
        for (row, spec) in rows.iter().zip(FUTURE_SPECS.iter()) {
            assert_eq!(row["root"], spec.root);
            assert_eq!(row["exchange"], spec.exchange);
            assert_eq!(row["tick_size"], spec.tick_size_str);
            assert_eq!(row["point_value"], spec.point_value_str);
            assert_eq!(row["tick_value"], spec.tick_value_str);
            assert_eq!(row["currency"], "USD");
            assert!(row["session"].is_string());
        }
        let nq: serde_json::Value = serde_json::from_str(&spec_json("NQ").unwrap()).unwrap();
        assert_eq!(nq["tick_value"], "5");
        assert!(dec_eq(&Money::parse(nq["tick_value"].as_str().unwrap()).unwrap(), &Money::parse("5.00").unwrap()));
    }
}
