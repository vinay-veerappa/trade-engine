use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use te_core::runtime::eod_flow::{self as flow, Account, Boundary, EodHost, Finish, Mode, Observation};

type Value<'py> = Bound<'py, PyAny>;

fn rule(op: &str, text: Vec<String>, numbers: Vec<i64>, flags: Vec<bool>) -> PyResult<te_core::runtime::Plan> {
    te_core::runtime::decide(op, &text, &numbers, &flags, &[])
        .map_err(super::flow_refusal)
}
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

struct Host<'py> {
    runner: Value<'py>,
    session: Value<'py>,
    through: Value<'py>,
    name: Value<'py>,
    closed: RefCell<Option<Value<'py>>>,
}

impl<'py> Host<'py> {
    fn config(&self) -> PyResult<Value<'py>> { self.runner.getattr("_config") }
    fn clock(&self) -> PyResult<Value<'py>> { self.runner.getattr("_clock") }
    fn calendar(&self) -> PyResult<Value<'py>> { self.runner.getattr("_calendar") }
    fn module(&self) -> PyResult<Value<'py>> {
        Ok(self.runner.py().import("trade_engine.eod.runner")?.into_any())
    }
    fn command(&self, account: &str, pass: bool) -> PyResult<Value<'py>> {
        if pass {
            self.runner.call_method1("_pass_command", (account, &self.session, &self.name))
        } else {
            self.runner.call_method1("_run_command", (account, &self.session))
        }
    }
    fn marker(&self, command: &Value<'py>) -> PyResult<Value<'py>> {
        self.runner.getattr("_ledger")?.call_method1("event_by_command", (command,))
    }
    fn close(&self) -> PyResult<Value<'py>> {
        self.calendar()?.call_method1("session_close", (&self.session,))
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
            let text = PyList::new(py, [&self.name, &self.name.repr()?.into_any()])?;
            py.import("trade_engine.eod._runtime")?.call_method1("decide", ("eod:pass_name", text))?;
        }
        let dt = py.import("datetime")?;
        let valid = self.session.is_instance(&dt.getattr("date")?)?
            && !self.session.is_instance(&dt.getattr("datetime")?)?;
        rule("eod:session_type", vec![self.session.get_type().getattr("__name__")?.extract()?], vec![], vec![valid])?;
        let calendar = self.calendar()?;
        rule("eod:session", vec![iso(&self.session)?, calendar.getattr("exchange")?.extract()?], vec![],
            vec![calendar.call_method1("is_session", (&self.session,))?.is_truthy()?])?;
        if mode == Mode::Pass {
            let valid = self.through.is_instance(&dt.getattr("datetime")?)?
                && !self.through.getattr("tzinfo")?.is_none()
                && !self.through.call_method0("utcoffset")?.is_none();
            rule("eod:through", vec![repr(&self.through)?], vec![], vec![valid])?;
            let open = calendar.call_method1("session_open", (&self.session,))?;
            let close = self.close()?;
            rule("eod:pass_boundary", vec![self.name.extract()?, iso(&self.session)?, iso(&open)?, iso(&close)?, iso(&self.through)?],
                vec![micros(&open)?, micros(&self.through)?, micros(&close)?], vec![])?;
        }
        self.runner.call_method1("_require_previous_session_complete", (&self.session,))?;
        Ok(())
    }
    fn accounts(&self) -> PyResult<Vec<String>> { strings(&self.config()?.getattr("brokers")?) }
    fn is_options(&self, account: &str) -> PyResult<bool> {
        self.runner.call_method1("_is_options", (account,))?.extract()
    }
    fn completed(&self, account: &str, mode: Mode) -> PyResult<bool> {
        if !self.marker(&self.command(account, false)?)?.is_none() { return Ok(true); }
        Ok(mode == Mode::Pass && !self.marker(&self.command(account, true)?)?.is_none())
    }
    fn prepare(&mut self, account: &str) -> PyResult<Self::Value> {
        self.runner.call_method1("_prepare_account", (account,))
    }
    fn tally(&self, account: &str) -> PyResult<Self::Value> {
        let kwargs = PyDict::new(self.runner.py());
        kwargs.set_item("fills_before", self.runner.call_method1("_fill_count", (account,))?)?;
        self.module()?.getattr("_Tally")?.call((), Some(&kwargs))
    }
    fn snapshots(&self, options: &[String], mode: Mode) -> PyResult<Vec<Self::Value>> {
        let kwargs = PyDict::new(self.runner.py());
        if mode == Mode::Pass { kwargs.set_item("covered", false)?; }
        let snapshots = self.runner.call_method("_session_snapshots",
            (&self.session, options.to_vec()), Some(&kwargs))?;
        let mut out = Vec::new();
        for snapshot in snapshots.try_iter()? {
            let snapshot = snapshot?;
            if mode == Mode::Close || self.covered(&snapshot, &self.through)? {
                out.push(snapshot);
            }
        }
        Ok(out)
    }
    fn pass_end(&self, account: &str) -> PyResult<Option<Self::Value>> {
        let end = self.runner.call_method1("_passes_through", (account, &self.session))?;
        Ok(if end.is_none() { None } else { Some(end) })
    }
    fn validate_resume(&self, account: &str, end: &Self::Value) -> PyResult<()> {
        rule("eod:resume", vec![self.name.extract()?, iso(&self.session)?, account.into(), iso(end)?, iso(&self.through)?],
            vec![micros(&self.through)?, micros(end)?], vec![])?;
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
        if rule("eod:after", vec![], vec![micros(&read)?, micros(&open)?], vec![])?.flags[0] {
            rule("eod:replay_clock", vec![iso(&self.session)?, iso(&self.clock()?.call_method0("now_utc")?)?, iso(&open)?],
                vec![micros(&read)?, micros(&open)?], vec![])?;
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
        Ok(rule("eod:cutoff", vec![], vec![micros(&snapshot.getattr("as_of")?)?, micros(end)?], vec![])?.flags[0])
    }
    fn remember(&self, account: &Account<Self::Value>, snapshot: &Self::Value) -> PyResult<()> {
        account.tally.getattr("snapshots")?.set_item(snapshot.getattr("underlying")?, snapshot)
    }
    fn snapshot(&self, account: &Account<Self::Value>, snapshot: &Self::Value) -> PyResult<()> {
        self.runner.call_method1("_options_at_snapshot", (&account.id, &self.session, snapshot, &account.tally))?;
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
        account.tally.setattr("bars_processed",
            rule("routing:increment", vec![], vec![count, 1], vec![])?.numbers[0])
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
        self.runner.call_method1("_settle_options", (&self.session, accounts.to_vec()))?;
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
            Finish::Dividends => { self.runner.call_method1("_credit_dividends", (id, &self.session))?; }
            Finish::Marks => {
                if account.options {
                    self.runner.call_method1("_mark_options_account", (id, &self.session, tally))?;
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
            Finish::Marker => {
                let kwargs = PyDict::new(self.runner.py());
                let bars = if self.name.is_none() { tally.getattr("bars_processed")? } else {
                    kwargs.set_item("job", self.runner.call_method1("_pass_job", (&self.name,))?)?;
                    kwargs.set_item("command_id", self.command(id, true)?)?;
                    0_i64.into_pyobject(self.runner.py())?.into_any()
                };
                self.runner.call_method("_append_run_marker", (id, &self.session, bars), Some(&kwargs))?;
            }
        }
        Ok(0)
    }
    fn result(&self, account: &Account<Self::Value>, mode: Mode, orders: i64, exits: i64) -> PyResult<Self::Value> {
        let kwargs = self.result_kwargs(&account.id)?;
        let tally = &account.tally;
        let fills = self.runner.call_method1("_fill_count", (&account.id,))?.extract::<i64>()?;
        let before = tally.getattr("fills_before")?.extract::<i64>()?;
        kwargs.set_item("fills_recorded", rule("eod:difference", vec![], vec![fills, before], vec![])?.numbers[0])?;
        if mode == Mode::Close {
            kwargs.set_item("marks_appended", self.runner.call_method1("_marks_appended", (&account.id, &self.session))?)?;
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
    run(Host { runner, session, through: py.None().into_bound(py), name: py.None().into_bound(py),
        closed: RefCell::new(None) }, Mode::Close)
}

#[pyfunction]
fn eod_pass<'py>(runner: Value<'py>, session: Value<'py>, through: Value<'py>, name: Value<'py>) -> PyResult<Value<'py>> {
    run(Host { runner, session, through, name, closed: RefCell::new(None) }, Mode::Pass)
}

#[pyfunction]
fn eod_finish<'py>(runner: Value<'py>, account: String, session: Value<'py>, instruments: Value<'py>,
    tally: Value<'py>, options: bool) -> PyResult<Value<'py>> {
    let py = runner.py();
    let host = Host { runner, session, through: py.None().into_bound(py), name: py.None().into_bound(py),
        closed: RefCell::new(None) };
    flow::finish(&host, &Account { id: account, instruments, tally, options }, Mode::Close)
}

#[pyfunction]
#[pyo3(signature = (runner, session, replays, tallies, snapshots, options, mornings=None))]
fn eod_replay<'py>(runner: Value<'py>, session: Value<'py>, replays: Value<'py>, tallies: Value<'py>,
    snapshots: Value<'py>, options: Vec<String>, mornings: Option<Value<'py>>) -> PyResult<()> {
    let py = runner.py();
    let host = Host { runner, session, through: py.None().into_bound(py), name: py.None().into_bound(py),
        closed: RefCell::new(None) };
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
use std::cell::RefCell;
