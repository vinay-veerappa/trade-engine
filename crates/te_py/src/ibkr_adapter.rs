//! W7-CORE-C20: `IbkrAccountsAdapter`'s method bodies, moved into Rust
//! (`IbkrAccountsAdapterBase`).
//!
//! The adapter is a read-only I/O shell stub: the caller hands one read as text through
//! `feed`, and `accounts` parses it with `te_core::ibkr::snapshot_from_text` (called
//! directly, never back through `trade_engine_rs`), then rebuilds the Python carriers
//! (`AccountRecord`, `AccountSnapshot`, `Route`) from the `Snapshot`. The rules (both
//! source shapes, the key, the mask, the paper prefix, the cap) stay in `te_core::ibkr`.
//!
//! The poll helper (`accounts.poll.PollAndDiff`) is a Python collaborator, created lazily
//! by the `poller` property through Python attribute lookup and called by
//! `account_events`; nothing here assumes a concrete Rust type behind it. The clock is a
//! Python collaborator reached through `now_utc()`. It holds no Rust state: the Python
//! subclass's `__init__` stores `_clock`, `_enabled`, `_ledger`, `_on_poll`, `_text`,
//! `_last`, `_poller` and the `stats` counters on the instance `__dict__`; every method
//! here reads and writes them back through Python attribute lookup, so a subclass or a
//! monkeypatched attribute keeps working. `capabilities`, `name`, `env` and `stale_after`
//! stay Python class attributes on the subclass.
//!
//! `AccountRuleError` and `UnsupportedCapability` stay Python, imported once and cached in
//! a `GILOnceCell`; the logging logger is `trade_engine.ibkr.adapter`, called with the same
//! name, level and message the Python method used. This adapter carries no `raise X from
//! err`, so no `set_cause` is needed here.

use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use te_core::accounts as ac;
use te_core::ibkr;

const _READ_ONLY: &str = "this adapter is read-only: it carries no orders";
const _DISABLED: &str = "the IBKR accounts adapter is off; its switch defaults to off";

/// The Python classes, helpers and the logger the base reaches, imported once.
struct Ctx {
    account_rule_error: Py<PyAny>,
    unsupported: Py<PyAny>,
    account_record: Py<PyAny>,
    account_snapshot: Py<PyAny>,
    route: Py<PyAny>,
    poll_and_diff: Py<PyAny>,
    datetime: Py<PyAny>,
    logger: Py<PyAny>,
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let broker = py.import("trade_engine.interfaces.broker")?;
        let errors = py.import("trade_engine.accounts.errors")?;
        let poll = py.import("trade_engine.accounts.poll")?;
        let datetime = py.import("datetime")?;
        let logger = py
            .import("logging")?
            .call_method1("getLogger", ("trade_engine.ibkr.adapter",))?;
        Ok(Ctx {
            account_rule_error: errors.getattr("AccountRuleError")?.unbind(),
            unsupported: broker.getattr("UnsupportedCapability")?.unbind(),
            account_record: broker.getattr("AccountRecord")?.unbind(),
            account_snapshot: broker.getattr("AccountSnapshot")?.unbind(),
            route: broker.getattr("Route")?.unbind(),
            poll_and_diff: poll.getattr("PollAndDiff")?.unbind(),
            datetime: datetime.getattr("datetime")?.unbind(),
            logger: logger.unbind(),
        })
    }
}

fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

/// `raise AccountRuleError(kind, message)`.
fn account_error(py: Python<'_>, kind: &str, msg: &str) -> PyErr {
    let cls = ctx(py).expect("the ibkr context is initialized at construction");
    PyErr::from_value(
        cls.account_rule_error
            .bind(py)
            .call1((kind, msg))
            .expect("AccountRuleError is callable with (kind, message)"),
    )
}

/// `raise UnsupportedCapability(message)`.
fn unsupported(py: Python<'_>, msg: &str) -> PyErr {
    let cls = ctx(py).expect("the ibkr context is initialized at construction");
    PyErr::from_value(
        cls.unsupported
            .bind(py)
            .call1((msg,))
            .expect("UnsupportedCapability is callable with a message"),
    )
}

/// `logger.warning(message)`, never allowed to fail the caller.
fn warn(py: Python<'_>, msg: &str) {
    if let Ok(c) = ctx(py) {
        let _ = c.logger.bind(py).call_method1("warning", (msg,));
    }
}

/// `stats.<field> += 1`.
fn bump(slf: &Bound<'_, IbkrAccountsAdapterBase>, field: &str) -> PyResult<()> {
    let stats = slf.getattr("stats")?;
    let current: i64 = stats.getattr(field)?.extract()?;
    stats.setattr(field, current + 1)?;
    Ok(())
}

/// `datetime.fromisoformat(iso.replace("Z", "+00:00"))` (the old `_snapshot`).
fn datetime_from_iso(py: Python<'_>, iso: &str) -> PyResult<Py<PyAny>> {
    let replaced = iso.replace("Z", "+00:00");
    Ok(ctx(py)?
        .datetime
        .bind(py)
        .call_method1("fromisoformat", (replaced,))?
        .unbind())
}

/// `AccountRecord(...)` from one `te_core` record; its `__post_init__` re-checks the record
/// through `accounts_snapshot_check`, whose refusal propagates as `AccountRuleError`.
fn build_record(py: Python<'_>, r: &ac::Record) -> PyResult<Py<PyAny>> {
    let c = ctx(py)?;
    let routes = PyTuple::new(
        py,
        r.routes
            .iter()
            .map(|rt| {
                c.route
                    .bind(py)
                    .call1((rt.platform.as_str(), rt.local_name.as_str()))
                    .map(Bound::unbind)
            })
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    let confirmed_at = datetime_from_iso(py, &ac::iso_z(&r.confirmed_at))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("key", r.key.as_str())?;
    kwargs.set_item("display", r.display.as_str())?;
    kwargs.set_item("env", r.env.as_str())?;
    kwargs.set_item("status", r.status.as_str())?;
    kwargs.set_item("class_", r.class_.as_deref())?;
    kwargs.set_item("firm", r.firm.as_deref())?;
    kwargs.set_item("account_type", r.account_type.as_deref())?;
    kwargs.set_item("api_ref", py.None())?;
    kwargs.set_item("routes", routes)?;
    kwargs.set_item("source", r.source.as_str())?;
    kwargs.set_item("confirmed_at", confirmed_at)?;
    Ok(c.account_record.bind(py).call((), Some(&kwargs))?.unbind())
}

/// `AccountSnapshot(records=..., confirmed_at=...)` from a `te_core` snapshot.
fn build_snapshot(py: Python<'_>, snap: &ac::Snapshot) -> PyResult<Py<PyAny>> {
    let c = ctx(py)?;
    let mut records = Vec::with_capacity(snap.records.len());
    for r in &snap.records {
        records.push(build_record(py, r)?);
    }
    let records = PyTuple::new(py, records)?;
    let confirmed_at = match &snap.confirmed_at {
        Some(dt) => datetime_from_iso(py, &ac::iso_z(dt))?,
        None => {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "an IBKR snapshot has no confirmed_at",
            ))
        }
    };
    let kwargs = PyDict::new(py);
    kwargs.set_item("records", records)?;
    kwargs.set_item("confirmed_at", confirmed_at)?;
    Ok(c.account_snapshot.bind(py).call((), Some(&kwargs))?.unbind())
}

/// The `poller` property's lazily-built collaborator, cached on `_poller`.
fn ensure_poller(slf: &Bound<'_, IbkrAccountsAdapterBase>) -> PyResult<Py<PyAny>> {
    let py = slf.py();
    let cached = slf.getattr("_poller")?;
    if !cached.is_none() {
        return Ok(cached.unbind());
    }
    let kwargs = PyDict::new(py);
    kwargs.set_item("clock", slf.getattr("_clock")?)?;
    kwargs.set_item("ledger", slf.getattr("_ledger")?)?;
    kwargs.set_item("on_poll", slf.getattr("_on_poll")?)?;
    kwargs.set_item("stale_after", slf.getattr("stale_after")?)?;
    kwargs.set_item("broker", "ibkr")?;
    let poller = ctx(py)?
        .poll_and_diff
        .bind(py)
        .call((slf.as_any(),), Some(&kwargs))?
        .unbind();
    slf.setattr("_poller", &poller)?;
    Ok(poller)
}

/// `IbkrAccountsAdapter`'s body, as a `#[pyclass(subclass)]` the Python module subclasses.
/// All state is Python instance state (`__dict__`), read and written through attribute
/// lookup so a test subclass or monkeypatched attribute keeps working.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct IbkrAccountsAdapterBase;

#[pymethods]
impl IbkrAccountsAdapterBase {
    #[new]
    #[pyo3(signature = (*args, **kwargs))]
    fn new(args: &Bound<'_, PyAny>, kwargs: Option<&Bound<'_, PyAny>>) -> Self {
        // The Python subclass's __init__ stores the state; these are accepted only so
        // __new__ agrees with __init__'s signature.
        let _ = (args, kwargs);
        IbkrAccountsAdapterBase
    }

    // -- the switch -------------------------------------------------------------

    #[getter]
    fn enabled(slf: &Bound<'_, Self>) -> PyResult<bool> {
        slf.getattr("_enabled")?.extract()
    }

    fn set_enabled(slf: &Bound<'_, Self>, on: &Bound<'_, PyAny>) -> PyResult<()> {
        slf.setattr("_enabled", on.is_truthy()?)?;
        Ok(())
    }

    // -- the I/O shell stub -----------------------------------------------------

    fn feed(slf: &Bound<'_, Self>, text: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        if !text.is_instance_of::<PyString>() {
            return Err(account_error(py, "bad_shape", "an IBKR read is text"));
        }
        let text_str: String = text.extract()?;
        let size = text_str.len();
        if size > ibkr::MAX_TEXT_BYTES {
            bump(slf, "over_cap_refused")?;
            return Err(account_error(
                py,
                "cap_exceeded",
                &format!(
                    "the IBKR read is {size} bytes, over the {}-byte cap (B7)",
                    ibkr::MAX_TEXT_BYTES
                ),
            ));
        }
        slf.setattr("_text", text)?;
        bump(slf, "fed")?;
        slf.getattr("stats")?.setattr("fed_bytes", size)?;
        Ok(())
    }

    // -- the account port -------------------------------------------------------

    fn accounts(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        bump(slf, "reads")?;
        let enabled: bool = slf.getattr("_enabled")?.extract()?;
        if !enabled {
            bump(slf, "disabled_reads")?;
            return Err(account_error(py, "ibkr_disabled", _DISABLED));
        }
        let text = slf.getattr("_text")?;
        if text.is_none() {
            bump(slf, "read_failures")?;
            return Err(account_error(
                py,
                "bad_shape",
                "no IBKR read has been fed; this slice connects to nothing",
            ));
        }
        let text_str: String = text.extract()?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        let now_iso: String = now.call_method0("isoformat")?.extract()?;
        let at = match ac::parse_at(&now_iso, "now") {
            Ok(at) => at,
            Err(e) => return refuse(slf, &e.kind, &e.msg),
        };
        let snap = match ibkr::snapshot_from_text(&text_str, &at) {
            Ok(s) => s,
            Err(e) => return refuse(slf, &e.kind, &e.msg),
        };
        let snapshot = match build_snapshot(py, &snap) {
            Ok(s) => s,
            Err(e) => return refuse_err(slf, e),
        };
        slf.setattr("_last", &snapshot)?;
        Ok(snapshot)
    }

    fn account_events(slf: &Bound<'_, Self>, since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let poller = ensure_poller(slf)?;
        poller
            .bind(slf.py())
            .call_method1("account_events", (since,))
            .map(Bound::unbind)
    }

    #[getter]
    fn poller(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        ensure_poller(slf)
    }

    fn is_stale(slf: &Bound<'_, Self>, now: &Bound<'_, PyAny>) -> PyResult<bool> {
        let last = slf.getattr("_last")?;
        if last.is_none() {
            return Ok(true);
        }
        let stale_after = slf.getattr("stale_after")?;
        last.call_method1("is_stale", (now, &stale_after))?.extract()
    }

    // -- the protocol's remaining reads and the refused writes ------------------

    fn connect(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        Err(unsupported(
            slf.py(),
            "the IBKR adapter is an account-only stub: it connects to nothing",
        ))
    }

    fn orders(slf: &Bound<'_, Self>, _since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Ok(PyList::empty(slf.py()).into_any().unbind())
    }

    fn fills(slf: &Bound<'_, Self>, _since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Ok(PyList::empty(slf.py()).into_any().unbind())
    }

    fn positions(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        Ok(PyList::empty(slf.py()).into_any().unbind())
    }

    fn cash_events(slf: &Bound<'_, Self>, _since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Ok(PyList::empty(slf.py()).into_any().unbind())
    }

    fn submit(slf: &Bound<'_, Self>, _order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Err(unsupported(slf.py(), _READ_ONLY))
    }

    fn cancel(slf: &Bound<'_, Self>, _venue_order_id: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Err(unsupported(slf.py(), _READ_ONLY))
    }

    fn replace(
        slf: &Bound<'_, Self>,
        _venue_order_id: &Bound<'_, PyAny>,
        _changes: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Err(unsupported(slf.py(), _READ_ONLY))
    }
}

/// The `except AccountRuleError` of `accounts`: count the refusal, log it, keep the last
/// good snapshot, and re-raise when there is none (B3).
fn refuse(slf: &Bound<'_, IbkrAccountsAdapterBase>, kind: &str, msg: &str) -> PyResult<Py<PyAny>> {
    let py = slf.py();
    bump(slf, "refused_snapshots")?;
    if kind == "cap_exceeded" {
        bump(slf, "over_cap_refused")?;
    }
    warn(py, &format!("IBKR read refused ({kind}); keeping the last good list"));
    let last = slf.getattr("_last")?;
    if !last.is_none() {
        return Ok(last.unbind());
    }
    Err(account_error(py, kind, msg))
}

/// The same refusal path, from a carrier-build `AccountRuleError` (its `kind` and
/// `message` read off the exception); any other error propagates unchanged.
fn refuse_err(slf: &Bound<'_, IbkrAccountsAdapterBase>, e: PyErr) -> PyResult<Py<PyAny>> {
    let py = slf.py();
    let cls = ctx(py)?.account_rule_error.bind(py).clone();
    if !e.is_instance(py, &cls) {
        return Err(e);
    }
    let kind: String = e.value(py).getattr("kind")?.extract()?;
    let msg: String = e.value(py).getattr("message")?.extract()?;
    refuse(slf, &kind, &msg)
}
