use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::num::ParseIntError;

use chrono::NaiveDate;
use pyo3::exceptions::{PyAttributeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyCFunction, PyDate, PyDateTime, PyDict, PyFloat, PyInt, PyList, PyTuple};
use te_core::ledger::model::{err, parse_datetime, DateTime, Instrument, LErr, Obj, R as LR};
use te_core::money::Money;
use te_core::runtime::eod as rules;
use te_core::runtime::eod_flow::{self as flow, Account, Boundary, EodHost, Finish, Mode, Observation};
use te_core::runtime::eod_glue::{self as glue, Clock as _, Ledger as _, OptionQuote};
use te_core::runtime::routing;

use crate::store::LedgerStore;
use crate::store_ledger::{self as sl, native_store, with_native, Pending};

type Value<'py> = Bound<'py, PyAny>;

fn iso(value: &Value<'_>) -> PyResult<String> {
    value.call_method0("isoformat")?.extract()
}
fn repr(value: &Value<'_>) -> PyResult<String> {
    value.repr()?.extract()
}
fn micros(value: &Value<'_>) -> PyResult<i64> {
    value.py().import("trade_engine.eod._runtime")?.call_method1("micros", (value,))?.extract()
}
fn values<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    value.try_iter()?.collect()
}
fn sorted<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    values(&value.py().import("builtins")?.call_method1("sorted", (value,))?)
}
fn strings(value: &Value<'_>) -> PyResult<Vec<String>> {
    sorted(value)?.iter().map(|v| v.extract()).collect()
}
/// A Python `str` as its text. Any other value is `None`, which the typed rules refuse.
fn text_of(value: &Value<'_>) -> Option<String> {
    value.extract::<String>().ok()
}

/// A Python `datetime` as the te_core instant it stands for, from its ISO text.
fn datetime_of(value: &Value<'_>) -> PyResult<DateTime> {
    parse_datetime(&iso(value)?).map(|(instant, _)| instant).map_err(super::flow_refusal)
}

/// A Python error raised inside a glue callback. A `StaleDataError` becomes the refusal that
/// `refusal` builds from its text; any other Python error is held (see `Pending`).
fn host_error(pending: &Pending, e: PyErr, stale: &Value<'_>, refusal: impl FnOnce(String) -> LErr) -> LErr {
    let py = stale.py();
    if e.is_instance(py, stale) {
        let text = e.value(py).as_any().str().and_then(|s| s.extract::<String>()).unwrap_or_default();
        return refusal(text);
    }
    pending.hold(e)
}

/// `config.dividends.dividends(symbol, session)`, as `(as_of, amount)` in the source's order.
fn dividend_records(source: &Value<'_>, symbol: &str, session: &Value<'_>) -> PyResult<Vec<(DateTime, Money)>> {
    let mut records = Vec::new();
    for item in values(&source.call_method1("dividends", (symbol, session))?)? {
        records.push((datetime_of(&item.getattr("as_of")?)?, sl::money(&item.getattr("amount")?)?));
    }
    Ok(records)
}

/// One option's quote as `_mark_options_account` reads it: the snapshot for its underlying,
/// then that snapshot's quote for the instrument. `None` when either is missing.
fn option_quote(snapshots: &Value<'_>, instrument: &Instrument) -> PyResult<Option<OptionQuote>> {
    let py = snapshots.py();
    let underlying = te_core::sim::snapshot::underlying_of(instrument).map_err(super::flow_refusal)?;
    let snapshot = snapshots.call_method1("get", (underlying,))?;
    if snapshot.is_none() {
        return Ok(None);
    }
    let quote = snapshot.call_method1("get", (sl::instrument_py(py, instrument)?,))?;
    if quote.is_none() {
        return Ok(None);
    }
    Ok(Some(OptionQuote {
        mid: sl::money(&quote.getattr("mid")?)?,
        quote_as_of: datetime_of(&quote.getattr("as_of")?)?,
        snapshot_as_of: datetime_of(&snapshot.getattr("as_of")?)?,
    }))
}

/// A settlement's fields as te_core reads them: underlying, session, settle time, as-of, price, source.
fn settlement_fields(price: &Value<'_>) -> PyResult<(String, String, String, DateTime, Money, String)> {
    let underlying: String = price.getattr("underlying")?.extract()?;
    let session = sl::date_of(&price.getattr("session")?)?.to_string();
    let settle_time: String = price.getattr("settle_time")?.getattr("value")?.extract()?;
    let as_of = datetime_of(&price.getattr("as_of")?)?;
    let amount = sl::money(&price.getattr("price")?)?;
    let source: String = price.getattr("source")?.extract()?;
    Ok((underlying, session, settle_time, as_of, amount, source))
}

/// The PM settlement for `symbol` on `session`, checked against the close asked for
/// (I5), the clock (I7) and the session close (I9). `close` is the session close in micros.
fn official_close(settlements: &Value<'_>, pm: &Value<'_>, stale: &Value<'_>, session_py: &Value<'_>,
    session: NaiveDate, pending: &Pending, clock: &sl::PyClock<'_, '_>, close: i64, symbol: &str) -> LR<(Money, String)> {
    let asked_close = session.to_string();
    let price = settlements.call_method1("settlement", (symbol, session_py, pm)).map_err(|e| {
        host_error(pending, e, stale, |text| LErr {
            kind: "runtime_replay",
            msg: format!("No official {asked_close} close for {symbol}: {text}"),
        })
    })?;
    let (underlying, price_session, settle_time, as_of, amount, source) =
        settlement_fields(&price).map_err(|e| pending.hold(e))?;
    rules::official_identity(symbol, &underlying, &asked_close, &price_session, &settle_time)?;
    let now = clock.now_utc();
    rules::official_future(symbol, &as_of.iso(), sl::micros(&as_of), sl::micros(&now), &now.iso())?;
    rules::official_close(symbol, &as_of.iso(), sl::micros(&as_of), close)?;
    Ok((amount, source))
}

struct Host<'py> {
    runner: Value<'py>,
    session: Value<'py>,
    through: Value<'py>,
    name: Value<'py>,
    closed: RefCell<Option<Value<'py>>>,
    /// The plain ledger's store, past the gate in `store_ledger::native_store`.
    native: Bound<'py, LedgerStore>,
    /// A Python error held while the native glue unwinds (see `store_ledger::Pending`).
    pending: Pending,
}

impl<'py> Host<'py> {
    fn new(runner: Value<'py>, session: Value<'py>, through: Value<'py>, name: Value<'py>) -> PyResult<Self> {
        let native = native_store(&runner)?;
        Ok(Host { runner, session, through, name, closed: RefCell::new(None), native, pending: Pending::default() })
    }
    fn config(&self) -> PyResult<Value<'py>> { self.runner.getattr("_config") }
    fn clock(&self) -> PyResult<Value<'py>> { self.runner.getattr("_clock") }
    fn calendar(&self) -> PyResult<Value<'py>> { self.runner.getattr("_calendar") }
    fn module(&self) -> PyResult<Value<'py>> {
        Ok(self.runner.py().import("trade_engine.eod.runner")?.into_any())
    }
    fn job(&self) -> PyResult<String> {
        self.config()?.getattr("job_name")?.extract()
    }
    fn passes(&self) -> PyResult<Vec<String>> {
        values(&self.module()?.getattr("PASSES")?)?.iter().map(|name| name.extract()).collect()
    }
    /// The session's open, as te_core's XNYS calendar gives it (a UTC datetime).
    fn session_open(&self) -> PyResult<Value<'py>> {
        let session = sl::date_of(&self.session)?;
        sl::datetime_py(self.runner.py(), &te_core::sim::session_open(session).map_err(super::flow_refusal)?.dt)
    }
    /// The session's close, as te_core's XNYS calendar gives it (a UTC datetime).
    fn close(&self) -> PyResult<Value<'py>> {
        let session = sl::date_of(&self.session)?;
        sl::datetime_py(self.runner.py(), &te_core::sim::session_close(session).map_err(super::flow_refusal)?.dt)
    }
    /// The command id of the account's run, or of its pass named by `self.name`.
    fn command(&self, account: &str, pass: bool) -> PyResult<String> {
        let job = self.job()?;
        let session = sl::date_of(&self.session)?;
        if pass {
            let name: String = self.name.extract()?;
            Ok(rules::command("eod", &rules::pass_job(&job, &name), account, &session.to_string()))
        } else {
            glue::run_command(&job, account, session).map_err(super::flow_refusal)
        }
    }
    /// Whether the ledger holds an event for this command.
    fn has_marker(&self, command: &str) -> PyResult<bool> {
        with_native(&self.native, &self.clock()?, &self.pending, |l, _| l.event_by_command(command))
            .map(|event| event.is_some())
    }
    /// The account's fill count (`fill_count`).
    fn fill_count(&self, account: &str) -> PyResult<i64> {
        with_native(&self.native, &self.clock()?, &self.pending, |l, _| glue::fill_count(&*l, account))
    }
    /// The account's marks appended this session (`marks_appended`).
    fn marks_appended(&self, account: &str) -> PyResult<i64> {
        let session = sl::date_of(&self.session)?;
        with_native(&self.native, &self.clock()?, &self.pending, |l, _| glue::marks_appended(&*l, account, session))
    }
    /// The session snapshots: the wiring, each snapshot's place in the session, and the
    /// coverage of each options account (I5, I7). The chain fetch and its sort stay Python.
    fn session_snapshots(&self, options: &[String], covered: bool) -> PyResult<Vec<Value<'py>>> {
        let py = self.runner.py();
        if options.is_empty() {
            return Ok(Vec::new());
        }
        let config = self.config()?;
        let accounts = repr(&PyList::new(py, options)?.into_any())?;
        rules::snapshots_wiring(&accounts, !config.getattr("chain_snapshots")?.is_none(),
            !config.getattr("settlements")?.is_none()).map_err(super::flow_refusal)?;
        let session = sl::date_of(&self.session)?;
        let session_iso = session.to_string();
        let open = sl::micros(&te_core::sim::session_open(session).map_err(super::flow_refusal)?.dt);
        let close = sl::micros(&te_core::sim::session_close(session).map_err(super::flow_refusal)?.dt);
        let chain = config.call_method1("chain_snapshots", (&self.session,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("key", py.import("operator")?.call_method1("attrgetter", ("as_of", "underlying"))?)?;
        let items = values(&py.import("builtins")?.getattr("sorted")?.call((chain,), Some(&kwargs))?)?;
        let chain_snapshot = py.import("trade_engine.market_data.chains")?.getattr("ChainSnapshot")?;
        for item in &items {
            let valid = item.is_instance(&chain_snapshot)?;
            let type_name: String = item.get_type().getattr("__name__")?.extract()?;
            let (underlying, as_of, at) = if valid {
                // The native scale for all three instants: the open and close are te_core's.
                (item.getattr("underlying")?.extract::<String>()?, iso(&item.getattr("as_of")?)?,
                    sl::micros(&datetime_of(&item.getattr("as_of")?)?))
            } else {
                (String::new(), String::new(), 0)
            };
            rules::snapshot(&type_name, valid, &underlying, &as_of, &session_iso, open, at, close)
                .map_err(super::flow_refusal)?;
        }
        if covered {
            let mut have = BTreeSet::new();
            for item in &items {
                have.insert(item.getattr("underlying")?.extract::<String>()?);
            }
            for account in options {
                let missing = with_native(&self.native, &self.clock()?, &self.pending, |l, _| {
                    let state = l.state(account)?;
                    let needed = glue::option_underlyings(&state)?;
                    Ok(needed.difference(&have).cloned().collect::<Vec<_>>().join(", "))
                })?;
                rules::coverage(&missing, &session_iso, account).map_err(super::flow_refusal)?;
            }
        }
        Ok(items)
    }

    /// The ex-dividends on the shares the account held at the open (O2, I5, I7).
    fn credit_dividends(&self, account: &str) -> PyResult<()> {
        let py = self.runner.py();
        let source = self.config()?.getattr("dividends")?;
        let has_source = !source.is_none();
        let session = sl::date_of(&self.session)?;
        let open = sl::micros(&te_core::sim::session_open(session).map_err(super::flow_refusal)?.dt);
        let stale = py.import("trade_engine.interfaces.market_data")?.getattr("StaleDataError")?;
        let pending = &self.pending;
        with_native(&self.native, &self.clock()?, pending, |l, c| {
            glue::credit_dividends(l, c, account, session, open, has_source, |symbol| {
                dividend_records(&source, symbol, &self.session).map_err(|e| {
                    host_error(pending, e, &stale, |text| LErr {
                        kind: "runtime_replay",
                        msg: format!("'{account}' holds {symbol}: {text}"),
                    })
                })
            })
        })
    }

    /// The options account's marks: options at the newest snapshot's mid, shares and
    /// underlyings at the official close (O3, I5, I6, I9).
    fn mark_options(&self, account: &str, tally: &Value<'py>) -> PyResult<()> {
        let py = self.runner.py();
        let config = self.config()?;
        let snapshots = tally.getattr("snapshots")?;
        let settlements = config.getattr("settlements")?;
        let max_age: f64 = config.getattr("mark_max_quote_age_seconds")?.extract()?;
        let session = sl::date_of(&self.session)?;
        let close = sl::micros(&te_core::sim::session_close(session).map_err(super::flow_refusal)?.dt);
        let pm = py.import("trade_engine.domain.option_roots")?.getattr("SettleTime")?.getattr("PM")?;
        let stale = py.import("trade_engine.interfaces.market_data")?.getattr("StaleDataError")?;
        let pending = &self.pending;
        with_native(&self.native, &self.clock()?, pending, |l, c| {
            glue::mark_options_account(l, c, account, session,
                |instrument| option_quote(&snapshots, instrument).map_err(|e| pending.hold(e)),
                |symbol| official_close(&settlements, &pm, &stale, &self.session, session, pending, c, close, symbol),
                max_age)
        })
    }

    fn result_kwargs(&self, account: &str) -> PyResult<Bound<'py, PyDict>> {
        let kwargs = PyDict::new(self.runner.py());
        kwargs.set_item("account_id", account)?;
        Ok(kwargs)
    }
    /// The venue's working book: venue order id to filled quantity as Python `str` spells it,
    /// so the I5 refusal text is the venue's own (`Decimal("2.0")` stays `"2.0"`).
    fn held_at_venue(&self, broker: &Value<'py>) -> PyResult<BTreeMap<String, String>> {
        let min_time = self.module()?.getattr("MIN_TIME")?;
        let mut held = BTreeMap::new();
        for order in values(&broker.call_method1("orders", (&min_time,))?)? {
            let venue_id: String = order.getattr("venue_order_id")?.extract()?;
            held.insert(venue_id, order.getattr("filled_quantity")?.str()?.extract()?);
        }
        Ok(held)
    }
    /// One instrument's one-minute bars for the session, each with whether it is
    /// a regular-hours bar. The fetch stays Python; the series checks are the typed `eod::bars`.
    fn load_bars(&self, instrument: &Value<'py>, open: &Value<'py>, close: &Value<'py>)
        -> PyResult<Vec<(Value<'py>, bool)>> {
        let py = self.runner.py();
        let symbol: String = instrument.getattr("symbol")?.extract()?;
        let max_age = self.config()?.getattr("bars_max_age_seconds")?;
        let raw = values(&self.runner.getattr("_market_data")?.call_method1("bars",
            (instrument, "1m", open, close, max_age))?)?;
        let final_minute = {
            let kwargs = PyDict::new(py);
            kwargs.set_item("minutes", 1)?;
            close.sub(py.import("datetime")?.getattr("timedelta")?.call((), Some(&kwargs))?)?
        };
        let (open_us, close_us) = (micros(open)?, micros(close)?);
        let (first_us, last_us, first_text, last_text) = match (raw.first(), raw.last()) {
            (Some(first), Some(last)) => {
                let (first_at, last_at) = (first.getattr("timestamp")?, last.getattr("timestamp")?);
                (micros(&first_at)?, micros(&last_at)?, iso(&first_at)?, iso(&last_at)?)
            }
            _ => (0, 0, String::new(), String::new()),
        };
        let (open_text, final_text) = (iso(open)?, iso(&final_minute)?);
        rules::bars(&symbol, !raw.is_empty(), [first_us, open_us, last_us, close_us],
            [&first_text, &open_text, &last_text, &final_text]).map_err(super::flow_refusal)?;
        let mut bars = Vec::with_capacity(raw.len());
        for bar in raw {
            let at = micros(&bar.getattr("timestamp")?)?;
            let regular = rules::regular(open_us, at, close_us);
            bars.push((bar, regular));
        }
        Ok(bars)
    }

    /// The underlyings one options account wants snapshots for: `None` when the account
    /// wants every snapshot (its strategy has no `option_universe` hook, or the hook
    /// returned `None`); otherwise the strategy's universe plus the account's held option
    /// and share underlyings. The hook is read the way `manage_options` is (~`Finish::Manage`):
    /// a missing `option_universe` attribute is `None`, and a non-iterable result, or an
    /// item that is not a `str`, surfaces as the Python error that refuses the run.
    fn wanted_underlyings(&self, account: &str) -> PyResult<Option<BTreeSet<String>>> {
        let strategy = self.config()?.getattr("strategies")?.call_method1("get", (account,))?;
        let universe = strategy.getattr("option_universe").or_else(|e| {
            if e.is_instance_of::<pyo3::exceptions::PyAttributeError>(self.runner.py()) {
                Ok(self.runner.py().None().into_bound(self.runner.py()))
            } else {
                Err(e)
            }
        })?;
        let mut wanted = if universe.is_none() {
            None
        } else {
            let result = universe.call1((&self.session,))?;
            if result.is_none() {
                None
            } else {
                let mut set = BTreeSet::new();
                for item in result.try_iter()? {
                    set.insert(item?.extract::<String>()?);
                }
                Some(set)
            }
        };
        with_native(&self.native, &self.clock()?, &self.pending, |l, _| {
            let state = l.state(account)?;
            let mut held = glue::option_underlyings(&state)?;
            held.extend(glue::share_underlyings(&state)?);
            if let Some(set) = &mut wanted {
                set.extend(held);
            }
            Ok(())
        })?;
        Ok(wanted)
    }
}

impl<'py> EodHost for Host<'py> {
    type Value = Value<'py>;
    type Error = PyErr;

    fn validate(&mut self, mode: Mode) -> PyResult<()> {
        let py = self.runner.py();
        if mode == Mode::Pass {
            rules::pass_name(&self.name.extract::<String>()?, &repr(&self.name)?).map_err(super::flow_refusal)?;
        }
        let dt = py.import("datetime")?;
        let valid = self.session.is_instance(&dt.getattr("date")?)?
            && !self.session.is_instance(&dt.getattr("datetime")?)?;
        let type_name: String = self.session.get_type().getattr("__name__")?.extract()?;
        rules::session_type(&type_name, valid).map_err(super::flow_refusal)?;
        let calendar = self.calendar()?;
        let exchange: String = calendar.getattr("exchange")?.extract()?;
        let is_session = calendar.call_method1("is_session", (&self.session,))?.is_truthy()?;
        rules::session(&iso(&self.session)?, &exchange, is_session).map_err(super::flow_refusal)?;
        if mode == Mode::Pass {
            let valid = self.through.is_instance(&dt.getattr("datetime")?)?
                && !self.through.getattr("tzinfo")?.is_none()
                && !self.through.call_method0("utcoffset")?.is_none();
            rules::through(&repr(&self.through)?, valid).map_err(super::flow_refusal)?;
            let open = self.session_open()?;
            let close = self.close()?;
            rules::pass_boundary(&self.name.extract::<String>()?, &iso(&self.session)?, &iso(&open)?, &iso(&close)?,
                &iso(&self.through)?, micros(&open)?, micros(&self.through)?, micros(&close)?).map_err(super::flow_refusal)?;
        }
        let job = self.job()?;
        let accounts = self.accounts()?;
        let session = sl::date_of(&self.session)?;
        with_native(&self.native, &self.clock()?, &self.pending, |l, _| {
            glue::require_previous_session_complete(&*l, &job, &accounts, session)
        })?;
        Ok(())
    }
    fn accounts(&self) -> PyResult<Vec<String>> { strings(&self.config()?.getattr("brokers")?) }
    fn is_options(&self, account: &str) -> PyResult<bool> {
        self.runner.call_method1("_is_options", (account,))?.extract()
    }
    fn completed(&self, account: &str, mode: Mode) -> PyResult<bool> {
        if self.has_marker(&self.command(account, false)?)? { return Ok(true); }
        Ok(mode == Mode::Pass && self.has_marker(&self.command(account, true)?)?)
    }
    fn prepare(&mut self, account: &str) -> PyResult<Self::Value> {
        let py = self.runner.py();
        let broker = self.config()?.getattr("brokers")?.get_item(account)?;
        broker.call_method0("connect")?;
        // An options account's orders fill at snapshots and its shares are marked at the
        // official close, so it replays no bars.
        let mut instruments = Vec::new();
        if !self.is_options(account)? {
            let replay = with_native(&self.native, &self.clock()?, &self.pending, |l, _| {
                let state = l.state(account)?;
                glue::replay_instruments(&state)
            })?;
            for instrument in &replay {
                instruments.push(sl::instrument_py(py, instrument)?);
            }
        }
        self.runner.call_method1("_restore_venue", (account, &broker))?;
        let held = self.held_at_venue(&broker)?;
        with_native(&self.native, &self.clock()?, &self.pending, |l, _| {
            let state = l.state(account)?;
            glue::check_venue(account, &state, &held)
        })?;
        Ok(PyTuple::new(py, instruments)?.into_any())
    }
    fn tally(&self, account: &str) -> PyResult<Self::Value> {
        let kwargs = PyDict::new(self.runner.py());
        kwargs.set_item("fills_before", self.fill_count(account)?)?;
        self.module()?.getattr("_Tally")?.call((), Some(&kwargs))
    }
    fn snapshots(&self, options: &[String], mode: Mode) -> PyResult<Vec<Self::Value>> {
        let snapshots = self.session_snapshots(options, mode != Mode::Pass)?;
        let mut out = Vec::new();
        for snapshot in snapshots {
            if mode == Mode::Close || self.covered(&snapshot, &self.through)? {
                out.push(snapshot);
            }
        }
        Ok(out)
    }
    fn pass_end(&self, account: &str) -> PyResult<Option<Self::Value>> {
        // The at_close of each pass that ran, and the latest of them.
        let job = self.job()?;
        let session = sl::date_of(&self.session)?;
        let names = self.passes()?;
        let ends = with_native(&self.native, &self.clock()?, &self.pending, |l, _| {
            let mut ends = Vec::new();
            for name in &names {
                let command = rules::command("eod", &rules::pass_job(&job, name), account, &session.to_string());
                if let Some(event) = l.event_by_command(&command)? {
                    match event.payload {
                        Obj::EodRun(run) => ends.push(run.at_close),
                        _ => return err("value", format!("pass command {command:?} is not a run marker")),
                    }
                }
            }
            Ok(ends)
        })?;
        let times: Vec<i64> = ends.iter().map(sl::micros).collect();
        match rules::latest(&times) {
            Some(index) => Ok(Some(sl::datetime_py(self.runner.py(), &ends[index])?)),
            None => Ok(None),
        }
    }
    fn validate_resume(&self, account: &str, end: &Self::Value) -> PyResult<()> {
        rules::resume(&self.name.extract::<String>()?, &iso(&self.session)?, account, &iso(end)?, &iso(&self.through)?,
            micros(&self.through)?, micros(end)?).map_err(super::flow_refusal)?;
        Ok(())
    }
    fn timeline(&self, accounts: &[Account<Self::Value>], snapshots: &[Self::Value],
        options: &[String]) -> PyResult<Vec<Observation<Self::Value>>> {
        let py = self.runner.py();
        let holders = PyDict::new(py);
        for account in accounts {
            for instrument in account.instruments.try_iter()? {
                let instrument = instrument?;
                let ids = match holders.get_item(&instrument)? {
                    Some(ids) => ids,
                    None => {
                        let ids = PyList::empty(py).into_any();
                        holders.set_item(&instrument, &ids)?;
                        ids
                    }
                };
                ids.call_method1("append", (&account.id,))?;
            }
        }
        if holders.is_empty() && snapshots.is_empty() { return Ok(Vec::new()); }
        let open = self.session_open()?;
        let close = self.close()?;
        let read = self.clock()?.call_method0("now_utc")?;
        if rules::after(micros(&read)?, micros(&open)?) {
            rules::replay_clock(&iso(&self.session)?, &iso(&self.clock()?.call_method0("now_utc")?)?, &iso(&open)?,
                micros(&read)?, micros(&open)?).map_err(super::flow_refusal)?;
        }
        let operator = py.import("operator")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("key", operator.call_method1("attrgetter", ("symbol",))?)?;
        let instruments = py.import("builtins")?.getattr("sorted")?.call((&holders,), Some(&kwargs))?;
        let timeline = PyList::empty(py);
        for instrument in instruments.try_iter()? {
            let instrument = instrument?;
            for (bar, regular) in self.load_bars(&instrument, &open, &close)? {
                timeline.append((&bar.getattr("timestamp")?, 1, instrument.getattr("symbol")?, &bar, regular))?;
            }
        }
        for snapshot in snapshots {
            timeline.append((snapshot.getattr("as_of")?, 0, snapshot.getattr("underlying")?, snapshot, false))?;
        }
        kwargs.set_item("key", operator.call_method1("itemgetter", (0, 1, 2))?)?;
        timeline.call_method("sort", (), Some(&kwargs))?;
        // One wanted set per options account, computed once before the row loop. `None`
        // means the account wants every snapshot.
        let mut wanted: BTreeMap<String, Option<BTreeSet<String>>> = BTreeMap::new();
        for account in options {
            wanted.insert(account.clone(), self.wanted_underlyings(account)?);
        }
        let mut rows = Vec::new();
        for row in timeline.iter() {
            let item = row.get_item(3)?;
            let snapshot = row.get_item(1)?.extract::<i64>()? == 0;
            let ids = if snapshot {
                // The options accounts, in `options` order, whose wanted set is "all" or
                // contains this snapshot's underlying (an exact string compare). An empty
                // `ids` still keeps the row: replay advances the clock on every row.
                let underlying: String = item.getattr("underlying")?.extract()?;
                options.iter().filter(|account| match wanted.get(*account) {
                    Some(None) => true,
                    Some(Some(set)) => set.contains(&underlying),
                    None => false,
                }).cloned().collect()
            } else {
                strings(&holders.get_item(item.getattr("instrument")?)?.ok_or_else(
                    || pyo3::exceptions::PyKeyError::new_err("missing bar holders"))?)?
            };
            rows.push(Observation { at: row.get_item(0)?, item, accounts: ids, snapshot,
                regular: row.get_item(4)?.extract()? });
        }
        Ok(rows)
    }
    fn replay_accounts(&self, accounts: &[Account<Self::Value>], snapshots: &[Self::Value],
        options: &[String], ends: &[(String, Self::Value)]) -> PyResult<()> {
        let py = self.runner.py();
        let replays = PyDict::new(py);
        let tallies = PyDict::new(py);
        let mornings = PyDict::new(py);
        for account in accounts {
            replays.set_item(&account.id, &account.instruments)?;
            tallies.set_item(&account.id, &account.tally)?;
        }
        for (id, end) in ends { mornings.set_item(id, end)?; }
        self.runner.call_method1("_replay_session",
            (&self.session, replays, tallies, PyList::new(py, snapshots)?, options.to_vec(), mornings))?;
        Ok(())
    }
    fn advance(&self, at: &Self::Value) -> PyResult<()> {
        self.runner.call_method1("_advance_clock", (at,))?;
        Ok(())
    }
    fn covered(&self, snapshot: &Self::Value, end: &Self::Value) -> PyResult<bool> {
        Ok(rules::cutoff(micros(&snapshot.getattr("as_of")?)?, micros(end)?))
    }
    fn remember(&self, account: &Account<Self::Value>, snapshot: &Self::Value) -> PyResult<()> {
        account.tally.getattr("snapshots")?.set_item(snapshot.getattr("underlying")?, snapshot)
    }
    fn snapshot(&self, account: &Account<Self::Value>, snapshot: &Self::Value) -> PyResult<()> {
        // `manage_at_snapshot` stays Python until W6-E; the tally merge and the command are native.
        let tally = &account.tally;
        let router = self.runner.call_method1("_router", (&account.id,))?;
        let command = self.command(&account.id, false)?;
        let tallied = router.call_method1("manage_at_snapshot",
            (&account.id, &self.session, snapshot, tally.getattr("snapshots")?, command))?;
        let fields = ["orders_submitted", "exit_actions", "snapshots_processed"];
        let mut text = Vec::with_capacity(6);
        for field in fields {
            text.push(tally.getattr(field)?.str()?.extract::<String>()?);
        }
        for field in fields {
            text.push(tallied.getattr(field)?.str()?.extract::<String>()?);
        }
        let merged = routing::tally(&text).map_err(super::flow_refusal)?;
        for (field, value) in fields.iter().zip(&merged) {
            let value = value.parse::<i64>().map_err(|e: ParseIntError| PyValueError::new_err(e.to_string()))?;
            tally.setattr(*field, value)?;
        }
        Ok(())
    }
    fn bar(&self, account: &Account<Self::Value>, row: &Observation<Self::Value>) -> PyResult<()> {
        let broker = self.config()?.getattr("brokers")?.get_item(&account.id)?;
        broker.call_method1("process_bar", (&row.item,))?;
        if row.regular {
            account.tally.getattr("last_regular_closes")?.set_item(
                row.item.getattr("instrument")?, row.item.getattr("close")?)?;
        }
        let manager = self.runner.call_method1("_manager_for", (&account.id, &broker))?;
        self.runner.call_method1("_reconcile_after_bar", (&account.id, broker, manager, &row.at))?;
        let count = account.tally.getattr("bars_processed")?.extract::<i64>()?;
        account.tally.setattr("bars_processed", routing::increment(count, 1))
    }
    fn boundary(&self, boundary: Boundary) -> PyResult<()> {
        let at = match boundary {
            Boundary::Through => self.through.clone(),
            Boundary::Close => {
                let close = self.close()?;
                *self.closed.borrow_mut() = Some(close.clone());
                close
            }
            Boundary::Settled => {
                let close = self.closed.borrow().as_ref().ok_or_else(
                    || pyo3::exceptions::PyRuntimeError::new_err("settlement boundary before close"))?.clone();
                close.call_method1("__add__", (self.config()?.getattr("settle_delay")?,))?
            }
        };
        self.advance(&at)
    }
    fn settle(&self, accounts: &[String]) -> PyResult<()> {
        let holding = with_native(&self.native, &self.clock()?, &self.pending, |l, _| glue::options_holders(&*l, accounts))?;
        if holding.is_empty() {
            return Ok(());
        }
        let lifecycle = self.config()?.getattr("lifecycle")?;
        let text = repr(&PyList::new(self.runner.py(), &holding)?.into_any())?;
        rules::lifecycle(&text, true, !lifecycle.is_none()).map_err(super::flow_refusal)?;
        lifecycle.call_method1("run", (&self.session, holding))?;
        Ok(())
    }
    fn finish(&self, account: &Account<Self::Value>, effect: Finish) -> PyResult<i64> {
        let id = &account.id;
        let tally = &account.tally;
        match effect {
            Finish::Reconcile => {
                let broker = self.config()?.getattr("brokers")?.get_item(id)?;
                if account.options || account.instruments.is_truthy()? {
                    let manager = if account.options {
                        self.runner.call_method1("_option_manager", (id,))?.getattr("orders")?
                    } else { self.runner.call_method1("_manager_for", (id, &broker))? };
                    self.runner.call_method1("_reconcile_after_bar", (id, broker, manager, self.runner.py().None()))?;
                }
            }
            Finish::Sync => {
                self.runner.call_method1("_option_manager", (id,))?.call_method1("sync",
                    (id, format!("eod:{}:{id}:{}:settled", self.config()?.getattr("job_name")?.extract::<String>()?, iso(&self.session)?)))?;
            }
            Finish::Dividends => self.credit_dividends(id)?,
            Finish::Marks => {
                if account.options {
                    self.mark_options(id, tally)?;
                } else {
                    let job = self.job()?;
                    let closes = sl::closes(&tally.getattr("last_regular_closes")?)?;
                    let session = sl::date_of(&self.session)?;
                    with_native(&self.native, &self.clock()?, &self.pending, |l, c| {
                        glue::mark_positions(l, c, &job, id, &closes, session)
                    })?;
                }
            }
            Finish::Manage => {
                if account.options {
                    let router = self.runner.call_method1("_router", (id,))?;
                    let strategy = self.config()?.getattr("strategies")?.call_method1("get", (id,))?;
                    let manage = strategy.getattr("manage_options").or_else(|e| {
                        if e.is_instance_of::<pyo3::exceptions::PyAttributeError>(self.runner.py()) {
                            Ok(self.runner.py().None().into_bound(self.runner.py()))
                        } else { Err(e) }
                    })?;
                    if manage.is_callable() {
                        let now = self.clock()?.call_method0("now_utc")?;
                        let state = self.runner.getattr("_ledger")?.call_method1("state", (id,))?;
                        let structures = self.runner.py().import("trade_engine.oms.options")?.call_method1("open_structures", (state,))?;
                        let kwargs = PyDict::new(self.runner.py());
                        kwargs.set_item("snapshots", tally.getattr("snapshots")?)?;
                        let context = router.call_method("context", (id, &self.session, now, structures), Some(&kwargs))?;
                        let actions = self.runner.py().import("builtins")?.call_method1("list", (manage.call1((context,))?,))?;
                        let tallied = router.call_method1("apply", (id, &self.session, actions,
                            self.runner.py().None(), tally.getattr("snapshots")?,
                            format!("eod:{}:{id}:{}:close", self.config()?.getattr("job_name")?.extract::<String>()?, iso(&self.session)?)))?;
                        for field in ["orders_submitted", "exit_actions"] {
                            tally.setattr(field, tally.getattr(field)?.call_method1("__add__", (tallied.getattr(field)?,))?)?;
                        }
                    }
                } else {
                    return self.runner.call_method1("_manage_positions", (id, &self.session, tally.getattr("last_regular_closes")?))?.extract();
                }
            }
            Finish::Entries => {
                if account.options { self.runner.call_method1("_submit_new_option_entries", (id, &self.session, tally))?; }
                else { return self.runner.call_method1("_submit_new_orders", (id, &self.session))?.extract(); }
            }
            Finish::Marker => {
                let job = self.job()?;
                let session = sl::date_of(&self.session)?;
                let (job, command, bars) = if self.name.is_none() {
                    (job, None, tally.getattr("bars_processed")?.extract::<i64>()?)
                } else {
                    (rules::pass_job(&job, &self.name.extract::<String>()?), Some(self.command(id, true)?), 0)
                };
                with_native(&self.native, &self.clock()?, &self.pending, |l, c| {
                    glue::append_run_marker(l, c, &job, id, session, i128::from(bars), command.as_deref())
                })?;
            }
        }
        Ok(0)
    }
    fn result(&self, account: &Account<Self::Value>, mode: Mode, orders: i64, exits: i64) -> PyResult<Self::Value> {
        let kwargs = self.result_kwargs(&account.id)?;
        let tally = &account.tally;
        let fills = self.fill_count(&account.id)?;
        let before = tally.getattr("fills_before")?.extract::<i64>()?;
        kwargs.set_item("fills_recorded", rules::difference(fills, before))?;
        if mode == Mode::Close {
            kwargs.set_item("marks_appended", self.marks_appended(&account.id)?)?;
        }
        if account.options {
            for field in ["orders_submitted", "exit_actions", "snapshots_processed"] {
                kwargs.set_item(field, tally.getattr(field)?)?;
            }
        } else {
            kwargs.set_item("bars_processed", tally.getattr("bars_processed")?)?;
            kwargs.set_item("orders_submitted", orders)?;
            kwargs.set_item("exit_actions", exits)?;
        }
        self.module()?.getattr("AccountRunResult")?.call((), Some(&kwargs))
    }
    fn finish_account(&self, account: &Account<Self::Value>) -> PyResult<Self::Value> {
        if account.options {
            self.runner.call_method1("_finish_options_account", (&account.id, &self.session, &account.tally))
        } else {
            self.runner.call_method1("_finish_account",
                (&account.id, &self.session, &account.instruments, &account.tally))
        }
    }
    fn empty_result(&self, account: &str) -> PyResult<Self::Value> {
        self.module()?.getattr("AccountRunResult")?.call((), Some(&self.result_kwargs(account)?))
    }
    /// Each sink in destination order. A sink without a callable `publish`
    /// refuses (I5); the outbox calls `publish(event_seq, payload)` for each item.
    fn drain(&self) -> PyResult<()> {
        let py = self.runner.py();
        let ledger = self.runner.getattr("_ledger")?;
        let clock = self.clock()?;
        let sinks = self.config()?.getattr("sinks")?;
        for pair in sorted(&sinks.call_method0("items")?)? {
            let destination: String = pair.get_item(0)?.extract()?;
            let sink = pair.get_item(1)?;
            let publish = match sink.getattr("publish") {
                Ok(publish) => Some(publish).filter(|publish| publish.is_callable()),
                Err(e) if e.is_instance_of::<PyAttributeError>(py) => None,
                Err(e) => return Err(e),
            };
            rules::sink(&destination, publish.is_some()).map_err(super::flow_refusal)?;
            if let Some(publish) = publish {
                let publish = publish.unbind();
                let deliver = PyCFunction::new_closure(py, None, None, move |args: &Bound<'_, PyTuple>, _| -> PyResult<Py<PyAny>> {
                    let item = args.get_item(0)?;
                    let delivered = publish.bind(args.py()).call1((item.getattr("event_seq")?, item.getattr("payload")?))?;
                    Ok(delivered.unbind())
                })?;
                ledger.call_method1("drain_outbox", (&destination, deliver, &clock))?;
            }
        }
        Ok(())
    }
}

fn run<'py>(mut host: Host<'py>, mode: Mode) -> PyResult<Value<'py>> {
    let py = host.runner.py();
    let results = flow::run(&mut host, mode)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("session", &host.session)?;
    kwargs.set_item("accounts", PyTuple::new(py, results)?)?;
    host.module()?.getattr("EodRunResult")?.call((), Some(&kwargs))
}

#[pyfunction]
fn eod_run<'py>(runner: Value<'py>, session: Value<'py>) -> PyResult<Value<'py>> {
    let py = runner.py();
    run(Host::new(runner, session, py.None().into_bound(py), py.None().into_bound(py))?, Mode::Close)
}

#[pyfunction]
fn eod_pass<'py>(runner: Value<'py>, session: Value<'py>, through: Value<'py>, name: Value<'py>) -> PyResult<Value<'py>> {
    run(Host::new(runner, session, through, name)?, Mode::Pass)
}

#[pyfunction]
fn eod_finish<'py>(runner: Value<'py>, account: String, session: Value<'py>, instruments: Value<'py>,
    tally: Value<'py>, options: bool) -> PyResult<Value<'py>> {
    let py = runner.py();
    let host = Host::new(runner, session, py.None().into_bound(py), py.None().into_bound(py))?;
    flow::finish(&host, &Account { id: account, instruments, tally, options }, Mode::Close)
}

#[pyfunction]
#[pyo3(signature = (runner, session, replays, tallies, snapshots, options, mornings=None))]
fn eod_replay<'py>(runner: Value<'py>, session: Value<'py>, replays: Value<'py>, tallies: Value<'py>,
    snapshots: Value<'py>, options: Vec<String>, mornings: Option<Value<'py>>) -> PyResult<()> {
    let py = runner.py();
    let host = Host::new(runner, session, py.None().into_bound(py), py.None().into_bound(py))?;
    let mut accounts = Vec::new();
    for id in replays.try_iter()? {
        let id = id?;
        let account = id.extract::<String>()?;
        accounts.push(Account { instruments: replays.get_item(&id)?, tally: tallies.get_item(&id)?,
            options: options.contains(&account), id: account });
    }
    let mut ends = Vec::new();
    if let Some(mornings) = mornings {
        if mornings.is_truthy()? {
            for item in mornings.call_method0("items")?.try_iter()? {
                let item = item?;
                ends.push((item.get_item(0)?.extract()?, item.get_item(1)?));
            }
        }
    }
    flow::replay(&host, &accounts, &values(&snapshots)?, &options, &ends)
}

/// The construction gate: a plain `Ledger` on the XNYS `ExchangeCalendar`, or `EodRunnerError`.
#[pyfunction]
fn eod_require_native<'py>(ledger: Value<'py>, calendar: Value<'py>) -> PyResult<()> {
    sl::require_native(&ledger, &calendar).map(|_| ())
}

/// `void_session`: the owner's void of `session` for each account, native.
#[pyfunction]
fn eod_void_session<'py>(ledger: Value<'py>, clock: Value<'py>, calendar: Value<'py>, job: String,
    session: Value<'py>, accounts: Value<'py>, reason: Value<'py>, owner: Value<'py>) -> PyResult<Vec<String>> {
    let store = sl::require_native(&ledger, &calendar)?;
    // A datetime passes `isinstance(date)` but is not a session: refuse it with the core's text.
    if !session.is_instance_of::<PyDate>() || session.is_instance_of::<PyDateTime>() {
        let msg = format!("void session must be a date, got {}", repr(&session)?);
        return Err(sl::refuse(session.py(), &msg));
    }
    let session = sl::date_of(&session)?;
    let accounts = values(&accounts)?.iter().map(|account| account.extract::<String>()).collect::<PyResult<Vec<_>>>()?;
    let (reason, owner) = (text_of(&reason), text_of(&owner));
    let pending = Pending::default();
    with_native(&store, &clock, &pending, |l, c| {
        glue::void_session(l, c, &job, session, &accounts, reason.as_deref(), owner.as_deref())
    })
}

/// `write_off_order`: the owner's write-off of one stuck working order, native. Takes no calendar.
#[pyfunction]
fn eod_write_off<'py>(ledger: Value<'py>, clock: Value<'py>, account: String, order_id: String,
    reason: Value<'py>, owner: Value<'py>, venue_cancel_confirmed: bool) -> PyResult<Vec<String>> {
    let store = sl::ledger_store(&ledger)?;
    let (reason, owner) = (text_of(&reason), text_of(&owner));
    let pending = Pending::default();
    with_native(&store, &clock, &pending, |l, c| {
        glue::write_off_order(l, c, &account, &order_id, reason.as_deref(), owner.as_deref(), venue_cancel_confirmed)
    })
}

/// `EodRunnerConfig.__post_init__`'s checks (`eod:config`): the job, the brokers, the bar age
/// and the settle delay. The mark-quote check stays in Python.
#[pyfunction]
fn eod_config_check(job_name: Value<'_>, brokers: Value<'_>, bars_max_age_seconds: Value<'_>,
    settle_delay: Value<'_>) -> PyResult<()> {
    let py = settle_delay.py();
    let age_type = (bars_max_age_seconds.is_instance_of::<PyInt>() || bars_max_age_seconds.is_instance_of::<PyFloat>())
        && !bars_max_age_seconds.is_instance_of::<PyBool>();
    let delay_type = settle_delay.is_instance(&py.import("datetime")?.getattr("timedelta")?)?;
    let delay = if delay_type { micros(&settle_delay)? } else { 0 };
    let bars_age = if age_type { bars_max_age_seconds.extract::<f64>()? } else { 0.0 };
    rules::config(job_name.is_truthy()?, brokers.is_truthy()?, age_type, bars_age, delay_type, delay)
        .map_err(super::flow_refusal)
}

/// The command id of an account's session void.
#[pyfunction]
fn eod_void_command(job: String, account: String, session: Value<'_>) -> PyResult<String> {
    glue::void_command(&job, &account, sl::date_of(&session)?).map_err(super::flow_refusal)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(eod_config_check, m)?)?;
    m.add_function(wrap_pyfunction!(eod_void_command, m)?)?;
    m.add_function(wrap_pyfunction!(eod_run, m)?)?;
    m.add_function(wrap_pyfunction!(eod_pass, m)?)?;
    m.add_function(wrap_pyfunction!(eod_finish, m)?)?;
    m.add_function(wrap_pyfunction!(eod_replay, m)?)?;
    m.add_function(wrap_pyfunction!(eod_require_native, m)?)?;
    m.add_function(wrap_pyfunction!(eod_void_session, m)?)?;
    m.add_function(wrap_pyfunction!(eod_write_off, m)?)?;
    Ok(())
}
