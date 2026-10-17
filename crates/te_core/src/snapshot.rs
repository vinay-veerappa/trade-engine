//! Daily account snapshot (E8b, rules doc §11).
//!
//! Derived from the ledger: one snapshot per account per session close, using
//! the Mark events the EOD runner wrote. Nothing is written back to the ledger (I1, I2);
//! the peak margin and drawdown are defined over the requested window, not the
//! ledger's lifetime. Heat is the sum of open risk against the current protective
//! stops (working stop orders in the folded state on the reduce side); a position
//! with no working stop contributes an unknown heat entry, listed — never guessed (I5).

use std::collections::{HashMap, HashSet};
use std::fmt;

use chrono::NaiveDate;
use rust_decimal::Decimal;
use rust_decimal::RoundingStrategy;

use crate::ledger::fold::{fold_account, AccountState, Position as LedgerPosition};
use crate::ledger::model::{DateTime, Event, EventKind, Instrument, Obj, OrderState, OrderType, Side};
use crate::margin::option::{
    definitions as strat_definitions, Contract as MarginContract, DEFAULT_SEARCH_LIMIT,
    Style as MarginStyle,
};
use crate::margin::{
    account_margin, margin_summary, AccountInput, AccountMargin, Kind as MarginKind, MarginError,
    Position as MarginPosition,
};
use crate::options::option_style;

/// Raised when the ledger cannot support a snapshot (I5).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SnapshotError {
    NoAsOf { kind: String },
    NoSessionCloseMark { symbol: String },
    NoSessionCloseMarkOpenPosition { symbol: String },
    EmptySnapshots(&'static str),
    Margin(MarginError),
    Ledger(String),
    Overflow,
    Value(String),
}

impl SnapshotError {
    pub fn message(&self) -> String {
        format!("{self}")
    }
}

impl fmt::Display for SnapshotError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SnapshotError::NoAsOf { kind } => {
                write!(f, "Event {kind} has no as_of to date a session close (I5)")
            }
            SnapshotError::NoSessionCloseMark { symbol } => {
                write!(f, "No session-close mark for {symbol} (I5)")
            }
            SnapshotError::NoSessionCloseMarkOpenPosition { symbol } => {
                write!(f, "No session-close mark for open position {symbol} (I5)")
            }
            SnapshotError::EmptySnapshots(msg) => write!(f, "{msg}"),
            SnapshotError::Margin(m) => write!(f, "{}", m.message()),
            SnapshotError::Ledger(m) => write!(f, "{m}"),
            SnapshotError::Overflow => write!(f, "decimal arithmetic overflow (D6)"),
            SnapshotError::Value(m) => write!(f, "{m}"),
        }
    }
}

impl std::error::Error for SnapshotError {}

/// One account's margin/exposure figures at one session close.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountSnapshot {
    pub session: NaiveDate,
    pub equity: Decimal,
    pub cash: Decimal,
    pub gross_exposure: Decimal,
    pub net_exposure: Decimal,
    pub margin_used: Decimal,
    pub margin_available: Decimal,
    pub margin_peak: Decimal,
    pub heat: Decimal,
    pub heat_unknown_positions: Vec<String>,
    pub drawdown_from_peak: Decimal,
    pub drawdown_duration_sessions: usize,
}

/// The session date of an event carrying an `as_of` timestamp.
pub fn session_of(event: &Event) -> Result<NaiveDate, SnapshotError> {
    payload_as_of(event).map(|dt| dt.date)
}

/// The `as_of` timestamp of an event payload.
pub fn payload_as_of(event: &Event) -> Result<DateTime, SnapshotError> {
    match &event.payload {
        Obj::Mark(m) => Ok(m.as_of.clone()),
        Obj::CashFlow(cf) => Ok(cf.as_of.clone()),
        Obj::Reconcile(r) => Ok(r.as_of.clone()),
        _ => Err(SnapshotError::NoAsOf {
            kind: event.kind.value().to_string(),
        }),
    }
}

/// The stop price of a working order on the opposite side (reduce side) of a position.
///
/// Returns `None` if the account holds no open position in the instrument, or if no
/// working STOP or STOP_LIMIT order exists on the reduce side.
pub fn protective_stop(state: &AccountState, instrument: &Instrument) -> Option<Decimal> {
    let pos = state.positions.get(&instrument.hk())?;
    if pos.quantity.is_zero() {
        return None;
    }
    let reduce_side = if pos.quantity.is_negative() {
        Side::Buy
    } else {
        Side::Sell
    };
    protective_stop_for_side(state, instrument, reduce_side)
}

/// The stop price of a working order on `reduce_side` for `instrument`.
///
/// Last working stop on this instrument wins.
pub fn protective_stop_for_side(
    state: &AccountState,
    instrument: &Instrument,
    reduce_side: Side,
) -> Option<Decimal> {
    let mut candidate: Option<&crate::ledger::model::Order> = None;
    for order in state.orders.values() {
        if !order.instrument.same(instrument) || order.stop_price.is_none() {
            continue;
        }
        if !matches!(order.order_type, OrderType::Stop | OrderType::StopLimit) {
            continue;
        }
        if !matches!(
            order.state,
            OrderState::New
                | OrderState::Submitted
                | OrderState::Accepted
                | OrderState::PartiallyFilled
        ) {
            continue;
        }
        if order.side != reduce_side {
            continue;
        }
        candidate = Some(order);
    }
    candidate.and_then(|o| o.stop_price.as_ref().map(|sp| sp.to_decimal()))
}

/// Sum (mark − stop) risk over open positions with a working protective stop.
///
/// A position whose protective stop is missing from the working set is listed
/// in `heat_unknown_positions` instead of being folded into the number.
pub fn working_stop_risk(
    state: &AccountState,
) -> Result<(Decimal, Vec<String>), SnapshotError> {
    let mut heat = Decimal::ZERO;
    let mut unknown = Vec::new();

    let mut sorted_positions: Vec<(&Instrument, &LedgerPosition)> = state.positions.iter().collect();
    sorted_positions.sort_by(|(a, _), (b, _)| {
        let sa = a.symbol().unwrap_or_default();
        let sb = b.symbol().unwrap_or_default();
        sa.cmp(&sb)
    });

    for (instrument, position) in sorted_positions {
        if position.quantity.is_zero() {
            continue;
        }
        let symbol = instrument.symbol().map_err(|e| SnapshotError::Value(e.msg))?;
        let mark = match state.marks.get(&instrument.hk()) {
            Some(m) if !m.is_zero() && !m.is_negative() => m.to_decimal(),
            _ => return Err(SnapshotError::NoSessionCloseMark { symbol }),
        };
        let stop = protective_stop(state, instrument);
        let Some(stop) = stop else {
            unknown.push(symbol);
            continue;
        };
        let qty = position.quantity.to_decimal();
        let risk = if !position.quantity.is_negative() {
            mark.checked_sub(stop).ok_or(SnapshotError::Overflow)?
        } else {
            stop.checked_sub(mark).ok_or(SnapshotError::Overflow)?
        };
        let pos_risk = risk
            .checked_mul(qty.abs())
            .ok_or(SnapshotError::Overflow)?;
        heat = heat
            .checked_add(pos_risk)
            .ok_or(SnapshotError::Overflow)?;
    }
    Ok((heat, unknown))
}

/// Compute AccountMargin for folded AccountState.
pub fn compute_account_margin(
    state: &AccountState,
    overrides: Option<&HashMap<String, (Decimal, Decimal)>>,
) -> Result<AccountMargin, SnapshotError> {
    let mut table: Vec<Instrument> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    let mut wpos = Vec::new();

    let mut sorted_positions: Vec<(&Instrument, &LedgerPosition)> = state.positions.iter().collect();
    sorted_positions.sort_by(|(a, _), (b, _)| {
        let sa = a.symbol().unwrap_or_default();
        let sb = b.symbol().unwrap_or_default();
        sa.cmp(&sb)
    });

    for (instrument, position) in sorted_positions {
        let symbol = instrument.symbol().map_err(|e| SnapshotError::Value(e.msg))?;
        let mark = state.marks.get(&instrument.hk()).map(|m| m.to_decimal());
        let flat = position.quantity.is_zero();
        let (kind, mult, contract, avg_cost) = match instrument {
            Instrument::Combo(_) => (MarginKind::Combo, 1, None, None),
            Instrument::Option(c) => {
                let hk = instrument.hk();
                let at = match index.get(&hk) {
                    Some(&i) => i,
                    None => {
                        let i = table.len();
                        index.insert(hk, i);
                        table.push(instrument.clone());
                        i
                    }
                };
                (
                    MarginKind::Option,
                    c.multiplier as i64,
                    Some(at),
                    Some(position.avg_cost.to_decimal()),
                )
            }
            Instrument::Equity(_) => (MarginKind::Equity, 1, None, None),
            Instrument::Future(_) => (MarginKind::Equity, 1, None, None),
        };
        wpos.push(MarginPosition {
            symbol,
            kind,
            quantity: position.quantity.to_decimal(),
            mark,
            multiplier: mult,
            flat,
            contract,
            avg_cost,
        });
    }

    let mut contracts = Vec::new();
    for instrument in &table {
        let Instrument::Option(c) = instrument else { continue };
        let occ = c.occ().map_err(|e| SnapshotError::Value(e.msg))?.trim().to_string();
        let style = match option_style(&c.underlying) {
            Ok(s) => MarginStyle::Resolved {
                underlying: s.underlying,
                index: s.settlement == crate::options::Settlement::Cash,
            },
            Err(e) if e.kind() == "unresolvable" => MarginStyle::Unresolved(e.message().to_string()),
            Err(e) => return Err(SnapshotError::Value(e.message().to_string())),
        };
        if c.multiplier <= 0 {
            return Err(SnapshotError::Value(format!(
                "Multiplier must be positive, got {}",
                c.multiplier
            )));
        }
        contracts.push(MarginContract {
            root: c.underlying.clone(),
            expiry: c.expiry,
            strike: c.strike.to_decimal(),
            right: match c.right {
                crate::options::Right::Call => crate::margin::option::Right::Call,
                crate::options::Right::Put => crate::margin::option::Right::Put,
            },
            multiplier: c.multiplier as i64,
            occ,
            style,
        });
    }

    let mut equity_marks = HashMap::new();
    for (instrument, mark) in state.marks.iter() {
        if let Instrument::Equity(symbol) = instrument {
            equity_marks.insert(symbol.clone(), mark.to_decimal());
        }
    }

    let empty_overrides = HashMap::new();
    let overrides_map = overrides.unwrap_or(&empty_overrides);
    let definitions = strat_definitions();

    let input = AccountInput {
        cash: state.cash.to_decimal(),
        positions: &wpos,
        contracts: &contracts,
        equity_marks: &equity_marks,
        underlying_prices: &HashMap::new(),
        overrides: overrides_map,
        definitions: &definitions,
        search_limit: DEFAULT_SEARCH_LIMIT,
    };

    account_margin(&input).map_err(|e| {
        let msg = e.message();
        if msg.starts_with("No session-close mark for open position ") {
            let symbol = msg
                .strip_prefix("No session-close mark for open position ")
                .and_then(|s| s.strip_suffix(" (I5)"))
                .unwrap_or(msg);
            SnapshotError::NoSessionCloseMarkOpenPosition {
                symbol: symbol.to_string(),
            }
        } else {
            SnapshotError::Margin(e)
        }
    })
}

/// Fold events day by day, then emit one snapshot per session close for one account.
///
/// A session close is a Mark as_of date; state before the first Mark produces
/// no snapshot (there is nothing to mark yet). Marks must exist for every open
/// position at each close (I5).
pub fn daily_snapshots(
    events: &[Event],
    account: &str,
) -> Result<Vec<AccountSnapshot>, SnapshotError> {
    daily_snapshots_with_overrides(events, account, None)
}

/// Fold events with optional per-symbol margin overrides.
pub fn daily_snapshots_with_overrides(
    events: &[Event],
    account: &str,
    overrides: Option<&HashMap<String, (Decimal, Decimal)>>,
) -> Result<Vec<AccountSnapshot>, SnapshotError> {
    let mut closes_set = HashSet::new();
    for event in events {
        if event.account == account {
            if let Obj::Mark(ref mark) = event.payload {
                closes_set.insert(mark.as_of.date);
            }
        }
    }
    if closes_set.is_empty() {
        return Ok(Vec::new());
    }
    let mut closes: Vec<NaiveDate> = closes_set.into_iter().collect();
    closes.sort();

    let mut snapshots = Vec::new();
    let mut peak_margin: Option<Decimal> = None;
    let mut peak_equity: Option<Decimal> = None;
    let mut dd_start: Option<usize> = None;

    let mut ordered_events: Vec<&Event> = events.iter().filter(|e| e.account == account).collect();
    ordered_events.sort_by(|a, b| {
        a.ts_utc
            .instant()
            .cmp(&b.ts_utc.instant())
            .then_with(|| a.seq.unwrap_or(0).cmp(&b.seq.unwrap_or(0)))
    });

    let mut upto = 0;
    for (index, &session) in closes.iter().enumerate() {
        let mut close_stamp: Option<&DateTime> = None;
        for event in events {
            if event.account == account {
                if let Obj::Mark(ref mark) = event.payload {
                    if mark.as_of.date == session {
                        if close_stamp.is_none()
                            || mark.as_of.instant() > close_stamp.unwrap().instant()
                        {
                            close_stamp = Some(&mark.as_of);
                        }
                    }
                }
            }
        }
        let close_stamp = close_stamp.ok_or_else(|| SnapshotError::NoAsOf {
            kind: EventKind::Mark.value().to_string(),
        })?;

        while upto < ordered_events.len()
            && ordered_events[upto].ts_utc.instant() <= close_stamp.instant()
        {
            upto += 1;
        }

        let sub_events: Vec<Event> = ordered_events[..upto].iter().map(|&e| e.clone()).collect();
        let state = fold_account(&sub_events, account).map_err(|e| SnapshotError::Ledger(e.msg))?;

        if state.marks.is_empty() {
            continue;
        }

        let margin = compute_account_margin(&state, overrides)?;
        let (margin_used, margin_available) = margin_summary(margin.equity, margin.margin_maintenance)
            .map_err(SnapshotError::Margin)?;

        if peak_margin.is_none() || Some(margin_used) > peak_margin {
            peak_margin = Some(margin_used);
        }
        if peak_equity.is_none() || Some(margin.equity) > peak_equity {
            peak_equity = Some(margin.equity);
            dd_start = None;
        }

        let dd = match peak_equity {
            Some(pe) if pe > Decimal::ZERO => {
                if pe > margin.equity {
                    pe - margin.equity
                } else {
                    Decimal::ZERO
                }
            }
            _ => Decimal::ZERO,
        };

        if peak_equity.is_some() && dd > Decimal::ZERO && dd_start.is_none() {
            dd_start = Some(index);
        }

        let dd_duration = match dd_start {
            None => 0,
            Some(start) => index - start,
        };

        let (heat, unknown) = working_stop_risk(&state)?;

        snapshots.push(AccountSnapshot {
            session,
            equity: margin.equity,
            cash: margin.cash,
            gross_exposure: margin.gross_exposure,
            net_exposure: margin.net_exposure,
            margin_used,
            margin_available,
            margin_peak: peak_margin.unwrap_or(Decimal::ZERO),
            heat,
            heat_unknown_positions: unknown,
            drawdown_from_peak: dd,
            drawdown_duration_sessions: dd_duration,
        });
    }

    Ok(snapshots)
}

/// Peak margin over a list of snapshots.
///
/// Refuses over an empty snapshot list (I5).
pub fn peak_margin(snapshots: &[AccountSnapshot]) -> Result<Decimal, SnapshotError> {
    if snapshots.is_empty() {
        return Err(SnapshotError::EmptySnapshots(
            "peak_margin over an empty snapshot list refuses (I5)",
        ));
    }
    snapshots
        .iter()
        .map(|s| s.margin_used)
        .max()
        .ok_or_else(|| {
            SnapshotError::EmptySnapshots("peak_margin over an empty snapshot list refuses (I5)")
        })
}

/// Max drawdown over a list of snapshots.
///
/// Refuses over an empty snapshot list (I5).
pub fn max_drawdown(snapshots: &[AccountSnapshot]) -> Result<Decimal, SnapshotError> {
    if snapshots.is_empty() {
        return Err(SnapshotError::EmptySnapshots(
            "max_drawdown over an empty snapshot list refuses (I5)",
        ));
    }
    snapshots
        .iter()
        .map(|s| s.drawdown_from_peak)
        .max()
        .ok_or_else(|| {
            SnapshotError::EmptySnapshots("max_drawdown over an empty snapshot list refuses (I5)")
        })
}

/// The one-line daily journal note (E8b -> journal day note).
pub fn day_note_line(snapshot: &AccountSnapshot, account: &str) -> String {
    let gross_pct = if !snapshot.equity.is_zero() {
        let pct = (snapshot.gross_exposure * Decimal::from(100)) / snapshot.equity;
        let rounded = pct.round_dp_with_strategy(1, RoundingStrategy::MidpointNearestEven);
        format!("{:.1}%", rounded)
    } else {
        "n/a".to_string()
    };

    let heat_line = if !snapshot.equity.is_zero() {
        let pct = (snapshot.heat * Decimal::from(100)) / snapshot.equity;
        let rounded = pct.round_dp_with_strategy(1, RoundingStrategy::MidpointNearestEven);
        format!("{:.1}%", rounded)
    } else {
        "n/a".to_string()
    };

    let unknown = if !snapshot.heat_unknown_positions.is_empty() {
        format!(" (no stop: {})", snapshot.heat_unknown_positions.join(","))
    } else {
        String::new()
    };

    let margin_used_str = snapshot
        .margin_used
        .round_dp_with_strategy(0, RoundingStrategy::MidpointNearestEven)
        .to_string();
    let margin_peak_str = snapshot
        .margin_peak
        .round_dp_with_strategy(0, RoundingStrategy::MidpointNearestEven)
        .to_string();

    format!(
        "snapshot {account} {}: equity {}, gross {gross_pct}, margin used {margin_used_str} \
         (peak {margin_peak_str}), heat {heat_line}{unknown}, DD {}",
        snapshot.session, snapshot.equity, snapshot.drawdown_from_peak
    )
}
