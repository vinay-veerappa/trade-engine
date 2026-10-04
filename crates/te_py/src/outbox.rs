use crate::store::{sql_error, value_from_python, value_to_python};
use pyo3::{
    exceptions::{PyException, PyValueError},
    prelude::*,
    types::{PyDict, PyList, PyString},
};
use te_host::{
    outbox::{self, DrainHost, OutboxRow, TransactionHost},
    store::{sql, Store},
};

struct Error(PyErr);
impl From<sql::Error> for Error {
    fn from(error: sql::Error) -> Self {
        Python::with_gil(|py| Self(sql_error(py, error)))
    }
}
impl From<PyErr> for Error {
    fn from(error: PyErr) -> Self {
        Self(error)
    }
}
struct Effects<'py> {
    owner: Bound<'py, PyAny>,
}
impl TransactionHost for Effects<'_> {
    type Error = Error;
    fn commit(&self) -> Result<(), Error> {
        self.owner.call_method0("_commit")?;
        Ok(())
    }
    fn rollback(&self) -> Result<(), Error> {
        self.owner.call_method0("_rollback")?;
        Ok(())
    }
    fn is_exception(&self, error: &Error) -> bool {
        error.0.is_instance_of::<PyException>(self.owner.py())
    }
}

fn aware(value: &Bound<'_, PyAny>, message: &str) -> PyResult<()> {
    let tz = value.getattr("tzinfo")?;
    if tz.is_none() || tz.call_method1("utcoffset", (value,))?.is_none() {
        return Err(PyValueError::new_err(message.to_owned()));
    }
    Ok(())
}
fn stripped<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value.call_method0("strip")
}
fn make_item(py: Python<'_>, fields: &Bound<'_, PyDict>) -> PyResult<Py<PyAny>> {
    Ok(py
        .import("trade_engine.ledger.outbox")?
        .getattr("OutboxItem")?
        .call((), Some(fields))?
        .unbind())
}
pub(crate) fn row_item(row: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    ItemCodec::new(row.py())?.convert(|key| row.get_item(key))
}
struct ItemCodec<'py> {
    py: Python<'py>,
    loads: Bound<'py, PyAny>,
    timestamp: Bound<'py, PyAny>,
    status: Bound<'py, PyAny>,
    item: Bound<'py, PyAny>,
}
impl<'py> ItemCodec<'py> {
    fn new(py: Python<'py>) -> PyResult<Self> {
        let module = py.import("trade_engine.ledger.outbox")?;
        Ok(Self {
            py,
            loads: py.import("json")?.getattr("loads")?,
            timestamp: py
                .import("datetime")?
                .getattr("datetime")?
                .getattr("fromisoformat")?,
            status: module.getattr("OutboxStatus")?,
            item: module.getattr("OutboxItem")?,
        })
    }
    fn convert(&self, get: impl Fn(&str) -> PyResult<Bound<'py, PyAny>>) -> PyResult<Py<PyAny>> {
        let int = self.py.get_type::<pyo3::types::PyInt>();
        let id = int.call1((get("id")?,))?;
        let seq = int.call1((get("event_seq")?,))?;
        let destination = get("destination")?.str()?;
        let payload = self.loads.call1((get("payload_json")?,))?;
        let status = self.status.call1((get("status")?.str()?,))?;
        let attempts = int.call1((get("attempts")?,))?;
        let created = self.timestamp.call1((get("created_at")?,))?;
        let error = get("last_error")?;
        let delivered = get("delivered_at")?;
        let delivered = if delivered.is_truthy()? {
            self.timestamp.call1((delivered,))?
        } else {
            self.py.None().into_bound(self.py)
        };
        Ok(self
            .item
            .call1((
                id,
                seq,
                destination,
                payload,
                status,
                attempts,
                created,
                error,
                delivered,
            ))?
            .unbind())
    }
    fn stored(&self, row: OutboxRow) -> PyResult<Py<PyAny>> {
        self.convert(|key| {
            let value = match key {
                "id" => &row.id,
                "event_seq" => &row.event_seq,
                "destination" => &row.destination,
                "payload_json" => &row.payload,
                "status" => &row.status,
                "attempts" => &row.attempts,
                "created_at" => &row.created_at,
                "last_error" => &row.last_error,
                "delivered_at" => &row.delivered_at,
                _ => unreachable!("internal outbox column"),
            };
            Ok(value_to_python(self.py, value)?.into_bound(self.py))
        })
    }
}

pub(crate) fn insert(
    store: &Store,
    seq: &Bound<'_, PyAny>,
    destination: &Bound<'_, PyAny>,
    payload: &Bound<'_, PyAny>,
    created_at: &Bound<'_, PyAny>,
) -> PyResult<i64> {
    let py = seq.py();
    let destination = stripped(destination)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("separators", (",", ":"))?;
    kwargs.set_item("sort_keys", true)?;
    let json = py
        .import("json")?
        .getattr("dumps")?
        .call((payload,), Some(&kwargs))?;
    let ts = created_at.call_method0("isoformat")?;
    store
        .insert_outbox(
            value_from_python(seq, 1)?,
            value_from_python(&destination, 2)?,
            value_from_python(&json, 3)?,
            value_from_python(&ts, 5)?,
        )
        .map_err(|e| sql_error(py, e))
}

pub(crate) fn enqueue(
    store: &Store,
    owner: &Bound<'_, PyAny>,
    seq: &Bound<'_, PyAny>,
    destination: &Bound<'_, PyAny>,
    payload: &Bound<'_, PyAny>,
    created_at: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = owner.py();
    // Match validation order, including refusal before an unopened ledger.
    owner.getattr("conn")?;
    let ts = match store
        .event_timestamp(&value_from_python(seq, 1)?)
        .map_err(|e| sql_error(py, e))?
    {
        Some(ts) => ts,
        None => {
            return Err(PyValueError::new_err(format!(
                "Unknown event sequence {}",
                seq.str()?
            )))
        }
    };
    let created = if created_at.is_none() {
        py.import("datetime")?
            .getattr("datetime")?
            .call_method1("fromisoformat", (value_to_python(py, &ts)?,))?
    } else {
        aware(
            created_at,
            "Outbox created_at must be timezone-aware UTC datetime (I7)",
        )?;
        created_at.clone()
    };
    let effects = Effects {
        owner: owner.clone(),
    };
    let result = store.outbox_transaction(&effects, || {
        Ok(owner.call_method1("_insert_outbox", (seq, destination, payload, &created))?)
    });
    let id = match result {
        Ok(id) => id,
        Err(Error(error)) => {
            let integrity = py.import("sqlite3")?.getattr("IntegrityError")?;
            if error.value(py).is_instance(&integrity)? {
                let message = format!(
                    "Outbox entry violates constraint (e.g. duplicate or missing foreign key): {}",
                    error.value(py).str()?
                );
                let wrapped = PyValueError::new_err(message);
                wrapped.set_cause(py, Some(error));
                return Err(wrapped);
            }
            return Err(error);
        }
    };
    let fields = PyDict::new(py);
    fields.set_item("id", id)?;
    fields.set_item("event_seq", seq)?;
    fields.set_item("destination", stripped(destination)?)?;
    fields.set_item("payload", payload)?;
    fields.set_item(
        "status",
        py.import("trade_engine.ledger.outbox")?
            .getattr("OutboxStatus")?
            .getattr("PENDING")?,
    )?;
    fields.set_item("attempts", 0)?;
    fields.set_item("created_at", created)?;
    make_item(py, &fields)
}

pub(crate) fn pending(
    store: &Store,
    destination: &Bound<'_, PyAny>,
    include_failed: &Bound<'_, PyAny>,
) -> PyResult<Vec<Py<PyAny>>> {
    let py = destination.py();
    let dest = if destination.is_none() {
        None
    } else {
        Some(value_from_python(&stripped(destination)?, 1)?)
    };
    let rows = store
        .pending_outbox(dest, include_failed.is_truthy()?)
        .map_err(|e| sql_error(py, e))?;
    let codec = ItemCodec::new(py)?;
    rows.into_iter().map(|r| codec.stored(r)).collect()
}

pub(crate) fn delivered(
    store: &Store,
    owner: &Bound<'_, PyAny>,
    id: &Bound<'_, PyAny>,
    at: &Bound<'_, PyAny>,
) -> PyResult<()> {
    owner.getattr("conn")?;
    store
        .outbox_transaction(
            &Effects {
                owner: owner.clone(),
            },
            || {
                let timestamp = at.call_method0("isoformat")?;
                store
                    .deliver_outbox(value_from_python(id, 3)?, value_from_python(&timestamp, 2)?)?;
                Ok(())
            },
        )
        .map_err(|e| e.0)
}
pub(crate) fn failed(
    store: &Store,
    owner: &Bound<'_, PyAny>,
    id: &Bound<'_, PyAny>,
    error: &Bound<'_, PyAny>,
) -> PyResult<()> {
    owner.getattr("conn")?;
    store
        .outbox_transaction(
            &Effects {
                owner: owner.clone(),
            },
            || {
                let message = error.str()?;
                store.fail_outbox(
                    value_from_python(id, 3)?,
                    value_from_python(message.as_any(), 2)?,
                )?;
                Ok(())
            },
        )
        .map_err(|e| e.0)
}

struct Publisher<'py> {
    owner: Bound<'py, PyAny>,
    destination: Bound<'py, PyAny>,
    callback: Bound<'py, PyAny>,
    clock: Bound<'py, PyAny>,
}
impl DrainHost for Publisher<'_> {
    type Item = Py<PyAny>;
    type Error = PyErr;
    fn pending(&self) -> PyResult<Vec<Self::Item>> {
        let kwargs = PyDict::new(self.owner.py());
        kwargs.set_item("destination", &self.destination)?;
        kwargs.set_item("include_failed", true)?;
        self.owner
            .call_method("pending_outbox", (), Some(&kwargs))?
            .extract()
    }
    fn publish(&self, item: &Self::Item) -> PyResult<bool> {
        self.callback.call1((item,))?.is_truthy()
    }
    fn delivered(&self, item: &Self::Item) -> PyResult<()> {
        let id = item.bind(self.owner.py()).getattr("id")?;
        let timestamp = self.clock.call_method0("now_utc")?;
        self.owner
            .call_method1("mark_outbox_delivered", (id, timestamp))?;
        Ok(())
    }
    fn failed(&self, item: &Self::Item, error: &str) -> PyResult<Self::Item> {
        let py = self.owner.py();
        let item = item.bind(py);
        self.owner
            .call_method1("mark_outbox_failed", (item.getattr("id")?, error))?;
        let fields = PyDict::new(py);
        fields.set_item(
            "status",
            py.import("trade_engine.ledger.outbox")?
                .getattr("OutboxStatus")?
                .getattr("FAILED")?,
        )?;
        fields.set_item(
            "attempts",
            item.getattr("attempts")?.call_method1("__add__", (1,))?,
        )?;
        fields.set_item("last_error", error)?;
        Ok(py
            .import("dataclasses")?
            .getattr("replace")?
            .call((item,), Some(&fields))?
            .unbind())
    }
    fn exception_message(&self, error: &PyErr) -> PyResult<Option<String>> {
        if error.is_instance_of::<PyException>(self.owner.py()) {
            Ok(Some(
                error.value(self.owner.py()).str()?.to_str()?.to_owned(),
            ))
        } else {
            Ok(None)
        }
    }
    fn destination(&self) -> PyResult<String> {
        Ok(self.destination.str()?.to_str()?.to_owned())
    }
}
pub(crate) fn drain(
    owner: &Bound<'_, PyAny>,
    destination: &Bound<'_, PyAny>,
    publisher: &Bound<'_, PyAny>,
    clock: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = owner.py();
    let result = outbox::drain(&Publisher {
        owner: owner.clone(),
        destination: destination.clone(),
        callback: publisher.clone(),
        clock: clock.clone(),
    })?;
    let fields = PyDict::new(py);
    fields.set_item("drained_count", result.count)?;
    fields.set_item("failed_item", result.failed)?;
    fields.set_item("error", result.error)?;
    fields.set_item("remaining_count", result.remaining)?;
    Ok(py
        .import("trade_engine.ledger.outbox")?
        .getattr("DrainResult")?
        .call((), Some(&fields))?
        .unbind())
}

#[pyfunction]
pub(crate) fn ledger_outbox_items(value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py = value.py();
    if !value.is_truthy()? {
        return Ok(PyList::empty(py).into_any().unbind());
    }
    let mapping = py.import("collections.abc")?.getattr("Mapping")?;
    let source = if value.is_instance(&mapping)? {
        value.call_method0("items")?
    } else {
        value.clone()
    };
    let items = py.get_type::<PyList>().call1((source,))?;
    for item in items.try_iter()? {
        let item = item?;
        // CPython UNPACK_SEQUENCE consumes at most one extra element.
        if unsafe {
            (*pyo3::ffi::Py_TYPE(item.as_ptr())).tp_iter.is_none()
                && pyo3::ffi::PySequence_Check(item.as_ptr()) == 0
        } {
            return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                "cannot unpack non-iterable {} object",
                item.get_type().getattr("__name__")?.str()?
            )));
        }
        let mut iter = item.try_iter()?;
        let mut pair = Vec::new();
        for _ in 0..3 {
            match iter.next() {
                Some(value) => pair.push(value?),
                None => break,
            }
        }
        if pair.len() != 2 {
            return Err(PyValueError::new_err(if pair.len() < 2 {
                format!(
                    "not enough values to unpack (expected 2, got {})",
                    pair.len()
                )
            } else {
                "too many values to unpack (expected 2)".into()
            }));
        }
        let destination = &pair[0];
        if !destination.is_instance_of::<PyString>() || !stripped(destination)?.is_truthy()? {
            return Err(PyValueError::new_err(
                "Outbox destination must be non-empty string",
            ));
        }
        if !pair[1].is_instance_of::<PyDict>() {
            return Err(PyValueError::new_err(format!(
                "Outbox payload for '{}' must be a dict",
                destination.str()?
            )));
        }
    }
    Ok(items.unbind())
}
#[pyfunction]
pub(crate) fn ledger_outbox_row(row: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    row_item(row)
}

#[pyfunction]
fn ledger_outbox_enqueue(
    owner: &Bound<'_, PyAny>,
    seq: &Bound<'_, PyAny>,
    destination: &Bound<'_, PyAny>,
    payload: &Bound<'_, PyAny>,
    created_at: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    if !destination.is_truthy()? || !stripped(destination)?.is_truthy()? {
        return Err(PyValueError::new_err(
            "Outbox destination must be non-empty string",
        ));
    }
    Ok(owner
        .getattr("_store")?
        .call_method1(
            "enqueue_outbox",
            (owner, seq, destination, payload, created_at),
        )?
        .unbind())
}
#[pyfunction]
fn ledger_outbox_delivered(
    owner: &Bound<'_, PyAny>,
    id: &Bound<'_, PyAny>,
    at: &Bound<'_, PyAny>,
) -> PyResult<()> {
    aware(at, "delivered_at must be timezone-aware UTC datetime (I7)")?;
    owner
        .getattr("_store")?
        .call_method1("mark_outbox_delivered", (owner, id, at))?;
    Ok(())
}
#[pyfunction]
fn ledger_outbox_drain(
    owner: &Bound<'_, PyAny>,
    destination: &Bound<'_, PyAny>,
    publisher: &Bound<'_, PyAny>,
    clock: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    drain(owner, destination, publisher, clock)
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(ledger_outbox_items, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_outbox_row, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_outbox_enqueue, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_outbox_delivered, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_outbox_drain, m)?)?;
    Ok(())
}
