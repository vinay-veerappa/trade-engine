//! P3b-2b: `OrderManager`'s command flow (`te_core::oms::flow`) driven from Python. The
//! host is a Python adapter object; every effect is one adapter call, made where the flow
//! makes it. Plain values cross; a refusal crosses as `ValueError((kind, message))`; an
//! exception an adapter call raised is re-raised as itself, except a broker call's: that
//! one is held as the cause of the outcome-unknown refusal the flow returns, which
//! crosses as `ValueError((kind, message))` with `__cause__` set to it.

use std::cell::RefCell;

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use te_core::ledger::bridge as lb;
use te_core::ledger::codec::{event_from_bytes, event_to_bytes};
use te_core::ledger::fold::AccountState;
use te_core::ledger::json::dumps;
use te_core::ledger::model::{err, parse_datetime, DateTime, Event, EventKind, LErr, OrderState, OrderType, Side, Tif, R as LR};
use te_core::oms::flow::{self as fl, Capabilities, Host, Net, OrderChanges, VenueAck, VenueFill, VenueOrder, VenueOrderState};

use crate::sim::{dec, instrument, refuse, Host as Stash};

/// The adapter, the exception it raised (kind `host`), and a broker call's failure.
struct PyHost<'py> {
    adapter: Bound<'py, PyAny>,
    stash: Stash,
    cause: RefCell<Option<PyErr>>,
}

type AckT = (String, String, Option<String>);
type OrderStateT = (String, String, String, String, String);
type FillT = (String, String, String, String, String, String, String, String, Option<String>);

fn when(iso: &str) -> LR<DateTime> {
    Ok(parse_datetime(iso)?.0)
}

fn state(s: &str) -> LR<OrderState> {
    OrderState::parse(s).map_or_else(|| err("value", format!("bad order state {s:?}")), Ok)
}

impl<'py> PyHost<'py> {
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
}

impl<'py> Host for PyHost<'py> {
    type Venue = PyObject;

    fn now_utc(&self) -> LR<(DateTime, bool)> {
        parse_datetime(&self.call::<String>("now", ())?)
    }

    fn accounts(&self) -> LR<Vec<String>> {
        self.call("accounts", ())
    }

    fn account_state(&self, account: &str) -> LR<AccountState> {
        lb::account_from_text(&self.call::<String>("state", (account,))?)
    }

    fn event_by_command(&self, command: &str) -> LR<Option<Event>> {
        self.call::<Option<Vec<u8>>>("event_by_command", (command,))?.map(|b| event_from_bytes(&b)).transpose()
    }

    fn events_of_kind(&self, kind: EventKind) -> LR<Vec<Event>> {
        self.call::<Vec<Vec<u8>>>("events_of_kind", (kind.value(),))?.iter().map(|b| event_from_bytes(b)).collect()
    }

    fn append(&self, event: &Event) -> LR<Event> {
        let text = String::from_utf8(event_to_bytes(event)?).map_err(|e| LErr { kind: "value", msg: e.to_string() })?;
        event_from_bytes(&self.call::<Vec<u8>>("append", (text,))?)
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
}

/// One `OrderManager` method (`te_core::oms::flow::run`) against the adapter: the
/// request and the result are JSON text.
#[pyfunction]
pub(crate) fn oms_flow(op: &str, adapter: &Bound<'_, PyAny>, request: &str) -> PyResult<String> {
    let host = PyHost { adapter: adapter.clone(), stash: Stash::new(), cause: RefCell::new(None) };
    let r = lb::json_from_text(request).and_then(|j| fl::run(op, &host, &j)).map(|j| dumps(&j));
    host.finish(r)
}
