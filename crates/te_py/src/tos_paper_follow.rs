//! W7-CORE-C14: the `tos_paper.follow` / `tos_paper.exits` orchestration, moved into Rust.
//!
//! The pure decisions already live in `te_core::tos_paper::follow` and `te_core::tos_paper::exits`
//! (reached before through `tos_paper_plan.rs`'s typed doors). What is left in Python is the
//! sequencing: `follow_entries` and `plan_exits` build the mirror and the sim's account states and
//! rehydrate the orders the decision names; `follow_cycle` and `run_pass_mirror` drive the broker
//! and the ledger through `session_native`'s helpers (mirror appends go through the session host's
//! Rust `append`). Those helpers (`mirror_of`, `_with`,
//! `collect_only`, `run_mirror`, `cancel_ticket`) are Python collaborators in `session_native.py`
//! (a parallel ticket owns them), so they are reached through Python attribute lookup — never
//! assumed to be a concrete Rust type. `SplitLedger` becomes a `#[pyclass(subclass)]` base whose
//! methods delegate to the two Python ledgers it holds.
//!
//! `raise X from err` does not occur in this file's Python source (the `from err` raises live in
//! `broker.py` and `session_native.py`, owned by other tickets), so nothing here re-raises a cause.

use std::collections::{HashMap, HashSet};

use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use te_core::ledger::fold::AccountState;
use te_core::ledger::mirror::MirrorState;
use te_core::ledger::model::{self as lm, parse_datetime, DateTime, LErr, Side};
use te_core::ledger::ops::OMap;
use te_core::money::Money;
use te_core::tos_paper::exits::{self, Ctx, Exit, XOrder};
use te_core::tos_paper::follow;

use crate::sim::refuse;
use crate::store_ledger as sl;
use crate::tos_paper_session_host as host;

/// `_rs.call`'s mapping: a `LErr` refusal as `ValueError((kind, message))`.
fn aware_dt(value: &Bound<'_, PyAny>, key: &str) -> PyResult<DateTime> {
    let text: String = value.call_method0("isoformat")?.extract()?;
    let (t, is_aware) = parse_datetime(&text).map_err(refuse)?;
    if !is_aware {
        return Err(refuse(LErr { kind: "tos_wire", msg: format!("{key} must be timezone-aware") }));
    }
    Ok(t)
}

/// `pass_name`'s door: an aware instant, no check (the clock is always aware here).
fn dt_of(value: &Bound<'_, PyAny>) -> PyResult<DateTime> {
    let text: String = value.call_method0("isoformat")?.extract()?;
    let (t, _) = parse_datetime(&text).map_err(refuse)?;
    Ok(t)
}

/// The pass's `session` (`date.isoformat()`).
fn session_of(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.call_method0("isoformat")?.extract()
}

/// The Python classes and modules the orchestration reaches, imported once.
struct PyCtx {
    session_native: Py<PyAny>,
    mirror_refused: Py<PyAny>,
    exit_plan: Py<PyAny>,
    split_ledger_error: Py<PyAny>,
}

impl PyCtx {
    fn new(py: Python<'_>) -> PyResult<PyCtx> {
        let session_native = py.import("trade_engine.tos_paper.session_native")?;
        let events = py.import("trade_engine.ledger.events")?;
        let exits = py.import("trade_engine.tos_paper.exits")?;
        let follow = py.import("trade_engine.tos_paper.follow")?;
        Ok(PyCtx {
            session_native: session_native.into_any().unbind(),
            mirror_refused: events.getattr("MirrorRefused")?.unbind(),
            exit_plan: exits.getattr("ExitPlan")?.unbind(),
            split_ledger_error: follow.getattr("SplitLedgerError")?.unbind(),
        })
    }
}

fn pyctx(py: Python<'_>) -> PyResult<&'static PyCtx> {
    static CTX: GILOnceCell<PyCtx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || PyCtx::new(py))
}

/// `raise SplitLedgerError(msg)`.
fn split_ledger_error(py: Python<'_>, msg: &str) -> PyErr {
    let ctx = pyctx(py).expect("the follow context is initialized");
    match ctx.split_ledger_error.bind(py).call1((msg,)) {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

/// The venue's mirror and the mirrored accounts' states (`mirror_of` + `ledger.state`).
fn mirror_accounts<'py>(
    py: Python<'py>,
    ledger: &Bound<'py, PyAny>,
    binding: &Bound<'py, PyAny>,
) -> PyResult<(MirrorState, OMap<String, AccountState>, Vec<String>)> {
    let venue: String = binding.getattr("venue_account")?.extract()?;
    let mirrored: Vec<String> = binding
        .getattr("mirrored_accounts")?
        .try_iter()?
        .map(|v| v?.extract::<String>())
        .collect::<PyResult<_>>()?;
    let mirror_py = pyctx(py)?.session_native.bind(py).call_method1("mirror_of", (ledger, venue))?;
    let mirror = sl::mirror_of(&mirror_py)?;
    let mut amap = OMap::new();
    for account in &mirrored {
        let state_py = ledger.call_method1("state", (account,))?;
        let state = sl::account_of(&state_py)?;
        amap.insert(account.clone(), account.clone(), state);
    }
    Ok((mirror, amap, mirrored))
}

/// `follow_entries`, as Python did it: the mirror and account states, the decision, then the
/// orders the decision names rehydrated from the ledger.
fn follow_entries_impl(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    binding: &Bound<'_, PyAny>,
    session_open: &Bound<'_, PyAny>,
    now: &Bound<'_, PyAny>,
    max_age_us: i128,
) -> PyResult<(Vec<Py<PyAny>>, Vec<(String, String, String)>)> {
    let (mirror, amap, mirrored) = mirror_accounts(py, ledger, binding)?;
    let session_open = aware_dt(session_open, "session_open")?;
    let now = aware_dt(now, "now")?;
    let out = follow::follow_entries(&mirror, &amap, &mirrored, &session_open, &now, max_age_us).map_err(refuse)?;
    let mut send = Vec::with_capacity(out.send.len());
    for (account, order_id) in out.send {
        let state = ledger.call_method1("state", (&account,))?;
        let order = state.getattr("orders")?.get_item(&order_id)?;
        send.push(order.unbind());
    }
    Ok((send, out.refused))
}

/// An exit order as a Python `Order` (a LIMIT DAY order of the pass).
fn xorder_py(py: Python<'_>, x: &XOrder, at: &DateTime) -> PyResult<Py<PyAny>> {
    let order = lm::Order {
        order_id: x.order_id.clone(),
        account_id: x.account_id.clone(),
        instrument: x.instrument.clone(),
        order_type: lm::OrderType::Limit,
        side: x.side,
        quantity: x.quantity.clone(),
        command_id: x.order_id.clone(),
        created_at: at.clone(),
        limit_price: x.limit_price.clone(),
        stop_price: None,
        trail_amount: None,
        tif: lm::Tif::Day,
        state: lm::OrderState::New,
        parent_order_id: None,
        oco_group: None,
    };
    Ok(sl::obj_py(py, &lm::Obj::Order(order))?.unbind())
}

/// `(cancel, orders, refused, waits)` from a finished `Exit`.
fn convert_exit(
    py: Python<'_>,
    out: &Exit,
    at: &DateTime,
) -> PyResult<(Vec<String>, Vec<Py<PyAny>>, Vec<(String, String, String)>, Vec<(String, String)>)> {
    let mut orders = Vec::with_capacity(out.orders.len());
    for x in &out.orders {
        orders.push(xorder_py(py, x, at)?);
    }
    Ok((out.cancel.clone(), orders, out.refused.clone(), out.waits.clone()))
}

/// `plan_exits`, as Python did it: the planning loop fetches the first quote the plan has not been
/// answered for, in order, until it needs nothing more.
fn plan_exits_impl(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    binding: &Bound<'_, PyAny>,
    session: &Bound<'_, PyAny>,
    name: &str,
    price: &Bound<'_, PyAny>,
    at: &Bound<'_, PyAny>,
) -> PyResult<(Vec<String>, Vec<Py<PyAny>>, Vec<(String, String, String)>, Vec<(String, String)>)> {
    let (mirror, amap, mirrored) = mirror_accounts(py, ledger, binding)?;
    let session_str = session_of(session)?;
    let at_text: String = at.call_method0("isoformat")?.extract()?;
    let (at_dt, aware) = parse_datetime(&at_text).map_err(refuse)?;

    let mut prices: Vec<(String, Side, Option<Money>)> = Vec::new();
    let mut seen: HashSet<(String, Side)> = HashSet::new();
    loop {
        let ctx = Ctx {
            mirror: mirror.clone(),
            accounts: amap.clone(),
            empty: AccountState::new(""),
            mirrored: mirrored.clone(),
            session: session_str.clone(),
            prices: prices.clone(),
            at: at_text.clone(),
            aware,
        };
        let out = exits::plan_exits(&ctx, name).map_err(refuse)?;
        let need = out.priced.iter().find(|(contract, side)| !seen.contains(&(contract.hk(), *side)));
        let Some((contract, side)) = need else {
            return Ok(convert_exit(py, &out, &at_dt)?);
        };
        let key = (contract.hk(), *side);
        seen.insert(key.clone());
        let contract_py = sl::instrument_py(py, contract)?;
        let side_py = sl::side_py(py, *side)?;
        let quote = price.call1((contract_py, side_py))?;
        let money = if quote.is_none() { None } else { Some(sl::money(&quote)?) };
        prices.push((key.0, *side, money));
    }
}

/// The `(order_id, account_id)` map of the planned orders, as `run_pass_mirror`'s cancel loop reads it.
fn accounts_of(py: Python<'_>, orders: &[Py<PyAny>]) -> PyResult<HashMap<String, String>> {
    let mut map = HashMap::new();
    for order in orders {
        let order = order.bind(py);
        let order_id: String = order.getattr("order_id")?.extract()?;
        let account_id: String = order.getattr("account_id")?.extract()?;
        map.insert(order_id, account_id);
    }
    Ok(map)
}

/// `[oid for ticket, oid in plan.waits if ticket == key]`.
fn waiting_for(waits: &[(String, String)], key: &str) -> Vec<String> {
    waits.iter().filter(|(ticket, _)| ticket == key).map(|(_, oid)| oid.clone()).collect()
}

/// `a + b` on two Python sequences (the report's tuples).
fn concat<'py>(a: &Bound<'py, PyAny>, b: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    a.call_method1("__add__", (b,))
}

/// `a or b` on two Python values (Python truthiness).
fn py_or<'py>(a: &Bound<'py, PyAny>, b: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if a.is_truthy()? { Ok(a.clone()) } else { Ok(b.clone()) }
}

/// Append one `MirrorRefused` per unique `(oid, account, reason)` (`dict.fromkeys(refused)`), each
/// paired with its `refused:<oid>` command suffix, through the session host's `append`.
fn append_refused<'py>(
    py: Python<'py>,
    ledger: &Bound<'py, PyAny>,
    venue: &Bound<'py, PyAny>,
    clock: &Bound<'py, PyAny>,
    now: &Bound<'py, PyAny>,
    refused: &[(String, String, String)],
) -> PyResult<Bound<'py, PyAny>> {
    let mut seen: HashSet<(String, String, String)> = HashSet::new();
    let mut items = Vec::new();
    for (oid, account, reason) in refused {
        if !seen.insert((oid.clone(), account.clone(), reason.clone())) {
            continue;
        }
        let kw = PyDict::new(py);
        kw.set_item("venue", venue)?;
        kw.set_item("strategy_order_id", oid)?;
        kw.set_item("strategy_account", account)?;
        kw.set_item("reason", reason)?;
        kw.set_item("at", now)?;
        let event = pyctx(py)?.mirror_refused.bind(py).call((), Some(&kw))?;
        items.push((event, format!("refused:{oid}")));
    }
    let venue_name: String = venue.extract()?;
    let written = host::append(py, host::ctx(py)?, ledger, &venue_name, clock, items)?;
    Ok(PyList::new(py, written)?.into_any())
}

/// `follow_entries(ledger, binding, *, session_open, now, max_age) -> (entries, refused)`.
#[pyfunction]
#[pyo3(signature = (ledger, binding, session_open, now, max_age_us))]
pub(crate) fn tos_paper_follow_entries(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    binding: &Bound<'_, PyAny>,
    session_open: &Bound<'_, PyAny>,
    now: &Bound<'_, PyAny>,
    max_age_us: i128,
) -> PyResult<Py<PyAny>> {
    let (send, refused) = follow_entries_impl(py, ledger, binding, session_open, now, max_age_us)?;
    let send_t = PyTuple::new(py, send)?;
    let refused_t = PyTuple::new(py, refused)?;
    Ok(PyTuple::new(py, [send_t, refused_t])?.into_any().unbind())
}

/// `plan_exits(ledger, binding, session, *, name, price, at) -> ExitPlan`.
#[pyfunction]
#[pyo3(signature = (ledger, binding, session, name, price, at))]
pub(crate) fn tos_paper_follow_plan_exits(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    binding: &Bound<'_, PyAny>,
    session: &Bound<'_, PyAny>,
    name: &str,
    price: &Bound<'_, PyAny>,
    at: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let (cancel, orders, refused, waits) = plan_exits_impl(py, ledger, binding, session, name, price, at)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("cancel", PyTuple::new(py, cancel)?)?;
    kwargs.set_item("orders", PyTuple::new(py, orders)?)?;
    kwargs.set_item("refused", PyTuple::new(py, refused)?)?;
    kwargs.set_item("waits", PyTuple::new(py, waits)?)?;
    Ok(pyctx(py)?.exit_plan.bind(py).call((), Some(&kwargs))?.unbind())
}

/// `run_pass_mirror(...)`: collect, cancel what the exits replace, then send the pass's entries
/// and exits in one batch. Every collaborator is Python, reached by attribute lookup.
#[pyfunction]
#[pyo3(signature = (ledger, broker, entries, session, name, price, clock, express=None, may_wait=false, settle=None))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn tos_paper_follow_run_pass_mirror(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    broker: &Bound<'_, PyAny>,
    entries: &Bound<'_, PyAny>,
    session: &Bound<'_, PyAny>,
    name: &str,
    price: &Bound<'_, PyAny>,
    clock: &Bound<'_, PyAny>,
    express: Option<&Bound<'_, PyAny>>,
    may_wait: bool,
    settle: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyAny>> {
    let ctx = pyctx(py)?;
    let sn = ctx.session_native.bind(py);
    let venue = broker.getattr("venue")?;

    // collected = collect_only(ledger, broker, clock=clock, settle=settle)
    let collect_kw = PyDict::new(py);
    collect_kw.set_item("clock", clock)?;
    collect_kw.set_item("settle", settle)?;
    let collected = sn.call_method("collect_only", (ledger, broker), Some(&collect_kw))?;
    if !collected.getattr("deferred")?.is_none() {
        return Ok(collected.unbind());
    }

    // plan = plan_exits(ledger, broker.binding, session, name=name, price=price, at=clock.now_utc())
    let binding = broker.getattr("binding")?;
    let now_utc = clock.call_method0("now_utc")?;
    let (plan_cancel, plan_orders, mut refused, plan_waits) =
        plan_exits_impl(py, ledger, &binding, session, name, price, &now_utc)?;

    // sending = [*entries, *plan.orders]; cancels = list(plan.cancel)
    let mut sending: Vec<Bound<'_, PyAny>> = entries.try_iter()?.collect::<PyResult<_>>()?;
    for order in &plan_orders {
        sending.push(order.bind(py).clone());
    }
    let mut cancels: Vec<String> = plan_cancel;

    if let Some(express) = express {
        let mut expressed: Vec<Bound<'_, PyAny>> = Vec::new();
        for order in &sending {
            let out = express.call1((order,))?;
            if out.is_instance_of::<PyString>() {
                let reason: String = out.extract()?;
                let order_id: String = order.getattr("order_id")?.extract()?;
                let account_id: String = order.getattr("account_id")?.extract()?;
                refused.push((order_id, account_id, reason));
            } else {
                expressed.push(out);
            }
        }
        sending = expressed;
        let dropped: HashSet<String> = refused.iter().map(|(oid, _, _)| oid.clone()).collect();
        let mut kept: Vec<String> = Vec::new();
        for key in &cancels {
            let waiting = waiting_for(&plan_waits, key);
            if waiting.is_empty() || waiting.iter().any(|oid| !dropped.contains(oid)) {
                kept.push(key.clone());
            }
        }
        cancels = kept;
    }

    if !collected.getattr("halted")?.is_truthy()? {
        let accounts = accounts_of(py, &plan_orders)?;
        for key in &cancels {
            let cancel_kw = PyDict::new(py);
            cancel_kw.set_item("clock", clock)?;
            let ack = sn.call_method("cancel_ticket", (ledger, broker, key), Some(&cancel_kw))?;
            let ack_status: String = ack.getattr("status")?.extract()?;
            if ack_status != "ACCEPTED" {
                let ack_message: String = ack.getattr("message")?.extract()?;
                let why = if ack_message.is_empty() { ack_status.clone() } else { ack_message.clone() };
                let waiting = waiting_for(&plan_waits, key);
                for oid in &waiting {
                    refused.push((
                        oid.clone(),
                        accounts[oid].clone(),
                        format!("the resting ticket {key} was not cancelled ({why}); nothing is sent over it"),
                    ));
                }
                if waiting.is_empty() {
                    let ticket = sn
                        .call_method1("mirror_of", (ledger, &venue))?
                        .getattr("tickets")?
                        .get_item(key)?;
                    let account: String = ticket
                        .getattr("queued")?
                        .getattr("allocations")?
                        .get_item(0)?
                        .getattr("strategy_account")?
                        .extract()?;
                    refused.push((
                        format!("cancel:{key}@{}", session_of(session)?),
                        account,
                        format!("the resting ticket {key} was not cancelled ({why}); it is tried again next pass"),
                    ));
                }
            }
        }
    }

    // written = _append(ledger, venue, clock, [...MirrorRefused...])
    let now = clock.call_method0("now_utc")?;
    let written = append_refused(py, ledger, &venue, clock, &now, &refused)?;

    // report = run_mirror(ledger, broker, sending, clock=clock, may_wait=may_wait)
    let sending_list = PyList::new(py, sending)?;
    let run_kw = PyDict::new(py);
    run_kw.set_item("clock", clock)?;
    run_kw.set_item("may_wait", may_wait)?;
    let report = sn.call_method("run_mirror", (ledger, broker, &sending_list), Some(&run_kw))?;

    // return _with(report, fills=..., closes=..., refused=..., halted=...)
    let with_kw = PyDict::new(py);
    with_kw.set_item("fills", concat(&collected.getattr("fills")?, &report.getattr("fills")?)?)?;
    with_kw.set_item("closes", concat(&collected.getattr("closes")?, &report.getattr("closes")?)?)?;
    let written_t = py.import("builtins")?.call_method1("tuple", (&written,))?;
    with_kw.set_item("refused", concat(&written_t, &report.getattr("refused")?)?)?;
    with_kw.set_item("halted", py_or(&report.getattr("halted")?, &collected.getattr("halted")?)?)?;
    Ok(sn.call_method("_with", (&report,), Some(&with_kw))?.unbind())
}

/// `follow_cycle(...)`: one follow pass. The entries refused here are the report's too.
#[pyfunction]
#[pyo3(signature = (ledger, broker, session, session_open, price, clock, max_age_us, express=None, settle=None))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn tos_paper_follow_cycle(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    broker: &Bound<'_, PyAny>,
    session: &Bound<'_, PyAny>,
    session_open: &Bound<'_, PyAny>,
    price: &Bound<'_, PyAny>,
    clock: &Bound<'_, PyAny>,
    max_age_us: i128,
    express: Option<&Bound<'_, PyAny>>,
    settle: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyAny>> {
    let ctx = pyctx(py)?;
    let sn = ctx.session_native.bind(py);
    let venue = broker.getattr("venue")?;
    let binding = broker.getattr("binding")?;
    let now = clock.call_method0("now_utc")?;

    let (entries, refused) = follow_entries_impl(py, ledger, &binding, session_open, &now, max_age_us)?;
    let written = append_refused(py, ledger, &venue, clock, &now, &refused)?;

    let now_dt = dt_of(&now)?;
    let name = follow::pass_name(&now_dt).map_err(refuse)?;
    let entries_list = PyList::new(py, entries)?;
    let report = tos_paper_follow_run_pass_mirror(
        py,
        ledger,
        broker,
        &entries_list,
        session,
        &name,
        price,
        clock,
        express,
        true,
        settle,
    )?;

    // return _with(report, refused=tuple(written) + report.refused)
    let report_b = report.bind(py);
    let written_t = py.import("builtins")?.call_method1("tuple", (&written,))?;
    let refused = concat(&written_t, &report_b.getattr("refused")?)?;
    let with_kw = PyDict::new(py);
    with_kw.set_item("refused", &refused)?;
    Ok(sn.call_method("_with", (report_b,), Some(&with_kw))?.unbind())
}

/// `SplitLedger`: the sim's strategy accounts (read-only) joined to the follower's own ledger.
/// The two ledgers are Python collaborators; every method reaches them through attribute lookup.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct SplitLedgerBase {
    sim: Py<PyAny>,
    mirror: Py<PyAny>,
    sim_accounts: Py<PyAny>,
}

#[pymethods]
impl SplitLedgerBase {
    #[new]
    fn new(
        py: Python<'_>,
        sim: &Bound<'_, PyAny>,
        mirror: &Bound<'_, PyAny>,
        sim_accounts: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        if !sim_accounts.is_truthy()? {
            return Err(split_ledger_error(py, "a split ledger needs the sim accounts it reads"));
        }
        let frozen = py.import("builtins")?.call_method1("frozenset", (sim_accounts,))?;
        Ok(SplitLedgerBase {
            sim: sim.clone().unbind(),
            mirror: mirror.clone().unbind(),
            sim_accounts: frozen.unbind(),
        })
    }

    fn _of(&self, py: Python<'_>, account: &str) -> PyResult<Py<PyAny>> {
        let contains = self.sim_accounts.bind(py).call_method1("__contains__", (account,))?;
        if contains.is_truthy()? { Ok(self.sim.clone_ref(py)) } else { Ok(self.mirror.clone_ref(py)) }
    }

    fn state(&self, py: Python<'_>, account: &str) -> PyResult<Py<PyAny>> {
        let ledger = self._of(py, account)?;
        Ok(ledger.bind(py).call_method1("state", (account,))?.unbind())
    }

    fn accounts(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let builtins = py.import("builtins")?;
        let sim_set = builtins.call_method1("set", (self.sim_accounts.bind(py),))?;
        let mirror_accounts = self.mirror.bind(py).call_method0("accounts")?;
        let mirror_set = builtins.call_method1("set", (&mirror_accounts,))?;
        let union = sim_set.call_method1("__or__", (&mirror_set,))?;
        Ok(builtins.call_method1("sorted", (&union,))?.unbind())
    }

    #[pyo3(signature = (*, after=None, account=None))]
    fn events(
        &self,
        py: Python<'_>,
        after: Option<&Bound<'_, PyAny>>,
        account: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let Some(account) = account else {
            return Err(split_ledger_error(
                py,
                "name the account: two ledgers' sequence numbers do not compare",
            ));
        };
        let account_str: String = account.extract()?;
        let ledger = self._of(py, &account_str)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("after", after)?;
        kwargs.set_item("account", account)?;
        Ok(ledger.bind(py).call_method("events", (), Some(&kwargs))?.unbind())
    }

    #[pyo3(signature = (kind, *, account=None))]
    fn events_of_kind(
        &self,
        py: Python<'_>,
        kind: &Bound<'_, PyAny>,
        account: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        if let Some(account) = account {
            let account_str: String = account.extract()?;
            let ledger = self._of(py, &account_str)?;
            return Ok(ledger.bind(py).call_method1("events_of_kind", (kind, account))?.unbind());
        }
        let sim_events = self.sim.bind(py).call_method1("events_of_kind", (kind,))?;
        let filtered = PyList::empty(py);
        for event in sim_events.try_iter()? {
            let event = event?;
            let account = event.getattr("account")?;
            let contains = self.sim_accounts.bind(py).call_method1("__contains__", (&account,))?;
            if contains.is_truthy()? {
                filtered.append(event)?;
            }
        }
        let mirror_events = self.mirror.bind(py).call_method1("events_of_kind", (kind,))?;
        Ok(mirror_events.call_method1("__add__", (&filtered,))?.unbind())
    }

    fn _writes(&self, py: Python<'_>, events: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let events_list = py.import("builtins")?.call_method1("list", (events,))?;
        let mut into_sim: Vec<String> = Vec::new();
        for event in events_list.try_iter()? {
            let event = event?;
            let account = event.getattr("account")?;
            let contains = self.sim_accounts.bind(py).call_method1("__contains__", (&account,))?;
            if contains.is_truthy()? {
                let account: String = account.extract()?;
                if !into_sim.contains(&account) {
                    into_sim.push(account);
                }
            }
        }
        if !into_sim.is_empty() {
            into_sim.sort();
            return Err(split_ledger_error(
                py,
                &format!("the follower never writes the sim's accounts ({})", into_sim.join(", ")),
            ));
        }
        Ok(events_list.unbind())
    }

    fn extend(&self, py: Python<'_>, events: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let writes = self._writes(py, events)?;
        Ok(self.mirror.bind(py).call_method1("extend", (&writes,))?.unbind())
    }

    #[pyo3(signature = (event, **kwargs))]
    fn append(
        &self,
        py: Python<'_>,
        event: &Bound<'_, PyAny>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        let single = PyList::empty(py);
        single.append(event)?;
        let writes = self._writes(py, &single)?;
        let event_item = writes.bind(py).get_item(0)?;
        match kwargs {
            Some(kw) => Ok(self.mirror.bind(py).call_method("append", (event_item,), Some(kw))?.unbind()),
            None => Ok(self.mirror.bind(py).call_method1("append", (event_item,))?.unbind()),
        }
    }
}
