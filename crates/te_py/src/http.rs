//! Carrier and stdlib text adapters; the native host owns every socket/task/reader.
use pyo3::{
    exceptions::{PyOSError, PyRuntimeError, PyValueError},
    prelude::*,
    types::PyBytes,
};
use std::sync::Arc;
use te_core::ledger::bridge;
use te_host::http::{self, Cursor, RequestText, Text};

struct Stdlib {
    bytes_io: Py<PyAny>,
    parse_headers: Py<PyAny>,
    urlparse: Py<PyAny>,
    parse_qs: Py<PyAny>,
    int: Py<PyAny>,
}
impl Stdlib {
    fn new(py: Python<'_>) -> PyResult<Self> {
        let url = py.import("urllib.parse")?;
        Ok(Self {
            bytes_io: py.import("io")?.getattr("BytesIO")?.unbind(),
            parse_headers: py.import("http.client")?.getattr("parse_headers")?.unbind(),
            urlparse: url.getattr("urlparse")?.unbind(),
            parse_qs: url.getattr("parse_qs")?.unbind(),
            int: py.import("builtins")?.getattr("int")?.unbind(),
        })
    }
}
fn diagnostic(py: Python<'_>, error: PyErr) -> String {
    format!(
        "{}: {}",
        error
            .get_type(py)
            .name()
            .map_or_else(|_| "PythonError".into(), |name| name.to_string()),
        error.value(py)
    )
}
impl Text for Stdlib {
    fn request(&self, bytes: &[u8], target: &str) -> Result<RequestText, String> {
        Python::with_gil(|py| {
            let result = (|| -> PyResult<_> {
                let stream = self.bytes_io.bind(py).call1((PyBytes::new(py, bytes),))?;
                let headers = self
                    .parse_headers
                    .bind(py)
                    .call1((stream,))?
                    .call_method0("items")?
                    .extract()?;
                // URL conversion errors are observed only after native method/Host checks.
                let target = (|| -> PyResult<_> {
                    let parsed = self.urlparse.bind(py).call1((target,))?;
                    let query = self.parse_qs.bind(py).call1((parsed.getattr("query")?,))?;
                    let first = query.call_method1("get", ("after",))?;
                    Ok((
                        parsed.getattr("path")?.extract()?,
                        if first.is_none() {
                            None
                        } else {
                            Some(first.get_item(0)?.extract()?)
                        },
                    ))
                })()
                .map_err(|e| diagnostic(py, e));
                Ok(RequestText { headers, target })
            })();
            result.map_err(|e| diagnostic(py, e))
        })
    }
    fn integer(&self, text: &str) -> Result<Option<Cursor>, String> {
        Python::with_gil(|py| {
            let result = (|| -> PyResult<_> {
                let text = text.into_pyobject(py)?;
                if !text.call_method0("isdigit")?.extract::<bool>()? {
                    return Ok(None);
                }
                let integer = self.int.bind(py).call1((text,))?;
                Ok(Some(match integer.extract::<i64>() {
                    Ok(value) => Cursor::Sequence(value),
                    Err(_) => Cursor::Overflow,
                }))
            })();
            result.map_err(|e| diagnostic(py, e))
        })
    }
}

#[pyfunction]
fn http_validate_host(host: &str) -> PyResult<()> {
    if !matches!(host, "127.0.0.1" | "localhost") {
        return Err(PyValueError::new_err(format!(
            "Engine server must bind to localhost only (127.0.0.1), got {host}"
        )));
    }
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (query=None, header=None))]
fn http_cursor(
    py: Python<'_>,
    query: Option<Vec<String>>,
    header: Option<&str>,
) -> PyResult<Option<i64>> {
    let after = query
        .as_ref()
        .map(|values| {
            values
                .first()
                .ok_or_else(|| pyo3::exceptions::PyIndexError::new_err("list index out of range"))
        })
        .transpose()?;
    match http::cursor(&Stdlib::new(py)?, after.map(String::as_str), header)
        .map_err(PyValueError::new_err)?
    {
        Err(reason) => Err(PyValueError::new_err(reason)),
        Ok(Cursor::Sequence(seq)) => Ok(Some(seq)),
        Ok(Cursor::Overflow) => Ok(None),
    }
}

#[pyclass(name = "HttpServer")]
struct Server {
    server: http::Server,
}
impl Drop for Server {
    fn drop(&mut self) {
        Python::with_gil(|py| {
            py.allow_threads(|| {
                if let Err(error) = self.server.stop() {
                    eprintln!("Engine HTTP shutdown failed: {error}");
                }
            })
        });
    }
}
#[pymethods]
impl Server {
    #[new]
    fn new(py: Python<'_>, path: String, host: &str, port: u16, ping: f64) -> PyResult<Self> {
        http_validate_host(host)?;
        let version: String = py
            .import("platform")?
            .call_method0("python_version")?
            .extract()?;
        let identity = format!("BaseHTTP/0.6 Python/{version}");
        let server =
            http::Server::start(path, host, port, ping, identity, Arc::new(Stdlib::new(py)?))
                .map_err(PyOSError::new_err)?;
        Ok(Self { server })
    }
    #[getter]
    fn server_port(&self) -> u16 {
        self.server.port
    }
    #[getter]
    fn daemon_threads(&self) -> bool {
        true
    }
    #[getter]
    fn subscriber_count(&self) -> usize {
        self.server.subscriber_count()
    }
    #[getter]
    fn errors(&self) -> Vec<String> {
        self.server.errors()
    }
    fn pause_backlog(&self, paused: bool) {
        self.server.pause_backlog(paused);
    }
    fn stop(&mut self, py: Python<'_>) -> PyResult<()> {
        py.allow_threads(|| self.server.stop())
            .map_err(PyRuntimeError::new_err)
    }
    fn broadcast(&self, py: Python<'_>, event: &Bound<'_, PyAny>) -> PyResult<()> {
        let payload: String = py
            .import("trade_engine.ledger.codec")?
            .call_method1("payload_text", (event.getattr("payload")?,))?
            .extract()?;
        let account: String = event.getattr("account")?.extract()?;
        let kind: String = event.getattr("kind")?.getattr("value")?.extract()?;
        let ts: String = event
            .getattr("ts_utc")?
            .call_method0("isoformat")?
            .extract()?;
        let command: Option<String> = event.getattr("command_id")?.extract()?;
        let schema: i128 = event.getattr("schema_version")?.extract()?;
        let seq: Option<i128> = event.getattr("seq")?.extract()?;
        let event = bridge::event_from_row(
            &account,
            &kind,
            &payload,
            &ts,
            command.as_deref(),
            schema,
            seq,
        )
        .map_err(crate::refuse_ledger)?;
        self.server
            .broadcast(http::frame(&event).map_err(PyValueError::new_err)?);
        Ok(())
    }
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Server>()?;
    module.add_function(wrap_pyfunction!(http_validate_host, module)?)?;
    module.add_function(wrap_pyfunction!(http_cursor, module)?)?;
    Ok(())
}
