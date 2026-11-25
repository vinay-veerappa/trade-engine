//! `SimBrokerBase`: the conversion shell for `trade_engine.sim.broker.SimBroker`.
//!
//! The rules live in `te_core::sim::broker::Book` (reached before through `SimBook`); this
//! type owns the `Book`, reads the clock, turns the Python carriers (VenueOrder, VenueAck,
//! VenueFill, positions, bars) into the plain values the book takes, and turns what comes
//! back into carriers. It reuses `sim.rs`'s tuple converters (`vorder`, `ack`, `bar`, ...),
//! so nothing is decided twice. The carrier classes and the exception types are imported
//! once and cached in a `GILOnceCell`.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyTuple};

use te_core::ledger::model::{err, LErr, OrderState, R as LR};
use te_core::sim::broker::{self as sb, Book, Pos, VFill, VOrder};
use te_core::sim::{Clock, Ts};

use crate::sim::{
    ack, dec, instrument, opt_dec, refuse as sim_refuse, side, vorder, AckT, BarT, Host, OrderT,
};

/// The Python classes and functions `SimBrokerBase` reaches, imported once.
struct Ctx {
    carriers: Py<PyAny>,
    venue_ack: Py<PyAny>,
    venue_fill: Py<PyAny>,
    venue_position: Py<PyAny>,
    venue_order_state: Py<PyAny>,
    venue_order_alloc: Py<PyAny>,
    venue_identity: Py<PyAny>,
    account_snapshot: Py<PyAny>,
    account_record: Py<PyAny>,
    order_state: Py<PyAny>,
    decimal: Py<PyAny>,
    replace: Py<PyAny>,
    sim_error: Py<PyAny>,
    missing_bar: Py<PyAny>,
    unknown_order: Py<PyAny>,
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let broker = py.import("trade_engine.interfaces.broker")?;
        let orders = py.import("trade_engine.domain.orders")?;
        let sim_broker = py.import("trade_engine.sim.broker")?;
        let decimal = py.import("decimal")?;
        let dataclasses = py.import("dataclasses")?;
        Ok(Ctx {
            carriers: sim_broker.getattr("_Carriers")?.unbind(),
            venue_ack: broker.getattr("VenueAck")?.unbind(),
            venue_fill: broker.getattr("VenueFill")?.unbind(),
            venue_position: broker.getattr("VenuePosition")?.unbind(),
            venue_order_state: broker.getattr("VenueOrderState")?.unbind(),
            venue_order_alloc: broker.getattr("VenueOrderAllocation")?.unbind(),
            venue_identity: broker.getattr("VenueIdentity")?.unbind(),
            account_snapshot: broker.getattr("AccountSnapshot")?.unbind(),
            account_record: broker.getattr("AccountRecord")?.unbind(),
            order_state: orders.getattr("OrderState")?.unbind(),
            decimal: decimal.getattr("Decimal")?.unbind(),
            replace: dataclasses.getattr("replace")?.unbind(),
            sim_error: sim_broker.getattr("SimBrokerError")?.unbind(),
            missing_bar: sim_broker.getattr("MissingBarError")?.unbind(),
            unknown_order: sim_broker.getattr("UnknownVenueOrderError")?.unbind(),
        })
    }
}

fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

/// Raise `cls(msg)` and return it as a `PyErr`.
fn raise(py: Python<'_>, cls: &Py<PyAny>, msg: &str) -> PyErr {
    PyErr::from_value(
        cls.bind(py)
            .call1((msg,))
            .expect("a SimBroker exception class is callable with a message"),
    )
}

/// The exception today's code raised for one te_core refusal kind.
fn refusal(py: Python<'_>, e: LErr) -> PyErr {
    let ctx = ctx(py).expect("the carrier context is initialized at construction");
    match e.kind {
        "sim" => raise(py, &ctx.sim_error, &e.msg),
        "missing_bar" => raise(py, &ctx.missing_bar, &e.msg),
        "unknown_order" => raise(py, &ctx.unknown_order, &e.msg),
        "value" => PyValueError::new_err(e.msg),
        _ => sim_refuse(e),
    }
}

/// `None` -> `None`, else `str(value)` (the old `_opt`).
fn opt_str(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if value.is_none() {
        Ok(None)
    } else {
        Ok(Some(value.str()?.to_str()?.to_string()))
    }
}

/// The `OrderT` tuple a `VenueOrder` carrier becomes.
fn order_tuple(py: Python<'_>, carriers: &Py<PyAny>, order: &Bound<'_, PyAny>) -> PyResult<OrderT> {
    let venue_order_id: String = order.getattr("venue_order_id")?.extract()?;
    let instrument = order.getattr("instrument")?;
    let instrument_text: String = carriers
        .bind(py)
        .call_method1("text", (&instrument,))?
        .extract()?;
    let otype: String = order.getattr("order_type")?.getattr("value")?.extract()?;
    let side_value: String = order.getattr("side")?.getattr("value")?.extract()?;
    let quantity = order.getattr("quantity")?.str()?.to_str()?.to_string();
    let submitted = order.getattr("submitted_at")?;
    let submitted_iso: String = carriers.bind(py).call_method1("iso", (&submitted,))?.extract()?;
    let tif: String = order.getattr("tif")?.getattr("value")?.extract()?;
    let limit = opt_str(&order.getattr("limit_price")?)?;
    let stop = opt_str(&order.getattr("stop_price")?)?;
    let trail = opt_str(&order.getattr("trail_amount")?)?;
    let mut allocs = Vec::new();
    for a in order.getattr("allocations")?.try_iter()? {
        let a = a?;
        let soid: String = a.getattr("strategy_order_id")?.extract()?;
        let account: String = a.getattr("account_id")?.extract()?;
        let qty = a.getattr("quantity")?.str()?.to_str()?.to_string();
        allocs.push((soid, account, qty));
    }
    let parent: Option<String> = order.getattr("parent_order_id")?.extract()?;
    let oco: Option<String> = order.getattr("oco_group")?.extract()?;
    Ok((
        venue_order_id,
        instrument_text,
        otype,
        side_value,
        quantity,
        submitted_iso,
        tif,
        (limit, stop, trail),
        allocs,
        parent,
        oco,
    ))
}

/// `SimBroker`'s book and carriers. A Python subclass instance keeps a `__dict__`, so the
/// existing tests can still subclass it and monkeypatch instance attributes; every public
/// method here is reached through normal Python attribute lookup.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct SimBrokerBase {
    book: Book,
    account_id: String,
    name: String,
    env: String,
    clock: Py<PyAny>,
    carriers: Py<PyAny>,
    orders: HashMap<String, Py<PyAny>>,
    fills: Vec<Py<PyAny>>,
}

impl SimBrokerBase {
    /// Run `f` with the book and a clock that reads `self._clock.now_utc()`, caching the
    /// timestamp through the shared `_Carriers`. A host callback exception is re-raised.
    fn run_book<R>(
        &mut self,
        py: Python<'_>,
        f: impl FnOnce(&mut Book, &mut Clock<'_>) -> LR<R>,
    ) -> PyResult<R> {
        let host = Host::new();
        let Self { book, clock, carriers, .. } = self;
        let mut now = || -> LR<String> {
            let dt = clock.bind(py).call_method0("now_utc").map_err(|e| host.fail(e))?;
            let text = carriers.bind(py).call_method1("iso", (&dt,)).map_err(|e| host.fail(e))?;
            text.extract::<String>().map_err(|e| host.fail(e))
        };
        let r = f(book, &mut now);
        match r {
            Ok(v) => Ok(v),
            Err(e) if e.kind == "host" => Err(host.take()),
            Err(e) => Err(refusal(py, e)),
        }
    }

    /// `_hold`: keep the `VenueOrder` carrier for a book order, once.
    fn hold(&mut self, _py: Python<'_>, order: &Bound<'_, PyAny>) -> PyResult<()> {
        let id: String = order.getattr("venue_order_id")?.extract()?;
        if !self.orders.contains_key(&id) && self.book.state_of(&id).is_some() {
            self.orders.insert(id, order.clone().unbind());
        }
        Ok(())
    }

    /// `_sync_fills`: build `VenueFill` carriers for every book fill not yet held. A fill
    /// with `src` is a restored carrier handed back as-is (`restored[src]`).
    fn sync_fills(&mut self, py: Python<'_>, restored: &[Py<PyAny>]) -> PyResult<()> {
        let Self { book, fills, orders, carriers, .. } = self;
        while fills.len() < book.fills.len() {
            let i = fills.len();
            let f = &book.fills[i];
            if let Some(src) = f.src {
                fills.push(restored[src].clone_ref(py));
                continue;
            }
            let order = orders.get(&f.order_id).expect("a fill's order is held");
            let ctx = ctx(py)?;
            let filled_at = carriers.bind(py).call_method1("dt", (f.filled_at.iso.as_str(),))?;
            let quantity = ctx.decimal.bind(py).call1((f.quantity.canon(),))?;
            let price = ctx.decimal.bind(py).call1((f.price.canon(),))?;
            let instrument = order.bind(py).getattr("instrument")?;
            let side_ = order.bind(py).getattr("side")?;
            let carrier = ctx
                .venue_fill
                .bind(py)
                .call1((
                    f.fill_id.as_str(),
                    f.order_id.as_str(),
                    instrument,
                    quantity,
                    price,
                    filled_at,
                    side_,
                ))?
                .unbind();
            fills.push(carrier);
        }
        Ok(())
    }

    /// `_ack`: a `VenueAck` carrier from a book ack.
    fn ack_carrier(&self, py: Python<'_>, a: AckT) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let (venue_order_id, status, timestamp, message) = a;
        let dt = self.carriers.bind(py).call_method1("dt", (timestamp,))?;
        Ok(ctx
            .venue_ack
            .bind(py)
            .call1((venue_order_id, status, dt, message))?
            .unbind())
    }
}

#[pymethods]
impl SimBrokerBase {
    #[new]
    fn new(
        py: Python<'_>,
        account_id: &str,
        clock: &Bound<'_, PyAny>,
        slippage_bps: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let ctx = ctx(py)?;
        let is_decimal = slippage_bps.is_instance(&ctx.decimal.bind(py))?;
        let slippage_text = if is_decimal {
            slippage_bps.str()?.to_str()?.to_string()
        } else {
            "0".to_string()
        };
        let slippage = dec(&slippage_text).map_err(|e| refusal(py, e))?;
        let book = Book::new(account_id, is_decimal, slippage).map_err(|e| refusal(py, e))?;
        let carriers = ctx.carriers.bind(py).call0()?.unbind();
        Ok(SimBrokerBase {
            book,
            account_id: account_id.to_string(),
            name: "SimBroker".to_string(),
            env: "sim".to_string(),
            clock: clock.clone().unbind(),
            carriers,
            orders: HashMap::new(),
            fills: Vec::new(),
        })
    }

    fn connect(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let ts = self.run_book(py, |book, clock| book.connect(clock))?;
        let dt = self.carriers.bind(py).call_method1("dt", (ts.iso.as_str(),))?;
        Ok(ctx
            .venue_identity
            .bind(py)
            .call1((
                self.account_id.as_str(),
                self.env.as_str(),
                dt,
                self.name.as_str(),
            ))?
            .unbind())
    }

    fn restore(
        &mut self,
        py: Python<'_>,
        orders: &Bound<'_, PyAny>,
        fills: &Bound<'_, PyAny>,
        positions: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        // `orders` is a list of `(VenueOrder, OrderState)`; parents restore last.
        let mut order_pairs: Vec<(Py<PyAny>, String)> = Vec::new();
        for item in orders.try_iter()? {
            let item = item?;
            let order = item.get_item(0)?;
            let state: String = item.get_item(1)?.getattr("value")?.extract()?;
            order_pairs.push((order.unbind(), state));
        }
        order_pairs.sort_by_key(|(o, _)| {
            let parent = o.bind(py).getattr("parent_order_id").ok();
            parent.is_some_and(|p| !p.is_none())
        });

        let mut fill_carriers: Vec<Py<PyAny>> = Vec::new();
        let mut fill_vos: Vec<VFill> = Vec::new();
        for (i, item) in fills.try_iter()?.enumerate() {
            let f = item?;
            let fill_id: String = f.getattr("venue_fill_id")?.extract()?;
            let order_id: String = f.getattr("venue_order_id")?.extract()?;
            let instr_text: String = self
                .carriers
                .bind(py)
                .call_method1("text", (f.getattr("instrument")?,))?
                .extract()?;
            let qty = f.getattr("quantity")?.str()?.to_str()?.to_string();
            let price = f.getattr("price")?.str()?.to_str()?.to_string();
            let filled_at: String = self
                .carriers
                .bind(py)
                .call_method1("iso", (f.getattr("filled_at")?,))?
                .extract()?;
            let side_value: String = f.getattr("side")?.getattr("value")?.extract()?;
            fill_carriers.push(f.clone().unbind());
            fill_vos.push(VFill {
                fill_id,
                order_id,
                instr: instrument(&instr_text).map_err(|e| refusal(py, e))?,
                quantity: dec(&qty).map_err(|e| refusal(py, e))?,
                price: dec(&price).map_err(|e| refusal(py, e))?,
                filled_at: Ts::aware(&filled_at, "filled_at").map_err(|e| refusal(py, e))?,
                side: side(&side_value).map_err(|e| refusal(py, e))?,
                src: Some(i),
            });
        }

        let mut pos_vos: Vec<Pos> = Vec::new();
        for item in positions.try_iter()? {
            let p = item?;
            let instr_text: String = self
                .carriers
                .bind(py)
                .call_method1("text", (p.getattr("instrument")?,))?
                .extract()?;
            let qty = p.getattr("quantity")?.str()?.to_str()?.to_string();
            let avg = p.getattr("avg_price")?.str()?.to_str()?.to_string();
            let as_of: String = self
                .carriers
                .bind(py)
                .call_method1("iso", (p.getattr("as_of")?,))?
                .extract()?;
            pos_vos.push(Pos {
                instr: instrument(&instr_text).map_err(|e| refusal(py, e))?,
                qty: dec(&qty).map_err(|e| refusal(py, e))?,
                avg: dec(&avg).map_err(|e| refusal(py, e))?,
                as_of: Ts::aware(&as_of, "as_of").map_err(|e| refusal(py, e))?,
            });
        }

        let mut vos: Vec<(VOrder, OrderState)> = Vec::new();
        for (o, state) in &order_pairs {
            let ot = order_tuple(py, &self.carriers, &o.bind(py))?;
            let st = OrderState::parse(state)
                .map_or_else(|| err("value", format!("bad state {state:?}")), Ok)
                .map_err(|e| refusal(py, e))?;
            vos.push((vorder(&ot).map_err(|e| refusal(py, e))?, st));
        }

        self.run_book(py, |book, clock| book.restore(vos, fill_vos, pos_vos, clock))?;

        for (o, _) in &order_pairs {
            self.hold(py, &o.bind(py))?;
        }
        self.sync_fills(py, &fill_carriers)?;
        Ok(())
    }

    fn submit(&mut self, py: Python<'_>, order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let ot = order_tuple(py, &self.carriers, order)?;
        let a = self.run_book(py, |book, clock| book.submit(vorder(&ot)?, clock))?;
        self.hold(py, order)?;
        self.sync_fills(py, &[])?;
        Ok(self.ack_carrier(py, ack(a))?)
    }

    fn cancel(&mut self, py: Python<'_>, venue_order_id: &str) -> PyResult<Py<PyAny>> {
        let a = self.run_book(py, |book, clock| book.cancel(venue_order_id, clock))?;
        Ok(self.ack_carrier(py, ack(a))?)
    }

    fn replace(
        &mut self,
        py: Python<'_>,
        venue_order_id: &str,
        changes: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let new_quantity = changes.getattr("new_quantity")?;
        let new_limit = changes.getattr("new_limit_price")?;
        let new_stop = changes.getattr("new_stop_price")?;
        let new_quantity_opt = opt_str(&new_quantity)?;

        let begin = self.run_book(py, |book, clock| {
            book.replace_begin(venue_order_id, opt_dec(&new_quantity_opt)?, clock)
        })?;
        match begin {
            sb::Begin::Done(a) => Ok(self.ack_carrier(py, ack(a))?),
            sb::Begin::Go(_qty) => {
                let current = self
                    .orders
                    .get(venue_order_id)
                    .expect("replace_begin requires a held order")
                    .bind(py)
                    .clone();
                let updated = match self.build_updated(
                    py,
                    &current,
                    &new_quantity,
                    &new_limit,
                    &new_stop,
                ) {
                    Ok(u) => u,
                    Err(e) if e.is_instance_of::<PyValueError>(py) => {
                        let msg = e.value(py).str()?.to_str()?.to_string();
                        let a = self.run_book(py, |book, clock| {
                            book.replace_reject(venue_order_id, msg, clock)
                        })?;
                        return Ok(self.ack_carrier(py, ack(a))?);
                    }
                    Err(e) => return Err(e),
                };
                self.orders
                    .insert(venue_order_id.to_string(), updated.clone_ref(py));
                let ot = order_tuple(py, &self.carriers, &updated.bind(py))?;
                let a = self.run_book(py, |book, clock| {
                    book.replace_commit(venue_order_id, vorder(&ot)?, clock)
                })?;
                Ok(self.ack_carrier(py, ack(a))?)
            }
        }
    }

    fn orders(&mut self, py: Python<'_>, since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let since_iso = since.call_method0("isoformat")?.extract::<String>()?;
        let rows = self.run_book(py, |book, clock| book.orders_since(&since_iso, clock))?;
        let list = PyList::empty(py);
        for (id, st, filled, remaining, updated_at) in rows {
            let state = ctx.order_state.bind(py).call1((st.value(),))?;
            let filled_d = ctx.decimal.bind(py).call1((filled.canon(),))?;
            let remaining_d = ctx.decimal.bind(py).call1((remaining.canon(),))?;
            let dt = self
                .carriers
                .bind(py)
                .call_method1("dt", (updated_at.iso.as_str(),))?;
            list.append(
                ctx.venue_order_state
                    .bind(py)
                    .call1((id, state, filled_d, remaining_d, dt))?,
            )?;
        }
        Ok(list.into_any().unbind())
    }

    fn fills(&mut self, py: Python<'_>, since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let since_iso = since.call_method0("isoformat")?.extract::<String>()?;
        let indices = self.run_book(py, |book, clock| book.fills_since(&since_iso, clock))?;
        let list = PyList::empty(py);
        for i in indices {
            list.append(self.fills[i].clone_ref(py))?;
        }
        Ok(list.into_any().unbind())
    }

    fn positions(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let rows = self.run_book(py, |book, clock| book.positions(clock))?;
        let list = PyList::empty(py);
        for p in rows {
            let instrument = self
                .carriers
                .bind(py)
                .call_method1("instrument", (p.instr.hk(),))?;
            let qty = ctx.decimal.bind(py).call1((p.qty.canon(),))?;
            let avg = ctx.decimal.bind(py).call1((p.avg.canon(),))?;
            let dt = self.carriers.bind(py).call_method1("dt", (p.as_of.iso.as_str(),))?;
            list.append(
                ctx.venue_position
                    .bind(py)
                    .call1((instrument, qty, avg, dt))?,
            )?;
        }
        Ok(list.into_any().unbind())
    }

    fn cash_events(&mut self, py: Python<'_>, since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let since_iso = since.call_method0("isoformat")?.extract::<String>()?;
        self.run_book(py, |book, _clock| book.cash_events(&since_iso))?;
        Ok(PyList::empty(py).into_any().unbind())
    }

    fn accounts(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let confirmed_at = self.clock.bind(py).call_method0("now_utc")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("env", self.env.as_str())?;
        kwargs.set_item("source", "sim")?;
        kwargs.set_item("confirmed_at", &confirmed_at)?;
        let record = ctx.account_record.bind(py).call_method(
            "of",
            ("sim", self.account_id.as_str()),
            Some(&kwargs),
        )?;
        let records = PyTuple::new(py, [record])?;
        Ok(ctx
            .account_snapshot
            .bind(py)
            .call1((records, &confirmed_at))?
            .unbind())
    }

    fn account_events(&self, py: Python<'_>, _since: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Ok(PyList::empty(py).into_any().unbind())
    }

    #[pyo3(signature = (bar=None))]
    fn process_bar(
        &mut self,
        py: Python<'_>,
        bar: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let crossing: Option<BarT> = match bar {
            None => None,
            Some(b) => Some((
                self.carriers
                    .bind(py)
                    .call_method1("text", (b.getattr("instrument")?,))?
                    .extract()?,
                self.carriers
                    .bind(py)
                    .call_method1("iso", (b.getattr("timestamp")?,))?
                    .extract()?,
                b.getattr("open")?.str()?.to_str()?.to_string(),
                b.getattr("high")?.str()?.to_str()?.to_string(),
                b.getattr("low")?.str()?.to_str()?.to_string(),
                b.getattr("close")?.str()?.to_str()?.to_string(),
                b.getattr("volume")?.str()?.to_str()?.to_string(),
                self.carriers
                    .bind(py)
                    .call_method1("iso", (b.getattr("as_of")?,))?
                    .extract()?,
            )),
        };
        let indices = self.run_book(py, move |book, _clock| match crossing {
            None => book.process_bar(None),
            Some(t) => book.process_bar(Some(crate::sim::bar(&t)?)),
        })?;
        self.sync_fills(py, &[])?;
        let fills = PyTuple::new(py, indices.iter().map(|&i| self.fills[i].clone_ref(py)))?;
        Ok(fills.into_any().unbind())
    }
}

impl SimBrokerBase {
    /// `dataclasses.replace` the replacement order; a `ValueError` (the constructor's own
    /// refusal) becomes the reject message.
    fn build_updated(
        &self,
        py: Python<'_>,
        current: &Bound<'_, PyAny>,
        new_quantity: &Bound<'_, PyAny>,
        new_limit: &Bound<'_, PyAny>,
        new_stop: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let kwargs = PyDict::new(py);
        if new_quantity.is_none() {
            kwargs.set_item("quantity", current.getattr("quantity")?)?;
        } else {
            kwargs.set_item("quantity", new_quantity.clone())?;
        }
        if new_limit.is_none() {
            kwargs.set_item("limit_price", current.getattr("limit_price")?)?;
        } else {
            kwargs.set_item("limit_price", new_limit.clone())?;
        }
        if new_stop.is_none() {
            kwargs.set_item("stop_price", current.getattr("stop_price")?)?;
        } else {
            kwargs.set_item("stop_price", new_stop.clone())?;
        }
        if new_quantity.is_none() {
            kwargs.set_item("allocations", current.getattr("allocations")?)?;
        } else {
            let alloc = current.getattr("allocations")?.get_item(0)?;
            let new_alloc = ctx.venue_order_alloc.bind(py).call1((
                alloc.getattr("strategy_order_id")?,
                alloc.getattr("account_id")?,
                new_quantity.clone(),
            ))?;
            let allocs = PyTuple::new(py, [new_alloc])?;
            kwargs.set_item("allocations", allocs)?;
        }
        let updated = ctx
            .replace
            .bind(py)
            .call((current.clone(),), Some(&kwargs))?;
        Ok(updated.unbind())
    }
}
