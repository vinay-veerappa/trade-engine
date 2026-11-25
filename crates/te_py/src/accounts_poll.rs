//! W7-CORE-C15: `PollAndDiff`'s and the NT8 route helpers' method bodies, in Rust.
//!
//! `PollAndDiff` sequences one broker account poll: it reads the adapter's snapshot, diffs it
//! against the last good one (or the ledger's replayed membership), journals the events, pushes
//! them into the bounded ring and hands the good read to the registry sink. The NT8
//! `map_account` / `map_accounts` helpers build the Python `Nt8Account` / `Route` carriers from
//! the one name map in `te_core::nt8_routes`.
//!
//! The heavy rules stay native (`te_core::accounts`), called here directly where Python reached
//! them through `_rs`; the collaborators (adapter, clock, ledger, sink, pnl source) and the
//! carriers stay Python objects reached through attribute lookup. The poller's state
//! (`_adapter`, `_clock`, `_ledger`, `_last_good`, `_seq`, `_scope`, `_ring`, `_on_poll`,
//! `_pnl_source`, `interval_seconds`, `stale_after`) stays Python instance attributes set by
//! `PollAndDiff.__init__`, so the tests keep monkeypatching them and the feed hosts keep reading
//! `poller.last_good`. `poll` is reached through `slf.call_method0("poll")` from `account_events`
//! because `test_bp3_tradovate_ntx.py` monkeypatches `poller.poll`; every other method is a
//! direct call (none is subclassed or monkeypatched).
//!
//! The module-level helpers `_internal_event`, `_membership_events` (and their `_record_dict`,
//! `_value`) stay Python and are called here by name, as do the `_iso_z` formatter and the
//! `logging` logger (`trade_engine.accounts.poll`), so the tests' `caplog` and the exact internal
//! event JSON are unchanged.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyTuple};
use te_core::accounts as ac;
use te_core::ledger::model::LErr;

/// The Python classes, functions and the logger `PollAndDiffBase` reaches, imported once.
struct Ctx {
    account_event: Py<PyAny>,
    account_rule_error: Py<PyAny>,
    nt8_account: Py<PyAny>,
    route: Py<PyAny>,
    iso_z: Py<PyAny>,
    internal_event: Py<PyAny>,
    membership_events: Py<PyAny>,
    accounts_account: Py<PyAny>,
    account_ledger_record: Py<PyAny>,
    account_membership: Py<PyAny>,
    event: Py<PyAny>,
    event_kind: Py<PyAny>,
    logger: Py<PyAny>,
    json: Py<PyAny>,
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let broker = py.import("trade_engine.interfaces.broker")?;
        let poll = py.import("trade_engine.accounts.poll")?;
        let events = py.import("trade_engine.ledger.events")?;
        Ok(Ctx {
            account_event: broker.getattr("AccountEvent")?.unbind(),
            account_rule_error: py
                .import("trade_engine.accounts.errors")?
                .getattr("AccountRuleError")?
                .unbind(),
            nt8_account: py.import("trade_engine.accounts.nt8")?.getattr("Nt8Account")?.unbind(),
            route: broker.getattr("Route")?.unbind(),
            iso_z: broker.getattr("_iso_z")?.unbind(),
            internal_event: poll.getattr("_internal_event")?.unbind(),
            membership_events: poll.getattr("_membership_events")?.unbind(),
            accounts_account: events.getattr("ACCOUNTS_ACCOUNT")?.unbind(),
            account_ledger_record: events.getattr("AccountLedgerRecord")?.unbind(),
            account_membership: events.getattr("AccountMembership")?.unbind(),
            event: events.getattr("Event")?.unbind(),
            event_kind: events.getattr("EventKind")?.unbind(),
            logger: py
                .import("logging")?
                .call_method1("getLogger", ("trade_engine.accounts.poll",))?
                .unbind(),
            json: py.import("json")?.into_any().unbind(),
        })
    }
}

fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

/// `raise AccountRuleError(kind, msg)` from a `te_core` refusal (the shim `_call` maps a
/// `ValueError((kind, msg))` the same way).
fn refuse_account(py: Python<'_>, e: LErr) -> PyErr {
    match ctx(py) {
        Ok(ctx) => match ctx.account_rule_error.bind(py).call1((e.kind, e.msg)) {
            Ok(value) => PyErr::from_value(value),
            Err(fallback) => fallback,
        },
        Err(fallback) => fallback,
    }
}

fn exc_name(py: Python<'_>, e: &PyErr) -> String {
    e.get_type(py)
        .name()
        .map_or_else(|_| "Exception".to_string(), |name| name.to_string())
}

/// `logger.warning(fmt, arg)`: the same lazy `%`-style formatting and logger the tests read.
fn warn(py: Python<'_>, ctx: &Ctx, fmt: &'static str, arg: &str) -> PyResult<()> {
    let tuple = PyTuple::new(py, [fmt, arg])?;
    ctx.logger.bind(py).call_method1("warning", tuple)?;
    Ok(())
}

/// The diff between `old` (a contract dict or None) and `new` (a contract dict), as the Python
/// `_rs.diff` produced it, rebuilt into `AccountEvent` carriers.
fn diff<'py>(
    py: Python<'py>,
    ctx: &Ctx,
    old: Option<&Bound<'py, PyAny>>,
    new: &Bound<'py, PyAny>,
    now: &Bound<'py, PyAny>,
    first_seq: u64,
) -> PyResult<Bound<'py, PyList>> {
    let old_snap = match old {
        Some(old) if !old.is_none() => {
            let text: String = ctx.json.bind(py).call_method1("dumps", (old,))?.extract()?;
            Some(ac::snapshot_from_json(&text).map_err(|e| refuse_account(py, e))?)
        }
        _ => None,
    };
    let new_text: String = ctx.json.bind(py).call_method1("dumps", (new,))?.extract()?;
    let new_snap = ac::snapshot_from_json(&new_text).map_err(|e| refuse_account(py, e))?;
    let now_iso: String = now.call_method0("isoformat")?.extract()?;
    let at = ac::parse_at(&now_iso, "now").map_err(|e| refuse_account(py, e))?;
    let events = ac::diff(old_snap.as_ref(), &new_snap, &at, first_seq);
    let events_json = ac::events_to_json(&events);
    let rows = ctx.json.bind(py).call_method1("loads", (events_json,))?;
    let list = PyList::empty(py);
    for row in rows.try_iter()? {
        let row = row?;
        let carrier = ctx.account_event.bind(py).call_method1("from_internal", (&row,))?;
        list.append(carrier)?;
    }
    Ok(list)
}

/// `[ _internal_event(e) for e in events ]`: the internal event JSON the ring and the sink read.
fn internal_events<'py>(
    py: Python<'py>,
    ctx: &Ctx,
    events: &Bound<'py, PyList>,
) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty(py);
    for e in events.iter() {
        list.append(ctx.internal_event.bind(py).call1((e,))?)?;
    }
    Ok(list)
}

/// `_replayed_membership`: the ledger's membership over `scope`, or None (no ledger / no row).
fn replayed_membership<'py>(
    slf: &Bound<'py, PollAndDiffBase>,
    scope: &[String],
) -> PyResult<Option<Py<PyAny>>> {
    let py = slf.py();
    let ctx = ctx(py)?;
    let ledger = slf.getattr("_ledger")?;
    if ledger.is_none() {
        return Ok(None);
    }
    let rows = ctx.membership_events.bind(py).call1((&ledger,))?;
    let rows_json: String = ctx.json.bind(py).call_method1("dumps", (rows,))?.extract()?;
    let found = ac::membership(&rows_json, scope).map_err(|e| refuse_account(py, e))?;
    match found {
        None => Ok(None),
        Some((snapshot_json, max_seq)) => {
            let seq = slf.getattr("_seq")?.extract::<u64>()?;
            if max_seq > seq {
                slf.setattr("_seq", max_seq)?;
            }
            Ok(Some(ctx.json.bind(py).call_method1("loads", (snapshot_json,))?.unbind()))
        }
    }
}

/// `_append`: journal the events under `ACCOUNTS_ACCOUNT` (I1, I2). No ledger: nothing.
fn append<'py>(
    slf: &Bound<'py, PollAndDiffBase>,
    events: &Bound<'py, PyList>,
    now: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = slf.py();
    let ctx = ctx(py)?;
    let ledger = slf.getattr("_ledger")?;
    if ledger.is_none() {
        return Ok(());
    }
    for event in events.iter() {
        let record_attr = event.getattr("record")?;
        let record: Py<PyAny> = if record_attr.is_none() {
            py.None()
        } else {
            let kwargs = PyDict::new(py);
            kwargs.set_item("key", record_attr.getattr("key")?)?;
            kwargs.set_item("display", record_attr.getattr("display")?)?;
            kwargs.set_item("env", record_attr.getattr("env")?)?;
            kwargs.set_item("status", record_attr.getattr("status")?)?;
            kwargs.set_item("class_", record_attr.getattr("class_")?)?;
            kwargs.set_item("firm", record_attr.getattr("firm")?)?;
            kwargs.set_item("account_type", record_attr.getattr("account_type")?)?;
            kwargs.set_item("routes", record_attr.getattr("routes")?)?;
            kwargs.set_item("source", record_attr.getattr("source")?)?;
            kwargs.set_item("confirmed_at", record_attr.getattr("confirmed_at")?)?;
            ctx.account_ledger_record.bind(py).call((), Some(&kwargs))?.unbind()
        };
        let kind_str: String = event.getattr("kind")?.extract()?;
        let kind_name = match kind_str.as_str() {
            "added" => "ACCOUNT_ADDED",
            "removed" => "ACCOUNT_REMOVED",
            "changed" => "ACCOUNT_CHANGED",
            other => return Err(PyValueError::new_err(format!("unknown account event kind {other:?}"))),
        };
        let kind = ctx.event_kind.bind(py).get_item(kind_name)?;
        let key: String = event.getattr("key")?.extract()?;
        let seq: u64 = event.getattr("seq")?.extract()?;
        let payload_kwargs = PyDict::new(py);
        payload_kwargs.set_item("key", event.getattr("key")?)?;
        payload_kwargs.set_item("display", event.getattr("display")?)?;
        payload_kwargs.set_item("seq", event.getattr("seq")?)?;
        payload_kwargs.set_item("at", event.getattr("at")?)?;
        payload_kwargs.set_item("record", record)?;
        payload_kwargs.set_item("changes", event.getattr("changes")?)?;
        let payload = ctx.account_membership.bind(py).call((), Some(&payload_kwargs))?.unbind();
        let command_id = format!("account:{kind_str}:{key}:{seq}");
        let event_kwargs = PyDict::new(py);
        event_kwargs.set_item("account", ctx.accounts_account.bind(py))?;
        event_kwargs.set_item("kind", kind)?;
        event_kwargs.set_item("payload", payload)?;
        event_kwargs.set_item("ts_utc", now)?;
        event_kwargs.set_item("command_id", command_id)?;
        let ev = ctx.event.bind(py).call((), Some(&event_kwargs))?.unbind();
        ledger.call_method1("append", (ev,))?;
    }
    Ok(())
}

/// `_notify`: hand the good read to the registry sink. A sink failure never fails the poll.
fn notify<'py>(
    slf: &Bound<'py, PollAndDiffBase>,
    snapshot: &Bound<'py, PyAny>,
    internal: &Bound<'py, PyList>,
) -> PyResult<()> {
    let py = slf.py();
    let ctx = ctx(py)?;
    let on_poll = slf.getattr("_on_poll")?;
    if on_poll.is_none() {
        return Ok(());
    }
    let seq = slf.getattr("_seq")?;
    let pnl_source = slf.getattr("_pnl_source")?;
    let result = if pnl_source.is_none() {
        on_poll.call1((snapshot, internal.as_any(), &seq))
    } else {
        let pnl = pnl_source.call1((snapshot,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("pnl", pnl)?;
        on_poll.call((snapshot, internal.as_any(), &seq), Some(&kwargs))
    };
    if let Err(e) = result {
        warn(py, ctx, "registry sink observe failed (%s)", &exc_name(py, &e))?;
    }
    Ok(())
}

/// `PollAndDiff`'s body, as a `#[pyclass(subclass)]` the Python module subclasses. All state is
/// Python instance attributes set by `PollAndDiff.__init__`; this type owns only the method
/// bodies (`poll`, `account_events`) and their private helpers.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct PollAndDiffBase;

#[pymethods]
impl PollAndDiffBase {
    #[new]
    #[pyo3(signature = (adapter, *, clock, ledger=None, interval_seconds=None, stale_after=None, on_poll=None, pnl_source=None, broker=None))]
    fn new(
        adapter: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        ledger: Option<&Bound<'_, PyAny>>,
        interval_seconds: Option<&Bound<'_, PyAny>>,
        stale_after: Option<&Bound<'_, PyAny>>,
        on_poll: Option<&Bound<'_, PyAny>>,
        pnl_source: Option<&Bound<'_, PyAny>>,
        broker: Option<&Bound<'_, PyAny>>,
    ) -> Self {
        // The Python subclass's __init__ sets the attributes; these are accepted only so
        // __new__ agrees with __init__'s signature.
        let _ = (adapter, clock, ledger, interval_seconds, stale_after, on_poll, pnl_source, broker);
        PollAndDiffBase
    }

    fn poll<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyList>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let adapter = slf.getattr("_adapter")?;
        let snapshot = match adapter.call_method0("accounts") {
            Ok(s) => s,
            Err(e) => {
                warn(py, ctx, "account poll failed (%s); keeping the last good list", &exc_name(py, &e))?;
                return Ok(PyList::empty(py));
            }
        };
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        let scope_attr = slf.getattr("_scope")?;
        let scope: Vec<String> = if scope_attr.is_none() {
            let records = snapshot.getattr("records")?;
            let keys = records
                .try_iter()?
                .map(|r| r?.getattr("key")?.extract::<String>())
                .collect::<PyResult<Vec<String>>>()?;
            ac::scope(None, &keys)
        } else {
            scope_attr
                .try_iter()?
                .map(|x| x?.extract::<String>())
                .collect::<PyResult<Vec<String>>>()?
        };
        let last_good = slf.getattr("_last_good")?;
        let old: Option<Bound<'py, PyAny>> = if !last_good.is_none() {
            Some(last_good.call_method0("as_contract")?)
        } else {
            match replayed_membership(slf, &scope)? {
                Some(s) => Some(s.bind(py).clone()),
                None => None,
            }
        };
        let seq = slf.getattr("_seq")?.extract::<u64>()?;
        let new_contract = snapshot.call_method0("as_contract")?;
        let carriers = match diff(py, ctx, old.as_ref(), &new_contract, &now, seq + 1) {
            Ok(c) => c,
            Err(e) => {
                warn(py, ctx, "account snapshot refused (%s); keeping the last good list", &exc_name(py, &e))?;
                return Ok(PyList::empty(py));
            }
        };
        // The first good read fixes the scope (F11, I5).
        if scope_attr.is_none() {
            let fs = py.import("builtins")?.call_method1("frozenset", (scope.clone(),))?;
            slf.setattr("_scope", fs)?;
        }
        if !carriers.is_empty() {
            append(slf, &carriers, &now)?;
            let mut max_seq = 0u64;
            for c in carriers.iter() {
                let s: u64 = c.getattr("seq")?.extract()?;
                if s > max_seq {
                    max_seq = s;
                }
            }
            slf.setattr("_seq", max_seq)?;
        }
        // Exactly one last-good snapshot, replaced (never appended).
        slf.setattr("_last_good", &snapshot)?;
        let internal = internal_events(py, ctx, &carriers)?;
        let internal_json: String = ctx
            .json
            .bind(py)
            .call_method1("dumps", (internal.as_any(),))?
            .extract()?;
        let iso_z_now: String = ctx.iso_z.bind(py).call1((&now,))?.extract()?;
        slf.getattr("_ring")?.call_method1("push", (internal_json, iso_z_now))?;
        notify(slf, &snapshot, &internal)?;
        Ok(carriers)
    }

    fn account_events<'py>(
        slf: &Bound<'py, Self>,
        since: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyList>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        // Dispatch rule: `poll` may be monkeypatched, so it is reached by name.
        slf.call_method0("poll")?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        let since_z: String = ctx.iso_z.bind(py).call1((since,))?.extract()?;
        let now_z: String = ctx.iso_z.bind(py).call1((&now,))?.extract()?;
        let rows_text: String = slf
            .getattr("_ring")?
            .call_method1("since", (since_z, now_z))?
            .extract()?;
        let rows = ctx.json.bind(py).call_method1("loads", (rows_text,))?;
        let list = PyList::empty(py);
        for row in rows.try_iter()? {
            let row = row?;
            let carrier = ctx.account_event.bind(py).call_method1("from_internal", (&row,))?;
            list.append(carrier)?;
        }
        Ok(list)
    }
}

/// `map_account`: one NT8 account name to its `Nt8Account` carrier, or `unknown_nt8_account`.
#[pyfunction]
pub(crate) fn nt8_map_account(py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
    let ctx = ctx(py)?;
    let mapping = te_core::nt8_routes::map_account(name).map_err(|e| refuse_account(py, e))?;
    let route = ctx.route.bind(py).call1((mapping.route.platform, mapping.route.local_name))?;
    Ok(ctx.nt8_account.bind(py).call1((mapping.key, mapping.local, route))?.unbind())
}

/// `map_accounts`: the bridge's account-list JSON text to a tuple of `Nt8Account` carriers.
#[pyfunction]
pub(crate) fn nt8_map_accounts(py: Python<'_>, text: &str) -> PyResult<Py<PyAny>> {
    let ctx = ctx(py)?;
    let mappings = te_core::nt8_routes::map_accounts(text).map_err(|e| refuse_account(py, e))?;
    let mut items = Vec::with_capacity(mappings.len());
    for m in mappings {
        let route = ctx.route.bind(py).call1((m.route.platform, m.route.local_name))?;
        items.push(ctx.nt8_account.bind(py).call1((m.key, m.local, route))?.unbind());
    }
    Ok(PyTuple::new(py, items)?.into_any().unbind())
}
