//! W7-CORE-C2: `SnapshotVenue`'s conversion shell, as a `#[pyclass(subclass)]` base.
//!
//! `trade_engine.sim.snapshot_venue.SnapshotVenue` was a thin carrier shell over the
//! `SnapBook` pyclass: it read the clock, turned `VenueOrder`s into tuples, called the
//! `Venue` rules, and turned the results back into `VenueAck`/`VenueFill`/... carriers.
//! That shell now lives here: `SnapshotVenueBase` owns the `te_core::sim::snapshot::Venue`
//! state directly and builds its carriers by importing them once (a `GILOnceCell`).
//!
//! The Python module keeps its import name and public surface; the class is now
//! `class SnapshotVenue(rs.SnapshotVenueBase)`.
//!
//! A rule's refusal crosses as `ValueError((kind, msg))` from `te_core`; the base maps it
//! to the registered exception through `trade_engine.sim._rs.refusal` (so `snapshot` ->
//! `SnapshotVenueError`, `value` -> `ValueError`, ...), exactly as `sim/_rs.call` did.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use te_core::ledger::model::{err, Instrument, LErr, OrderState, Side, R as LR};
use te_core::money::Money;
use te_core::sim::broker::VOrder;
use te_core::sim::snapshot::{Origin, Quote, SFill, Snap, Venue};
use te_core::sim::{Clock, Ts};

use crate::sim::{ack, dec, instrument as parse_instrument, lookup_quote, side as parse_side, vorder, AckT, Host, OrderT};

/// A te_core refusal, raised as the exception its kind names through `sim._rs`.
fn snap_refusal(py: Python<'_>, error: LErr) -> PyErr {
    match py
        .import("trade_engine.sim._rs")
        .and_then(|module| module.call_method1("refusal", (error.kind, error.msg)))
    {
        Ok(value) => PyErr::from_value(value),
        Err(failed) => failed,
    }
}

/// Run `f` with a clock that reads `clock.now_utc().isoformat()`. A clock read error is
/// re-raised as itself; a rule's refusal is mapped through [`snap_refusal`].
fn with_clock<T>(clock: &Bound<'_, PyAny>, f: impl FnOnce(&mut Clock<'_>) -> LR<T>) -> PyResult<T> {
    let py = clock.py();
    let host = Host::new();
    let r = {
        let mut c = || -> LR<String> {
            match clock
                .call_method0("now_utc")
                .and_then(|v| v.call_method0("isoformat"))
                .and_then(|v| v.extract::<String>())
            {
                Ok(s) => Ok(s),
                Err(e) => Err(host.fail(e)),
            }
        };
        f(&mut c)
    };
    match r {
        Ok(v) => Ok(v),
        // A `host` refusal: the clock callback failed (re-raise it), or an outer host
        // (a quote lookup) failed and the caller re-raises it (fall through as the pair).
        Err(e) if e.kind == "host" => {
            if host.has() {
                Err(host.take())
            } else {
                Err(PyValueError::new_err(("host", e.msg)))
            }
        }
        Err(e) => Err(snap_refusal(py, e)),
    }
}

/// The Python carriers and helpers the base builds and reads, imported once.
struct Carriers {
    venue_identity: Py<PyAny>,
    venue_ack: Py<PyAny>,
    venue_fill: Py<PyAny>,
    venue_order_state: Py<PyAny>,
    venue_position: Py<PyAny>,
    account_snapshot: Py<PyAny>,
    account_record: Py<PyAny>,
    side: Py<PyAny>,
    order_state: Py<PyAny>,
    combo: Py<PyAny>,
    equity: Py<PyAny>,
    option_contract: Py<PyAny>,
    decimal: Py<PyAny>,
    datetime: Py<PyAny>,
    quotes: Py<PyAny>,
    codec_text: Py<PyAny>,
    codec_encode: Py<PyAny>,
}

fn carriers(py: Python<'_>) -> PyResult<&Carriers> {
    static CELL: GILOnceCell<Carriers> = GILOnceCell::new();
    CELL.get_or_try_init(py, || {
        let broker = py.import("trade_engine.interfaces.broker")?;
        let instruments = py.import("trade_engine.domain.instruments")?;
        let orders = py.import("trade_engine.domain.orders")?;
        let codec = py.import("trade_engine.ledger.codec")?;
        Ok(Carriers {
            venue_identity: broker.getattr("VenueIdentity")?.unbind(),
            venue_ack: broker.getattr("VenueAck")?.unbind(),
            venue_fill: broker.getattr("VenueFill")?.unbind(),
            venue_order_state: broker.getattr("VenueOrderState")?.unbind(),
            venue_position: broker.getattr("VenuePosition")?.unbind(),
            account_snapshot: broker.getattr("AccountSnapshot")?.unbind(),
            account_record: broker.getattr("AccountRecord")?.unbind(),
            side: instruments.getattr("Side")?.unbind(),
            order_state: orders.getattr("OrderState")?.unbind(),
            combo: instruments.getattr("Combo")?.unbind(),
            equity: instruments.getattr("Equity")?.unbind(),
            option_contract: instruments.getattr("OptionContract")?.unbind(),
            decimal: py.import("decimal")?.getattr("Decimal")?.unbind(),
            datetime: py.import("datetime")?.getattr("datetime")?.unbind(),
            quotes: py.import("trade_engine.sim.snapshot_venue")?.getattr("_Quotes")?.unbind(),
            codec_text: codec.getattr("text")?.unbind(),
            codec_encode: codec.getattr("_encode")?.unbind(),
        })
    })
}

/// `codec.text(codec._encode(instrument))`: the canonical text of a Python instrument.
fn instrument_text(py: Python<'_>, instrument: &Bound<'_, PyAny>) -> PyResult<String> {
    let carriers = carriers(py)?;
    let encoded = carriers.codec_encode.bind(py).call1((instrument,))?;
    carriers.codec_text.bind(py).call1((encoded,))?.extract()
}

/// `Decimal(money.canon())`.
fn decimal_py<'py>(py: Python<'py>, money: &Money) -> PyResult<Bound<'py, PyAny>> {
    carriers(py)?.decimal.bind(py).call1((money.canon(),))
}

/// `datetime.fromisoformat(iso)`.
fn dt_py<'py>(py: Python<'py>, iso: &str) -> PyResult<Bound<'py, PyAny>> {
    carriers(py)?.datetime.bind(py).call_method1("fromisoformat", (iso,))
}

/// `Side(value)`.
fn side_py<'py>(py: Python<'py>, side: Side) -> PyResult<Bound<'py, PyAny>> {
    carriers(py)?.side.bind(py).call1((side.value(),))
}

/// `OrderState(value)`.
fn order_state_py<'py>(py: Python<'py>, state: OrderState) -> PyResult<Bound<'py, PyAny>> {
    carriers(py)?.order_state.bind(py).call1((state.value(),))
}

/// The account id as the old shell sent it to `SnapBook`: a str passes through, any other
/// truthy value is `str()`-ed, anything else is `""`.
fn account_id_text(account_id: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(s) = account_id.downcast::<PyString>() {
        return Ok(s.to_str()?.to_owned());
    }
    if account_id.is_truthy()? {
        Ok(account_id.str()?.extract()?)
    } else {
        Ok(String::new())
    }
}

/// `str(value)` for a `Decimal`, `None` otherwise (the rule refuses it by name).
fn decimal_arg(
    py: Python<'_>,
    decimal: &Bound<'_, PyAny>,
    value: Option<&Bound<'_, PyAny>>,
    default: &str,
) -> PyResult<Option<Money>> {
    let defaulted = match value {
        None => true,
        Some(v) => v.is_none(),
    };
    if defaulted {
        return Ok(Some(dec(default).map_err(|e| snap_refusal(py, e))?));
    }
    let v = value.expect("non-default");
    if v.is_instance(decimal)? {
        Ok(Some(dec(&v.str()?.extract::<String>()?).map_err(|e| snap_refusal(py, e))?))
    } else {
        Ok(None)
    }
}

/// `max_quote_age_seconds` as an f64 when it is an int or float (never a bool), else None.
fn age_arg(value: &Bound<'_, PyAny>) -> PyResult<Option<f64>> {
    let number = value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>();
    let is_bool = value.is_instance_of::<PyBool>();
    if number && !is_bool {
        Ok(Some(value.extract::<f64>()?))
    } else {
        Ok(None)
    }
}

/// `SnapshotVenue`'s carrier shell, as a Rust base class. It owns the `Venue` (the rules),
/// the clock, and the carriers it has built so far (`orders` by id, the `fills`, and the
/// `restored_positions` it hands back from `positions`).
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct SnapshotVenueBase {
    venue: Venue,
    account_id: Py<PyAny>,
    clock: Py<PyAny>,
    orders: HashMap<String, Py<PyAny>>,
    fills: Vec<Py<PyAny>>,
    restored_positions: Vec<Py<PyAny>>,
}

#[pymethods]
impl SnapshotVenueBase {
    #[new]
    #[pyo3(signature = (account_id, clock, *, fill_fraction=None, fee_per_contract=None, equity_slippage_bps=None, max_quote_age_seconds=None, calendar=None))]
    fn new(
        py: Python<'_>,
        account_id: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        fill_fraction: Option<&Bound<'_, PyAny>>,
        fee_per_contract: Option<&Bound<'_, PyAny>>,
        equity_slippage_bps: Option<&Bound<'_, PyAny>>,
        max_quote_age_seconds: Option<&Bound<'_, PyAny>>,
        calendar: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let decimal = carriers(py)?.decimal.bind(py);
        let fraction = decimal_arg(py, decimal, fill_fraction, "0.25")?;
        let fee = decimal_arg(py, decimal, fee_per_contract, "0.65")?;
        let slippage = decimal_arg(py, decimal, equity_slippage_bps, "5")?;
        let age = match max_quote_age_seconds {
            None => Some(900.0),
            Some(v) => age_arg(v)?,
        };
        if let Some(calendar) = calendar {
            let exchange = py.import("trade_engine.calendar.sessions")?.getattr("ExchangeCalendar")?;
            if !calendar.is_instance(&exchange)? || calendar.getattr("exchange")?.extract::<String>()? != "XNYS" {
                let shown = calendar.repr()?;
                return Err(PyValueError::new_err(format!(
                    "SnapshotVenue sessions are the XNYS ExchangeCalendar, got {shown} (I5)"
                )));
            }
        }
        let text = account_id_text(account_id)?;
        let venue = Venue::new(&text, fraction, fee, slippage, age).map_err(|e| snap_refusal(py, e))?;
        Ok(SnapshotVenueBase {
            venue,
            account_id: account_id.clone().unbind(),
            clock: clock.clone().unbind(),
            orders: HashMap::new(),
            fills: Vec::new(),
            restored_positions: Vec::new(),
        })
    }

    fn connect(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let clock = self.clock.clone_ref(py);
        let clock = clock.bind(py);
        let connected = with_clock(clock, |c| self.venue.connect(c))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("account_id", self.account_id.bind(py))?;
        kwargs.set_item("env", "sim")?;
        kwargs.set_item("connected_at", dt_py(py, &connected.iso)?)?;
        kwargs.set_item("broker_name", "SnapshotVenue")?;
        Ok(carriers(py)?.venue_identity.bind(py).call((), Some(&kwargs))?.unbind())
    }

    fn restore(
        &mut self,
        py: Python<'_>,
        orders: &Bound<'_, PyAny>,
        fills: &Bound<'_, PyAny>,
        positions: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        // `self._restored_positions` is set before the call, as the pre-port venue did.
        let given: Vec<Py<PyAny>> = positions
            .try_iter()?
            .map(|p| Ok(p?.unbind()))
            .collect::<PyResult<_>>()?;
        self.restored_positions = given;

        let restored: Vec<(Bound<'_, PyAny>, Bound<'_, PyAny>)> = orders
            .try_iter()?
            .map(|item| item?.extract())
            .collect::<PyResult<_>>()?;
        let held: Vec<Bound<'_, PyAny>> = fills.try_iter()?.map(|f| Ok(f?)).collect::<PyResult<_>>()?;

        let os = self.vorders(py, &restored)?;
        let fs = self.sfills(py, &held)?;
        let ps = self.vpositions(py, &self.restored_positions)?;

        let result = self.venue.restore(os, fs, ps).map_err(|e| snap_refusal(py, e));

        // The `finally`: hold every restored order, then fold in the fills the restore made.
        for (order, _state) in &restored {
            self.hold(order)?;
        }
        self.sync_fills(py, Some(&held))?;

        result
    }

    fn submit(&mut self, py: Python<'_>, order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let t = self.order_tuple(py, order)?;
        let clock = self.clock.clone_ref(py);
        let clock = clock.bind(py);
        let r = with_clock(clock, |c| self.venue.submit(vorder(&t)?, c));
        // `finally`: hold the order even when submit refused, then re-raise.
        let held = self.hold(order);
        let ack_result = r?;
        held?;
        self.build_ack(py, &ack(ack_result))
    }

    fn cancel(&mut self, py: Python<'_>, venue_order_id: &str) -> PyResult<Py<PyAny>> {
        let clock = self.clock.clone_ref(py);
        let clock = clock.bind(py);
        let ack_result = with_clock(clock, |c| self.venue.cancel(venue_order_id, c))?;
        self.build_ack(py, &ack(ack_result))
    }

    fn replace(
        &mut self,
        py: Python<'_>,
        venue_order_id: &str,
        _changes: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let clock = self.clock.clone_ref(py);
        let clock = clock.bind(py);
        let ack_result = with_clock(clock, |c| self.venue.replace(venue_order_id, c))?;
        self.build_ack(py, &ack(ack_result))
    }

    fn orders(&mut self, py: Python<'_>, since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let since: String = since.call_method0("isoformat")?.extract()?;
        let clock = self.clock.clone_ref(py);
        let clock = clock.bind(py);
        let rows = with_clock(clock, |c| self.venue.orders_since(&since, c))?;
        let out = PyList::empty(py);
        for (id, state, filled, remaining, at) in rows {
            let kwargs = PyDict::new(py);
            kwargs.set_item("venue_order_id", id)?;
            kwargs.set_item("state", order_state_py(py, state)?)?;
            kwargs.set_item("filled_quantity", decimal_py(py, &filled)?)?;
            kwargs.set_item("remaining_quantity", decimal_py(py, &remaining)?)?;
            kwargs.set_item("updated_at", dt_py(py, &at.iso)?)?;
            out.append(carriers(py)?.venue_order_state.bind(py).call((), Some(&kwargs))?)?;
        }
        Ok(out.into_any().unbind())
    }

    fn fills(&mut self, py: Python<'_>, since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let since: String = since.call_method0("isoformat")?.extract()?;
        let clock = self.clock.clone_ref(py);
        let clock = clock.bind(py);
        let indices = with_clock(clock, |c| self.venue.fills_since(&since, c))?;
        let out = PyList::empty(py);
        for i in indices {
            out.append(self.fills[i].bind(py))?;
        }
        Ok(out.into_any().unbind())
    }

    fn positions(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let clock = self.clock.clone_ref(py);
        let clock = clock.bind(py);
        let (at, rows) = with_clock(clock, |c| self.venue.positions(c))?;
        let now = dt_py(py, &at.iso)?;
        let zero = decimal_py(py, &Money::parse("0").expect("a literal Decimal"))?;
        let out = PyList::empty(py);
        for (origin, quantity) in rows {
            let source = match origin {
                Origin::Restored(i) => self.restored_positions[i].bind(py),
                Origin::Fill(i) => self.fills[i].bind(py),
            };
            let kwargs = PyDict::new(py);
            kwargs.set_item("instrument", source.getattr("instrument")?)?;
            kwargs.set_item("quantity", decimal_py(py, &quantity)?)?;
            kwargs.set_item("avg_price", &zero)?;
            kwargs.set_item("as_of", &now)?;
            out.append(carriers(py)?.venue_position.bind(py).call((), Some(&kwargs))?)?;
        }
        Ok(out.into_any().unbind())
    }

    fn cash_events(&self, py: Python<'_>, _since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Ok(PyList::empty(py).into_any().unbind())
    }

    fn accounts(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let clock = self.clock.clone_ref(py);
        let confirmed_at = clock.bind(py).call_method0("now_utc")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("broker", "sim")?;
        kwargs.set_item("account_id", self.account_id.bind(py))?;
        kwargs.set_item("env", "sim")?;
        kwargs.set_item("source", "snapshot_venue")?;
        kwargs.set_item("confirmed_at", &confirmed_at)?;
        let record = carriers(py)?.account_record.bind(py).getattr("of")?.call((), Some(&kwargs))?;
        let snapshot = PyDict::new(py);
        snapshot.set_item("records", PyTuple::new(py, [record])?)?;
        snapshot.set_item("confirmed_at", &confirmed_at)?;
        Ok(carriers(py)?.account_snapshot.bind(py).call((), Some(&snapshot))?.unbind())
    }

    fn account_events(&self, py: Python<'_>, _since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Ok(PyList::empty(py).into_any().unbind())
    }

    fn process_snapshot(&mut self, py: Python<'_>, snapshot: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let underlying: String = snapshot.getattr("underlying")?.extract()?;
        let as_of: String = snapshot.getattr("as_of")?.call_method0("isoformat")?.extract()?;
        let underlying_price: String = snapshot.getattr("underlying_price")?.str()?.extract()?;
        let quote = carriers(py)?.quotes.bind(py).call1((snapshot,))?;
        let clock = self.clock.clone_ref(py);
        let clock = clock.bind(py);
        let host = Host::new();
        let r = with_clock(clock, |c| {
            let mut lookup = |occ: &str| -> LR<Option<Quote>> { lookup_quote(&quote, occ, &host) };
            let mut s = Snap {
                underlying: underlying.clone(),
                as_of: Ts::aware(&as_of, "Snapshot as_of")?,
                underlying_price: dec(&underlying_price)?,
                quote: &mut lookup,
            };
            self.venue.process_snapshot(&mut s, c)
        });
        let made = match r {
            Err(_) if host.has() => return Err(host.take()),
            other => other?,
        };
        self.sync_fills(py, None)?;
        let tuple = PyTuple::new(py, made.iter().map(|&i| self.fills[i].clone_ref(py)))?;
        Ok(tuple.into_any().unbind())
    }

    fn model_price(
        &self,
        py: Python<'_>,
        instrument: &Bound<'_, PyAny>,
        side: &Bound<'_, PyAny>,
        snapshot: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Py<PyAny>>> {
        let carriers = carriers(py)?;
        let equity = carriers.equity.bind(py);
        let option = carriers.option_contract.bind(py);
        if !(instrument.is_instance(&equity)? || instrument.is_instance(&option)?) {
            return Ok(None);
        }
        let text = instrument_text(py, instrument)?;
        let instr = parse_instrument(&text).map_err(|e| snap_refusal(py, e))?;
        let side_text: String = side.getattr("value")?.extract()?;
        let side = parse_side(&side_text).map_err(|e| snap_refusal(py, e))?;
        let underlying: String = snapshot.getattr("underlying")?.extract()?;
        let as_of: String = snapshot.getattr("as_of")?.call_method0("isoformat")?.extract()?;
        let underlying_price: String = snapshot.getattr("underlying_price")?.str()?.extract()?;
        let quote = carriers.quotes.bind(py).call1((snapshot,))?;
        let host = Host::new();
        let r = (|| -> LR<Option<Money>> {
            let mut lookup = |occ: &str| -> LR<Option<Quote>> { lookup_quote(&quote, occ, &host) };
            let mut s = Snap {
                underlying,
                as_of: Ts::aware(&as_of, "Snapshot as_of")?,
                underlying_price: dec(&underlying_price)?,
                quote: &mut lookup,
            };
            self.venue.model_price(&instr, side, &mut s)
        })();
        let price = match r {
            Err(_) if host.has() => return Err(host.take()),
            Err(e) => return Err(snap_refusal(py, e)),
            Ok(p) => p,
        };
        match price {
            Some(p) => Ok(Some(decimal_py(py, &p)?.unbind())),
            None => Ok(None),
        }
    }
}

impl SnapshotVenueBase {
    /// `_order`: the `VenueOrder` as the tuple `vorder` reads.
    fn order_tuple(&self, py: Python<'_>, order: &Bound<'_, PyAny>) -> PyResult<OrderT> {
        let opt = |v: &Bound<'_, PyAny>| -> PyResult<Option<String>> {
            if v.is_none() {
                Ok(None)
            } else {
                Ok(Some(v.str()?.extract()?))
            }
        };
        let mut allocs = Vec::new();
        for a in order.getattr("allocations")?.try_iter()? {
            let a = a?;
            allocs.push((
                a.getattr("strategy_order_id")?.extract::<String>()?,
                a.getattr("account_id")?.extract::<String>()?,
                a.getattr("quantity")?.str()?.extract::<String>()?,
            ));
        }
        Ok((
            order.getattr("venue_order_id")?.extract()?,
            instrument_text(py, &order.getattr("instrument")?)?,
            order.getattr("order_type")?.getattr("value")?.extract()?,
            order.getattr("side")?.getattr("value")?.extract()?,
            order.getattr("quantity")?.str()?.extract()?,
            order.getattr("submitted_at")?.call_method0("isoformat")?.extract()?,
            order.getattr("tif")?.getattr("value")?.extract()?,
            (
                opt(&order.getattr("limit_price")?)?,
                opt(&order.getattr("stop_price")?)?,
                opt(&order.getattr("trail_amount")?)?,
            ),
            allocs,
            order.getattr("parent_order_id")?.extract()?,
            order.getattr("oco_group")?.extract()?,
        ))
    }

    /// `restore`'s `(order, state)` list, as `(VOrder, OrderState)`.
    fn vorders(
        &self,
        py: Python<'_>,
        restored: &[(Bound<'_, PyAny>, Bound<'_, PyAny>)],
    ) -> PyResult<Vec<(VOrder, OrderState)>> {
        let mut os = Vec::new();
        for (order, state) in restored {
            let t = self.order_tuple(py, order)?;
            let v = vorder(&t).map_err(|e| snap_refusal(py, e))?;
            let state: String = state.getattr("value")?.extract()?;
            let state = OrderState::parse(&state)
                .map_or_else(|| err("value", format!("bad state {state:?}")), Ok)
                .map_err(|e| snap_refusal(py, e))?;
            os.push((v, state));
        }
        Ok(os)
    }

    /// `restore`'s fills, as the `SFill`s the venue reads (each `src` is its input index).
    fn sfills(&self, py: Python<'_>, fills: &[Bound<'_, PyAny>]) -> PyResult<Vec<SFill>> {
        let mut fs = Vec::new();
        for (i, f) in fills.iter().enumerate() {
            let text = instrument_text(py, &f.getattr("instrument")?)?;
            fs.push(SFill {
                fill_id: f.getattr("venue_fill_id")?.extract()?,
                order_id: f.getattr("venue_order_id")?.extract()?,
                instr: parse_instrument(&text).map_err(|e| snap_refusal(py, e))?,
                quantity: dec(&f.getattr("quantity")?.str()?.extract::<String>()?)
                    .map_err(|e| snap_refusal(py, e))?,
                price: dec(&f.getattr("price")?.str()?.extract::<String>()?)
                    .map_err(|e| snap_refusal(py, e))?,
                filled_at: Ts::aware(
                    &f.getattr("filled_at")?.call_method0("isoformat")?.extract::<String>()?,
                    "filled_at",
                )
                .map_err(|e| snap_refusal(py, e))?,
                side: parse_side(&f.getattr("side")?.getattr("value")?.extract::<String>()?)
                    .map_err(|e| snap_refusal(py, e))?,
                fee: dec(&f.getattr("fee")?.str()?.extract::<String>()?)
                    .map_err(|e| snap_refusal(py, e))?,
                leg_id: f.getattr("leg_id")?.extract()?,
                leg: None,
                src: Some(i),
            });
        }
        Ok(fs)
    }

    /// `restore`'s positions, as `(Instrument, Money)`.
    fn vpositions(&self, py: Python<'_>, positions: &[Py<PyAny>]) -> PyResult<Vec<(Instrument, Money)>> {
        let mut ps = Vec::new();
        for p in positions {
            let p = p.bind(py);
            let text = instrument_text(py, &p.getattr("instrument")?)?;
            ps.push((
                parse_instrument(&text).map_err(|e| snap_refusal(py, e))?,
                dec(&p.getattr("quantity")?.str()?.extract::<String>()?)
                    .map_err(|e| snap_refusal(py, e))?,
            ));
        }
        Ok(ps)
    }

    /// `_hold`: keep an order once the book holds it.
    fn hold(&mut self, order: &Bound<'_, PyAny>) -> PyResult<()> {
        let id: String = order.getattr("venue_order_id")?.extract()?;
        if !self.orders.contains_key(&id) && self.venue.has(&id) {
            self.orders.insert(id, order.clone().unbind());
        }
        Ok(())
    }

    /// `_sync_fills`: fold the venue's new fills into `self.fills`, using the restored
    /// carriers for their `src` rows and building the rest from the held orders.
    fn sync_fills(&mut self, py: Python<'_>, restored: Option<&[Bound<'_, PyAny>]>) -> PyResult<()> {
        let combo = carriers(py)?.combo.bind(py);
        while self.fills.len() < self.venue.fills.len() {
            let f = self.venue.fills[self.fills.len()].clone();
            if let Some(src) = f.src {
                let list = restored.ok_or_else(|| PyValueError::new_err("restore fills missing"))?;
                self.fills.push(list[src].clone().unbind());
                continue;
            }
            let order = self
                .orders
                .get(&f.order_id)
                .ok_or_else(|| PyValueError::new_err(format!("fill references unknown order '{}'", f.order_id)))?;
            let instrument = order.bind(py).getattr("instrument")?;
            let contract = if instrument.is_instance(&combo)? {
                let leg = f.leg.ok_or_else(|| PyValueError::new_err("combo fill has no leg"))?;
                instrument.getattr("legs")?.get_item(leg)?.getattr("contract")?
            } else {
                instrument
            };
            let kwargs = PyDict::new(py);
            kwargs.set_item("venue_fill_id", &f.fill_id)?;
            kwargs.set_item("venue_order_id", &f.order_id)?;
            kwargs.set_item("instrument", contract)?;
            kwargs.set_item("quantity", decimal_py(py, &f.quantity)?)?;
            kwargs.set_item("price", decimal_py(py, &f.price)?)?;
            kwargs.set_item("filled_at", dt_py(py, &f.filled_at.iso)?)?;
            kwargs.set_item("side", side_py(py, f.side)?)?;
            kwargs.set_item("fee", decimal_py(py, &f.fee)?)?;
            kwargs.set_item("leg_id", f.leg_id.as_deref())?;
            let fill = carriers(py)?.venue_fill.bind(py).call((), Some(&kwargs))?;
            self.fills.push(fill.unbind());
        }
        Ok(())
    }

    /// `_ack`: the `AckT` the venue returned, as a `VenueAck`.
    fn build_ack(&self, py: Python<'_>, ack: &AckT) -> PyResult<Py<PyAny>> {
        let (id, status, ts, msg) = ack;
        let kwargs = PyDict::new(py);
        kwargs.set_item("venue_order_id", id)?;
        kwargs.set_item("status", *status)?;
        kwargs.set_item("timestamp", dt_py(py, ts)?)?;
        kwargs.set_item("message", msg)?;
        Ok(carriers(py)?.venue_ack.bind(py).call((), Some(&kwargs))?.unbind())
    }
}
