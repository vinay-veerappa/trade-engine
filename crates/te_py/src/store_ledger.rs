//! W6-C2a: the EOD host's native ledger and clock: `eod_glue`'s [`Ledger`] over the store and
//! [`Clock`] over the Python clock, the gate that decides when the native path runs, and the
//! conversions between Python carriers and te_core values.
//!
//! The native path runs only for a plain `Ledger` on the XNYS `ExchangeCalendar`: the glue
//! uses te_core's XNYS calendar. Any other ledger or calendar is refused with `EodRunnerError`;
//! there is no Python fallback.
//!
//! The glue's traits return te_core results, so a Python exception raised inside a store read
//! or write or a clock read cannot cross them as itself. It is held in a [`Pending`] while
//! te_core unwinds with kind `host`, and [`Pending::settle`] re-raises it. A held error wins
//! over whatever the glue returned.

use std::cell::RefCell;

use chrono::NaiveDate;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::ledger::bridge as lb;
use te_core::ledger::codec::{enc_instrument, enc_obj, jdec, jdt, jenum};
use te_core::ledger::fold::AccountState;
use te_core::ledger::json::{dumps, Json};
use te_core::ledger::model::{
    err, parse_datetime, DateTime, Event, Instrument, LErr, Obj, RiskVerdict, Side, Signal, R as LR,
};
use te_core::ledger::ops::OMap;
use te_core::money::Money;
use te_core::runtime::eod_glue::{Clock, Ledger};

use crate::sim;
use crate::store::LedgerStore;

/// A Python exception held while te_core unwinds with kind `host`.
#[derive(Default)]
pub(crate) struct Pending(RefCell<Option<PyErr>>);

impl Pending {
    fn is_held(&self) -> bool {
        self.0.borrow().is_some()
    }

    /// Hold one Python error (the first one wins) and return the unwinding refusal.
    pub(crate) fn hold(&self, e: PyErr) -> LErr {
        self.0.borrow_mut().get_or_insert(e);
        LErr { kind: "host", msg: String::new() }
    }

    /// The glue's result, as Python sees it: a held Python error first, then the refusal.
    pub(crate) fn settle<T>(&self, r: LR<T>) -> PyResult<T> {
        if let Some(e) = self.0.borrow_mut().take() {
            return Err(e);
        }
        r.map_err(crate::runtime::flow_refusal)
    }
}

/// The store's plain ledger effects for the glue. Store errors are held in `pending`.
pub(crate) struct StoreLedger<'a> {
    py: Python<'a>,
    store: &'a LedgerStore,
    pending: &'a Pending,
}

impl Ledger for StoreLedger<'_> {
    fn accounts(&self) -> LR<Vec<String>> {
        self.store.account_names(self.py).map_err(|e| self.pending.hold(e))
    }

    fn events(&self, account: &str) -> LR<Vec<Event>> {
        self.store.account_events(self.py, account).map_err(|e| self.pending.hold(e))
    }

    /// The committed fold state; a missing account is the `key` error `Ledger.state` raises.
    fn state(&self, account: &str) -> LR<AccountState> {
        match self.store.fold_state(self.py, account).map_err(|e| self.pending.hold(e))? {
            Some(state) => Ok(state),
            None => err("key", format!("'{account}'")),
        }
    }

    fn event_by_command(&self, command_id: &str) -> LR<Option<Event>> {
        self.store.command_event(self.py, command_id).map_err(|e| self.pending.hold(e))
    }

    /// `Ledger.append`: the ledger assigns the seq, so a seq the event carries is cleared.
    fn append(&mut self, events: Vec<Event>) -> LR<Vec<Event>> {
        // A held clock or store error ends the unit of work: nothing is written after it.
        if self.pending.is_held() {
            return Err(LErr { kind: "host", msg: String::new() });
        }
        let entries = events
            .into_iter()
            .map(|mut event| {
                event.seq = None;
                (event, Vec::new())
            })
            .collect();
        self.store.write_events(self.py, entries).map_err(|e| self.pending.hold(e))
    }
}

/// The Python clock (`now_utc()`), as te_core's clock. A failed read is held in `pending`,
/// and the placeholder instant it returns is never written: the glue's next append refuses.
pub(crate) struct PyClock<'a, 'py> {
    clock: &'a Bound<'py, PyAny>,
    pending: &'a Pending,
}

impl PyClock<'_, '_> {
    fn read(&self) -> PyResult<DateTime> {
        let text: String = self.clock.call_method0("now_utc")?.call_method0("isoformat")?.extract()?;
        py_result(parse_datetime(&text).map(|(now, _)| now))
    }
}

impl Clock for PyClock<'_, '_> {
    fn now_utc(&self) -> DateTime {
        match self.read() {
            Ok(now) => now,
            Err(e) => {
                self.pending.hold(e);
                parse_datetime("1970-01-01T00:00:00+00:00").expect("constant instant").0
            }
        }
    }
}

/// The refusal the EOD gate raises: `EodRunnerError(msg)`, the error the runner raises for a bad run.
pub(crate) fn refuse(py: Python<'_>, msg: &str) -> PyErr {
    let raised = py
        .import("trade_engine.eod.runner")
        .and_then(|module| module.getattr("EodRunnerError"))
        .and_then(|cls| cls.call1((msg,)));
    match raised {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

/// The exception a gate raises for its refusal text: `refuse` for EOD, the lifecycle pass's own class otherwise.
pub(crate) type Raise = for<'py> fn(Python<'py>, &str) -> PyErr;

/// The store behind a plain `Ledger` that is open. `refusal` is the text for a ledger that is not one.
fn plain_store<'py>(ledger: &Bound<'py, PyAny>, refusal: &str, raise: Raise) -> PyResult<Bound<'py, LedgerStore>> {
    let py = ledger.py();
    let plain = py.import("trade_engine.ledger.store")?.getattr("Ledger")?;
    if !ledger.get_type().is(&plain) {
        return Err(raise(py, refusal));
    }
    // A closed ledger has no store (`_native` is None).
    let native = ledger.getattr("_native")?;
    if native.is_none() {
        return Err(raise(py, "the ledger is closed"));
    }
    Ok(native.downcast_into::<LedgerStore>()?)
}

/// `require_native` for a gate that raises its own class: the same checks, the caller's `raise` and `refusal`.
pub(crate) fn require_native_with<'py>(
    ledger: &Bound<'py, PyAny>,
    calendar: &Bound<'py, PyAny>,
    raise: Raise,
    refusal: &str,
) -> PyResult<Bound<'py, LedgerStore>> {
    let py = calendar.py();
    let exchange = py.import("trade_engine.calendar.sessions")?.getattr("ExchangeCalendar")?;
    if !calendar.get_type().is(&exchange) || calendar.getattr("exchange")?.extract::<String>()? != "XNYS" {
        return Err(raise(py, refusal));
    }
    plain_store(ledger, refusal, raise)
}

/// The store behind the native path: a plain `Ledger` on the XNYS `ExchangeCalendar`. Anything
/// else is refused; there is no Python fallback.
pub(crate) fn require_native<'py>(
    ledger: &Bound<'py, PyAny>,
    calendar: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, LedgerStore>> {
    require_native_with(ledger, calendar, refuse, "EOD needs a plain Ledger on the XNYS ExchangeCalendar")
}

/// The store behind the runner's ledger and calendar (`require_native` on the runner's attributes).
pub(crate) fn native_store<'py>(runner: &Bound<'py, PyAny>) -> PyResult<Bound<'py, LedgerStore>> {
    require_native(&runner.getattr("_ledger")?, &runner.getattr("_calendar")?)
}

/// The store behind a write-off, which takes no calendar: a plain open `Ledger` only.
pub(crate) fn ledger_store<'py>(ledger: &Bound<'py, PyAny>) -> PyResult<Bound<'py, LedgerStore>> {
    plain_store(ledger, "EOD needs a plain Ledger", refuse)
}

/// Run `f` against the native ledger and clock. The store's borrow lasts for the call.
pub(crate) fn with_native<T>(
    store: &Bound<'_, LedgerStore>,
    clock: &Bound<'_, PyAny>,
    pending: &Pending,
    f: impl FnOnce(&mut StoreLedger<'_>, &PyClock<'_, '_>) -> LR<T>,
) -> PyResult<T> {
    let guard = store.borrow();
    let mut ledger = StoreLedger { py: store.py(), store: &*guard, pending };
    let clock = PyClock { clock, pending };
    let result = f(&mut ledger, &clock);
    pending.settle(result)
}

fn py_result<T>(r: LR<T>) -> PyResult<T> {
    r.map_err(crate::runtime::flow_refusal)
}

/// The instant as one integer (microseconds since 0001-01-01 UTC), as `_runtime.micros`.
pub(crate) fn micros(dt: &DateTime) -> i64 {
    let (secs, us) = dt.instant();
    secs * 1_000_000 + us as i64
}

/// A Python value's stored payload text (`codec.payload_text`).
fn payload_text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.py().import("trade_engine.ledger.codec")?.call_method1("payload_text", (value,))?.extract()
}

/// A Python carrier as the te_core object it encodes (`codec.payload_text` then the bridge).
pub(crate) fn payload(value: &Bound<'_, PyAny>) -> PyResult<Obj> {
    py_result(lb::obj_from_text(&payload_text(value)?))
}

pub(crate) fn signal_of(value: &Bound<'_, PyAny>) -> PyResult<Signal> {
    match payload(value)? {
        Obj::Signal(signal) => Ok(signal),
        other => Err(PyValueError::new_err(format!("expected a Signal, got {}", other.tag()))),
    }
}

pub(crate) fn verdict_of(value: &Bound<'_, PyAny>) -> PyResult<RiskVerdict> {
    match payload(value)? {
        Obj::RiskVerdict(verdict) => Ok(verdict),
        other => Err(PyValueError::new_err(format!("expected a RiskVerdict, got {}", other.tag()))),
    }
}

pub(crate) fn instrument_of(value: &Bound<'_, PyAny>) -> PyResult<Instrument> {
    py_result(sim::instrument(&payload_text(value)?))
}

/// A Python `Decimal` as a te_core money value, from its `str` (exact).
pub(crate) fn money(value: &Bound<'_, PyAny>) -> PyResult<Money> {
    py_result(sim::dec(&value.str()?.extract::<String>()?))
}

/// A Python `date` as a `NaiveDate`, from its ISO text.
pub(crate) fn date_of(value: &Bound<'_, PyAny>) -> PyResult<NaiveDate> {
    let text: String = value.call_method0("isoformat")?.extract()?;
    NaiveDate::parse_from_str(&text, "%Y-%m-%d").map_err(|e| PyValueError::new_err(e.to_string()))
}

/// `last_regular_closes` (a `Mapping[Instrument, Decimal]`) as the glue's map.
pub(crate) fn closes(value: &Bound<'_, PyAny>) -> PyResult<OMap<Instrument, Money>> {
    let mut out = OMap::new();
    for item in value.call_method0("items")?.try_iter()? {
        let (key, close): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
        let instrument = instrument_of(&key)?;
        let price = money(&close)?;
        out.insert(instrument.hk(), instrument, price);
    }
    Ok(out)
}

/// A te_core JSON tree as the Python object the codec builds from it (`codec.build_text`).
fn to_py<'py>(py: Python<'py>, json: &Json) -> PyResult<Bound<'py, PyAny>> {
    py.import("trade_engine.ledger.codec")?.call_method1("build_text", (dumps(json),))
}

pub(crate) fn decimal_py<'py>(py: Python<'py>, value: &Money) -> PyResult<Bound<'py, PyAny>> {
    to_py(py, &py_result(jdec(value))?)
}

pub(crate) fn datetime_py<'py>(py: Python<'py>, value: &DateTime) -> PyResult<Bound<'py, PyAny>> {
    to_py(py, &jdt(value))
}

pub(crate) fn date_py<'py>(py: Python<'py>, value: NaiveDate) -> PyResult<Bound<'py, PyAny>> {
    py.import("datetime")?.getattr("date")?.call_method1("fromisoformat", (value.to_string(),))
}

pub(crate) fn instrument_py<'py>(py: Python<'py>, value: &Instrument) -> PyResult<Bound<'py, PyAny>> {
    to_py(py, &py_result(enc_instrument(value))?)
}

pub(crate) fn side_py<'py>(py: Python<'py>, value: Side) -> PyResult<Bound<'py, PyAny>> {
    to_py(py, &jenum("Side", value.value()))
}

/// A stored payload's Python object, from the codec's tree of it.
pub(crate) fn obj_py<'py>(py: Python<'py>, value: &Obj) -> PyResult<Bound<'py, PyAny>> {
    to_py(py, &py_result(enc_obj(value))?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn micros_orders_like_the_instant() {
        let early = parse_datetime("2026-11-24T09:45:00+00:00").unwrap().0;
        let late = parse_datetime("2026-11-24T09:45:00.000001+00:00").unwrap().0;
        assert!(micros(&late) - micros(&early) == 1);
    }
}
