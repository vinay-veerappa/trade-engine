//! W7-CORE-C13: `tos_paper.session_native`'s orchestration, moved into Rust.
//!
//! The module has no classes, so this file adds four `#[pyfunction]`s — `run_mirror`,
//! `collect_only`, `cancel_ticket` and `clear_halt` — each with the Python wrapper's exact
//! name and signature. They sequence the mirror session (collect → reconcile → queue → drain)
//! step for step, calling `te_core::tos_paper::session` directly instead of through `_rs.py`.
//!
//! Every collaborator (the ledger, the broker, the clock, the settle helper) is a Python object
//! reached through attribute lookup, so a test that monkeypatches `TosPaperBroker.collect_fills`
//! or `ledger.extend` still reaches the override. The report, exception and carrier classes stay
//! Python; this file builds and reads them by importing them once, cached in a `GILOnceCell`.
//!
//! `raise X from err` does not appear in `session_native.py`: the module only re-raises (`raise`)
//! or builds reports, never chains a cause, so no `set_cause` is needed here.

use std::collections::{BTreeSet, HashMap};

use pyo3::exceptions::{PyKeyError, PyOverflowError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use te_core::ledger::model::{LErr, MirrorAck, Obj, Order};
use te_core::tos_paper::netting::Ticket;
use te_core::tos_paper::session::{self, WriteAheadItem};
use te_core::tos_paper::wire::state_of;

use crate::store_ledger as sl;

/// The Python classes and functions the host reaches, imported once.
pub(crate) struct Ctx {
    event: Py<PyAny>,
    mirror_account: Py<PyAny>,
    mirror_state: Py<PyAny>,
    halted_venues: Py<PyAny>,
    mirror_fill: Py<PyAny>,
    mirror_ack: Py<PyAny>,
    mirror_refused: Py<PyAny>,
    mirror_queued: Py<PyAny>,
    venue_reconcile: Py<PyAny>,
    kind_fill: Py<PyAny>,
    kind_ack: Py<PyAny>,
    kind_refused: Py<PyAny>,
    kind_queued: Py<PyAny>,
    kind_venue_reconcile: Py<PyAny>,
    kind_venue_halt_cleared: Py<PyAny>,
    ledger_fold_error: Py<PyAny>,
    venue_unreadable: Py<PyAny>,
    transport_unavailable: Py<PyAny>,
    mirror_run_report: Py<PyAny>,
    mirror_session_error: Py<PyAny>,
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let events = py.import("trade_engine.ledger.events")?;
        let state = py.import("trade_engine.ledger.state")?;
        let session = py.import("trade_engine.tos_paper.session_native")?;
        let broker = py.import("trade_engine.tos_paper.broker")?;
        let transport = py.import("trade_engine.tos_paper.transport")?;
        let event_kind = events.getattr("EventKind")?;
        Ok(Ctx {
            event: events.getattr("Event")?.unbind(),
            mirror_account: events.getattr("mirror_account")?.unbind(),
            mirror_fill: events.getattr("MirrorFill")?.unbind(),
            mirror_ack: events.getattr("MirrorAck")?.unbind(),
            mirror_refused: events.getattr("MirrorRefused")?.unbind(),
            mirror_queued: events.getattr("MirrorQueued")?.unbind(),
            venue_reconcile: events.getattr("VenueReconcile")?.unbind(),
            kind_fill: event_kind.getattr("MIRROR_FILL")?.unbind(),
            kind_ack: event_kind.getattr("MIRROR_ACK")?.unbind(),
            kind_refused: event_kind.getattr("MIRROR_REFUSED")?.unbind(),
            kind_queued: event_kind.getattr("MIRROR_QUEUED")?.unbind(),
            kind_venue_reconcile: event_kind.getattr("VENUE_RECONCILE")?.unbind(),
            kind_venue_halt_cleared: event_kind.getattr("VENUE_HALT_CLEARED")?.unbind(),
            mirror_state: state.getattr("mirror_state")?.unbind(),
            halted_venues: state.getattr("halted_venues")?.unbind(),
            ledger_fold_error: state.getattr("LedgerFoldError")?.unbind(),
            venue_unreadable: broker.getattr("VenueUnreadable")?.unbind(),
            transport_unavailable: transport.getattr("TransportUnavailable")?.unbind(),
            mirror_run_report: session.getattr("MirrorRunReport")?.unbind(),
            mirror_session_error: session.getattr("MirrorSessionError")?.unbind(),
        })
    }
}

pub(crate) fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

/// `MirrorSessionError(msg)`.
fn mirror_session_error(py: Python<'_>, ctx: &Ctx, msg: &str) -> PyErr {
    match ctx.mirror_session_error.bind(py).call1((msg,)) {
        Ok(value) => PyErr::from_value(value),
        Err(err) => err,
    }
}

/// A te_core refusal, as the Python exception `_rs.call` would have raised (`refusal`).
fn refuse(py: Python<'_>, ctx: &Ctx, e: LErr) -> PyErr {
    match e.kind {
        "tos_session_error" => mirror_session_error(py, ctx, &e.msg),
        "value" => PyValueError::new_err(e.msg),
        "key" => {
            let bytes = e.msg.as_bytes();
            let quoted = e.msg.len() >= 2
                && bytes[0] == bytes[e.msg.len() - 1]
                && (bytes[0] == b'\'' || bytes[0] == b'"');
            let inner = if quoted { e.msg[1..e.msg.len() - 1].to_string() } else { e.msg };
            PyKeyError::new_err(inner)
        }
        "tos_overflow_error" => PyOverflowError::new_err(e.msg),
        other => match ctx.ledger_fold_error.bind(py).call1((format!("{other}: {}", e.msg),)) {
            Ok(value) => PyErr::from_value(value),
            Err(err) => err,
        },
    }
}

/// The venue's folded mirror carrier (`mirror_of`): `mirror_state({mirror_account(venue): state}, venue)`.
fn mirror_carrier<'py>(
    py: Python<'py>,
    ctx: &Ctx,
    ledger: &Bound<'py, PyAny>,
    venue: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let account = ctx.mirror_account.bind(py).call1((venue,))?;
    let state = ledger.call_method1("state", (&account,))?;
    let dict = PyDict::new(py);
    dict.set_item(&account, &state)?;
    ctx.mirror_state.bind(py).call1((dict, venue))
}

/// Every venue any account has halted (`halted_venues({account: ledger.state(account)})`).
fn halted<'py>(py: Python<'py>, ctx: &Ctx, ledger: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let dict = PyDict::new(py);
    for account in ledger.call_method0("accounts")?.try_iter()? {
        let account = account?;
        let state = ledger.call_method1("state", (&account,))?;
        dict.set_item(&account, &state)?;
    }
    ctx.halted_venues.bind(py).call1((dict,))
}

/// `broker.restore(mirror, halted_venues=halted)`.
fn restore<'py>(
    py: Python<'py>,
    broker: &Bound<'py, PyAny>,
    mirror: &Bound<'py, PyAny>,
    halted: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("halted_venues", halted)?;
    broker.call_method("restore", (mirror,), Some(&kwargs))?;
    Ok(())
}

/// The `EventKind` for a mirror payload, keyed by its exact type (`_KIND[type(payload)]`):
/// an unknown payload type is a `KeyError`, never a silent `VENUE_RECONCILE`.
fn kind_of<'py>(py: Python<'py>, ctx: &Ctx, payload: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let ty = payload.get_type();
    let table = [
        (&ctx.mirror_queued, &ctx.kind_queued),
        (&ctx.mirror_refused, &ctx.kind_refused),
        (&ctx.mirror_ack, &ctx.kind_ack),
        (&ctx.mirror_fill, &ctx.kind_fill),
        (&ctx.venue_reconcile, &ctx.kind_venue_reconcile),
    ];
    for (cls, kind) in table {
        if ty.is(cls.bind(py)) {
            return Ok(kind.bind(py).clone());
        }
    }
    Err(PyKeyError::new_err(ty.unbind()))
}

/// One Python `Event`, built like the module's `Event(account, kind, payload, ts_utc, command_id)`.
#[allow(clippy::too_many_arguments)]
fn make_event<'py>(
    py: Python<'py>,
    ctx: &Ctx,
    account: &Bound<'py, PyAny>,
    kind: &Bound<'py, PyAny>,
    payload: &Bound<'py, PyAny>,
    ts_utc: &Bound<'py, PyAny>,
    command_id: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("account", account)?;
    kwargs.set_item("kind", kind)?;
    kwargs.set_item("payload", payload)?;
    kwargs.set_item("ts_utc", ts_utc)?;
    kwargs.set_item("command_id", command_id)?;
    ctx.event.bind(py).call((), Some(&kwargs))
}

/// Append `(payload, suffix)` pairs in one transaction (I2: all or nothing), return the written
/// payloads. The one mirror-append path: the session host and `tos_paper_follow` both call it.
pub(crate) fn append<'py>(
    py: Python<'py>,
    ctx: &Ctx,
    ledger: &Bound<'py, PyAny>,
    venue: &str,
    clock: &Bound<'py, PyAny>,
    items: Vec<(Bound<'py, PyAny>, String)>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    if items.is_empty() {
        return Ok(Vec::new());
    }
    let now = clock.call_method0("now_utc")?;
    let account = ctx.mirror_account.bind(py).call1((venue,))?;
    let mut events = Vec::new();
    for (payload, suffix) in items {
        let kind = kind_of(py, ctx, &payload)?;
        let command_id = format!("mirror:{venue}:{suffix}");
        events.push(make_event(py, ctx, &account, &kind, &payload, &now, &command_id)?);
    }
    let events_list = PyList::new(py, events)?;
    let written = ledger.call_method1("extend", (events_list,))?;
    let mut payloads = Vec::new();
    for event in written.try_iter()? {
        payloads.push(event?.getattr("payload")?);
    }
    Ok(payloads)
}

/// `_items`: strip the `mirror:{venue}:` prefix from Rust's command ids.
fn strip_items<'py>(venue: &str, rows: Vec<(Bound<'py, PyAny>, String)>) -> Vec<(Bound<'py, PyAny>, String)> {
    let prefix = format!("mirror:{venue}:");
    rows.into_iter()
        .map(|(payload, command)| (payload, command.strip_prefix(&prefix).unwrap_or(&command).to_string()))
        .collect()
}

/// `command.removeprefix(f"mirror:{venue}:")`.
fn suffix_of(venue: &str, command: &str) -> String {
    command.strip_prefix(&format!("mirror:{venue}:")).unwrap_or(command).to_string()
}

/// `_reconcile`: append one `VenueReconcile` and return the written event.
fn reconcile_append<'py>(
    py: Python<'py>,
    ctx: &Ctx,
    ledger: &Bound<'py, PyAny>,
    broker: &Bound<'py, PyAny>,
    clock: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    phase: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let venue: String = broker.getattr("venue")?.extract()?;
    let as_of = event.getattr("as_of")?;
    let reconcile = sl::reconcile_of(event)?;
    let command = session::reconcile_command_id(&venue, phase, &sl::datetime_of(&as_of)?, &reconcile)
        .map_err(|e| refuse(py, ctx, e))?;
    let suffix = suffix_of(&venue, &command);
    let written = append(py, ctx, ledger, &venue, clock, vec![(event.clone(), suffix)])?;
    Ok(written.into_iter().next().unwrap())
}

/// `_halt_unproven`: halt the venue when an open ticket has no proven Order ID, append it.
fn halt_unproven<'py>(
    py: Python<'py>,
    ctx: &Ctx,
    ledger: &Bound<'py, PyAny>,
    broker: &Bound<'py, PyAny>,
    clock: &Bound<'py, PyAny>,
    phase: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let venue: String = broker.getattr("venue")?.extract()?;
    let mirror = mirror_carrier(py, ctx, ledger, &venue)?;
    let mirror_state = sl::mirror_state_of(&mirror)?;
    let now = clock.call_method0("now_utc")?;
    let as_of = sl::datetime_of(&now)?;
    let halt = session::halt_unproven(&venue, &mirror_state, &as_of, phase).map_err(|e| refuse(py, ctx, e))?;
    match halt {
        None => Ok(None),
        Some((event, command)) => {
            let event_py = sl::obj_py(py, &Obj::Reconcile(event))?;
            let suffix = suffix_of(&venue, &command);
            let written = append(py, ctx, ledger, &venue, clock, vec![(event_py, suffix)])?;
            Ok(Some(written.into_iter().next().unwrap()))
        }
    }
}

/// `_settle_pass`: this pass's expiry settle, `(events, unsettled)`, or `((), ())` without `settle`.
fn settle_pass<'py>(
    py: Python<'py>,
    ledger: &Bound<'py, PyAny>,
    venue: &str,
    clock: &Bound<'py, PyAny>,
    settle: Option<&Bound<'py, PyAny>>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let Some(settle) = settle else {
        let empty = PyTuple::empty(py).into_any();
        return Ok((empty.clone(), empty));
    };
    static SETTLE: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    let settle_expired = SETTLE.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        Ok(py.import("trade_engine.tos_paper.expiry")?.getattr("settle_expired")?.unbind())
    })?;
    let calendar = settle.get_item(0)?;
    let settlements = settle.get_item(1)?;
    let outcome = settle_expired.bind(py).call1((ledger, venue, clock, calendar, settlements))?;
    Ok((outcome.getattr("events")?, outcome.getattr("unsettled")?))
}

/// The reason a run was deferred, in the operator's words (`session::deferred_why`).
fn deferred_why(py: Python<'_>, doing: &str, err: &PyErr, recorded: Option<&str>) -> PyResult<String> {
    let exc_text: String = err.value(py).str().and_then(|s| s.extract()).unwrap_or_default();
    Ok(session::deferred_why(doing, &exc_text, recorded))
}

/// `_with`: `dataclasses.replace(report, **changes)`.
fn with_report<'py>(
    py: Python<'py>,
    report: &Bound<'py, PyAny>,
    changes: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyAny>> {
    py.import("dataclasses")?.getattr("replace")?.call((report,), Some(changes))
}

/// `write-ahead` items as `(payload, command id)` pairs.
fn write_ahead_py<'py>(py: Python<'py>, items: &[WriteAheadItem]) -> PyResult<Vec<(Bound<'py, PyAny>, String)>> {
    items
        .iter()
        .map(|item| match item {
            WriteAheadItem::Refused(r, cmd) => Ok((sl::obj_py(py, &Obj::MRefused(r.clone()))?, cmd.clone())),
            WriteAheadItem::Queued(q, cmd) => Ok((sl::obj_py(py, &Obj::MQueued(q.clone()))?, cmd.clone())),
        })
        .collect()
}

/// Drain acks as `(payload, command id)` pairs.
fn drain_acks_py<'py>(py: Python<'py>, items: &[(MirrorAck, String)]) -> PyResult<Vec<(Bound<'py, PyAny>, String)>> {
    items
        .iter()
        .map(|(ack, cmd)| Ok((sl::obj_py(py, &Obj::MAck(ack.clone()))?, cmd.clone())))
        .collect()
}

/// A Python tuple of `(str, str)` pairs.
fn pairs_tuple<'py>(py: Python<'py>, pairs: &[(String, String)]) -> PyResult<Bound<'py, PyAny>> {
    let items = pairs.iter().map(|(a, b)| PyTuple::new(py, [a.as_str(), b.as_str()])).collect::<PyResult<Vec<_>>>()?;
    Ok(PyTuple::new(py, items)?.into_any())
}

/// A Python tuple of arbitrary values.
fn to_tuple<'py>(py: Python<'py>, items: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyTuple::new(py, items.iter())?.into_any())
}

/// The `reason` `clear_halt` accepts, or `MirrorSessionError` for a non-str or blank one.
fn reason_text(py: Python<'_>, ctx: &Ctx, reason: &Bound<'_, PyAny>) -> PyResult<String> {
    if !reason.is_instance_of::<PyString>() {
        return Err(mirror_session_error(py, ctx, "a halt is lifted only with the operator's reason (I11)"));
    }
    let s: String = reason.extract()?;
    if s.trim().is_empty() {
        return Err(mirror_session_error(py, ctx, "a halt is lifted only with the operator's reason (I11)"));
    }
    Ok(s)
}

/// `_collect`: fills and closes read back and appended, then the closing reconcile.
fn collect<'py>(
    py: Python<'py>,
    ctx: &Ctx,
    ledger: &Bound<'py, PyAny>,
    broker: &Bound<'py, PyAny>,
    clock: &Bound<'py, PyAny>,
    settle: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let venue: String = broker.getattr("venue")?.extract()?;
    let mirror = mirror_carrier(py, ctx, ledger, &venue)?;
    let collection = match broker.call_method1("collect_fills", (&mirror,)) {
        Ok(collection) => collection,
        Err(e) if e.is_instance(py, ctx.transport_unavailable.bind(py)) => {
            let (expired, unsettled) = settle_pass(py, ledger, &venue, clock, settle)?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("venue", &venue)?;
            kwargs.set_item("halted", broker.getattr("halted")?)?;
            kwargs.set_item("deferred", deferred_why(py, "collecting fills", &e, None)?)?;
            kwargs.set_item("expired", &expired)?;
            kwargs.set_item("unsettled", &unsettled)?;
            return ctx.mirror_run_report.bind(py).call((), Some(&kwargs));
        }
        Err(e) if e.is_instance(py, ctx.venue_unreadable.bind(py)) => {
            let (expired, unsettled) = settle_pass(py, ledger, &venue, clock, settle)?;
            let reconcile = e.value(py).getattr("reconcile")?;
            let written = reconcile_append(py, ctx, ledger, broker, clock, &reconcile, "collect")?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("venue", &venue)?;
            kwargs.set_item("reconcile", written)?;
            kwargs.set_item("halted", true)?;
            kwargs.set_item("expired", &expired)?;
            kwargs.set_item("unsettled", &unsettled)?;
            return ctx.mirror_run_report.bind(py).call((), Some(&kwargs));
        }
        Err(e) => return Err(e),
    };

    let fills = collection.getattr("fills")?;
    let closes = collection.getattr("closes")?;
    let mut collected: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut fills_input: Vec<(String, String)> = Vec::new();
    for fill in fills.try_iter()? {
        let fill = fill?;
        let key: String = fill.getattr("ticket_key")?.extract()?;
        let filled: String = fill.getattr("filled")?.str()?.extract()?;
        fills_input.push((key, filled));
        collected.push(fill);
    }
    let mut closes_input: Vec<(String, Option<String>)> = Vec::new();
    for close in closes.try_iter()? {
        let close = close?;
        let key: String = close.getattr("ticket_key")?.extract()?;
        let book_status = close.getattr("book_status")?;
        let status: Option<String> = if book_status.is_none() {
            None
        } else {
            Some(book_status.getattr("value")?.extract()?)
        };
        closes_input.push((key, status));
        collected.push(close);
    }
    let commands = session::collect_items(&venue, &fills_input, &closes_input).map_err(|e| refuse(py, ctx, e))?;
    let items: Vec<(Bound<'py, PyAny>, String)> = collected
        .into_iter()
        .zip(commands.into_iter())
        .map(|(obj, command)| (obj, suffix_of(&venue, &command)))
        .collect();

    let written = match append(py, ctx, ledger, &venue, clock, items) {
        Ok(written) => written,
        Err(e) if e.is_instance(py, ctx.ledger_fold_error.bind(py)) => {
            let (expired, unsettled) = settle_pass(py, ledger, &venue, clock, settle)?;
            let now = clock.call_method0("now_utc")?;
            let mirror_state = sl::mirror_state_of(&mirror)?;
            let err_msg: String = e.value(py).str()?.extract()?;
            let halt_event = session::fold_halt(&venue, &sl::datetime_of(&now)?, &err_msg, &mirror_state, &fills_input)
                .map_err(|e2| refuse(py, ctx, e2))?;
            let halt_py = sl::obj_py(py, &Obj::Reconcile(halt_event))?;
            let written_halt = reconcile_append(py, ctx, ledger, broker, clock, &halt_py, "collect")?;
            let halted = halted(py, ctx, ledger)?;
            restore(py, broker, &mirror, &halted)?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("venue", &venue)?;
            kwargs.set_item("reconcile", written_halt)?;
            kwargs.set_item("halted", true)?;
            kwargs.set_item("expired", &expired)?;
            kwargs.set_item("unsettled", &unsettled)?;
            return ctx.mirror_run_report.bind(py).call((), Some(&kwargs));
        }
        Err(e) => return Err(e),
    };

    let mut fills_out: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut closes_out: Vec<Bound<'py, PyAny>> = Vec::new();
    for payload in &written {
        if payload.is_instance(ctx.mirror_fill.bind(py))? {
            fills_out.push(payload.clone());
        } else if payload.is_instance(ctx.mirror_ack.bind(py))? {
            closes_out.push(payload.clone());
        }
    }
    halt_unproven(py, ctx, ledger, broker, clock, "collect")?;
    let (expired, unsettled) = settle_pass(py, ledger, &venue, clock, settle)?;
    let mirror = mirror_carrier(py, ctx, ledger, &venue)?;
    let halted = halted(py, ctx, ledger)?;
    restore(py, broker, &mirror, &halted)?;

    let kwargs = PyDict::new(py);
    kwargs.set_item("defer_unavailable", true)?;
    let now_state = match broker.call_method("reconcile_now", (), Some(&kwargs)) {
        Ok(state) => state,
        Err(e) if e.is_instance(py, ctx.transport_unavailable.bind(py)) => {
            let kwargs = PyDict::new(py);
            kwargs.set_item("venue", &venue)?;
            kwargs.set_item("fills", to_tuple(py, &fills_out)?)?;
            kwargs.set_item("closes", to_tuple(py, &closes_out)?)?;
            kwargs.set_item("halted", broker.getattr("halted")?)?;
            kwargs.set_item(
                "deferred",
                deferred_why(
                    py,
                    "reconciling",
                    &e,
                    Some("the fills and closes listed are recorded, only the closing reconcile was not made"),
                )?,
            )?;
            kwargs.set_item("expired", &expired)?;
            kwargs.set_item("unsettled", &unsettled)?;
            return ctx.mirror_run_report.bind(py).call((), Some(&kwargs));
        }
        Err(e) => return Err(e),
    };
    let check = reconcile_append(py, ctx, ledger, broker, clock, &now_state, "collect")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("venue", &venue)?;
    kwargs.set_item("fills", to_tuple(py, &fills_out)?)?;
    kwargs.set_item("closes", to_tuple(py, &closes_out)?)?;
    kwargs.set_item("reconcile", check)?;
    kwargs.set_item("halted", broker.getattr("halted")?)?;
    kwargs.set_item("expired", &expired)?;
    kwargs.set_item("unsettled", &unsettled)?;
    ctx.mirror_run_report.bind(py).call((), Some(&kwargs))
}

/// `collect_only(ledger, broker, *, clock, settle=None)`.
#[pyfunction]
#[pyo3(signature = (ledger, broker, *, clock, settle=None))]
pub(crate) fn collect_only<'py>(
    py: Python<'py>,
    ledger: &Bound<'py, PyAny>,
    broker: &Bound<'py, PyAny>,
    clock: &Bound<'py, PyAny>,
    settle: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let ctx = ctx(py)?;
    collect(py, ctx, ledger, broker, clock, settle.as_ref())
}

/// `run_mirror(ledger, broker, orders, *, clock, may_wait=False, settle=None)`.
#[pyfunction]
#[pyo3(signature = (ledger, broker, orders, *, clock, may_wait=false, settle=None))]
pub(crate) fn run_mirror<'py>(
    py: Python<'py>,
    ledger: &Bound<'py, PyAny>,
    broker: &Bound<'py, PyAny>,
    orders: &Bound<'py, PyAny>,
    clock: &Bound<'py, PyAny>,
    may_wait: bool,
    settle: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let ctx = ctx(py)?;
    let venue: String = broker.getattr("venue")?.extract()?;
    let collected = collect(py, ctx, ledger, broker, clock, settle.as_ref())?;
    if !collected.getattr("deferred")?.is_none() {
        return Ok(collected);
    }

    let mirror = mirror_carrier(py, ctx, ledger, &venue)?;
    let mirror_state = sl::mirror_state_of(&mirror)?;

    let mut orders_by_id: HashMap<String, Bound<'py, PyAny>> = HashMap::new();
    let mut orders_vec: Vec<Order> = Vec::new();
    for order in orders.try_iter()? {
        let order = order?;
        let oid: String = order.getattr("order_id")?.extract()?;
        orders_by_id.insert(oid, order.clone());
        orders_vec.push(sl::order_of(&order)?);
    }
    let candidates = session::filter_candidates(&orders_vec, &mirror_state);
    let (todo, waiting, cover_refused) =
        session::classify_cover(&mirror_state, &candidates, may_wait).map_err(|e| refuse(py, ctx, e))?;

    let mut todo_py: Vec<Bound<'py, PyAny>> = Vec::new();
    for order in &todo {
        match orders_by_id.get(&order.order_id) {
            Some(original) => todo_py.push(original.clone()),
            None => todo_py.push(sl::obj_py(py, &Obj::Order(order.clone()))?),
        }
    }
    let accounts: HashMap<String, String> =
        orders_vec.iter().map(|o| (o.order_id.clone(), o.account_id.clone())).collect();

    if todo.is_empty() {
        if cover_refused.is_empty() {
            let kwargs = PyDict::new(py);
            kwargs.set_item("waiting", pairs_tuple(py, &waiting)?)?;
            return Ok(with_report(py, &collected, &kwargs)?);
        }
        let now = clock.call_method0("now_utc")?;
        let items = session::prepare_write_ahead(&venue, &accounts, &cover_refused, &[], &[], &BTreeSet::new(), &sl::datetime_of(&now)?);
        let rows = write_ahead_py(py, &items)?;
        let written = append(py, ctx, ledger, &venue, clock, strip_items(&venue, rows))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("refused", to_tuple(py, &written)?)?;
        kwargs.set_item("waiting", pairs_tuple(py, &waiting)?)?;
        return Ok(with_report(py, &collected, &kwargs)?);
    }

    if !broker.getattr("halted")?.is_truthy()? {
        match broker.call_method0("preflight") {
            Ok(_) => {}
            Err(e) if e.is_instance(py, ctx.transport_unavailable.bind(py)) => {
                let deferred = deferred_why(py, &format!("reading before sending {} order(s)", todo.len()), &e, None)?;
                let kwargs = PyDict::new(py);
                kwargs.set_item("deferred", deferred)?;
                kwargs.set_item("waiting", pairs_tuple(py, &waiting)?)?;
                return Ok(with_report(py, &collected, &kwargs)?);
            }
            Err(e) => return Err(e),
        }
    }

    let exposure = mirror.call_method0("exposure")?;
    let todo_list = PyList::new(py, todo_py)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("holdings", exposure)?;
    let batch = broker.call_method("mirror_batch", (todo_list,), Some(&kwargs))?;
    let queued = broker.getattr("queued")?;
    let mut queued_keys: BTreeSet<String> = BTreeSet::new();
    for ticket in queued.try_iter()? {
        let ticket = ticket?;
        queued_keys.insert(ticket.getattr("venue_order_id")?.extract::<String>()?);
    }
    let now = clock.call_method0("now_utc")?;
    let mut batch_refused: Vec<(String, String)> = Vec::new();
    for refusal in batch.getattr("refused")?.try_iter()? {
        let refusal = refusal?;
        let oid: String = refusal.get_item(0)?.extract()?;
        let reason: String = refusal.get_item(1)?.extract()?;
        batch_refused.push((oid, reason));
    }
    let mut batch_tickets: Vec<Ticket> = Vec::new();
    for ticket in batch.getattr("venue_orders")?.try_iter()? {
        batch_tickets.push(sl::venue_order_ticket_of(&ticket?)?);
    }
    let items = session::prepare_write_ahead(
        &venue,
        &accounts,
        &cover_refused,
        &batch_refused,
        &batch_tickets,
        &queued_keys,
        &sl::datetime_of(&now)?,
    );
    let rows = write_ahead_py(py, &items)?;
    let written = match append(py, ctx, ledger, &venue, clock, strip_items(&venue, rows)) {
        Ok(written) => written,
        Err(e) => {
            for ticket in queued.try_iter()? {
                let ticket = ticket?;
                let id = ticket.getattr("venue_order_id")?;
                broker.call_method1("cancel", (&id,))?;
            }
            return Err(e);
        }
    };

    let report = broker.call_method0("drain")?;
    let mut rows: Vec<(String, String, Option<String>, te_core::ledger::model::DateTime)> = Vec::new();
    for ack in report.getattr("acks")?.try_iter()? {
        let ack = ack?;
        let key: String = ack.getattr("venue_order_id")?.extract()?;
        let status: String = ack.getattr("status")?.extract()?;
        let msg_val = ack.getattr("message")?;
        let msg: String = if msg_val.is_none() { String::new() } else { msg_val.extract()? };
        let ts = sl::datetime_of(&ack.getattr("timestamp")?)?;
        rows.push((key, status, (!msg.is_empty()).then_some(msg), ts));
    }
    let mut proven_states: HashMap<String, (String, te_core::ledger::model::OrderState)> = HashMap::new();
    for item in report.getattr("proven")?.call_method0("items")?.try_iter()? {
        let (key, prov): (Bound<'py, PyAny>, Bound<'py, PyAny>) = item?.extract()?;
        let key: String = key.extract()?;
        let order_id: String = prov.get_item(0)?.extract()?;
        let book_state: String = prov.get_item(1)?.getattr("value")?.extract()?;
        proven_states.insert(key, (order_id, state_of(&book_state).map_err(sl::door_err)?));
    }
    let ack_items = session::build_drain_acks(&venue, &rows, &proven_states);
    let ack_rows = drain_acks_py(py, &ack_items)?;
    let acked = append(py, ctx, ledger, &venue, clock, strip_items(&venue, ack_rows))?;

    if halt_unproven(py, ctx, ledger, broker, clock, "drain")?.is_some() {
        let mirror = mirror_carrier(py, ctx, ledger, &venue)?;
        let halted = halted(py, ctx, ledger)?;
        restore(py, broker, &mirror, &halted)?;
    }

    let mut drained: Option<Bound<'py, PyAny>> = None;
    let reconcile = report.getattr("reconcile")?;
    if !reconcile.is_none() {
        drained = Some(reconcile_append(py, ctx, ledger, broker, clock, &reconcile, "drain")?);
    }

    let mut refused_out: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut queued_out: Vec<Bound<'py, PyAny>> = Vec::new();
    for payload in &written {
        if payload.is_instance(ctx.mirror_refused.bind(py))? {
            refused_out.push(payload.clone());
        } else if payload.is_instance(ctx.mirror_queued.bind(py))? {
            queued_out.push(payload.clone());
        }
    }
    let kwargs = PyDict::new(py);
    kwargs.set_item("refused", to_tuple(py, &refused_out)?)?;
    kwargs.set_item("queued", to_tuple(py, &queued_out)?)?;
    kwargs.set_item("acks", to_tuple(py, &acked)?)?;
    kwargs.set_item("drain_reconcile", drained)?;
    kwargs.set_item("halted", broker.getattr("halted")?)?;
    kwargs.set_item("waiting", pairs_tuple(py, &waiting)?)?;
    Ok(with_report(py, &collected, &kwargs)?)
}

/// `cancel_ticket(ledger, broker, ticket_key, *, clock)`.
#[pyfunction]
#[pyo3(signature = (ledger, broker, ticket_key, *, clock))]
pub(crate) fn cancel_ticket<'py>(
    py: Python<'py>,
    ledger: &Bound<'py, PyAny>,
    broker: &Bound<'py, PyAny>,
    ticket_key: String,
    clock: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let ctx = ctx(py)?;
    let venue: String = broker.getattr("venue")?.extract()?;
    let mirror = mirror_carrier(py, ctx, ledger, &venue)?;
    let halted = halted(py, ctx, ledger)?;
    restore(py, broker, &mirror, &halted)?;
    let order_id_val = broker.call_method1("proven_order_id", (&ticket_key,))?;
    let order_id: Option<String> = if order_id_val.is_none() { None } else { Some(order_id_val.extract()?) };
    let ack = broker.call_method1("cancel", (&ticket_key,))?;
    let ack_status: String = ack.getattr("status")?.extract()?;
    let msg_val = ack.getattr("message")?;
    let msg: String = if msg_val.is_none() { String::new() } else { msg_val.extract()? };
    let ts = ack.getattr("timestamp")?;
    let res = session::build_cancel_ack(&venue, &ticket_key, order_id.as_deref(), &ack_status, &msg, &sl::datetime_of(&ts)?);
    if let Some((ack_ev, command)) = res {
        let ack_py = sl::obj_py(py, &Obj::MAck(ack_ev))?;
        let suffix = suffix_of(&venue, &command);
        append(py, ctx, ledger, &venue, clock, vec![(ack_py, suffix)])?;
    }
    Ok(ack)
}

/// `clear_halt(ledger, venue, *, reason, clock)`.
#[pyfunction]
#[pyo3(signature = (ledger, venue, *, reason, clock))]
pub(crate) fn clear_halt<'py>(
    py: Python<'py>,
    ledger: &Bound<'py, PyAny>,
    venue: String,
    reason: &Bound<'py, PyAny>,
    clock: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let ctx = ctx(py)?;
    let reason = reason_text(py, ctx, reason)?;

    let mut holders: Vec<String> = Vec::new();
    for account in ledger.call_method0("accounts")?.try_iter()? {
        let account = account?;
        let state = ledger.call_method1("state", (&account,))?;
        let halted_venues = state.getattr("halted_venues")?;
        if halted_venues.call_method1("__contains__", (&venue,))?.is_truthy()? {
            holders.push(account.extract::<String>()?);
        }
    }
    holders.sort();

    let kind_reconcile = ctx.kind_venue_reconcile.bind(py).clone();
    let mut reconciles = Vec::new();
    for event in ledger.call_method1("events_of_kind", (kind_reconcile,))?.try_iter()? {
        let event = event?;
        let payload = event.getattr("payload")?;
        let payload_venue: String = payload.getattr("venue")?.extract()?;
        if payload_venue == venue {
            let seq: i128 = event.getattr("seq")?.extract()?;
            reconciles.push((seq, sl::reconcile_of(&payload)?));
        }
    }

    let now = clock.call_method0("now_utc")?;
    let plan = session::clear_halt(&venue, &reason, &sl::datetime_of(&now)?, &holders, &reconciles)
        .map_err(|e| refuse(py, ctx, e))?;

    let mut events = Vec::new();
    for (account, payload, command) in plan.events {
        let account_py = PyString::new(py, account.as_str()).into_any();
        let payload_py = sl::obj_py(py, &Obj::HaltCleared(payload))?;
        let kind = ctx.kind_venue_halt_cleared.bind(py).clone();
        events.push(make_event(py, ctx, &account_py, &kind, &payload_py, &now, &command)?);
    }
    let events_list = PyList::new(py, events)?;
    ledger.call_method1("extend", (events_list,))?;

    let holders_py: Vec<Bound<'py, PyAny>> = plan.holders.iter().map(|h| PyString::new(py, h.as_str()).into_any()).collect();
    Ok(PyTuple::new(py, holders_py)?.into_any())
}
