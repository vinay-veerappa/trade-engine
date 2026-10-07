//! `trade_engine_rs`: the ONE pyo3 module Python reaches te_core by
//! (docs/RUST_PORT.md D2). Thin wrappers only: plain values in and out, every
//! te_core refusal raised as ValueError with te_core's own message. A rule
//! written here instead of in te_core would be a second reader.

use chrono::{DateTime, NaiveDate, Utc};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::calendar as cal;
use te_core::greeks as gk;

mod flow;
mod oms;
mod sim;
mod lock;
pub mod store;
mod outbox;
mod clock;
pub mod http;
mod eod_once;
mod factory_run;
mod mirror_loop;
pub mod plugins;

/// The same module initializer is registered before custom CPython startup.
#[cfg(feature = "embed")]
pub fn register_embedded_module() -> Result<(), &'static str> {
    if unsafe { pyo3::ffi::Py_IsInitialized() } != 0 {
        return Err("module registration requires an uninitialized interpreter");
    }
    pyo3::append_to_inittab!(trade_engine_rs);
    Ok(())
}

fn date(iso: &str) -> PyResult<NaiveDate> {
    NaiveDate::parse_from_str(iso, "%Y-%m-%d")
        .map_err(|_| PyValueError::new_err(format!("Invalid date string format: {iso:?}")))
}

fn refuse<E: std::fmt::Display>(e: E) -> PyErr {
    PyValueError::new_err(e.to_string())
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

#[pyfunction]
fn calendar_is_session(d: &str) -> PyResult<bool> {
    cal::is_session(date(d)?).map_err(refuse)
}

#[pyfunction]
fn calendar_is_holiday(d: &str) -> PyResult<bool> {
    cal::is_holiday(date(d)?).map_err(refuse)
}

#[pyfunction]
fn calendar_is_early_close(d: &str) -> PyResult<bool> {
    cal::is_early_close(date(d)?).map_err(refuse)
}

/// Seconds since the Unix epoch, UTC.
#[pyfunction]
fn calendar_session_open(d: &str) -> PyResult<i64> {
    cal::session_open(date(d)?).map(|t| t.timestamp()).map_err(refuse)
}

/// Seconds since the Unix epoch, UTC.
#[pyfunction]
fn calendar_session_close(d: &str) -> PyResult<i64> {
    cal::session_close(date(d)?).map(|t| t.timestamp()).map_err(refuse)
}

#[pyfunction]
fn calendar_next_session(d: &str) -> PyResult<String> {
    cal::next_session(date(d)?).map(iso).map_err(refuse)
}

#[pyfunction]
fn calendar_previous_session(d: &str) -> PyResult<String> {
    cal::previous_session(date(d)?).map(iso).map_err(refuse)
}

#[pyfunction]
fn calendar_roll_to_session(d: &str, forward: bool) -> PyResult<String> {
    cal::roll_to_session(date(d)?, forward).map(iso).map_err(refuse)
}

#[pyfunction]
fn calendar_sessions_in_range(start: &str, end: &str) -> PyResult<Vec<String>> {
    cal::sessions_in_range(date(start)?, date(end)?)
        .map(|v| v.into_iter().map(iso).collect())
        .map_err(refuse)
}

#[pyfunction]
fn calendar_sessions_window_back(end: &str, count: i64) -> PyResult<Vec<String>> {
    cal::sessions_window_back(date(end)?, count)
        .map(|v| v.into_iter().map(iso).collect())
        .map_err(refuse)
}

/// `epoch_us` is microseconds since the Unix epoch, UTC.
#[pyfunction]
fn calendar_is_open_at(epoch_us: i64) -> PyResult<bool> {
    let t = DateTime::<Utc>::from_timestamp_micros(epoch_us)
        .ok_or_else(|| PyValueError::new_err(format!("timestamp {epoch_us}us is out of range")))?;
    cal::is_open_at(t).map_err(refuse)
}

fn right(flag: &str) -> PyResult<gk::Right> {
    match flag {
        "c" => Ok(gk::Right::Call),
        "p" => Ok(gk::Right::Put),
        other => Err(PyValueError::new_err(format!("flag must be 'c' or 'p', got {other:?}"))),
    }
}

#[pyfunction]
fn greeks_price(flag: &str, spot: f64, strike: f64, t: f64, rate: f64, q: f64, sigma: f64) -> PyResult<f64> {
    gk::price(right(flag)?, spot, strike, t, rate, q, sigma).map_err(refuse)
}

#[pyfunction]
fn greeks_implied_vol(flag: &str, spot: f64, strike: f64, t: f64, rate: f64, q: f64, price: f64) -> PyResult<f64> {
    gk::implied_vol(right(flag)?, spot, strike, t, rate, q, price).map_err(refuse)
}

/// (delta, gamma, theta per day, vega per point, rho per point)
#[pyfunction]
fn greeks_greeks(flag: &str, spot: f64, strike: f64, t: f64, rate: f64, q: f64, sigma: f64) -> PyResult<(f64, f64, f64, f64, f64)> {
    let g = gk::greeks(right(flag)?, spot, strike, t, rate, q, sigma).map_err(refuse)?;
    Ok((g.delta, g.gamma, g.theta, g.vega, g.rho))
}

// --- margin (P1c) ------------------------------------------------------------------
// Decimals cross as strings, contracts and positions as JSON (te_core::margin::wire).
// A refusal is a ValueError whose args are (kind, message): the shim re-raises it as
// the Python exception type `kind` names ("value", "option", "unresolvable").

use te_core::margin::{wire, MarginError};

fn refuse_margin(e: MarginError) -> PyErr {
    PyValueError::new_err((e.kind(), e.message().to_string()))
}

#[pyfunction]
#[pyo3(signature = (quantity, mark, initial=None, maintenance=None))]
fn margin_requirement(
    quantity: &str,
    mark: &str,
    initial: Option<&str>,
    maintenance: Option<&str>,
) -> PyResult<(String, String, String)> {
    wire::requirement_str(quantity, mark, initial, maintenance).map_err(refuse_margin)
}

#[pyfunction]
fn margin_validate_override(initial: &str, maintenance: &str) -> PyResult<()> {
    wire::validate_override_str(initial, maintenance).map_err(refuse_margin)
}

#[pyfunction]
fn margin_summary(equity: &str, maintenance: &str) -> PyResult<(String, String)> {
    wire::summary_str(equity, maintenance).map_err(refuse_margin)
}

#[pyfunction]
fn margin_naked(request: &str) -> PyResult<String> {
    wire::naked_str(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_match(request: &str) -> PyResult<String> {
    wire::match_json(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_strategy(request: &str) -> PyResult<String> {
    wire::strategy_json(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_book(request: &str) -> PyResult<String> {
    wire::book_json(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_account(request: &str) -> PyResult<String> {
    wire::account_json(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_constants() -> PyResult<String> {
    wire::constants_json().map_err(refuse_margin)
}

#[pyfunction]
fn margin_definitions() -> PyResult<String> {
    wire::definitions_json().map_err(refuse_margin)
}

fn register_margin(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(margin_requirement, m)?)?;
    m.add_function(wrap_pyfunction!(margin_validate_override, m)?)?;
    m.add_function(wrap_pyfunction!(margin_summary, m)?)?;
    m.add_function(wrap_pyfunction!(margin_naked, m)?)?;
    m.add_function(wrap_pyfunction!(margin_match, m)?)?;
    m.add_function(wrap_pyfunction!(margin_strategy, m)?)?;
    m.add_function(wrap_pyfunction!(margin_book, m)?)?;
    m.add_function(wrap_pyfunction!(margin_account, m)?)?;
    m.add_function(wrap_pyfunction!(margin_constants, m)?)?;
    m.add_function(wrap_pyfunction!(margin_definitions, m)?)?;
    Ok(())
}
// --- end margin -----------------------------------------------------------------------

// --- option rules (P1e) -------------------------------------------------------------
// A refusal crosses as ValueError(kind, message); the Python shims map the kind back
// to the exception type the pre-port code raised.

use te_core::options as opt;

fn refuse_opt(e: opt::OptionError) -> PyErr {
    PyValueError::new_err((e.kind().to_string(), e.message().to_string()))
}

/// A Decimal as the shims send it: negative, digits, exponent, special code, str().
type DecArgs = (bool, String, i64, String, String);
/// A contract: underlying, year, month, day, right code, then the strike's Decimal.
type ContractArgs = (String, i32, u32, u32, String, bool, String, i64, String, String);

fn dec_wire(a: DecArgs) -> PyResult<opt::DecWire> {
    let special = opt::Special::from_code(&a.3)
        .ok_or_else(|| PyValueError::new_err(("value".to_string(), format!("Unknown Decimal special {:?}", a.3))))?;
    Ok(opt::DecWire { neg: a.0, digits: a.1, exp: a.2, special, text: a.4 })
}

fn contract_wire(a: ContractArgs) -> PyResult<opt::ContractWire> {
    let right = opt::Right::from_code(&a.4)
        .ok_or_else(|| PyValueError::new_err(("value".to_string(), format!("Invalid option right: {}", a.4))))?;
    let strike = dec_wire((a.5, a.6, a.7, a.8, a.9))?;
    Ok(opt::ContractWire { underlying: a.0, year: a.1, month: a.2, day: a.3, right, strike })
}

#[pyfunction]
#[pyo3(signature = (symbol))]
fn option_equity_symbol(symbol: Option<&str>) -> PyResult<String> {
    opt::equity_symbol(symbol).map_err(refuse_opt)
}

#[pyfunction]
fn option_validate_underlying(underlying: &str) -> PyResult<String> {
    opt::validate_underlying(underlying).map_err(refuse_opt)
}

/// Strike, multiplier and right, in `__post_init__` order; returns the right's code.
#[pyfunction]
#[pyo3(signature = (strike, multiplier, multiplier_text, right, right_text))]
fn option_validate_rest(
    strike: DecArgs,
    multiplier: Option<&str>,
    multiplier_text: &str,
    right: Option<&str>,
    right_text: &str,
) -> PyResult<&'static str> {
    let strike = dec_wire(strike)?;
    opt::validate_rest(&strike, multiplier, multiplier_text, right, right_text).map(opt::Right::code).map_err(refuse_opt)
}

#[pyfunction]
fn option_to_occ(c: ContractArgs) -> PyResult<String> {
    contract_wire(c)?.occ().map_err(refuse_opt)
}

/// (root, year, month, day, right code, strike text).
#[pyfunction]
#[pyo3(signature = (occ))]
fn option_parse_occ(occ: Option<&str>) -> PyResult<(String, i32, u32, u32, &'static str, String)> {
    let p = opt::parse_occ(occ).map_err(refuse_opt)?;
    Ok((p.root, p.year, p.month, p.day, p.right.code(), p.strike_text))
}

/// (root, underlying, exercise code, settlement code, settle-time code).
#[pyfunction]
fn option_style(root: &str) -> PyResult<(String, String, &'static str, &'static str, &'static str)> {
    let s = opt::option_style(root).map_err(refuse_opt)?;
    Ok((s.root, s.underlying, s.exercise.code(), s.settlement.code(), s.settle_time.code()))
}

#[pyfunction]
fn option_chain_roots(underlying: &str) -> PyResult<Vec<String>> {
    opt::chain_roots(underlying).map_err(refuse_opt)
}

/// Seconds since the Unix epoch, UTC.
#[pyfunction]
fn option_settlement_instant(c: ContractArgs) -> PyResult<i64> {
    opt::settlement_instant(&contract_wire(c)?).map_err(refuse_opt)
}

#[pyfunction]
fn option_last_trade_date(c: ContractArgs) -> PyResult<String> {
    opt::last_trade_date(&contract_wire(c)?).map(iso).map_err(refuse_opt)
}

#[pyfunction]
fn option_intrinsic(c: ContractArgs, price: &str) -> PyResult<String> {
    opt::lifecycle::intrinsic(&contract_wire(c)?, price).map_err(refuse_opt)
}

#[pyfunction]
fn option_expiry_outcome(c: ContractArgs, held_buy: bool, price: &str) -> PyResult<&'static str> {
    opt::lifecycle::expiry_outcome(&contract_wire(c)?, held_buy, price).map_err(refuse_opt)
}

#[pyfunction]
fn option_is_cash_settled(c: ContractArgs) -> PyResult<bool> {
    opt::lifecycle::is_cash_settled(&contract_wire(c)?).map_err(refuse_opt)
}

#[pyfunction]
fn option_can_exercise_early(c: ContractArgs) -> PyResult<bool> {
    opt::lifecycle::can_exercise_early(&contract_wire(c)?).map_err(refuse_opt)
}

#[pyfunction]
fn option_deliverable(c: ContractArgs) -> PyResult<String> {
    opt::lifecycle::deliverable(&contract_wire(c)?).map_err(refuse_opt)
}

#[pyfunction]
fn option_delivery(c: ContractArgs, held_buy: bool, premium: &str) -> PyResult<(bool, String)> {
    opt::lifecycle::delivery(&contract_wire(c)?, held_buy, premium).map_err(refuse_opt)
}

#[pyfunction]
fn option_exercised_for_dividend(c: ContractArgs, close: &str, bid: &str, dividend: &str) -> PyResult<bool> {
    opt::lifecycle::exercised_for_dividend(&contract_wire(c)?, close, bid, dividend).map_err(refuse_opt)
}

#[pyfunction]
fn option_exercise_threshold() -> &'static str {
    opt::lifecycle::exercise_threshold()
}

fn register_options(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(option_equity_symbol, m)?)?;
    m.add_function(wrap_pyfunction!(option_validate_underlying, m)?)?;
    m.add_function(wrap_pyfunction!(option_validate_rest, m)?)?;
    m.add_function(wrap_pyfunction!(option_to_occ, m)?)?;
    m.add_function(wrap_pyfunction!(option_parse_occ, m)?)?;
    m.add_function(wrap_pyfunction!(option_style, m)?)?;
    m.add_function(wrap_pyfunction!(option_chain_roots, m)?)?;
    m.add_function(wrap_pyfunction!(option_settlement_instant, m)?)?;
    m.add_function(wrap_pyfunction!(option_last_trade_date, m)?)?;
    m.add_function(wrap_pyfunction!(option_intrinsic, m)?)?;
    m.add_function(wrap_pyfunction!(option_expiry_outcome, m)?)?;
    m.add_function(wrap_pyfunction!(option_is_cash_settled, m)?)?;
    m.add_function(wrap_pyfunction!(option_can_exercise_early, m)?)?;
    m.add_function(wrap_pyfunction!(option_deliverable, m)?)?;
    m.add_function(wrap_pyfunction!(option_delivery, m)?)?;
    m.add_function(wrap_pyfunction!(option_exercised_for_dividend, m)?)?;
    m.add_function(wrap_pyfunction!(option_exercise_threshold, m)?)?;
    Ok(())
}
// --- end option rules -----------------------------------------------------------------

#[pyfunction]
fn risk_rules_from_mapping(doc: &str) -> String {
    te_core::risk::json_rules_from_mapping(doc)
}

#[pyfunction]
fn risk_validate_rules(doc: &str) -> String {
    te_core::risk::json_validate_rules(doc)
}

#[pyfunction]
fn risk_validate_rails(doc: &str) -> String {
    te_core::risk::json_validate_rails(doc)
}

#[pyfunction]
fn risk_validate_hours(doc: &str) -> String {
    te_core::risk::json_validate_hours(doc)
}

#[pyfunction]
fn risk_validate_context(doc: &str) -> String {
    te_core::risk::json_validate_context(doc)
}

#[pyfunction]
fn risk_hours_is_open(doc: &str) -> String {
    te_core::risk::json_hours_is_open(doc)
}

#[pyfunction]
fn risk_drawdown_controls(doc: &str) -> String {
    te_core::risk::json_drawdown_controls(doc)
}

#[pyfunction]
fn risk_evaluate(doc: &str) -> String {
    te_core::risk::json_evaluate(doc)
}

// --- ledger (P2a shadow, P2b production) ---------------------------------------------
// A refusal crosses as ValueError(kind, message); `trade_engine.ledger._rs` maps the kind
// to the Python exception the old code raised.

fn refuse_ledger(e: te_core::ledger::model::LErr) -> PyErr {
    PyValueError::new_err((e.kind.to_string(), e.msg))
}

#[pyfunction]
fn ledger_reencode(data: &[u8]) -> PyResult<Vec<u8>> {
    te_core::ledger::codec::reencode(data).map_err(refuse_ledger)
}

fn decode_all(events: &[Vec<u8>]) -> Result<Vec<te_core::ledger::model::Event>, te_core::ledger::model::LErr> {
    events.iter().map(|b| te_core::ledger::codec::event_from_bytes(b)).collect()
}

/// Fold encoded events for one account; canonical state JSON bytes.
#[pyfunction]
fn ledger_fold(events: Vec<Vec<u8>>, account: &str) -> PyResult<Vec<u8>> {
    let run = || -> te_core::ledger::model::R<String> {
        let evs = decode_all(&events)?;
        let st = te_core::ledger::fold::fold_account(&evs, account)?;
        Ok(te_core::ledger::json::dumps(&te_core::ledger::canon::canon_account(&st)?))
    };
    run().map(String::into_bytes).map_err(refuse_ledger)
}

/// Fold every account: `{"m":[[account,state],...]}` canonical JSON bytes.
#[pyfunction]
fn ledger_fold_all(events: Vec<Vec<u8>>) -> PyResult<Vec<u8>> {
    let run = || -> te_core::ledger::model::R<String> {
        let evs = decode_all(&events)?;
        let st = te_core::ledger::fold::fold(&evs)?;
        Ok(te_core::ledger::json::dumps(&te_core::ledger::canon::canon_states(&st)?))
    };
    run().map(String::into_bytes).map_err(refuse_ledger)
}

// --- the ledger (P2b): Python's codec, fold and stores call these ----------------------

use te_core::ledger::bridge as lb;
use te_core::ledger::canon as lc;
use te_core::ledger::fold::{apply_event as fold_apply, AccountState};
use te_core::ledger::json::dumps as ldumps;
use te_core::ledger::model::{Event, LErr, Obj, R as LR};
use te_core::ledger::ops::OMap;
use te_core::money::Money;

fn dec_arg(text: &str) -> LR<Money> {
    Money::parse(text).ok_or_else(|| LErr { kind: "invalid_operation", msg: String::new() })
}

/// The old encoder's refusals over the walker's tree; its stored (sorted) bytes.
#[pyfunction]
fn ledger_check_payload(text: &str) -> PyResult<Vec<u8>> {
    lb::check_encoded(text).map_err(refuse_ledger)
}

/// The canonical spelling (P7, S1) of a decimal literal; a value outside the bound or a
/// non-number is a `ValueError` (I5).
#[pyfunction]
fn money_canon(text: &str) -> PyResult<String> {
    te_core::money::Money::try_parse(text)
        .map(|m| m.canon())
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e:?}")))
}

/// `decode_payload`'s refusals over an encoded tree (Python builds it once accepted).
#[pyfunction]
fn ledger_check_decode_payload(text: &str) -> PyResult<()> {
    lb::check_decode_payload(text).map_err(refuse_ledger)
}

/// `decode_event`'s refusals over an encoded event's JSON.
#[pyfunction]
fn ledger_check_event(text: &[u8]) -> PyResult<()> {
    te_core::ledger::codec::event_from_bytes(text).map(|_| ()).map_err(refuse_ledger)
}

/// `Ledger._row_to_event`'s refusals over one stored row.
#[pyfunction]
#[pyo3(signature = (account, kind, payload_json, ts_utc, command_id, schema_version, seq))]
fn ledger_check_row(
    account: &str,
    kind: &str,
    payload_json: &str,
    ts_utc: &str,
    command_id: Option<&str>,
    schema_version: i64,
    seq: Option<i64>,
) -> PyResult<()> {
    lb::event_from_row(account, kind, payload_json, ts_utc, command_id, schema_version as i128, seq.map(i128::from))
        .map(|_| ())
        .map_err(refuse_ledger)
}

/// `state.apply_event`: one event onto a state, both as canonical JSON.
#[pyfunction]
fn ledger_apply_event(state: &str, event: &[u8]) -> PyResult<Vec<u8>> {
    let run = || -> LR<Vec<u8>> {
        let mut st = lb::account_from_text(state)?;
        let ev = te_core::ledger::codec::event_from_bytes(event)?;
        fold_apply(&mut st, &ev)?;
        Ok(ldumps(&lc::canon_account(&st)?).into_bytes())
    };
    run().map_err(refuse_ledger)
}

/// `state.apply_fill`: a position (canonical JSON, or None) after one encoded `Fill`.
#[pyfunction]
#[pyo3(signature = (account_id, position, fill, multiplier))]
fn ledger_apply_fill(account_id: &str, position: Option<&str>, fill: &str, multiplier: i64) -> PyResult<Vec<u8>> {
    let run = || -> LR<Vec<u8>> {
        let pos = match position {
            Some(t) => Some(lb::uncanon_position(&lb::json_from_text(t)?)?),
            None => None,
        };
        let Obj::Fill(f) = lb::obj_from_text(fill)? else {
            return Err(LErr { kind: "type", msg: "apply_fill takes a Fill".into() });
        };
        let p = lb::apply_fill(account_id, pos.as_ref(), &f, multiplier as i128)?;
        Ok(ldumps(&lc::canon_position_pub(&p)?).into_bytes())
    };
    run().map_err(refuse_ledger)
}

/// One mirror fold step (`mirror.on_queued` / `on_refused` / `on_ack` / `on_fill`) over a
/// `MirrorState`'s canonical JSON; a contradiction is `mirror_fold`.
#[pyfunction]
fn ledger_mirror_step(step: &str, mirror: &str, payload: &str) -> PyResult<Vec<u8>> {
    use te_core::ledger::mirror as m;
    let run = || -> LR<Vec<u8>> {
        let mut st = lb::uncanon_mirror(&lb::json_from_text(mirror)?)?;
        let obj = lb::obj_from_text(payload)?;
        match (step, &obj) {
            ("queued", Obj::MQueued(q)) => m::on_queued(&mut st, q)?,
            ("refused", Obj::MRefused(r)) => m::on_refused(&mut st, r)?,
            ("ack", Obj::MAck(a)) => m::on_ack(&mut st, a)?,
            ("fill", Obj::MFill(f)) => m::on_fill(&mut st, f)?,
            _ => return Err(LErr { kind: "type", msg: format!("mirror step {step} does not take a {}", obj.tag()) }),
        }
        Ok(ldumps(&lc::canon_mirror_pub(&st)?).into_bytes())
    };
    run().map_err(refuse_ledger)
}

/// `mirror.pro_rata` over decimal strings.
#[pyfunction]
fn ledger_pro_rata(weights: Vec<String>, whole: &str, amount: &str) -> PyResult<Vec<String>> {
    let run = || -> LR<Vec<String>> {
        let w: Vec<Money> = weights.iter().map(|x| dec_arg(x)).collect::<LR<_>>()?;
        let out = te_core::ledger::mirror::pro_rata(&w, &dec_arg(whole)?, &dec_arg(amount)?)?;
        Ok(out.iter().map(|d| d.canon()).collect())
    };
    run().map_err(refuse_ledger)
}

/// `mirror.ticket_contracts`: `{"m": [[contract, signed contracts]]}` for an encoded queue.
#[pyfunction]
fn ledger_ticket_contracts(queued: &str, units: &str) -> PyResult<Vec<u8>> {
    let run = || -> LR<Vec<u8>> {
        let Obj::MQueued(q) = lb::obj_from_text(queued)? else {
            return Err(LErr { kind: "type", msg: "ticket_contracts takes a MirrorQueued".into() });
        };
        let m = te_core::ledger::mirror::ticket_contracts(&q, &dec_arg(units)?)?;
        Ok(ldumps(&lc::canon_contracts(&m)?).into_bytes())
    };
    run().map_err(refuse_ledger)
}

pub(crate) struct FoldEntry {
    st: AccountState,
    /// Fills the Python carrier holds (the delta appends the rest).
    fills_held: usize,
    /// Python holds a carrier built from this state; false = the next export is whole.
    exported: bool,
}

impl FoldEntry {
    fn fresh(st: AccountState) -> FoldEntry {
        FoldEntry { st, fills_held: 0, exported: false }
    }
}

/// The incremental fold the store, the reader and `FoldCache` hold (P2b): one state per
/// account, each event applied ONCE. `atomic`: a refused event leaves the account as it
/// was (`FoldCache`); otherwise the refused account is dropped and its owner reloads it
/// from the log (the store, whose transaction rolled back anyway).
#[pyclass(module = "trade_engine_rs")]
pub(crate) struct LedgerFold {
    atomic: bool,
    entries: OMap<String, FoldEntry>,
}

type Row = (String, String, String, Option<String>, i64, Option<i64>);

impl LedgerFold {
    /// The account's folded state, read in place (the OMS flow's host, P3b-2b).
    pub(crate) fn state_of(&self, account: &str) -> Option<&AccountState> {
        self.entries.get(account).map(|e| &e.st)
    }

    fn apply(&mut self, account: &str, ev: &Event) -> LR<()> {
        let created = !self.entries.contains(account);
        if created {
            self.entries.put(account, FoldEntry::fresh(AccountState::new(account)));
        }
        let atomic = self.atomic;
        let entry = self.entries.get_mut(account).expect("present");
        let backup = if atomic && !created { Some(entry.st.clone()) } else { None };
        match fold_apply(&mut entry.st, ev) {
            Ok(()) => Ok(()),
            Err(e) => {
                match backup {
                    Some(b) => entry.st = b,
                    None => {
                        self.entries.remove(account);
                    }
                }
                Err(e)
            }
        }
    }

    pub(crate) fn apply_row(&mut self, account: &str, row: &Row) -> LR<()> {
        let (kind, payload, ts, cmd, sv, seq) = row;
        match lb::event_from_row(account, kind, payload, ts, cmd.as_deref(), *sv as i128, seq.map(i128::from)) {
            Ok(ev) => self.apply(account, &ev),
            Err(e) => {
                if !self.atomic {
                    self.entries.remove(account);
                }
                Err(e)
            }
        }
    }
}

#[pymethods]
impl LedgerFold {
    #[new]
    #[pyo3(signature = (atomic=false))]
    pub(crate) fn new(atomic: bool) -> Self {
        LedgerFold { atomic, entries: OMap::new() }
    }

    pub(crate) fn has(&self, account: &str) -> bool {
        self.entries.contains(account)
    }

    /// Accounts in the order they were first folded.
    fn accounts(&self) -> Vec<String> {
        self.entries.iter().map(|(k, _)| k.clone()).collect()
    }

    pub(crate) fn drop(&mut self, account: &str) {
        self.entries.remove(account);
    }

    pub(crate) fn clear(&mut self) {
        self.entries = OMap::new();
    }

    /// Start an account from a carrier's canonical JSON (a `FoldCache` seed).
    fn seed(&mut self, account: &str, state: &str) -> PyResult<()> {
        let st = lb::account_from_text(state).map_err(refuse_ledger)?;
        self.entries.remove(account);
        self.entries.put(account, FoldEntry::fresh(st));
        Ok(())
    }

    /// Start (or restart) an account from its stored rows, in seq order:
    /// `(kind, payload_json, ts_utc, command_id, schema_version, seq)`.
    pub(crate) fn load(&mut self, account: &str, rows: Vec<Row>) -> PyResult<()> {
        self.entries.remove(account);
        self.entries.put(account, FoldEntry::fresh(AccountState::new(account)));
        self.apply_rows(account, rows)
    }

    /// Apply stored rows to an account (created empty if absent); a refusal drops it.
    pub(crate) fn apply_rows(&mut self, account: &str, rows: Vec<Row>) -> PyResult<()> {
        for row in &rows {
            if let Err(e) = self.apply_row(account, row) {
                self.entries.remove(account);
                return Err(refuse_ledger(e));
            }
        }
        Ok(())
    }

    /// Apply one stored row (the store's append, after its INSERT).
    #[pyo3(signature = (account, kind, payload_json, ts_utc, command_id, schema_version, seq))]
    #[allow(clippy::too_many_arguments)]
    fn apply_row1(
        &mut self,
        account: &str,
        kind: String,
        payload_json: String,
        ts_utc: String,
        command_id: Option<String>,
        schema_version: i64,
        seq: Option<i64>,
    ) -> PyResult<()> {
        self.apply_row(account, &(kind, payload_json, ts_utc, command_id, schema_version, seq)).map_err(refuse_ledger)
    }

    /// Apply one encoded event to its own account (`FoldCache.extend`).
    fn apply_event(&mut self, event: &[u8]) -> PyResult<()> {
        let ev = te_core::ledger::codec::event_from_bytes(event).map_err(refuse_ledger)?;
        let account = ev.account.clone();
        self.apply(&account, &ev).map_err(refuse_ledger)
    }

    /// The account's state for Python: `(True, whole canonical state)` the first time (or
    /// when `full`), else `(False, patch since the last export)`.
    #[pyo3(signature = (account, full=false))]
    pub(crate) fn export(&mut self, account: &str, full: bool) -> PyResult<(bool, Vec<u8>)> {
        let Some(entry) = self.entries.get_mut(account) else {
            return Err(refuse_ledger(LErr { kind: "key", msg: format!("'{account}'") }));
        };
        let whole = full || !entry.exported;
        let out = if whole { lc::export_full(&mut entry.st) } else { lc::export_delta(&mut entry.st, entry.fills_held) }
            .map_err(refuse_ledger)?;
        entry.fills_held = entry.st.fills.len();
        entry.exported = true;
        Ok((whole, ldumps(&out).into_bytes()))
    }
}

#[pymodule]
fn trade_engine_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    lock::register(m)?;
    store::register(m)?;
    outbox::register(m)?;
    clock::register(m)?;
    http::register(m)?;
    plugins::register(m)?;
    sim::register(m)?;
    oms::register(m)?;
    m.add_function(wrap_pyfunction!(calendar_is_session, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_is_holiday, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_is_early_close, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_session_open, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_session_close, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_next_session, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_previous_session, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_roll_to_session, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_sessions_in_range, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_sessions_window_back, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_is_open_at, m)?)?;
    m.add_function(wrap_pyfunction!(greeks_price, m)?)?;
    m.add_function(wrap_pyfunction!(greeks_implied_vol, m)?)?;
    m.add_function(wrap_pyfunction!(greeks_greeks, m)?)?;
    register_margin(m)?;
    m.add_function(wrap_pyfunction!(ledger_reencode, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_fold, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_fold_all, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_check_payload, m)?)?;
    m.add_function(wrap_pyfunction!(money_canon, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_check_decode_payload, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_check_event, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_check_row, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_apply_event, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_apply_fill, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_mirror_step, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_pro_rata, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_ticket_contracts, m)?)?;
    m.add_class::<LedgerFold>()?;
    register_options(m)?;
    m.add_function(wrap_pyfunction!(risk_rules_from_mapping, m)?)?;
    m.add_function(wrap_pyfunction!(risk_validate_rules, m)?)?;
    m.add_function(wrap_pyfunction!(risk_validate_rails, m)?)?;
    m.add_function(wrap_pyfunction!(risk_validate_hours, m)?)?;
    m.add_function(wrap_pyfunction!(risk_validate_context, m)?)?;
    m.add_function(wrap_pyfunction!(risk_hours_is_open, m)?)?;
    m.add_function(wrap_pyfunction!(risk_drawdown_controls, m)?)?;
    m.add_function(wrap_pyfunction!(risk_evaluate, m)?)?;
    runtime::register(m)?;
    eod_once::register(m)?;
    factory_run::register(m)?;
    mirror_loop::register(m)?;
    tos_paper::register(m)?;
    Ok(())
}

mod runtime;
mod tos_paper;
