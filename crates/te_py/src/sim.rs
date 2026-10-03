//! P3a: the simulated venues' rules (`te_core::sim`) and the option-risk rules
//! (`te_core::risk_options`), as the Python shims reach them. Plain values cross; a
//! refusal crosses as `ValueError((kind, message))`; an exception a host callback raised
//! (the clock, a quote lookup) is re-raised as itself.

use std::cell::RefCell;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::ledger::bridge as lb;
use te_core::ledger::model::{err, Instrument, LErr, Obj, OrderState, OrderType, Side, Tif, R as LR};
use te_core::ledger::pydec::PyDec;
use te_core::sim::broker::{self as sb, Alloc, Begin, Book, VFill, VOrder};
use te_core::sim::snapshot::{self as ss, Origin, Quote, SFill, Snap, Venue};
use te_core::sim::trailing as tr;
use te_core::sim::{Clock, Ts};

pub(crate) fn refuse(e: LErr) -> PyErr {
    PyValueError::new_err((e.kind.to_string(), e.msg))
}

/// A host callback's exception, held while te_core unwinds with kind `host`.
pub(crate) struct Host {
    stash: RefCell<Option<PyErr>>,
}

impl Host {
    pub(crate) fn new() -> Host {
        Host { stash: RefCell::new(None) }
    }

    pub(crate) fn fail(&self, e: PyErr) -> LErr {
        *self.stash.borrow_mut() = Some(e);
        LErr { kind: "host", msg: String::new() }
    }

    pub(crate) fn has(&self) -> bool {
        self.stash.borrow().is_some()
    }

    pub(crate) fn take(&self) -> PyErr {
        self.stash.borrow_mut().take().unwrap_or_else(|| PyValueError::new_err("host"))
    }

    pub(crate) fn finish<T>(self, r: LR<T>) -> PyResult<T> {
        match r {
            Ok(v) => Ok(v),
            Err(e) if e.kind == "host" => Err(self.stash.into_inner().unwrap_or_else(|| refuse(e))),
            Err(e) => Err(refuse(e)),
        }
    }
}

/// Run `f` with a clock that calls the host's `now()` (an `isoformat()` string).
fn with_clock<T>(now: &Bound<'_, PyAny>, f: impl FnOnce(&mut Clock<'_>) -> LR<T>) -> PyResult<T> {
    let host = Host::new();
    let r = {
        let mut clock = || -> LR<String> {
            match now.call0().and_then(|v| v.extract::<String>()) {
                Ok(s) => Ok(s),
                Err(e) => Err(host.fail(e)),
            }
        };
        f(&mut clock)
    };
    host.finish(r)
}

pub(crate) fn dec(s: &str) -> LR<PyDec> {
    match PyDec::parse(s) {
        Some(d) => Ok(d),
        None => err("value", format!("not a Decimal: {s:?}")),
    }
}

fn opt_dec(s: &Option<String>) -> LR<Option<PyDec>> {
    s.as_deref().map(dec).transpose()
}

pub(crate) fn instrument(text: &str) -> LR<Instrument> {
    match lb::obj_from_text(text)? {
        Obj::Instr(i) => Ok(i),
        _ => err("value", "expected an instrument"),
    }
}

fn side(s: &str) -> LR<Side> {
    Side::parse(s).map_or_else(|| err("value", format!("bad side {s:?}")), Ok)
}

type OrderT = (
    String,
    String,
    String,
    String,
    String,
    String,
    String,
    (Option<String>, Option<String>, Option<String>),
    Vec<(String, String, String)>,
    Option<String>,
    Option<String>,
);

fn vorder(t: &OrderT) -> LR<VOrder> {
    let (id, instr, otype, sd, qty, submitted, tif, (limit, stop, trail), allocs, parent, oco) = t;
    let mut a = Vec::new();
    for (soid, account, q) in allocs {
        a.push(Alloc { soid: soid.clone(), account: account.clone(), qty: dec(q)? });
    }
    Ok(VOrder {
        id: id.clone(),
        instr: instrument(instr)?,
        otype: OrderType::parse(otype).map_or_else(|| err("value", format!("bad order type {otype:?}")), Ok)?,
        side: side(sd)?,
        quantity: dec(qty)?,
        submitted_at: Ts::aware(submitted, "submitted_at")?,
        tif: Tif::parse(tif).map_or_else(|| err("value", format!("bad time in force {tif:?}")), Ok)?,
        limit: opt_dec(limit)?,
        stop: opt_dec(stop)?,
        trail: opt_dec(trail)?,
        allocs: a,
        parent: parent.clone(),
        oco: oco.clone(),
    })
}

type AckT = (String, &'static str, String, Option<String>);

fn ack(a: sb::Ack) -> AckT {
    (a.id, a.status, a.ts.iso, a.msg)
}

type BarT = (String, String, String, String, String, String, String, String);

fn bar(t: &BarT) -> LR<sb::Bar> {
    let (instr, ts, o, h, l, c, v, as_of) = t;
    Ok(sb::Bar {
        instr: instrument(instr)?,
        ts: Ts::aware(ts, "Bar timestamp")?,
        open: dec(o)?,
        high: dec(h)?,
        low: dec(l)?,
        close: dec(c)?,
        volume: dec(v)?,
        as_of: Ts::aware(as_of, "Bar as_of")?,
    })
}

/// `SimBroker`'s book: every rule the Python adapter ran, the adapter keeping only its
/// carriers (see `trade_engine.sim.broker`).
#[pyclass(module = "trade_engine_rs")]
pub(crate) struct SimBook {
    book: Book,
}

type PosT = (String, String, String, String);

#[pymethods]
impl SimBook {
    #[new]
    fn new(account_id: &str, is_decimal: bool, slippage_bps: &str) -> PyResult<SimBook> {
        let r = || -> LR<Book> { Book::new(account_id, is_decimal, dec(slippage_bps)?) };
        r().map(|book| SimBook { book }).map_err(refuse)
    }

    fn connect(&mut self, now: &Bound<'_, PyAny>) -> PyResult<String> {
        with_clock(now, |c| self.book.connect(c)).map(|t| t.iso)
    }

    /// `orders`: `(order, state)`; `fills`: `(fill_id, order_id, instrument, quantity,
    /// price, filled_at, side)`; `positions`: `(instrument, quantity, avg, as_of)`.
    fn restore(
        &mut self,
        orders: Vec<(OrderT, String)>,
        fills: Vec<(String, String, String, String, String, String, String)>,
        positions: Vec<PosT>,
        now: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let book = &mut self.book;
        with_clock(now, |c| {
            let mut os = Vec::new();
            for (t, state) in &orders {
                let st = OrderState::parse(state).map_or_else(|| err("value", format!("bad state {state:?}")), Ok)?;
                os.push((vorder(t)?, st));
            }
            let mut fs = Vec::new();
            for (i, (fid, oid, instr, q, p, at, sd)) in fills.iter().enumerate() {
                fs.push(VFill {
                    fill_id: fid.clone(),
                    order_id: oid.clone(),
                    instr: instrument(instr)?,
                    quantity: dec(q)?,
                    price: dec(p)?,
                    filled_at: Ts::aware(at, "filled_at")?,
                    side: side(sd)?,
                    src: Some(i),
                });
            }
            let mut ps = Vec::new();
            for (instr, q, avg, as_of) in &positions {
                ps.push(sb::Pos { instr: instrument(instr)?, qty: dec(q)?, avg: dec(avg)?, as_of: Ts::aware(as_of, "as_of")? });
            }
            book.restore(os, fs, ps, c)
        })
    }

    fn submit(&mut self, order: OrderT, now: &Bound<'_, PyAny>) -> PyResult<AckT> {
        let book = &mut self.book;
        with_clock(now, |c| book.submit(vorder(&order)?, c)).map(ack)
    }

    fn cancel(&mut self, id: &str, now: &Bound<'_, PyAny>) -> PyResult<AckT> {
        with_clock(now, |c| self.book.cancel(id, c)).map(ack)
    }

    /// `("ack", ack)` or `("go", quantity)`.
    #[pyo3(signature = (id, new_quantity, now))]
    fn replace_begin(
        &mut self,
        py: Python<'_>,
        id: &str,
        new_quantity: Option<String>,
        now: &Bound<'_, PyAny>,
    ) -> PyResult<(&'static str, PyObject)> {
        let book = &mut self.book;
        let r = with_clock(now, |c| book.replace_begin(id, opt_dec(&new_quantity)?, c))?;
        Ok(match r {
            Begin::Done(a) => ("ack", ack(a).into_pyobject(py)?.into_any().unbind()),
            Begin::Go(q) => ("go", q.to_py_string().into_pyobject(py)?.into_any().unbind()),
        })
    }

    fn replace_reject(&mut self, id: &str, msg: String, now: &Bound<'_, PyAny>) -> PyResult<AckT> {
        with_clock(now, |c| self.book.replace_reject(id, msg, c)).map(ack)
    }

    fn replace_commit(&mut self, id: &str, order: OrderT, now: &Bound<'_, PyAny>) -> PyResult<AckT> {
        let book = &mut self.book;
        with_clock(now, |c| book.replace_commit(id, vorder(&order)?, c)).map(ack)
    }

    fn orders(&mut self, since: &str, now: &Bound<'_, PyAny>) -> PyResult<Vec<(String, &'static str, String, String, String)>> {
        let rows = with_clock(now, |c| self.book.orders_since(since, c))?;
        Ok(rows
            .into_iter()
            .map(|(id, st, filled, rem, at)| (id, st.value(), filled.to_py_string(), rem.to_py_string(), at.iso))
            .collect())
    }

    fn fills(&mut self, since: &str, now: &Bound<'_, PyAny>) -> PyResult<Vec<usize>> {
        with_clock(now, |c| self.book.fills_since(since, c))
    }

    fn has(&self, id: &str) -> bool {
        self.book.state_of(id).is_some()
    }

    fn fill_count(&self) -> usize {
        self.book.fills.len()
    }

    /// `(fill_id, order_id, quantity, price, filled_at, side, src)`.
    fn fill(&self, i: usize) -> (String, String, String, String, String, &'static str, Option<usize>) {
        let f = &self.book.fills[i];
        (
            f.fill_id.clone(),
            f.order_id.clone(),
            f.quantity.to_py_string(),
            f.price.to_py_string(),
            f.filled_at.iso.clone(),
            f.side.value(),
            f.src,
        )
    }

    /// `(instrument hash key, quantity, avg, as_of)` by symbol.
    fn positions(&mut self, now: &Bound<'_, PyAny>) -> PyResult<Vec<PosT>> {
        let rows = with_clock(now, |c| self.book.positions(c))?;
        Ok(rows.into_iter().map(|p| (p.instr.hk(), p.qty.to_py_string(), p.avg.to_py_string(), p.as_of.iso)).collect())
    }

    fn cash_events(&self, since: &str) -> PyResult<()> {
        self.book.cash_events(since).map_err(refuse)
    }

    #[pyo3(signature = (b))]
    fn process_bar(&mut self, b: Option<BarT>) -> PyResult<Vec<usize>> {
        let mut run = || -> LR<Vec<usize>> {
            let parsed = match &b {
                None => None,
                Some(t) => Some(bar(t)?),
            };
            self.book.process_bar(parsed)
        };
        run().map_err(refuse)
    }
}

/// An instrument's hash key: the shim maps a returned position back to its object.
#[pyfunction]
pub(crate) fn sim_instrument_key(text: &str) -> PyResult<String> {
    instrument(text).map(|i| i.hk()).map_err(refuse)
}


// --- snapshot venue --------------------------------------------------------------------

/// `SnapshotVenue`'s book (see `trade_engine.sim.snapshot_venue`).
#[pyclass(module = "trade_engine_rs")]
pub(crate) struct SnapBook {
    venue: Venue,
}

type SFillIn = (String, String, String, String, String, String, String, String, Option<String>);
type SFillOut = (String, String, String, String, String, &'static str, String, Option<String>, Option<usize>, Option<usize>);
type Origins = Vec<((&'static str, usize), String)>;

#[pymethods]
impl SnapBook {
    /// A `None` decimal was not a `Decimal`; a `None` age not an int or float.
    #[new]
    #[pyo3(signature = (account_id, fill_fraction, fee_per_contract, equity_slippage_bps, max_quote_age_seconds))]
    fn new(
        account_id: &str,
        fill_fraction: Option<String>,
        fee_per_contract: Option<String>,
        equity_slippage_bps: Option<String>,
        max_quote_age_seconds: Option<f64>,
    ) -> PyResult<SnapBook> {
        let r = || -> LR<Venue> {
            Venue::new(
                account_id,
                opt_dec(&fill_fraction)?,
                opt_dec(&fee_per_contract)?,
                opt_dec(&equity_slippage_bps)?,
                max_quote_age_seconds,
            )
        };
        r().map(|venue| SnapBook { venue }).map_err(refuse)
    }

    fn connect(&mut self, now: &Bound<'_, PyAny>) -> PyResult<String> {
        with_clock(now, |c| self.venue.connect(c)).map(|t| t.iso)
    }

    /// `fills`: `(fill_id, order_id, instrument, quantity, price, filled_at, side, fee,
    /// leg_id)`; `positions`: `(instrument, quantity)`.
    fn restore(&mut self, orders: Vec<(OrderT, String)>, fills: Vec<SFillIn>, positions: Vec<(String, String)>) -> PyResult<()> {
        let mut r = || -> LR<()> {
            let mut os = Vec::new();
            for (t, state) in &orders {
                let st = OrderState::parse(state).map_or_else(|| err("value", format!("bad state {state:?}")), Ok)?;
                os.push((vorder(t)?, st));
            }
            let mut fs = Vec::new();
            for (i, (fid, oid, instr, q, p, at, sd, fee, leg_id)) in fills.iter().enumerate() {
                fs.push(SFill {
                    fill_id: fid.clone(),
                    order_id: oid.clone(),
                    instr: instrument(instr)?,
                    quantity: dec(q)?,
                    price: dec(p)?,
                    filled_at: Ts::aware(at, "filled_at")?,
                    side: side(sd)?,
                    fee: dec(fee)?,
                    leg_id: leg_id.clone(),
                    leg: None,
                    src: Some(i),
                });
            }
            let mut ps = Vec::new();
            for (instr, q) in &positions {
                ps.push((instrument(instr)?, dec(q)?));
            }
            self.venue.restore(os, fs, ps)
        };
        r().map_err(refuse)
    }

    fn submit(&mut self, order: OrderT, now: &Bound<'_, PyAny>) -> PyResult<AckT> {
        let v = &mut self.venue;
        with_clock(now, |c| v.submit(vorder(&order)?, c)).map(ack)
    }

    fn cancel(&mut self, id: &str, now: &Bound<'_, PyAny>) -> PyResult<AckT> {
        with_clock(now, |c| self.venue.cancel(id, c)).map(ack)
    }

    fn replace(&mut self, id: &str, now: &Bound<'_, PyAny>) -> PyResult<AckT> {
        with_clock(now, |c| self.venue.replace(id, c)).map(ack)
    }

    fn orders(&mut self, since: &str, now: &Bound<'_, PyAny>) -> PyResult<Vec<(String, &'static str, String, String, String)>> {
        let rows = with_clock(now, |c| self.venue.orders_since(since, c))?;
        Ok(rows
            .into_iter()
            .map(|(id, st, filled, rem, at)| (id, st.value(), filled.to_py_string(), rem.to_py_string(), at.iso))
            .collect())
    }

    fn fills(&mut self, since: &str, now: &Bound<'_, PyAny>) -> PyResult<Vec<usize>> {
        with_clock(now, |c| self.venue.fills_since(since, c))
    }

    fn has(&self, id: &str) -> bool {
        self.venue.has(id)
    }

    /// `model_price`: what `side` of an instrument trades at in the snapshot, or None.
    fn model_price(
        &self,
        instr: &str,
        side_: &str,
        underlying: String,
        as_of: &str,
        underlying_price: &str,
        quote: &Bound<'_, PyAny>,
    ) -> PyResult<Option<String>> {
        let host = Host::new();
        let v = &self.venue;
        let r = (|| -> LR<Option<PyDec>> {
            let mut lookup = |occ: &str| -> LR<Option<Quote>> { lookup_quote(quote, occ, &host) };
            let mut s = Snap {
                underlying,
                as_of: Ts::aware(as_of, "Snapshot as_of")?,
                underlying_price: dec(underlying_price)?,
                quote: &mut lookup,
            };
            v.model_price(&instrument(instr)?, side(side_)?, &mut s)
        })();
        match r {
            Ok(p) => Ok(p.map(|d| d.to_py_string())),
            Err(_) if host.has() => Err(host.take()),
            Err(e) => Err(refuse(e)),
        }
    }

    fn fill_count(&self) -> usize {
        self.venue.fills.len()
    }

    /// `(fill_id, order_id, quantity, price, filled_at, side, fee, leg_id, leg, src)`.
    fn fill(&self, i: usize) -> SFillOut {
        let f = &self.venue.fills[i];
        (
            f.fill_id.clone(),
            f.order_id.clone(),
            f.quantity.to_py_string(),
            f.price.to_py_string(),
            f.filled_at.iso.clone(),
            f.side.value(),
            f.fee.to_py_string(),
            f.leg_id.clone(),
            f.leg,
            f.src,
        )
    }

    /// `(as_of, [(("restored"|"fill", index), quantity)])` by symbol.
    fn positions(&self, now: &Bound<'_, PyAny>) -> PyResult<(String, Origins)> {
        let (at, rows) = with_clock(now, |c| self.venue.positions(c))?;
        let rows = rows
            .into_iter()
            .map(|(o, q)| {
                let origin = match o {
                    Origin::Restored(i) => ("restored", i),
                    Origin::Fill(i) => ("fill", i),
                };
                (origin, q.to_py_string())
            })
            .collect();
        Ok((at.iso, rows))
    }

    /// `quote(occ)` is `None` or `(mid, spread, as_of)`; the indices of the fills made.
    fn process_snapshot(
        &mut self,
        underlying: String,
        as_of: &str,
        underlying_price: &str,
        quote: &Bound<'_, PyAny>,
        now: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<usize>> {
        let host = Host::new();
        let v = &mut self.venue;
        let r = with_clock(now, |c| {
            let mut lookup = |occ: &str| -> LR<Option<Quote>> { lookup_quote(quote, occ, &host) };
            let mut s = Snap {
                underlying,
                as_of: Ts::aware(as_of, "Snapshot as_of")?,
                underlying_price: dec(underlying_price)?,
                quote: &mut lookup,
            };
            v.process_snapshot(&mut s, c)
        });
        match r {
            Err(_) if host.has() => Err(host.take()),
            other => other,
        }
    }
}

fn lookup_quote(quote: &Bound<'_, PyAny>, occ: &str, host: &Host) -> LR<Option<Quote>> {
    let got = quote.call1((occ,)).and_then(|o| o.extract::<Option<(String, String, String)>>());
    match got {
        Err(e) => Err(host.fail(e)),
        Ok(None) => Ok(None),
        Ok(Some((mid, spread, at))) => {
            Ok(Some(Quote { mid: dec(&mid)?, spread: dec(&spread)?, as_of: Ts::aware(&at, "OptionQuote as_of")? }))
        }
    }
}

/// The underlying whose chain snapshot prices an instrument.
#[pyfunction]
pub(crate) fn sim_underlying_of(text: &str) -> PyResult<String> {
    let r = || ss::underlying_of(&instrument(text)?);
    r().map_err(refuse)
}

// --- trailing --------------------------------------------------------------------------

type TrailT = (Option<String>, Option<String>, bool);

#[pyfunction]
pub(crate) fn trail_check_amount(trail_amount: &str) -> PyResult<()> {
    let r = || tr::check_trail_amount(&dec(trail_amount)?);
    r().map_err(refuse)
}

/// One `update(price)`: `(triggered, (extreme, stop_price, triggered), error)`. The state
/// comes back even when the update refused, since it may have moved first.
#[pyfunction]
pub(crate) fn trail_update(
    side_: &str,
    trail_amount: &str,
    state: TrailT,
    price: &str,
) -> PyResult<(Option<bool>, TrailT, Option<(String, String)>)> {
    let setup = || -> LR<(tr::Trail, PyDec)> {
        let (extreme, stop, triggered) = &state;
        Ok((
            tr::Trail {
                side: side(side_)?,
                trail_amount: dec(trail_amount)?,
                extreme: opt_dec(extreme)?,
                stop_price: opt_dec(stop)?,
                triggered: *triggered,
            },
            dec(price)?,
        ))
    };
    let (mut t, p) = setup().map_err(refuse)?;
    let r = tr::update(&mut t, &p);
    let out = (t.extreme.map(|d| d.to_py_string()), t.stop_price.map(|d| d.to_py_string()), t.triggered);
    Ok(match r {
        Ok(b) => (Some(b), out, None),
        Err(e) => (None, out, Some((e.kind.to_string(), e.msg))),
    })
}

// --- open structures / uncovered calls (oms/options.py) --------------------------------

type OpenT = (String, String, Vec<String>, String, String, usize, Option<String>, Option<String>);

/// `open_structures(state)`: per structure, `(entry_order_id, command_id, [open qty per
/// leg], units, entry_price, opened_fill_index, target_order_id, closing_order_id)`.
#[pyfunction]
pub(crate) fn oms_open_structures(state: &str) -> PyResult<Vec<OpenT>> {
    let r = || -> LR<Vec<OpenT>> {
        let st = lb::account_from_text(state)?;
        Ok(te_core::oms::structures::open_structures(&st)?
            .into_iter()
            .map(|o| {
                (
                    o.entry_order_id,
                    o.command_id,
                    o.open_quantities.iter().map(|q| q.to_py_string()).collect(),
                    o.units.to_py_string(),
                    o.entry_price.to_py_string(),
                    o.opened_fill,
                    o.target_order_id,
                    o.closing_order_id,
                )
            })
            .collect())
    };
    r().map_err(refuse)
}

/// `uncovered_calls(state, closing_counts=...)`: `[(underlying, uncovered, free)]`, ascending.
#[pyfunction]
pub(crate) fn oms_uncovered_calls(state: &str, closing_counts: bool) -> PyResult<Vec<(String, String, String)>> {
    let r = || -> LR<Vec<(String, String, String)>> {
        let st = lb::account_from_text(state)?;
        Ok(te_core::oms::structures::uncovered_calls(&st, closing_counts)?
            .into_iter()
            .map(|(u, a, b)| (u, a.to_py_string(), b.to_py_string()))
            .collect())
    };
    r().map_err(refuse)
}

// --- option risk (was risk_options.py) ------------------------------------------------

use te_core::risk_options as ro;

/// `EntryQuoteRules`: delta pair, min bid, bid-return pair, min iv, min open interest,
/// max leg spread, min underlying price, min credit/width, min credit return, max friction.
type EntryQuoteT = (
    Option<(String, String)>,
    Option<String>,
    Option<(String, String)>,
    Option<String>,
    Option<i128>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);
/// `OptionRiskRules`, in field order.
type RulesT = (
    String,
    Vec<String>,
    bool,
    Option<String>,
    Option<String>,
    Option<Vec<(String, String)>>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<EntryQuoteT>,
);
/// (occ, bid, ask, implied vol, str(delta), open interest)
type QuoteT = (String, String, String, Option<String>, Option<String>, Option<i128>);
/// (underlying, underlying price, live, quotes)
type SnapT = (String, String, bool, Vec<QuoteT>);
/// (name, passed, measured is a Decimal, measured, threshold is a Decimal, threshold, reason)
type ResultT = (String, bool, bool, String, bool, String, String);

fn pair(p: &Option<(String, String)>) -> LR<Option<(PyDec, PyDec)>> {
    match p {
        Some((a, b)) => Ok(Some((dec(a)?, dec(b)?))),
        None => Ok(None),
    }
}

fn rules_of(r: &RulesT) -> LR<ro::Rules> {
    let by_regime = match &r.5 {
        Some(v) => Some(v.iter().map(|(k, d)| Ok((k.clone(), dec(d)?))).collect::<LR<Vec<_>>>()?),
        None => None,
    };
    let entry_quote = match &r.10 {
        Some(q) => Some(ro::EntryQuote {
            short_put_abs_delta: pair(&q.0)?,
            min_short_bid: opt_dec(&q.1)?,
            short_bid_return: pair(&q.2)?,
            min_short_implied_vol: opt_dec(&q.3)?,
            min_open_interest: q.4,
            max_leg_spread_frac: opt_dec(&q.5)?,
            min_underlying_price: opt_dec(&q.6)?,
            min_credit_width_frac: opt_dec(&q.7)?,
            min_credit_return: opt_dec(&q.8)?,
            max_friction_frac: opt_dec(&q.9)?,
        }),
        None => None,
    };
    Ok(ro::Rules {
        max_margin_frac: dec(&r.0)?,
        allowed_regimes: r.1.clone(),
        no_earnings_before_expiry: r.2,
        max_name_margin_frac: opt_dec(&r.3)?,
        max_name_collateral_frac: opt_dec(&r.4)?,
        put_notional_frac_by_regime: by_regime,
        max_loss_per_structure_frac: opt_dec(&r.6)?,
        max_debit_per_structure_frac: opt_dec(&r.7)?,
        max_total_debit_frac: opt_dec(&r.8)?,
        max_share_notional_frac: opt_dec(&r.9)?,
        entry_quote,
    })
}

fn snapshot_of(s: &SnapT) -> LR<ro::Snapshot> {
    Ok(ro::Snapshot {
        underlying: s.0.clone(),
        underlying_price: dec(&s.1)?,
        quotes: s
            .3
            .iter()
            .map(|q| {
                Ok(ro::Quote {
                    occ: q.0.clone(),
                    bid: dec(&q.1)?,
                    ask: dec(&q.2)?,
                    implied_vol: opt_dec(&q.3)?,
                    delta: opt_dec(&q.4)?,
                    open_interest: q.5,
                })
            })
            .collect::<LR<Vec<_>>>()?,
    })
}

fn val(v: ro::Val) -> (bool, String) {
    match v {
        ro::Val::S(s) => (false, s),
        ro::Val::D(d) => (true, d.to_py_string()),
    }
}

/// Every options entry rule but the ledger's two. `regime()` returns the session's regime
/// or None; `earnings()` returns (known, ISO date or None).
#[pyfunction]
#[pyo3(signature = (state, instrument, side, quantity, limit_price, rules, snapshot, regime, earnings))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn option_risk_evaluate(
    state: &str,
    instrument: &str,
    side: &str,
    quantity: &str,
    limit_price: Option<String>,
    rules: RulesT,
    snapshot: Option<SnapT>,
    regime: &Bound<'_, PyAny>,
    earnings: &Bound<'_, PyAny>,
) -> PyResult<Vec<ResultT>> {
    let host = Host::new();
    let r = (|| -> LR<Vec<ResultT>> {
        let st = lb::account_from_text(state)?;
        let intent = ro::Intent {
            instrument: self::instrument(instrument)?,
            side: self::side(side)?,
            quantity: dec(quantity)?,
            limit_price: opt_dec(&limit_price)?,
        };
        let rules = rules_of(&rules)?;
        let snap = snapshot.as_ref().map(snapshot_of).transpose()?;
        let live = snapshot.as_ref().is_some_and(|s| s.2);
        let ctx = ro::Context { state: &st, snapshot: snap.as_ref(), live };
        let mut regime_cb = || -> LR<Option<String>> {
            match regime.call0().and_then(|v| v.extract::<Option<String>>()) {
                Ok(v) => Ok(v),
                Err(e) => Err(host.fail(e)),
            }
        };
        let mut earnings_cb = || -> LR<Option<Option<chrono::NaiveDate>>> {
            let (known, date) = match earnings.call0().and_then(|v| v.extract::<(bool, Option<String>)>()) {
                Ok(v) => v,
                Err(e) => return Err(host.fail(e)),
            };
            if !known {
                return Ok(None);
            }
            match date {
                None => Ok(Some(None)),
                Some(d) => match chrono::NaiveDate::parse_from_str(&d, "%Y-%m-%d") {
                    Ok(d) => Ok(Some(Some(d))),
                    Err(_) => err("value", format!("Invalid isoformat string: {d:?}")),
                },
            }
        };
        let results = ro::evaluate(&rules, &intent, &ctx, &mut regime_cb, &mut earnings_cb)?;
        Ok(results
            .into_iter()
            .map(|x| {
                let (md, m) = val(x.measured);
                let (td, t) = val(x.threshold);
                (x.name, x.passed, md, m, td, t, x.reason)
            })
            .collect())
    })();
    host.finish(r)
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<SimBook>()?;
    m.add_function(wrap_pyfunction!(sim_instrument_key, m)?)?;
    m.add_class::<SnapBook>()?;
    m.add_function(wrap_pyfunction!(sim_underlying_of, m)?)?;
    m.add_function(wrap_pyfunction!(trail_check_amount, m)?)?;
    m.add_function(wrap_pyfunction!(trail_update, m)?)?;
    m.add_function(wrap_pyfunction!(oms_open_structures, m)?)?;
    m.add_function(wrap_pyfunction!(oms_uncovered_calls, m)?)?;
    m.add_function(wrap_pyfunction!(option_risk_evaluate, m)?)?;
    Ok(())
}
