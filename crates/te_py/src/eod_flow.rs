use std::cell::RefCell;
use std::collections::BTreeSet;
use std::num::ParseIntError;

use chrono::NaiveDate;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
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

/// `_official_close(symbol, session)`: the PM settlement, checked against the close asked for
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
    /// The plain ledger's store when the native gate holds; `None` keeps the Python callbacks.
    native: Option<Bound<'py, LedgerStore>>,
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
    /// The command id of the account's run, or of its pass named by `self.name`
    /// (`_run_command` / `_pass_command`; the glue's builders when native).
    fn command(&self, account: &str, pass: bool) -> PyResult<String> {
        if self.native.is_none() {
            let command = if pass {
                self.runner.call_method1("_pass_command", (account, &self.session, &self.name))?
            } else {
                self.runner.call_method1("_run_command", (account, &self.session))?
            };
            return command.extract();
        }
        let job = self.job()?;
        let session = sl::date_of(&self.session)?;
        if pass {
            let name: String = self.name.extract()?;
            Ok(rules::command("eod", &rules::pass_job(&job, &name), account, &session.to_string()))
        } else {
            glue::run_command(&job, account, session).map_err(super::flow_refusal)
        }
    }
    /// Whether the ledger holds an event for this command (`event_by_command` is not None).
    fn has_marker(&self, command: &str) -> PyResult<bool> {
        match &self.native {
            Some(store) => with_native(store, &self.clock()?, &self.pending, |l, _| l.event_by_command(command))
                .map(|event| event.is_some()),
            None => Ok(!self.runner.getattr("_ledger")?.call_method1("event_by_command", (command,))?.is_none()),
        }
    }
    /// `_fill_count(account)`.
    fn fill_count(&self, account: &str) -> PyResult<i64> {
        match &self.native {
            Some(store) => with_native(store, &self.clock()?, &self.pending, |l, _| glue::fill_count(&*l, account)),
            None => self.runner.call_method1("_fill_count", (account,))?.extract(),
        }
    }
    /// `_marks_appended(account, session)`.
    fn marks_appended(&self, account: &str) -> PyResult<i64> {
        match &self.native {
            Some(store) => {
                let session = sl::date_of(&self.session)?;
                with_native(store, &self.clock()?, &self.pending, |l, _| glue::marks_appended(&*l, account, session))
            }
            None => self.runner.call_method1("_marks_appended", (account, &self.session))?.extract(),
        }
    }
    fn close(&self) -> PyResult<Value<'py>> {
        self.calendar()?.call_method1("session_close", (&self.session,))
    }
    /// `_session_snapshots`, native: the wiring, each snapshot's place in the session, and the
    /// coverage of each options account (I5, I7). The chain fetch and its sort stay Python.
    fn session_snapshots_native(&self, store: &Bound<'py, LedgerStore>, options: &[String],
        covered: bool) -> PyResult<Vec<Value<'py>>> {
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
                let missing = with_native(store, &self.clock()?, &self.pending, |l, _| {
                    let state = l.state(account)?;
                    let needed = glue::option_underlyings(&state)?;
                    Ok(needed.difference(&have).cloned().collect::<Vec<_>>().join(", "))
                })?;
                rules::coverage(&missing, &session_iso, account).map_err(super::flow_refusal)?;
            }
        }
        Ok(items)
    }

    /// `_credit_dividends`, native: the ex-dividends on the shares held at the open (O2, I5, I7).
    fn credit_dividends_native(&self, store: &Bound<'py, LedgerStore>, account: &str) -> PyResult<()> {
        let py = self.runner.py();
        let source = self.config()?.getattr("dividends")?;
        let has_source = !source.is_none();
        let session = sl::date_of(&self.session)?;
        let open = sl::micros(&te_core::sim::session_open(session).map_err(super::flow_refusal)?.dt);
        let stale = py.import("trade_engine.interfaces.market_data")?.getattr("StaleDataError")?;
        let pending = &self.pending;
        with_native(store, &self.clock()?, pending, |l, c| {
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

    /// `_mark_options_account`, native: options at the newest snapshot's mid, shares and
    /// underlyings at the official close (O3, I5, I6, I9).
    fn mark_options_native(&self, store: &Bound<'py, LedgerStore>, account: &str, tally: &Value<'py>) -> PyResult<()> {
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
        with_native(store, &self.clock()?, pending, |l, c| {
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
            let open = calendar.call_method1("session_open", (&self.session,))?;
            let close = self.close()?;
            rules::pass_boundary(&self.name.extract::<String>()?, &iso(&self.session)?, &iso(&open)?, &iso(&close)?,
                &iso(&self.through)?, micros(&open)?, micros(&self.through)?, micros(&close)?).map_err(super::flow_refusal)?;
        }
        if let Some(store) = &self.native {
            let job = self.job()?;
            let accounts = self.accounts()?;
            let session = sl::date_of(&self.session)?;
            with_native(store, &self.clock()?, &self.pending, |l, _| {
                glue::require_previous_session_complete(&*l, &job, &accounts, session)
            })?;
        } else {
            self.runner.call_method1("_require_previous_session_complete", (&self.session,))?;
        }
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
        self.runner.call_method1("_prepare_account", (account,))
    }
    fn tally(&self, account: &str) -> PyResult<Self::Value> {
        let kwargs = PyDict::new(self.runner.py());
        kwargs.set_item("fills_before", self.fill_count(account)?)?;
        self.module()?.getattr("_Tally")?.call((), Some(&kwargs))
    }
    fn snapshots(&self, options: &[String], mode: Mode) -> PyResult<Vec<Self::Value>> {
        let snapshots = match &self.native {
            Some(store) => self.session_snapshots_native(store, options, mode != Mode::Pass)?,
            None => {
                let kwargs = PyDict::new(self.runner.py());
                if mode == Mode::Pass { kwargs.set_item("covered", false)?; }
                let python = self.runner.call_method("_session_snapshots",
                    (&self.session, options.to_vec()), Some(&kwargs))?;
                values(&python)?
            }
        };
        let mut out = Vec::new();
        for snapshot in snapshots {
            if mode == Mode::Close || self.covered(&snapshot, &self.through)? {
                out.push(snapshot);
            }
        }
        Ok(out)
    }
    fn pass_end(&self, account: &str) -> PyResult<Option<Self::Value>> {
        let Some(store) = &self.native else {
            let end = self.runner.call_method1("_passes_through", (account, &self.session))?;
            return Ok(if end.is_none() { None } else { Some(end) });
        };
        // `_passes_through`: the at_close of each pass that ran, and the latest of them.
        let job = self.job()?;
        let session = sl::date_of(&self.session)?;
        let names = self.passes()?;
        let ends = with_native(store, &self.clock()?, &self.pending, |l, _| {
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
        let open = self.calendar()?.call_method1("session_open", (&self.session,))?;
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
            for row in self.runner.call_method1("_load_bars", (&instrument, &open, &close))?.try_iter()? {
                let row = row?;
                let bar = row.get_item(0)?;
                timeline.append((&bar.getattr("timestamp")?, 1, instrument.getattr("symbol")?, &bar, row.get_item(1)?))?;
            }
        }
        for snapshot in snapshots {
            timeline.append((snapshot.getattr("as_of")?, 0, snapshot.getattr("underlying")?, snapshot, false))?;
        }
        kwargs.set_item("key", operator.call_method1("itemgetter", (0, 1, 2))?)?;
        timeline.call_method("sort", (), Some(&kwargs))?;
        let mut rows = Vec::new();
        for row in timeline.iter() {
            let item = row.get_item(3)?;
            let snapshot = row.get_item(1)?.extract::<i64>()? == 0;
            let ids = if snapshot { options.to_vec() }
                else { strings(&holders.get_item(item.getattr("instrument")?)?.ok_or_else(
                    || pyo3::exceptions::PyKeyError::new_err("missing bar holders"))?)? };
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
        if self.native.is_none() {
            self.runner.call_method1("_options_at_snapshot", (&account.id, &self.session, snapshot, &account.tally))?;
            return Ok(());
        }
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
        let Some(store) = &self.native else {
            self.runner.call_method1("_settle_options", (&self.session, accounts.to_vec()))?;
            return Ok(());
        };
        let holding = with_native(store, &self.clock()?, &self.pending, |l, _| glue::options_holders(&*l, accounts))?;
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
            Finish::Dividends => match &self.native {
                Some(store) => self.credit_dividends_native(store, id)?,
                None => { self.runner.call_method1("_credit_dividends", (id, &self.session))?; }
            },
            Finish::Marks => {
                if account.options {
                    match &self.native {
                        Some(store) => self.mark_options_native(store, id, tally)?,
                        None => { self.runner.call_method1("_mark_options_account", (id, &self.session, tally))?; }
                    }
                } else if let Some(store) = &self.native {
                    let job = self.job()?;
                    let closes = sl::closes(&tally.getattr("last_regular_closes")?)?;
                    let session = sl::date_of(&self.session)?;
                    with_native(store, &self.clock()?, &self.pending, |l, c| {
                        glue::mark_positions(l, c, &job, id, &closes, session)
                    })?;
                } else {
                    self.runner.call_method1("_mark_positions", (id, tally.getattr("last_regular_closes")?, &self.session))?;
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
            Finish::Marker => match &self.native {
                Some(store) => {
                    let job = self.job()?;
                    let session = sl::date_of(&self.session)?;
                    let (job, command, bars) = if self.name.is_none() {
                        (job, None, tally.getattr("bars_processed")?.extract::<i64>()?)
                    } else {
                        (rules::pass_job(&job, &self.name.extract::<String>()?), Some(self.command(id, true)?), 0)
                    };
                    with_native(store, &self.clock()?, &self.pending, |l, c| {
                        glue::append_run_marker(l, c, &job, id, session, i128::from(bars), command.as_deref())
                    })?;
                }
                None => {
                    let kwargs = PyDict::new(self.runner.py());
                    let bars = if self.name.is_none() { tally.getattr("bars_processed")? } else {
                        kwargs.set_item("job", self.runner.call_method1("_pass_job", (&self.name,))?)?;
                        kwargs.set_item("command_id", self.command(id, true)?)?;
                        0_i64.into_pyobject(self.runner.py())?.into_any()
                    };
                    self.runner.call_method("_append_run_marker", (id, &self.session, bars), Some(&kwargs))?;
                }
            },
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
    fn drain(&self) -> PyResult<()> {
        self.runner.call_method0("_drain_outbox")?;
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

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(eod_run, m)?)?;
    m.add_function(wrap_pyfunction!(eod_pass, m)?)?;
    m.add_function(wrap_pyfunction!(eod_finish, m)?)?;
    m.add_function(wrap_pyfunction!(eod_replay, m)?)?;
    Ok(())
}
