//! Native store binding and SQLite compatibility surface, all over ONE connection.
use crate::{refuse_ledger, LedgerFold, Row};
use pyo3::{
    exceptions::{PyIndexError, PyStopIteration, PyValueError},
    prelude::*,
    types::{PyBytes, PyDict, PyFloat, PyInt, PySlice, PyString, PyTuple},
};
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{Arc, Mutex},
};
use te_host::{
    lock::LockError,
    store::{
        sql::{self, types::Value},
        OpenError, SharedConnection, Store, StoredEvent, StoredRow, WriteHost,
    },
};

fn exception(py: Python<'_>, name: &str, message: impl AsRef<str>) -> PyErr {
    match py
        .import("sqlite3")
        .and_then(|m| m.getattr(name))
        .and_then(|c| c.call1((message.as_ref(),)))
    {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

pub(crate) fn sql_error(py: Python<'_>, error: sql::Error) -> PyErr {
    let detail = match &error {
        sql::Error::SqliteFailure(code, message) => Some((code, message.as_deref())),
        sql::Error::SqlInputError {
            error: code, msg, ..
        } => Some((code, Some(msg.as_str()))),
        _ => None,
    };
    let class = match detail {
        Some((code, _)) => match code.code {
            sql::ErrorCode::ConstraintViolation => "IntegrityError",
            sql::ErrorCode::TypeMismatch => "IntegrityError",
            sql::ErrorCode::TooBig => "DataError",
            sql::ErrorCode::InternalMalfunction => "InternalError",
            sql::ErrorCode::ApiMisuse => "ProgrammingError",
            sql::ErrorCode::OutOfMemory => return pyo3::exceptions::PyMemoryError::new_err(()),
            sql::ErrorCode::DatabaseCorrupt
            | sql::ErrorCode::NotADatabase
            | sql::ErrorCode::AuthorizationForStatementDenied => "DatabaseError",
            _ => "OperationalError",
        },
        _ => "ProgrammingError",
    };
    let message = match detail {
        Some((code, _)) if code.code == sql::ErrorCode::CannotOpen => {
            "unable to open database file".into()
        }
        Some((_, Some(message))) => message.to_owned(),
        _ => match &error {
            sql::Error::MultipleStatement => "You can only execute one statement at a time.".into(),
            _ => error.to_string(),
        },
    };
    let err = exception(py, class, message);
    if let Some((code, _)) = detail {
        let value = err.value(py);
        let metadata = (|| -> PyResult<()> {
            value.setattr("sqlite_errorcode", code.extended_code)?;
            let base = match code.extended_code & 255 {
                1 => "SQLITE_ERROR",
                2 => "SQLITE_INTERNAL",
                3 => "SQLITE_PERM",
                4 => "SQLITE_ABORT",
                5 => "SQLITE_BUSY",
                6 => "SQLITE_LOCKED",
                7 => "SQLITE_NOMEM",
                8 => "SQLITE_READONLY",
                9 => "SQLITE_INTERRUPT",
                10 => "SQLITE_IOERR",
                11 => "SQLITE_CORRUPT",
                12 => "SQLITE_NOTFOUND",
                13 => "SQLITE_FULL",
                14 => "SQLITE_CANTOPEN",
                15 => "SQLITE_PROTOCOL",
                16 => "SQLITE_EMPTY",
                17 => "SQLITE_SCHEMA",
                18 => "SQLITE_TOOBIG",
                19 => "SQLITE_CONSTRAINT",
                20 => "SQLITE_MISMATCH",
                21 => "SQLITE_MISUSE",
                22 => "SQLITE_NOLFS",
                23 => "SQLITE_AUTH",
                24 => "SQLITE_FORMAT",
                25 => "SQLITE_RANGE",
                26 => "SQLITE_NOTADB",
                27 => "SQLITE_NOTICE",
                28 => "SQLITE_WARNING",
                _ => "unknown",
            };
            let mut name = if code.extended_code & 255 == code.extended_code {
                base.to_owned()
            } else {
                "unknown".into()
            };
            if name == "unknown" && base != "unknown" {
                for (key, number) in py.import("sqlite3")?.dict().iter() {
                    let key: String = key.extract()?;
                    if key.starts_with(&format!("{base}_"))
                        && number.is_instance_of::<PyInt>()
                        && number.extract::<i32>()? == code.extended_code
                    {
                        name = key;
                        break;
                    }
                }
            }
            // CPython uses "unknown" for a result absent from its exported constants.
            value.setattr("sqlite_errorname", name)
        })();
        if let Err(error) = metadata {
            return error;
        }
    }
    err
}

struct HostError(PyErr);
impl From<sql::Error> for HostError {
    fn from(error: sql::Error) -> Self {
        Python::with_gil(|py| Self(sql_error(py, error)))
    }
}
impl From<PyErr> for HostError {
    fn from(error: PyErr) -> Self {
        Self(error)
    }
}

pub(crate) fn value_from_python(value: &Bound<'_, PyAny>, index: usize) -> PyResult<Value> {
    let py = value.py();
    if value.is_none() {
        return Ok(Value::Null);
    }
    if value.is_instance_of::<PyInt>() {
        return value.extract::<i64>().map(Value::Integer).map_err(|_| {
            pyo3::exceptions::PyOverflowError::new_err(
                "Python int too large to convert to SQLite INTEGER",
            )
        });
    }
    if value.is_instance_of::<PyFloat>() {
        return Ok(Value::Real(value.extract()?));
    }
    if value.is_instance_of::<PyString>() {
        return Ok(Value::Text(value.extract()?));
    }
    if let Ok(buffer) = pyo3::buffer::PyBuffer::<u8>::get(value) {
        return Ok(Value::Blob(buffer.to_vec(py)?));
    }
    // sqlite3 reports tp_name, not Python's shorter __name__ (decimal.Decimal).
    let name = unsafe { std::ffi::CStr::from_ptr((*pyo3::ffi::Py_TYPE(value.as_ptr())).tp_name) }
        .to_string_lossy();
    Err(exception(
        py,
        "ProgrammingError",
        format!("Error binding parameter {index}: type '{name}' is not supported"),
    ))
}

pub(crate) fn value_to_python(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Integer(v) => v.into_pyobject(py)?.into_any().unbind(),
        Value::Real(v) => v.into_pyobject(py)?.into_any().unbind(),
        Value::Text(v) => v.into_pyobject(py)?.into_any().unbind(),
        Value::Blob(v) => PyBytes::new(py, v).into_any().unbind(),
    })
}

#[pyclass(module = "trade_engine_rs", name = "SqlRow")]
struct SqlRow {
    names: Vec<String>,
    values: Py<PyTuple>,
}
#[pymethods]
impl SqlRow {
    fn keys(&self) -> Vec<String> {
        self.names.clone()
    }
    fn __len__(&self, py: Python<'_>) -> usize {
        self.values.bind(py).len()
    }
    fn __iter__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.values.bind(py).call_method0("__iter__")?.unbind())
    }
    fn __getitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        if key.is_instance_of::<PyInt>() || key.is_instance_of::<PySlice>() {
            return self
                .values
                .bind(py)
                .as_any()
                .get_item(key)
                .map(Bound::unbind);
        }
        if let Ok(name) = key.extract::<String>() {
            if let Some(index) = self
                .names
                .iter()
                .position(|s| s.eq_ignore_ascii_case(&name))
            {
                return self.values.bind(py).get_item(index).map(Bound::unbind);
            }
            return Err(PyIndexError::new_err("No item with that key"));
        }
        Err(PyIndexError::new_err("Index must be int or string"))
    }
    fn __eq__(&self, py: Python<'_>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        if let Ok(other) = other.extract::<PyRef<SqlRow>>() {
            Ok(self.names == other.names && self.values.bind(py).eq(other.values.bind(py))?)
        } else {
            Ok(false)
        }
    }
}

#[derive(Clone)]
struct ConnectionRef {
    connection: SharedConnection,
    _guard: Arc<Mutex<Option<te_host::lock::SingleInstanceGuard>>>,
    thread: u64,
}
impl ConnectionRef {
    fn check_thread(&self, py: Python<'_>) -> PyResult<()> {
        let current: u64 = py
            .import("threading")?
            .call_method0("get_ident")?
            .extract()?;
        if current != self.thread {
            return Err(exception(py, "ProgrammingError", format!(
                "SQLite objects created in a thread can only be used in that same thread. The object was created in thread id {} and this is thread id {}.", self.thread, current)));
        }
        Ok(())
    }
    fn check(&self, py: Python<'_>) -> PyResult<()> {
        self.check_thread(py)?;
        if self
            .connection
            .lock()
            .expect("connection mutex poisoned")
            .is_none()
        {
            return Err(exception(
                py,
                "ProgrammingError",
                "Cannot operate on a closed database.",
            ));
        }
        Ok(())
    }
}

#[pyclass(module = "trade_engine_rs", name = "SqlCursor")]
struct SqlCursor {
    conn: ConnectionRef,
    rows: VecDeque<Vec<Value>>,
    names: Vec<String>,
    description: Option<Py<PyTuple>>,
    lastrowid: Option<i64>,
    rowcount: i64,
    arraysize: i64,
    row_factory: Py<PyAny>,
    closed: bool,
}
impl SqlCursor {
    fn new(_py: Python<'_>, conn: ConnectionRef, factory: Py<PyAny>) -> Self {
        Self {
            conn,
            rows: VecDeque::new(),
            names: Vec::new(),
            description: None,
            lastrowid: None,
            rowcount: -1,
            arraysize: 1,
            row_factory: factory,
            closed: false,
        }
    }
    fn check(&self, py: Python<'_>) -> PyResult<()> {
        self.conn.check(py)?;
        if self.closed {
            return Err(exception(
                py,
                "ProgrammingError",
                "Cannot operate on a closed cursor.",
            ));
        }
        Ok(())
    }
    fn run(
        &mut self,
        py: Python<'_>,
        query: &str,
        parameters: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        self.check(py)?;
        if query.contains('\0') {
            return Err(exception(
                py,
                "ProgrammingError",
                "the query contains a null character",
            ));
        }
        self.rows.clear();
        self.names.clear();
        self.description = None;
        self.rowcount = -1;
        let borrowed = self
            .conn
            .connection
            .lock()
            .expect("connection mutex poisoned");
        let conn = borrowed.as_ref().expect("checked");
        let mut stmt = conn.prepare(query).map_err(|e| sql_error(py, e))?;
        // prepare() ignores trailing text; SQLite's execute does not.
        if !te_host::store::single_statement(conn, query).map_err(|e| sql_error(py, e))? {
            return Err(exception(
                py,
                "ProgrammingError",
                "You can only execute one statement at a time.",
            ));
        }
        let expected = stmt.parameter_count();
        if let Some(parameters) = parameters.filter(|p| p.is_instance_of::<PyDict>()) {
            for index in 1..=expected {
                let name = stmt.parameter_name(index).ok_or_else(|| exception(py, "ProgrammingError",
                    format!("Binding {index} has no name, but you supplied a dictionary (which has only names).")))?;
                let key = &name[1..];
                let value = parameters.get_item(key).map_err(|_| {
                    exception(
                        py,
                        "ProgrammingError",
                        format!("You did not supply a value for binding parameter {name}."),
                    )
                })?;
                stmt.raw_bind_parameter(index, value_from_python(&value, index)?)
                    .map_err(|e| sql_error(py, e))?;
            }
        } else {
            let values = match parameters {
                Some(p) => p
                    .try_iter()
                    .map_err(|_| {
                        exception(py, "ProgrammingError", "parameters are of unsupported type")
                    })?
                    .collect::<PyResult<Vec<_>>>()?,
                None => Vec::new(),
            };
            if values.len() != expected {
                return Err(exception(py, "ProgrammingError", format!(
                    "Incorrect number of bindings supplied. The current statement uses {expected}, and there are {} supplied.", values.len())));
            }
            for (i, value) in values.iter().enumerate() {
                stmt.raw_bind_parameter(i + 1, value_from_python(value, i + 1)?)
                    .map_err(|e| sql_error(py, e))?;
            }
        }
        self.names = stmt.column_names().iter().map(|s| s.to_string()).collect();
        let dml = query
            .trim_start()
            .split_whitespace()
            .next()
            .is_some_and(|s| {
                ["INSERT", "UPDATE", "DELETE", "REPLACE"]
                    .iter()
                    .any(|q| s.eq_ignore_ascii_case(q))
            });
        if !self.names.is_empty() {
            let cols = self.names.len();
            let desc = self
                .names
                .iter()
                .map(|name| {
                    PyTuple::new(
                        py,
                        [
                            name.into_pyobject(py)?.into_any().unbind(),
                            py.None(),
                            py.None(),
                            py.None(),
                            py.None(),
                            py.None(),
                            py.None(),
                        ],
                    )
                    .map(Bound::unbind)
                })
                .collect::<PyResult<Vec<_>>>()?;
            self.description = Some(PyTuple::new(py, desc)?.unbind());
            let mut rows = stmt.raw_query();
            while let Some(row) = rows.next().map_err(|e| sql_error(py, e))? {
                let mut values = Vec::with_capacity(cols);
                for i in 0..cols {
                    values.push(row.get::<_, Value>(i).map_err(|e| sql_error(py, e))?);
                }
                self.rows.push_back(values);
            }
        } else {
            stmt.raw_execute().map_err(|e| sql_error(py, e))?;
        }
        self.lastrowid = Some(conn.last_insert_rowid());
        self.rowcount = if dml { conn.changes() as i64 } else { -1 };
        Ok(())
    }
    fn pop(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyTuple>>> {
        self.check(py)?;
        let Some(values) = self.rows.pop_front() else {
            return Ok(None);
        };
        let values = PyTuple::new(
            py,
            values
                .iter()
                .map(|v| value_to_python(py, v))
                .collect::<PyResult<Vec<_>>>()?,
        )?
        .unbind();
        Ok(Some(values))
    }
}
fn fetch_row(slf: &Bound<'_, SqlCursor>) -> PyResult<Option<Py<PyAny>>> {
    let py = slf.py();
    let (values, names, factory) = {
        let mut cursor = slf.borrow_mut();
        let Some(values) = cursor.pop(py)? else {
            return Ok(None);
        };
        (
            values,
            cursor.names.clone(),
            cursor.row_factory.clone_ref(py),
        )
    };
    if factory.bind(py).is_none() {
        return Ok(Some(values.into_any()));
    }
    let default = py.import("sqlite3")?.getattr("Row")?;
    if factory.bind(py).is(&default) {
        return Ok(Some(Py::new(py, SqlRow { names, values })?.into_any()));
    }
    Ok(Some(factory.bind(py).call1((slf, values))?.unbind()))
}
#[pymethods]
impl SqlCursor {
    #[pyo3(signature=(query, parameters=None))]
    fn execute<'py>(
        mut slf: PyRefMut<'py, Self>,
        query: &str,
        parameters: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let py = slf.py();
        slf.run(py, query, parameters)?;
        Ok(slf)
    }
    fn fetchone(slf: &Bound<'_, Self>) -> PyResult<Option<Py<PyAny>>> {
        fetch_row(slf)
    }
    fn fetchall(slf: &Bound<'_, Self>) -> PyResult<Vec<Py<PyAny>>> {
        let mut rows = Vec::new();
        while let Some(row) = fetch_row(slf)? {
            rows.push(row);
        }
        Ok(rows)
    }
    #[pyo3(signature=(size=None))]
    fn fetchmany(slf: &Bound<'_, Self>, size: Option<i64>) -> PyResult<Vec<Py<PyAny>>> {
        let size = size.unwrap_or(slf.borrow().arraysize);
        let mut rows = Vec::new();
        while size <= 0 || rows.len() < size as usize {
            match fetch_row(slf)? {
                Some(row) => rows.push(row),
                None => break,
            }
        }
        Ok(rows)
    }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __next__(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        fetch_row(slf)?.ok_or_else(|| PyStopIteration::new_err(()))
    }
    fn close(&mut self) {
        self.closed = true;
        self.rows.clear();
    }
    #[getter]
    fn description(&self, py: Python<'_>) -> Option<Py<PyTuple>> {
        self.description.as_ref().map(|d| d.clone_ref(py))
    }
    #[getter]
    fn lastrowid(&self) -> Option<i64> {
        self.lastrowid
    }
    #[getter]
    fn rowcount(&self) -> i64 {
        self.rowcount
    }
    #[getter]
    fn arraysize(&self) -> i64 {
        self.arraysize
    }
    #[setter]
    fn set_arraysize(&mut self, value: i64) {
        self.arraysize = value;
    }
    #[getter]
    fn row_factory(&self, py: Python<'_>) -> Py<PyAny> {
        self.row_factory.clone_ref(py)
    }
    #[setter]
    fn set_row_factory(&mut self, value: Py<PyAny>) {
        self.row_factory = value;
    }
}

#[pyclass(module = "trade_engine_rs", name = "SqlConnection")]
struct SqlConnection {
    conn: ConnectionRef,
    row_factory: Py<PyAny>,
}
#[pymethods]
impl SqlConnection {
    #[pyo3(signature=(query, parameters=None))]
    fn execute(
        &self,
        py: Python<'_>,
        query: &str,
        parameters: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<SqlCursor>> {
        let mut cursor = SqlCursor::new(py, self.conn.clone(), self.row_factory.clone_ref(py));
        cursor.run(py, query, parameters)?;
        Py::new(py, cursor)
    }
    fn cursor(&self, py: Python<'_>) -> PyResult<Py<SqlCursor>> {
        self.conn.check(py)?;
        Py::new(
            py,
            SqlCursor::new(py, self.conn.clone(), self.row_factory.clone_ref(py)),
        )
    }
    fn executescript(&self, py: Python<'_>, query: &str) -> PyResult<Py<SqlCursor>> {
        self.conn.check(py)?;
        let conn = self
            .conn
            .connection
            .lock()
            .expect("connection mutex poisoned");
        let conn = conn.as_ref().expect("checked");
        if !conn.is_autocommit() {
            conn.execute_batch("COMMIT").map_err(|e| sql_error(py, e))?;
        }
        conn.execute_batch(query).map_err(|e| sql_error(py, e))?;
        Py::new(
            py,
            SqlCursor::new(py, self.conn.clone(), self.row_factory.clone_ref(py)),
        )
    }
    fn commit(&self, py: Python<'_>) -> PyResult<()> {
        self.conn.check(py)?;
        let conn = self
            .conn
            .connection
            .lock()
            .expect("connection mutex poisoned");
        let conn = conn.as_ref().expect("checked");
        if !conn.is_autocommit() {
            conn.execute_batch("COMMIT").map_err(|e| sql_error(py, e))?;
        }
        Ok(())
    }
    fn rollback(&self, py: Python<'_>) -> PyResult<()> {
        self.conn.check(py)?;
        let conn = self
            .conn
            .connection
            .lock()
            .expect("connection mutex poisoned");
        let conn = conn.as_ref().expect("checked");
        if !conn.is_autocommit() {
            conn.execute_batch("ROLLBACK")
                .map_err(|e| sql_error(py, e))?;
        }
        Ok(())
    }
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.conn.check_thread(py)?;
        self.conn
            .connection
            .lock()
            .expect("connection mutex poisoned")
            .take();
        Ok(())
    }
    #[getter]
    fn in_transaction(&self, py: Python<'_>) -> PyResult<bool> {
        self.conn.check(py)?;
        Ok(!self
            .conn
            .connection
            .lock()
            .expect("connection mutex poisoned")
            .as_ref()
            .expect("checked")
            .is_autocommit())
    }
    #[getter]
    fn total_changes(&self, py: Python<'_>) -> PyResult<u64> {
        self.conn.check(py)?;
        Ok(self
            .conn
            .connection
            .lock()
            .expect("connection mutex poisoned")
            .as_ref()
            .expect("checked")
            .total_changes())
    }
    #[getter]
    fn isolation_level(&self) -> Option<String> {
        None
    }
    #[getter]
    fn row_factory(&self, py: Python<'_>) -> Py<PyAny> {
        self.row_factory.clone_ref(py)
    }
    #[setter]
    fn set_row_factory(&mut self, value: Py<PyAny>) {
        self.row_factory = value;
    }
    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __exit__(
        &self,
        py: Python<'_>,
        kind: &Bound<'_, PyAny>,
        _value: &Bound<'_, PyAny>,
        _trace: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        if kind.is_none() {
            self.commit(py)?;
        } else {
            self.rollback(py)?;
        }
        Ok(false)
    }
}

fn fold_row(event: &StoredEvent) -> Row {
    (
        event.kind.clone(),
        event.payload.clone(),
        event.ts.clone(),
        event.command.clone(),
        event.schema,
        Some(event.seq),
    )
}
fn stored_fold_row(py: Python<'_>, event: &StoredRow) -> PyResult<Py<PyTuple>> {
    PyTuple::new(
        py,
        [
            &event.kind,
            &event.payload,
            &event.ts,
            &event.command,
            &event.schema,
            &event.seq,
        ]
        .map(|v| value_to_python(py, v))
        .into_iter()
        .collect::<PyResult<Vec<_>>>()?,
    )
    .map(Bound::unbind)
}
fn event_carrier(py: Python<'_>, event: &StoredRow) -> PyResult<Py<PyAny>> {
    let args = PyTuple::new(
        py,
        [
            &event.account,
            &event.kind,
            &event.payload,
            &event.ts,
            &event.command,
            &event.schema,
            &event.seq,
        ]
        .map(|v| value_to_python(py, v))
        .into_iter()
        .collect::<PyResult<Vec<_>>>()?,
    )?;
    Ok(py
        .import("trade_engine.ledger.codec")?
        .call_method1("event_from_row", args)?
        .unbind())
}

#[pyclass(module = "trade_engine_rs", name = "LedgerStore")]
pub(crate) struct LedgerStore {
    store: Store,
    fold: Py<LedgerFold>,
    revision: Arc<Mutex<BTreeMap<String, u64>>>,
    folded_at: Mutex<BTreeMap<String, i64>>,
    reader: bool,
    thread: u64,
}
impl LedgerStore {
    /// The shared owner connection for same-crate native composition; never
    /// exposed to Python.
    pub(crate) fn store_connection(&self) -> SharedConnection {
        self.store.connection.clone()
    }
    fn check(&self, py: Python<'_>) -> PyResult<()> {
        ConnectionRef {
            connection: self.store.connection.clone(),
            _guard: self.store.guard.clone(),
            thread: self.thread,
        }
        .check(py)
    }
    fn ensure(&self, py: Python<'_>, account: &str, refresh: bool) -> PyResult<()> {
        self.check(py)?;
        let newest = if self.reader && refresh {
            Some(self.store.next_seq().map_err(|e| sql_error(py, e))? - 1)
        } else {
            None
        };
        let previous = self
            .folded_at
            .lock()
            .expect("fold cursor mutex poisoned")
            .get(account)
            .copied();
        let has = self.fold.borrow(py).has(account);
        if !has || (self.reader && refresh && previous != newest) {
            self.folded_at
                .lock()
                .expect("fold cursor mutex poisoned")
                .remove(account);
            let after = previous.filter(|n| has && newest.is_some_and(|m| *n < m));
            let rows = self
                .store
                .events(after, Some(account), None, None)
                .map_err(|e| sql_error(py, e))?
                .iter()
                .map(|row| stored_fold_row(py, row))
                .collect::<PyResult<Vec<_>>>()?;
            bump(&self.revision, account);
            self.fold.bind(py).call_method1(
                if after.is_some() {
                    "apply_rows"
                } else {
                    "load"
                },
                (account, rows),
            )?;
            if let Some(newest) = newest {
                self.folded_at
                    .lock()
                    .expect("fold cursor mutex poisoned")
                    .insert(account.into(), newest);
            }
        }
        Ok(())
    }
}
fn bump(revisions: &Mutex<BTreeMap<String, u64>>, account: &str) {
    *revisions
        .lock()
        .expect("fold revision mutex poisoned")
        .entry(account.into())
        .or_default() += 1;
}
#[pymethods]
impl LedgerStore {
    #[new]
    #[pyo3(signature=(path, sidecar, pid, reader_uri=None))]
    fn new(
        py: Python<'_>,
        path: &Bound<'_, PyAny>,
        sidecar: &Bound<'_, PyAny>,
        pid: &str,
        reader_uri: Option<&str>,
    ) -> PyResult<Self> {
        if reader_uri.is_none() {
            crate::plugins::refuse_factory_writer()?;
        }
        let path = crate::lock::path_from_python(path)?;
        let sidecar = crate::lock::path_from_python(sidecar)?;
        let store=match reader_uri {
            Some(uri)=>Store::reader(uri).map_err(|e| sql_error(py,e))?,
            None=>Store::open(&path,&sidecar,pid).map_err(|error| match error {
                OpenError::Sql(e)=>sql_error(py,e),
                OpenError::Lock(LockError::Contended)=>{
                    let message=format!("Another process already holds the ledger lock at {} (I4). Refusing to start a second writer.",sidecar.display());
                    match py.import("trade_engine.ledger.lock").and_then(|m| m.getattr("LedgerLockError")).and_then(|c| c.call1((message,))) {
                        Ok(e)=>PyErr::from_value(e), Err(e)=>e,
                    }
                }
                OpenError::Lock(LockError::NullPath{mkdir})=>PyValueError::new_err(if cfg!(windows) && mkdir {
                    "mkdir: embedded null character in path"
                } else if cfg!(windows) { "embedded null character" } else { "embedded null byte" }),
                OpenError::Lock(LockError::Io{error,path,mkdir})=>crate::lock::io_error(py,error,&path,mkdir).unwrap_or_else(|e| e),
            })?,
        };
        Ok(Self {
            store,
            fold: Py::new(py, LedgerFold::new(false))?,
            revision: Arc::new(Mutex::new(BTreeMap::new())),
            folded_at: Mutex::new(BTreeMap::new()),
            reader: reader_uri.is_some(),
            thread: py
                .import("threading")?
                .call_method0("get_ident")?
                .extract()?,
        })
    }
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        ConnectionRef {
            connection: self.store.connection.clone(),
            _guard: self.store.guard.clone(),
            thread: self.thread,
        }
        .check_thread(py)?;
        self.store.close();
        Ok(())
    }
    #[getter]
    fn held(&self) -> bool {
        self.store.held()
    }
    fn reopen(&self, py: Python<'_>, uri: &str) -> PyResult<()> {
        let other = Store::reader(uri).map_err(|e| sql_error(py, e))?;
        *self
            .store
            .connection
            .lock()
            .expect("connection mutex poisoned") = other
            .connection
            .lock()
            .expect("connection mutex poisoned")
            .take();
        Ok(())
    }
    fn connection(&self, py: Python<'_>) -> PyResult<Py<SqlConnection>> {
        self.check(py)?;
        Py::new(
            py,
            SqlConnection {
                conn: ConnectionRef {
                    connection: self.store.connection.clone(),
                    _guard: self.store.guard.clone(),
                    thread: self.thread,
                },
                row_factory: py.import("sqlite3")?.getattr("Row")?.unbind(),
            },
        )
    }
    fn fold_handle(&self, py: Python<'_>, account: &str) -> PyResult<Py<LedgerFold>> {
        self.ensure(py, account, false)?;
        Ok(self.fold.clone_ref(py))
    }
    fn revision(&self, py: Python<'_>, account: &str) -> PyResult<u64> {
        self.ensure(py, account, true)?;
        Ok(*self
            .revision
            .lock()
            .expect("fold revision mutex poisoned")
            .get(account)
            .expect("loaded"))
    }
    fn export(&self, py: Python<'_>, account: &str, full: bool) -> PyResult<(bool, Vec<u8>)> {
        self.ensure(py, account, false)?;
        self.fold.borrow_mut(py).export(account, full)
    }
    fn next_seq(&self, py: Python<'_>) -> PyResult<i64> {
        self.check(py)?;
        self.store.next_seq().map_err(|e| sql_error(py, e))
    }
    fn count(&self, py: Python<'_>) -> PyResult<i64> {
        self.check(py)?;
        self.store.count().map_err(|e| sql_error(py, e))
    }
    fn accounts(&self, py: Python<'_>) -> PyResult<Vec<Py<PyAny>>> {
        self.check(py)?;
        self.store
            .accounts()
            .map_err(|e| sql_error(py, e))?
            .iter()
            .map(|v| value_to_python(py, v))
            .collect()
    }
    #[pyo3(signature=(after=None, account=None, kind=None))]
    fn events(
        &self,
        py: Python<'_>,
        after: Option<&Bound<'_, PyAny>>,
        account: Option<&Bound<'_, PyAny>>,
        kind: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Vec<Py<PyAny>>> {
        self.check(py)?;
        let mut filters = Vec::new();
        for (column, op, value) in [
            ("seq", ">", after),
            ("account", "=", account),
            ("kind", "=", kind),
        ] {
            if let Some(value) = value.filter(|v| !v.is_none()) {
                filters.push((column, op, value_from_python(value, filters.len() + 1)?));
            }
        }
        self.store
            .query_events(filters)
            .map_err(|e| sql_error(py, e))?
            .iter()
            .map(|e| event_carrier(py, e))
            .collect()
    }
    #[pyo3(signature=(account, after=None))]
    fn rows(
        &self,
        py: Python<'_>,
        account: &str,
        after: Option<i64>,
    ) -> PyResult<Vec<Py<PyTuple>>> {
        self.check(py)?;
        self.store
            .events(after, Some(account), None, None)
            .map_err(|e| sql_error(py, e))?
            .iter()
            .map(|row| stored_fold_row(py, row))
            .collect()
    }
    fn event_by_command(
        &self,
        py: Python<'_>,
        command: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Py<PyAny>>> {
        self.check(py)?;
        self.store
            .by_command_value(&value_from_python(command, 1)?)
            .map_err(|e| sql_error(py, e))?
            .as_ref()
            .map(|e| event_carrier(py, e))
            .transpose()
    }
    fn has_command(&self, py: Python<'_>, command: &Bound<'_, PyAny>) -> PyResult<bool> {
        self.check(py)?;
        if !command.is_truthy()? {
            return Err(PyValueError::new_err("command_id must be non-empty"));
        }
        self.store
            .has_command_value(&value_from_python(command, 1)?)
            .map_err(|e| sql_error(py, e))
    }
    #[pyo3(signature=(account, at_seq=None))]
    fn snapshot(
        &self,
        py: Python<'_>,
        account: &str,
        at_seq: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Vec<u8>> {
        self.check(py)?;
        // snapshot() decoded the whole account before filtering its sequence.
        let rows = self
            .store
            .events(None, Some(account), None, None)
            .map_err(|e| sql_error(py, e))?;
        let events = decode_stored(py, rows)?;
        let mut selected = Vec::new();
        for event in events {
            let keep = match at_seq.filter(|a| !a.is_none()) {
                Some(seq) => event.seq.expect("stored").into_pyobject(py)?.le(seq)?,
                None => true,
            };
            if keep {
                selected.push(event);
            }
        }
        let state =
            te_core::ledger::fold::fold_account(&selected, account).map_err(refuse_ledger)?;
        Ok(te_core::ledger::json::dumps(
            &te_core::ledger::canon::canon_account(&state).map_err(refuse_ledger)?,
        )
        .into_bytes())
    }
    fn fold(&self, py: Python<'_>) -> PyResult<Vec<(String, Vec<u8>)>> {
        self.check(py)?;
        // Decode the entire log first, matching fold(self.events()) error ordering.
        let rows = self
            .store
            .events(None, None, None, None)
            .map_err(|e| sql_error(py, e))?;
        let events = decode_stored(py, rows)?;
        let states = te_core::ledger::fold::fold(&events).map_err(refuse_ledger)?;
        states
            .iter()
            .map(|(account, state)| {
                Ok((
                    account.clone(),
                    te_core::ledger::json::dumps(
                        &te_core::ledger::canon::canon_account(state).map_err(refuse_ledger)?,
                    )
                    .into_bytes(),
                ))
            })
            .collect()
    }

    fn enqueue_outbox(
        &self, py: Python<'_>, owner: &Bound<'_, PyAny>, seq: &Bound<'_, PyAny>,
        destination: &Bound<'_, PyAny>, payload: &Bound<'_, PyAny>, created_at: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        self.check(py)?;
        crate::outbox::enqueue(&self.store, owner, seq, destination, payload, created_at)
    }
    fn insert_outbox(
        &self, py: Python<'_>, seq: &Bound<'_, PyAny>, destination: &Bound<'_, PyAny>,
        payload: &Bound<'_, PyAny>, created_at: &Bound<'_, PyAny>,
    ) -> PyResult<i64> {
        self.check(py)?;
        crate::outbox::insert(&self.store, seq, destination, payload, created_at)
    }
    fn pending_outbox(
        &self, py: Python<'_>, destination: &Bound<'_, PyAny>, include_failed: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<Py<PyAny>>> {
        self.check(py)?;
        crate::outbox::pending(&self.store, destination, include_failed)
    }
    fn mark_outbox_delivered(
        &self, py: Python<'_>, owner: &Bound<'_, PyAny>, id: &Bound<'_, PyAny>, at: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        self.check(py)?;
        crate::outbox::delivered(&self.store, owner, id, at)
    }
    fn mark_outbox_failed(
        &self, py: Python<'_>, owner: &Bound<'_, PyAny>, id: &Bound<'_, PyAny>, error: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        self.check(py)?;
        crate::outbox::failed(&self.store, owner, id, error)
    }
    fn set_meta(&self, py: Python<'_>, key: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        self.check(py)?;
        self.store.set_meta(value_from_python(key, 1)?, value_from_python(value, 2)?)
            .map_err(|e| sql_error(py, e))
    }
    fn get_meta(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        self.check(py)?;
        match self.store.get_meta(value_from_python(key, 1)?).map_err(|e| sql_error(py, e))? {
            Some(value) => Ok(value_to_python(py, &value)?.bind(py).str()?.into_any().unbind()),
            None => Ok(py.None()),
        }
    }
    fn schema_version(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let key = "schema_version".into_pyobject(py)?.into_any();
        let value = self.get_meta(py, &key)?;
        let value = if value.is_none(py) {
            te_core::ledger::model::SCHEMA_VERSION.into_pyobject(py)?.into_any().unbind()
        } else { value };
        Ok(py.get_type::<PyInt>().call1((value,))?.unbind())
    }
    fn write(
        &self,
        py: Python<'_>,
        entries: Vec<(Py<PyAny>, Py<PyAny>)>,
        owner: &Bound<'_, PyAny>,
    ) -> PyResult<(Vec<Py<PyAny>>, Vec<Py<PyAny>>)> {
        self.check(py)?;
        let mut host = BindingHost {
            py,
            entries,
            owner: owner.clone(),
            fold: self.fold.clone_ref(py),
            revision: self.revision.clone(),
            written: Vec::new(),
            new_events: Vec::new(),
        };
        self.store.write(&mut host).map_err(|e| e.0)?;
        Ok((host.written, host.new_events))
    }
}

struct BindingHost<'py> {
    py: Python<'py>,
    entries: Vec<(Py<PyAny>, Py<PyAny>)>,
    owner: Bound<'py, PyAny>,
    fold: Py<LedgerFold>,
    revision: Arc<Mutex<BTreeMap<String, u64>>>,
    written: Vec<Py<PyAny>>,
    new_events: Vec<Py<PyAny>>,
}
fn decode_stored(
    py: Python<'_>,
    rows: Vec<StoredRow>,
) -> PyResult<Vec<te_core::ledger::model::Event>> {
    let codec = py.import("trade_engine.ledger.codec")?;
    // Validate all envelopes before folding, including SQLite's lax field types.
    let carriers = rows
        .iter()
        .map(|row| event_carrier(py, row))
        .collect::<PyResult<Vec<_>>>()?;
    carriers
        .iter()
        .map(|event| {
            let bytes: Vec<u8> = codec.call_method1("event_bytes", (event,))?.extract()?;
            te_core::ledger::codec::event_from_bytes(&bytes).map_err(refuse_ledger)
        })
        .collect()
}
impl BindingHost<'_> {
    fn event(&self, index: usize) -> &Bound<'_, PyAny> {
        self.entries[index].0.bind(self.py)
    }
}
impl WriteHost for BindingHost<'_> {
    type Error = HostError;
    fn len(&self) -> usize {
        self.entries.len()
    }
    fn validate_sequences(&self) -> Result<(), HostError> {
        validate_sequences(self.py, &self.entries).map_err(HostError)
    }
    fn command(&self, index: usize) -> Result<Option<String>, HostError> {
        Ok(self.event(index).getattr("command_id")?.extract()?)
    }
    fn account(&self, index: usize) -> Result<String, HostError> {
        Ok(self.event(index).getattr("account")?.extract()?)
    }
    fn load(&mut self, store: &Store, account: &str) -> Result<(), HostError> {
        if !self.fold.borrow(self.py).has(account) {
            let rows = store.events(None, Some(account), None, None)?;
            bump(&self.revision, account);
            let rows = rows
                .iter()
                .map(|row| stored_fold_row(self.py, row))
                .collect::<PyResult<Vec<_>>>()?;
            self.fold
                .bind(self.py)
                .call_method1("load", (account, rows))?;
        }
        Ok(())
    }
    fn encode(&self, index: usize) -> Result<StoredEvent, HostError> {
        let event = self.event(index);
        let payload = self
            .py
            .import("trade_engine.ledger.codec")?
            .call_method1("payload_text", (event.getattr("payload")?,))?
            .extract()?;
        Ok(StoredEvent {
            seq: 0,
            account: event.getattr("account")?.extract()?,
            kind: event.getattr("kind")?.getattr("value")?.extract()?,
            command: event.getattr("command_id")?.extract()?,
            ts: event
                .getattr("ts_utc")?
                .call_method0("isoformat")?
                .extract()?,
            payload,
            schema: event.getattr("schema_version")?.extract()?,
        })
    }
    fn replay(&mut self, event: StoredRow) -> Result<(), HostError> {
        self.written.push(event_carrier(self.py, &event)?);
        Ok(())
    }
    fn apply(&mut self, index: usize, event: &StoredEvent) -> Result<(), HostError> {
        bump(&self.revision, &event.account);
        self.fold
            .borrow_mut(self.py)
            .apply_row(&event.account, &fold_row(event))
            .map_err(refuse_ledger)?;
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("seq", event.seq)?;
        let appended = self
            .py
            .import("dataclasses")?
            .getattr("replace")?
            .call((self.event(index),), Some(&kwargs))?
            .unbind();
        self.written.push(appended.clone_ref(self.py));
        self.new_events.push(appended);
        Ok(())
    }
    fn outbox(&self, index: usize, seq: i64) -> Result<(), HostError> {
        for item in self.entries[index].1.bind(self.py).try_iter()? {
            let item = item?;
            self.owner.call_method1(
                "_insert_outbox",
                (
                    seq,
                    item.get_item(0)?,
                    item.get_item(1)?,
                    self.event(index).getattr("ts_utc")?,
                ),
            )?;
        }
        Ok(())
    }
    fn commit(&self) -> Result<(), HostError> {
        self.owner.call_method0("_commit")?;
        Ok(())
    }
    fn rollback(&self) -> Result<(), HostError> {
        self.owner.call_method0("_rollback")?;
        Ok(())
    }
    fn drop_account(&mut self, account: &str) {
        LedgerFold::drop(&mut self.fold.borrow_mut(self.py), account);
        bump(&self.revision, account);
    }
    fn is_exception(&self, error: &HostError) -> bool {
        error
            .0
            .is_instance_of::<pyo3::exceptions::PyException>(self.py)
    }
}

fn validate_sequences(py: Python<'_>, entries: &[(Py<PyAny>, Py<PyAny>)]) -> PyResult<()> {
    for (event, _) in entries {
        if !event.bind(py).getattr("seq")?.is_none() {
            return Err(PyValueError::new_err(
                "Event.seq is assigned by the ledger; pass seq=None to append",
            ));
        }
    }
    Ok(())
}

#[pyfunction]
#[pyo3(signature=(store,entries,owner))]
fn ledger_store_write(
    py: Python<'_>,
    store: Option<PyRef<'_, LedgerStore>>,
    entries: Vec<(Py<PyAny>, Py<PyAny>)>,
    owner: &Bound<'_, PyAny>,
) -> PyResult<(Vec<Py<PyAny>>, Vec<Py<PyAny>>)> {
    // Original sequence validation preceded even the unopened-connection refusal.
    validate_sequences(py, &entries)?;
    match store {
        Some(store) => store.write(py, entries, owner),
        None => {
            owner.getattr("conn")?;
            Err(pyo3::exceptions::PyRuntimeError::new_err(
                "Ledger is not open; use `with Ledger(path):`",
            ))
        }
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<LedgerStore>()?;
    m.add_class::<SqlConnection>()?;
    m.add_class::<SqlCursor>()?;
    m.add_class::<SqlRow>()?;
    m.add_function(wrap_pyfunction!(ledger_store_write, m)?)?;
    m.add_function(wrap_pyfunction!(owner_ledger, m)?)?;
    m.add_function(wrap_pyfunction!(owner_attached, m)?)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// The runtime owner's single attached store (P4c T10): the embedded interpreter
// composes the owner's Python Ledger over the ONE native connection/guard the
// Rust owner already holds. Nothing re-opens or re-locks the ledger.
// ---------------------------------------------------------------------------

static OWNER: Mutex<Option<OwnerStore>> = Mutex::new(None);

/// The owner's attached parts: the shared connection/guard Arcs plus the
/// configured ledger path, so the composed Python Ledger reports its real
/// path without re-opening anything.
struct OwnerStore {
    connection: SharedConnection,
    guard: Arc<Mutex<Option<te_host::lock::SingleInstanceGuard>>>,
    path: String,
}

impl OwnerStore {
    fn held(&self) -> bool {
        self.connection.lock().expect("connection mutex poisoned").is_some()
    }
}

/// Attach the owner's already-held store parts (crate-internal; only
/// `te_runtime` calls this from its embedded serve loop, once, before any
/// job runs). The Arcs share the actor's one connection/guard; closing still
/// happens exactly once, through the actor.
pub fn attach_owner_parts(
    _py: Python<'_>,
    connection: SharedConnection,
    guard: Arc<Mutex<Option<te_host::lock::SingleInstanceGuard>>>,
    path: String,
) -> PyResult<()> {
    let mut owner = OWNER.lock().expect("owner mutex poisoned");
    if owner.is_some() {
        return Err(pyo3::exceptions::PyRuntimeError::new_err(
            "the owner store is already attached",
        ));
    }
    *owner = Some(OwnerStore {
        connection,
        guard,
        path,
    });
    Ok(())
}

/// Detach the owner's store without closing it (crate-internal; the actor
/// owns the close).
pub(crate) fn detach_owner() {
    OWNER
        .lock()
        .expect("owner mutex poisoned")
        .take();
}

/// Whether an owner store is attached and held.
#[pyfunction]
fn owner_attached() -> bool {
    OWNER
        .lock()
        .expect("owner mutex poisoned")
        .as_ref()
        .is_some_and(OwnerStore::held)
}

/// Build the owner's Python ``Ledger`` over the attached native store.
///
/// The returned Ledger wraps the one connection/guard the Rust owner holds:
/// it never opens a second writer (I4). Called only in the owner process.
#[pyfunction]
fn owner_ledger(py: Python<'_>) -> PyResult<Py<PyAny>> {
    let parts = OWNER
        .lock()
        .expect("owner mutex poisoned")
        .as_ref()
        .map(|store| (store.connection.clone(), store.guard.clone(), store.path.clone()))
        .ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(
                "no owner store is attached; the ledger belongs to the runtime owner",
            )
        })?;
    let store = Store {
        connection: parts.0,
        guard: parts.1,
    };
    if !store.held() {
        return Err(pyo3::exceptions::PyRuntimeError::new_err(
            "the owner store is not held; the ledger belongs to the runtime owner",
        ));
    }
    let native = Py::new(
        py,
        LedgerStore {
            store,
            fold: Py::new(py, LedgerFold::new(false))?,
            revision: Arc::new(Mutex::new(BTreeMap::new())),
            folded_at: Mutex::new(BTreeMap::new()),
            reader: false,
            thread: py
                .import("threading")?
                .call_method0("get_ident")?
                .extract()?,
        },
    )?;
    let connection = native.getattr(py, "connection")?.call0(py)?;
    // The Python Ledger is built without `__init__` (which wants a path and
    // mkdirs a parent); its internals are wired to the attached native store
    // directly, so it wraps the one held writer (I4). The path is the real
    // configured one, as a Path.
    let ledger_cls = py.import("trade_engine.ledger.store")?.getattr("Ledger")?;
    let ledger = py
        .import("builtins")?
        .getattr("object")?
        .call_method1("__new__", (ledger_cls,))?;
    let path = py
        .import("pathlib")?
        .getattr("Path")?
        .call1((parts.2,))?;
    ledger.setattr("path", path)?;
    ledger.setattr("_native", native)?;
    ledger.setattr("_conn", connection)?;
    ledger.setattr(
        "_carriers",
        py.import("builtins")?.getattr("dict")?.call0()?,
    )?;
    ledger.setattr("_listeners", py.import("builtins")?.getattr("list")?.call0()?)?;
    let lock_view = py
        .import("trade_engine.ledger.store")?
        .getattr("_LockView")?
        .call1((ledger.clone(),))?;
    ledger.setattr("_lock", lock_view)?;
    Ok(ledger.into_any().unbind())
}
