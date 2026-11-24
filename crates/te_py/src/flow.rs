//! P3b-2b: `OrderManager`'s command flow (`te_core::oms::flow`) driven from Python. The
//! host is a Python adapter object; every effect is one adapter call, made where the flow
//! makes it. Plain values cross; a refusal crosses as `ValueError((kind, message))`; an
//! exception an adapter call raised is re-raised as itself, except a broker call's: that
//! one is held as the cause of the outcome-unknown refusal the flow returns, which
//! crosses as `ValueError((kind, message))` with `__cause__` set to it.
//!
//! W6-C0c: the ledger's effects (`accounts`, `account_state`, `event_by_command`,
//! `events_of_kind`, `append`) are either the adapter's ledger methods (`PyBook`, for a
//! ledger that is not a plain `Ledger`) or the store's native reads and write (`NativeBook`,
//! for a plain `Ledger`). Every other effect is the adapter's in both, through one `Shared`.

use std::cell::RefCell;

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use te_core::ledger::bridge as lb;
use te_core::ledger::codec::{enc_order, event_from_bytes, event_to_bytes};
use te_core::ledger::fold::AccountState;
use te_core::ledger::json::dumps;
use te_core::ledger::model::{err, parse_datetime, DateTime, Event, EventKind, LErr, Order, OrderState, OrderType, Side, Tif, R as LR};
use te_core::oms::flow::{self as fl, Capabilities, Host, Net, OrderChanges, VenueAck, VenueFill, VenueOrder, VenueOrderState};

use crate::sim::{dec, instrument, refuse, Host as Stash};
use crate::store::LedgerStore;
use crate::LedgerFold;

type AckT = (String, String, Option<String>);
type OrderStateT = (String, String, String, String, String);
type FillT = (String, String, String, String, String, String, String, String, Option<String>);

fn when(iso: &str) -> LR<DateTime> {
    Ok(parse_datetime(iso)?.0)
}

fn state(s: &str) -> LR<OrderState> {
    OrderState::parse(s).map_or_else(|| err("value", format!("bad order state {s:?}")), Ok)
}

/// The adapter, the exception it raised (kind `host`), and a broker call's failure: the
/// machinery every host shares.
struct Shared<'py> {
    adapter: Bound<'py, PyAny>,
    stash: Stash,
    cause: RefCell<Option<PyErr>>,
}

impl<'py> Shared<'py> {
    fn new(adapter: &Bound<'py, PyAny>) -> Self {
        Self { adapter: adapter.clone(), stash: Stash::new(), cause: RefCell::new(None) }
    }

    fn py(&self) -> Python<'py> {
        self.adapter.py()
    }

    fn call<T: for<'a> FromPyObject<'a>>(&self, name: &str, args: impl IntoPyObject<'py, Target = pyo3::types::PyTuple>) -> LR<T> {
        match self.adapter.call_method1(name, args).and_then(|v| v.extract::<T>()) {
            Ok(v) => Ok(v),
            Err(e) => Err(self.stash.fail(e)),
        }
    }

    /// A broker call: an `Exception` is the venue failing (held as the cause); anything
    /// else (`KeyboardInterrupt`, `SystemExit`) unwinds as itself.
    fn broker(&self, name: &str, args: impl IntoPyObject<'py, Target = pyo3::types::PyTuple>) -> LR<Net<VenueAck>> {
        let py = self.adapter.py();
        match self.adapter.call_method1(name, args).and_then(|v| v.extract::<AckT>()) {
            Ok((venue_order_id, status, message)) => Ok(Net::Ok(VenueAck { venue_order_id, status, message })),
            Err(e) if e.is_instance_of::<PyException>(py) => {
                *self.cause.borrow_mut() = Some(e);
                Ok(Net::Failed)
            }
            Err(e) => Err(self.stash.fail(e)),
        }
    }

    fn finish<T>(self, r: LR<T>) -> PyResult<T> {
        match r {
            Err(e) if e.kind == "broker_unknown" || e.kind == "oco_unknown" => {
                let py = self.adapter.py();
                let refusal = refuse(e);
                if let Some(cause) = self.cause.into_inner() {
                    refusal.set_cause(py, Some(cause));
                }
                Err(refusal)
            }
            r => self.stash.finish(r),
        }
    }

    fn now_utc(&self) -> LR<(DateTime, bool)> {
        parse_datetime(&self.call::<String>("now", ())?)
    }

    fn capabilities(&self) -> LR<Capabilities> {
        let (types, tifs, native_stops) = self.call::<(Vec<String>, Vec<String>, bool)>("capabilities", ())?;
        let types = types
            .iter()
            .map(|t| OrderType::parse(t).map_or_else(|| err("value", format!("bad order type {t:?}")), Ok))
            .collect::<LR<_>>()?;
        let tifs = tifs
            .iter()
            .map(|t| Tif::parse(t).map_or_else(|| err("value", format!("bad time in force {t:?}")), Ok))
            .collect::<LR<_>>()?;
        Ok(Capabilities { types, tifs, native_stops })
    }

    fn env(&self) -> LR<String> {
        self.call("env", ())
    }

    fn venue_order(&self, order: &VenueOrder) -> LR<PyObject> {
        self.call("venue_order", (dumps(&fl::venue_json(order)?),))
    }

    fn submit(&self, order: &PyObject) -> LR<Net<VenueAck>> {
        self.broker("submit", (order.clone_ref(self.adapter.py()),))
    }

    fn cancel(&self, venue_order_id: &str) -> LR<Net<VenueAck>> {
        self.broker("cancel", (venue_order_id,))
    }

    fn replace(&self, venue_order_id: &str, changes: &OrderChanges) -> LR<Net<VenueAck>> {
        self.broker("replace", (venue_order_id, dumps(&fl::changes_json(changes))))
    }

    fn orders(&self, since: &DateTime) -> LR<Vec<VenueOrderState>> {
        let mut out = Vec::new();
        for (venue_order_id, st, filled, remaining, updated_at) in self.call::<Vec<OrderStateT>>("orders", (since.iso(),))? {
            out.push(VenueOrderState {
                venue_order_id,
                state: state(&st)?,
                filled_quantity: dec(&filled)?,
                remaining_quantity: dec(&remaining)?,
                updated_at,
            });
        }
        Ok(out)
    }

    fn fills(&self, since: &DateTime) -> LR<Vec<VenueFill>> {
        let mut out = Vec::new();
        for (venue_fill_id, venue_order_id, instr, qty, price, at, side, fee, leg_id) in
            self.call::<Vec<FillT>>("fills", (since.iso(),))?
        {
            out.push(VenueFill {
                venue_fill_id,
                venue_order_id,
                instrument: instrument(&instr)?,
                quantity: dec(&qty)?,
                price: dec(&price)?,
                filled_at: when(&at)?,
                side: Side::parse(&side).map_or_else(|| err("value", format!("bad side {side:?}")), Ok)?,
                fee: dec(&fee)?,
                leg_id,
            });
        }
        Ok(out)
    }

    fn cause(&self, e: LErr) {
        let held = if e.kind == "host" {
            self.stash.take()
        } else {
            match self.adapter.call_method1("refusal", (e.kind, e.msg)) {
                Ok(exc) => PyErr::from_value(exc),
                Err(raised) => raised,
            }
        };
        *self.cause.borrow_mut() = Some(held);
    }

    /// `self._send_reduce(reduce, command)` on the manager: an exception it raised (a
    /// refusal of the nested flow call included) is re-raised as itself.
    fn send_reduce(&self, reduce: &Order, command: &str) -> LR<Order> {
        let text = self.call::<String>("send_reduce", (dumps(&enc_order(reduce)?), command))?;
        fl::order_of(&lb::json_from_text(&text)?)
    }
}

/// The ledger's effects: the adapter's ledger methods, or the store's native ones.
trait Book<'py> {
    fn accounts(&self, host: &Shared<'py>) -> LR<Vec<String>>;
    fn account_state(&self, host: &Shared<'py>, account: &str) -> LR<AccountState>;
    fn event_by_command(&self, host: &Shared<'py>, command: &str) -> LR<Option<Event>>;
    fn events_of_kind(&self, host: &Shared<'py>, kind: EventKind) -> LR<Vec<Event>>;
    fn append(&self, host: &Shared<'py>, event: &Event) -> LR<Event>;
}

/// The adapter's ledger methods (`_Host`): the ledger of any `OrderManager` that is not a
/// plain `Ledger` (a wrapper such as `SplitLedger`).
struct PyBook;

impl<'py> Book<'py> for PyBook {
    fn accounts(&self, host: &Shared<'py>) -> LR<Vec<String>> {
        host.call("accounts", ())
    }

    /// `state(account)` is the ledger's `LedgerFold` (read in place) or canonical text.
    fn account_state(&self, host: &Shared<'py>, account: &str) -> LR<AccountState> {
        let held = host.adapter.call_method1("state", (account,)).map_err(|e| host.stash.fail(e))?;
        if let Ok(fold) = held.downcast::<LedgerFold>() {
            return match fold.borrow().state_of(account) {
                Some(st) => Ok(st.clone()),
                None => err("key", format!("'{account}'")),
            };
        }
        lb::account_from_text(&held.extract::<String>().map_err(|e| host.stash.fail(e))?)
    }

    fn event_by_command(&self, host: &Shared<'py>, command: &str) -> LR<Option<Event>> {
        host.call::<Option<Vec<u8>>>("event_by_command", (command,))?.map(|b| event_from_bytes(&b)).transpose()
    }

    fn events_of_kind(&self, host: &Shared<'py>, kind: EventKind) -> LR<Vec<Event>> {
        host.call::<Vec<Vec<u8>>>("events_of_kind", (kind.value(),))?.iter().map(|b| event_from_bytes(b)).collect()
    }

    fn append(&self, host: &Shared<'py>, event: &Event) -> LR<Event> {
        let text = String::from_utf8(event_to_bytes(event)?).map_err(|e| LErr { kind: "value", msg: e.to_string() })?;
        event_from_bytes(&host.call::<Vec<u8>>("append", (text,))?)
    }
}

/// The native ledger effects of a plain `Ledger`: its store's reads, and one event written
/// in its own transaction. The store's errors are held as the stash holds an adapter's.
struct NativeBook<'a> {
    store: &'a LedgerStore,
}

impl<'py> Book<'py> for NativeBook<'_> {
    fn accounts(&self, host: &Shared<'py>) -> LR<Vec<String>> {
        self.store.account_names(host.py()).map_err(|e| host.stash.fail(e))
    }

    /// The committed fold state, loaded if absent; a missing account is the same `key` error.
    fn account_state(&self, host: &Shared<'py>, account: &str) -> LR<AccountState> {
        match self.store.fold_state(host.py(), account).map_err(|e| host.stash.fail(e))? {
            Some(state) => Ok(state),
            None => err("key", format!("'{account}'")),
        }
    }

    fn event_by_command(&self, host: &Shared<'py>, command: &str) -> LR<Option<Event>> {
        self.store.command_event(host.py(), command).map_err(|e| host.stash.fail(e))
    }

    fn events_of_kind(&self, host: &Shared<'py>, kind: EventKind) -> LR<Vec<Event>> {
        self.store.kind_events(host.py(), kind.value()).map_err(|e| host.stash.fail(e))
    }

    /// `Ledger.append`: the ledger assigns the seq. A replayed command id writes nothing and
    /// returns the stored original; only a written event reaches `after_append`.
    fn append(&self, host: &Shared<'py>, event: &Event) -> LR<Event> {
        let py = host.py();
        // `_Host.append` builds the event without a seq, so the native path clears it too.
        let mut fresh = event.clone();
        fresh.seq = None;
        let command = fresh.command_id.clone().unwrap_or_default();
        let written = self
            .store
            .write_events(py, vec![(fresh, Vec::new())])
            .map_err(|e| host.stash.fail(e))?;
        match written.into_iter().next() {
            Some(written) => {
                after_append(host, &written)?;
                Ok(written)
            }
            None => match self.store.command_event(py, &command).map_err(|e| host.stash.fail(e))? {
                Some(original) => Ok(original),
                None => err("value", format!("no event for command {command:?}")),
            },
        }
    }
}

/// The test seam `_Host.after_append`: the written event's bytes, after the commit. An
/// exception it raises is a host error, like any adapter call's.
fn after_append(host: &Shared<'_>, event: &Event) -> LR<()> {
    let hook = host.adapter.getattr("after_append").map_err(|e| host.stash.fail(e))?;
    if hook.is_none() {
        return Ok(());
    }
    let bytes = PyBytes::new(host.py(), &event_to_bytes(event)?);
    hook.call1((bytes,)).map_err(|e| host.stash.fail(e))?;
    Ok(())
}

/// One flow host: the adapter's effects, with the ledger's taken by `B`.
struct OmsHost<'py, B> {
    host: Shared<'py>,
    book: B,
}

/// The flow over a ledger that is not a plain `Ledger`: every ledger effect is the adapter's.
type PyHost<'py> = OmsHost<'py, PyBook>;

/// The flow over a plain `Ledger`'s store: the ledger effects are native.
type NativeOmsHost<'py, 'a> = OmsHost<'py, NativeBook<'a>>;

impl<'py, B> OmsHost<'py, B> {
    fn finish<T>(self, r: LR<T>) -> PyResult<T> {
        self.host.finish(r)
    }
}

impl<'py, B: Book<'py>> Host for OmsHost<'py, B> {
    type Venue = PyObject;

    fn now_utc(&self) -> LR<(DateTime, bool)> {
        self.host.now_utc()
    }

    fn accounts(&self) -> LR<Vec<String>> {
        self.book.accounts(&self.host)
    }

    /// `state(account)` is the ledger's `LedgerFold` (read in place) or canonical text.
    fn account_state(&self, account: &str) -> LR<AccountState> {
        self.book.account_state(&self.host, account)
    }

    fn event_by_command(&self, command: &str) -> LR<Option<Event>> {
        self.book.event_by_command(&self.host, command)
    }

    fn events_of_kind(&self, kind: EventKind) -> LR<Vec<Event>> {
        self.book.events_of_kind(&self.host, kind)
    }

    fn append(&self, event: &Event) -> LR<Event> {
        self.book.append(&self.host, event)
    }

    fn capabilities(&self) -> LR<Capabilities> {
        self.host.capabilities()
    }

    fn env(&self) -> LR<String> {
        self.host.env()
    }

    fn venue_order(&self, order: &VenueOrder) -> LR<PyObject> {
        self.host.venue_order(order)
    }

    fn submit(&self, order: &PyObject) -> LR<Net<VenueAck>> {
        self.host.submit(order)
    }

    fn cancel(&self, venue_order_id: &str) -> LR<Net<VenueAck>> {
        self.host.cancel(venue_order_id)
    }

    fn replace(&self, venue_order_id: &str, changes: &OrderChanges) -> LR<Net<VenueAck>> {
        self.host.replace(venue_order_id, changes)
    }

    fn orders(&self, since: &DateTime) -> LR<Vec<VenueOrderState>> {
        self.host.orders(since)
    }

    fn fills(&self, since: &DateTime) -> LR<Vec<VenueFill>> {
        self.host.fills(since)
    }

    fn cause(&self, e: LErr) {
        self.host.cause(e)
    }

    /// `self._send_reduce(reduce, command)` on the manager: an exception it raised (a
    /// refusal of the nested flow call included) is re-raised as itself.
    fn send_reduce(&self, reduce: &Order, command: &str) -> LR<Order> {
        self.host.send_reduce(reduce, command)
    }
}

/// One `OrderManager` method (`te_core::oms::flow::run`) against a host: the request and
/// the result are JSON text.
fn run_flow<'py, B: Book<'py>>(op: &str, host: OmsHost<'py, B>, request: &str) -> PyResult<String> {
    let r = lb::json_from_text(request).and_then(|j| fl::run(op, &host, &j)).map(|j| dumps(&j));
    host.finish(r)
}

/// One `OrderManager` method against the adapter's ledger methods (any ledger but a plain `Ledger`).
#[pyfunction]
pub(crate) fn oms_flow(op: &str, adapter: &Bound<'_, PyAny>, request: &str) -> PyResult<String> {
    run_flow(op, PyHost { host: Shared::new(adapter), book: PyBook }, request)
}

/// One `OrderManager` method over a plain `Ledger`: `store` is the ledger's `_store`, and its
/// ledger effects run natively. The adapter still serves the broker, the clock and the gate.
#[pyfunction]
pub(crate) fn oms_flow_native(
    op: &str,
    store: PyRef<LedgerStore>,
    adapter: &Bound<'_, PyAny>,
    request: &str,
) -> PyResult<String> {
    let host = NativeOmsHost { host: Shared::new(adapter), book: NativeBook { store: &*store } };
    run_flow(op, host, request)
}
