//! W7-CORE-C19: the Schwab hub clients (`HubAccountsClient`, `HubOrdersClient`,
//! `HubHintListener`) move into te_py.
//!
//! The three classes are loopback HTTP/WS clients for the local Schwab hub (D3). The Rust
//! bases own the sequencing and the failure classification; the network I/O stays Python
//! stdlib. `HubAccountsClient` and `HubOrdersClient` call an injected `post`/`transport` or,
//! by default, the urllib functions `hub_client._default_post` / `orders.urllib_transport`
//! (looked up at call time). `HubHintListener` owns the RFC 6455 framing (handshake,
//! `Sec-WebSocket-Accept` check, masking, ping/pong, close and the jittered reconnect/backoff)
//! over a socket made by the injected Python `connect` (`socket.create_connection`). Every collaborator the tests hand in (`post`, `transport`,
//! `connect`, `rand`, `on_hint`, `wait`) is a Python object reached through attribute lookup,
//! so a fake stands in and the live hub is never contacted.
//!
//! The listener's counters are `AtomicI64` and its `failures` a Python dict, so its `run`
//! loop takes `&self` (no `&mut` borrow held while another thread's heartbeat reads them).
//! The GIL is released around the blocking socket reads (via `py.allow_threads`).

use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;

use pyo3::exceptions::{PyOSError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBytes, PyDict, PyInt, PyString};
use pyo3::IntoPyObjectExt;

/// Python's `urllib.parse.quote(value, safe='')`.
fn quote(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + value.len() / 3);
    for &b in value.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// `body[:200]`: the first 200 characters (Python slices by character, not byte).
fn head200(s: &str) -> String {
    s.chars().take(200).collect()
}

/// The Python classes, modules and helpers the bases reach, imported once.
struct Ctx {
    json: Py<PyAny>,
    hub_client: Py<PyAny>,
    hub_ws: Py<PyAny>,
    hub_read_error: Py<PyAny>,
    hub_order_result: Py<PyAny>,
    urlerror: Py<PyAny>,
    connection_refused: Py<PyAny>,
    urlencode: Py<PyAny>,
    urlsplit: Py<PyAny>,
    create_connection: Py<PyAny>,
    random_random: Py<PyAny>,
    os_urandom: Py<PyAny>,
    b64encode: Py<PyAny>,
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let hub_client = py.import("trade_engine.schwab.hub_client")?.into_any().unbind();
        let hub_ws = py.import("trade_engine.schwab.hub_ws")?.into_any().unbind();
        let orders = py.import("trade_engine.schwab.orders")?.into_any().unbind();
        Ok(Ctx {
            json: py.import("json")?.into_any().unbind(),
            hub_read_error: hub_client.bind(py).getattr("HubReadError")?.unbind(),
            hub_order_result: orders.bind(py).getattr("HubOrderResult")?.unbind(),
            urlerror: py.import("urllib.error")?.getattr("URLError")?.unbind(),
            connection_refused: py.import("builtins")?.getattr("ConnectionRefusedError")?.unbind(),
            urlencode: py.import("urllib.parse")?.getattr("urlencode")?.unbind(),
            urlsplit: py.import("urllib.parse")?.getattr("urlsplit")?.unbind(),
            create_connection: py.import("socket")?.getattr("create_connection")?.unbind(),
            random_random: py.import("random")?.getattr("random")?.unbind(),
            os_urandom: py.import("os")?.getattr("urandom")?.unbind(),
            b64encode: py.import("base64")?.getattr("b64encode")?.unbind(),
            hub_client: hub_client,
            hub_ws: hub_ws,
        })
    }
}

fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

/// `raise HubReadError(kind, detail)`.
fn hub_read_err(py: Python<'_>, ctx: &Ctx, kind: &str, detail: &str) -> PyErr {
    PyErr::from_value(
        ctx.hub_read_error
            .bind(py)
            .call1((kind, detail))
            .expect("HubReadError is callable with (kind, detail)"),
    )
}

/// `check_loopback(url)` from `trade_engine.schwab.hub_client`, kept Python so the exact
/// error message survives.
fn check_loopback(py: Python<'_>, ctx: &Ctx, url: &str) -> PyResult<()> {
    ctx.hub_client.bind(py).call_method1("check_loopback", (url,))?;
    Ok(())
}

// ------------------------------------------------------------------------------------------
// HubAccountsClientBase
// ------------------------------------------------------------------------------------------

/// `HubAccountsClient`: the hub's `/request` door for the two account reads (BP-5c).
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct HubAccountsClientBase {
    #[pyo3(get)]
    base_url: String,
    #[pyo3(get)]
    timeout_s: f64,
    #[pyo3(get)]
    reads: i64,
    post: Option<Py<PyAny>>,
}

impl HubAccountsClientBase {
    fn invoke_post(&self, py: Python<'_>, url: &str, body: &[u8]) -> PyResult<(i64, String)> {
        let ctx = ctx(py)?;
        match &self.post {
            Some(p) => {
                let headers = PyDict::new(py);
                headers.set_item("Content-Type", "application/json")?;
                let body_bytes = PyBytes::new(py, body);
                let (status, text): (i64, String) = p
                    .bind(py)
                    .call1((url, body_bytes, &headers, self.timeout_s))?
                    .extract()?;
                Ok((status, text))
            }
            None => {
                // `_default_post`: the urllib POST, looked up at call time.
                let headers = PyDict::new(py);
                headers.set_item("Content-Type", "application/json")?;
                let (status, text): (i64, String) = ctx
                    .hub_client
                    .bind(py)
                    .getattr("_default_post")?
                    .call1((url, PyBytes::new(py, body), &headers, self.timeout_s))?
                    .extract()?;
                Ok((status, text))
            }
        }
    }

    fn one(&self, py: Python<'_>, method: &str) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let req_path: String = ctx.hub_client.bind(py).getattr("REQUEST_PATH")?.extract()?;
        let url = format!("{}{}", self.base_url, req_path);
        let body_obj = PyDict::new(py);
        body_obj.set_item("method", method)?;
        let text: String = ctx.json.bind(py).call_method1("dumps", (&body_obj,))?.extract()?;
        let body = text.into_bytes();

        let (status, text) = match self.invoke_post(py, &url, &body) {
            Ok(ans) => ans,
            Err(e) => {
                // `except TimeoutError` / `except URLError` / `except OSError`.
                match classify_post_err(py, &ctx, &e) {
                    Some(PostErr::Timeout) => {
                        return Err(hub_read_err(
                            py,
                            &ctx,
                            "timeout",
                            &format!("{method} was not answered in time"),
                        ))
                    }
                    Some(PostErr::Unreachable) => {
                        return Err(hub_read_err(
                            py,
                            &ctx,
                            "unreachable",
                            &format!("{method} could not reach the hub"),
                        ))
                    }
                    None => return Err(e),
                }
            }
        };

        if status == 0 {
            return Err(hub_read_err(py, &ctx, "unreachable", &format!("{method} got no answer")));
        }
        if status == 403 {
            return Err(hub_read_err(py, &ctx, "refused", &format!("{method} was refused")));
        }
        if status >= 500 {
            return Err(hub_read_err(py, &ctx, "unreachable", &format!("{method} got HTTP {status}")));
        }
        if status != 200 {
            return Err(hub_read_err(py, &ctx, "refused", &format!("{method} got HTTP {status}")));
        }
        self.data(py, method, &text)
    }

    fn data(&self, py: Python<'_>, method: &str, text: &str) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let answer = match ctx.json.bind(py).call_method1("loads", (text,)) {
            Ok(a) => a,
            Err(e) => {
                if e.is_instance_of::<PyValueError>(py) {
                    return Err(hub_read_err(
                        py,
                        &ctx,
                        "bad_shape",
                        &format!("{method} did not answer with JSON"),
                    ));
                }
                return Err(e);
            }
        };
        if !answer.is_instance_of::<PyDict>() {
            return Err(hub_read_err(
                py,
                &ctx,
                "bad_shape",
                &format!("{method} did not answer with an object"),
            ));
        }
        let code = answer.call_method1("get", ("code",))?;
        if code.str()?.to_str()?.trim().to_lowercase() == "timeout" {
            return Err(hub_read_err(py, &ctx, "timeout", &format!("{method} timed out in the hub")));
        }
        let code_int = code.extract::<i64>().ok();
        let code_str = code.extract::<String>().ok();
        if code_int == Some(403)
            || code_str.as_deref() == Some("403")
            || code_str.as_deref() == Some("refused")
        {
            return Err(hub_read_err(
                py,
                &ctx,
                "refused",
                &format!("{method} is not an allowed hub read"),
            ));
        }
        let status = answer.call_method1("get", ("status",))?;
        if status.is_none() {
            return Err(hub_read_err(
                py,
                &ctx,
                "bad_shape",
                &format!("{method} answered without a status"),
            ));
        }
        let status_str = status.extract::<String>().ok();
        if status_str.as_deref() != Some("success") {
            let status_repr = status.repr()?.to_str()?.to_string();
            return Err(hub_read_err(
                py,
                &ctx,
                "refused",
                &format!("{method} answered status {status_repr}"),
            ));
        }
        if !answer.downcast::<PyDict>()?.contains("data")? {
            return Err(hub_read_err(
                py,
                &ctx,
                "bad_shape",
                &format!("{method} answered success without data"),
            ));
        }
        Ok(answer.get_item("data")?.unbind())
    }
}

enum PostErr {
    Timeout,
    Unreachable,
}

fn classify_post_err(py: Python<'_>, ctx: &Ctx, e: &PyErr) -> Option<PostErr> {
    if e.is_instance_of::<PyTimeoutError>(py) {
        return Some(PostErr::Timeout);
    }
    if e.is_instance(py, ctx.urlerror.bind(py)) {
        let reason = e.value(py).getattr("reason").ok()?;
        if reason.is_instance_of::<PyTimeoutError>() {
            return Some(PostErr::Timeout);
        }
        return Some(PostErr::Unreachable);
    }
    if e.is_instance_of::<PyOSError>(py) {
        return Some(PostErr::Unreachable);
    }
    None
}

#[pymethods]
impl HubAccountsClientBase {
    #[new]
    #[pyo3(signature = (base_url="http://127.0.0.1:8081", *, timeout_s=10.0, post=None))]
    fn new(py: Python<'_>, base_url: &str, timeout_s: f64, post: Option<Py<PyAny>>) -> PyResult<Self> {
        let ctx = ctx(py)?;
        check_loopback(py, &ctx, base_url)?;
        Ok(Self {
            base_url: base_url.trim().trim_end_matches('/').to_string(),
            timeout_s,
            reads: 0,
            post,
        })
    }

    fn read(&mut self, py: Python<'_>) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
        let ctx = ctx(py)?;
        let methods = ctx.hub_client.bind(py).getattr("READ_METHODS")?;
        let m0: String = methods.get_item(0)?.extract()?;
        let m1: String = methods.get_item(1)?.extract()?;
        let numbers = self.one(py, &m0)?;
        let accounts = self.one(py, &m1)?;
        self.reads += 1;
        let numbers_json = ctx.json.bind(py).call_method1("dumps", (&numbers,))?;
        let accounts_json = ctx.json.bind(py).call_method1("dumps", (&accounts,))?;
        Ok((numbers_json.unbind(), accounts_json.unbind()))
    }
}

// ------------------------------------------------------------------------------------------
// HubOrdersClientBase
// ------------------------------------------------------------------------------------------

enum TransportErr {
    NotSent,
    Unknown,
}

fn classify_transport_err(py: Python<'_>, ctx: &Ctx, e: &PyErr) -> Option<TransportErr> {
    if e.is_instance(py, ctx.connection_refused.bind(py)) {
        return Some(TransportErr::NotSent);
    }
    if e.is_instance_of::<PyTimeoutError>(py) {
        return Some(TransportErr::Unknown);
    }
    if e.is_instance_of::<PyOSError>(py) || e.is_instance(py, ctx.urlerror.bind(py)) {
        if let Ok(reason) = e.value(py).getattr("reason") {
            if reason.is_instance(ctx.connection_refused.bind(py)).unwrap_or(false) {
                return Some(TransportErr::NotSent);
            }
        }
        return Some(TransportErr::Unknown);
    }
    None
}

/// Build a `HubOrderResult` carrier (a frozen Python dataclass).
fn make_result(
    py: Python<'_>,
    ctx: &Ctx,
    outcome: &str,
    http: i64,
    order_id: Option<&str>,
    body: &str,
    message: &str,
) -> PyResult<Py<PyAny>> {
    let order_id_obj = match order_id {
        Some(s) => s.into_bound_py_any(py)?,
        None => py.None().into_bound_py_any(py)?,
    };
    Ok(ctx
        .hub_order_result
        .bind(py)
        .call1((outcome, http, order_id_obj, body, message))?
        .unbind())
}

fn schwab_message(py: Python<'_>, ctx: &Ctx, body: &str) -> PyResult<String> {
    let parsed = match ctx.json.bind(py).call_method1("loads", (body,)) {
        Ok(p) => p,
        Err(e) => {
            if e.is_instance_of::<PyValueError>(py) {
                return Ok(head200(body));
            }
            return Err(e);
        }
    };
    if parsed.is_instance_of::<PyDict>() {
        let msg = parsed.call_method1("get", ("message",))?;
        if msg.is_instance_of::<PyString>() {
            let s: String = msg.extract()?;
            return Ok(head200(&s));
        }
    }
    Ok(head200(body))
}

/// `HubOrdersClient`: the hub's order routes (OA-6). The `transport` collaborator (a fake in
/// tests) is a Python callable; the default is the `std::net` transport below.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct HubOrdersClientBase {
    base: String,
    timeout_s: f64,
    transport: Option<Py<PyAny>>,
}

impl HubOrdersClientBase {
    fn path(&self, account_hash: &str, order_id: Option<&str>) -> String {
        let path = format!(
            "{}/accounts/{}/orders",
            self.base,
            quote(account_hash)
        );
        match order_id {
            None => path,
            Some(id) => format!("{path}/{}", quote(id)),
        }
    }

    fn invoke_transport(
        &self,
        py: Python<'_>,
        method: &str,
        url: &str,
        body: Option<&[u8]>,
    ) -> PyResult<(i64, String)> {
        // No transport injected: `orders.urllib_transport`, looked up at call time.
        let transport = match &self.transport {
            Some(t) => t.bind(py).clone(),
            None => py.import("trade_engine.schwab.orders")?.getattr("urllib_transport")?,
        };
        let headers = PyDict::new(py);
        headers.set_item("Accept", "application/json")?;
        if body.is_some() {
            headers.set_item("Content-Type", "application/json")?;
        }
        let body_arg = match body {
            Some(b) => PyBytes::new(py, b).into_any(),
            None => py.None().into_bound(py).into_any(),
        };
        let (status, text): (i64, String) = transport
            .call1((method, url, body_arg, &headers, self.timeout_s))?
            .extract()?;
        Ok((status, text))
    }

    fn call(&self, py: Python<'_>, method: &str, url: &str, order: Option<&Bound<'_, PyAny>>) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let body: Option<Vec<u8>> = match order {
            None => None,
            Some(o) => {
                let kwargs = PyDict::new(py);
                kwargs.set_item("separators", (",", ":"))?;
                let text: String = ctx
                    .json
                    .bind(py)
                    .call_method("dumps", (o,), Some(&kwargs))?
                    .extract()?;
                Some(text.into_bytes())
            }
        };
        let (status, text) = match self.invoke_transport(py, method, url, body.as_deref()) {
            Ok(ans) => ans,
            Err(e) => {
                return match classify_transport_err(py, &ctx, &e) {
                    Some(TransportErr::NotSent) => {
                        Ok(make_result(py, &ctx, "not_sent", 0, None, "", "hub_unreachable")?)
                    }
                    Some(TransportErr::Unknown) => {
                        Ok(make_result(py, &ctx, "unknown", 0, None, "", "outcome_unknown")?)
                    }
                    None => Err(e),
                };
            }
        };
        self.classify(py, status, &text)
    }

    fn classify(&self, py: Python<'_>, status: i64, text: &str) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        let payload = if text.is_empty() {
            PyDict::new(py).into_any()
        } else {
            match ctx.json.bind(py).call_method1("loads", (text,)) {
                Ok(p) => p,
                Err(e) => {
                    if e.is_instance_of::<PyValueError>(py) {
                        py.None().into_bound(py).into_any()
                    } else {
                        return Err(e);
                    }
                }
            }
        };
        if !payload.is_instance_of::<PyDict>() {
            let outcome = if status == 0 || (status >= 200 && status < 300) || status >= 500 {
                "unknown"
            } else {
                "not_sent"
            };
            return make_result(py, &ctx, outcome, status, None, "", "bad_shape");
        }
        if payload.downcast::<PyDict>()?.contains("error")? {
            let error = payload.get_item("error")?.str()?.to_str()?.to_string();
            if status == 502 || status == 504 || status == 0 {
                return make_result(py, &ctx, "unknown", status, None, "", &error);
            }
            return make_result(py, &ctx, "not_sent", status, None, "", &error);
        }
        let schwab = payload.call_method1("get", ("status",))?;
        let order_id = payload.call_method1("get", ("order_id",))?;
        let body_raw = payload.call_method1("get", ("body",))?;
        let body = if body_raw.is_instance_of::<PyString>() {
            body_raw.str()?.to_str()?.to_string()
        } else {
            String::new()
        };
        let order_id: Option<String> = if order_id.is_none() {
            None
        } else {
            let s: String = order_id.str()?.to_str()?.to_string();
            if s.is_empty() {
                None
            } else {
                Some(s)
            }
        };
        if !schwab.is_instance_of::<PyInt>() {
            return make_result(py, &ctx, "unknown", status, None, "", "bad_shape");
        }
        let schwab_i: i64 = schwab.extract()?;
        if (200..300).contains(&schwab_i) {
            return make_result(py, &ctx, "ok", schwab_i, order_id.as_deref(), &body, "");
        }
        if (400..500).contains(&schwab_i) {
            let msg = schwab_message(py, &ctx, &body)?;
            return make_result(py, &ctx, "rejected", schwab_i, order_id.as_deref(), &body, &msg);
        }
        make_result(py, &ctx, "unknown", schwab_i, order_id.as_deref(), &body, "upstream_status")
    }
}

#[pymethods]
impl HubOrdersClientBase {
    #[new]
    #[pyo3(signature = (base_url="http://127.0.0.1:8081", transport=None, timeout_s=10.0))]
    fn new(py: Python<'_>, base_url: &str, transport: Option<Py<PyAny>>, timeout_s: f64) -> PyResult<Self> {
        let ctx = ctx(py)?;
        check_loopback(py, &ctx, base_url)?;
        Ok(Self {
            base: base_url.trim().trim_end_matches('/').to_string(),
            timeout_s,
            transport,
        })
    }

    fn place(&self, py: Python<'_>, account_hash: &str, order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let url = self.path(account_hash, None);
        self.call(py, "POST", &url, Some(order))
    }

    fn replace(
        &self,
        py: Python<'_>,
        account_hash: &str,
        order_id: &str,
        order: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let url = self.path(account_hash, Some(order_id));
        self.call(py, "PUT", &url, Some(order))
    }

    fn cancel(&self, py: Python<'_>, account_hash: &str, order_id: &str) -> PyResult<Py<PyAny>> {
        let url = self.path(account_hash, Some(order_id));
        self.call(py, "DELETE", &url, None)
    }

    fn list_orders(
        &self,
        py: Python<'_>,
        account_hash: &str,
        start: &Bound<'_, PyAny>,
        end: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Py<PyAny>>> {
        let ctx = ctx(py)?;
        let orders_mod = py.import("trade_engine.schwab.orders")?;
        let schwab_time = orders_mod.getattr("schwab_time")?;
        let from_ = schwab_time.call1((start,))?;
        let to_ = schwab_time.call1((end,))?;
        let query_kwargs = PyDict::new(py);
        query_kwargs.set_item("fromEnteredTime", &from_)?;
        query_kwargs.set_item("toEnteredTime", &to_)?;
        let query: String = ctx.urlencode.bind(py).call1((&query_kwargs,))?.extract()?;
        let url = format!("{}?{}", self.path(account_hash, None), query);
        let result = self.call(py, "GET", &url, None)?;
        let outcome: String = result.bind(py).getattr("outcome")?.extract()?;
        if outcome != "ok" {
            return Ok(None);
        }
        let body = result.bind(py).getattr("body")?;
        let body_text: String = body.str()?.to_str()?.to_string();
        let orders = match ctx.json.bind(py).call_method1("loads", (body_text,)) {
            Ok(o) => o,
            Err(e) => {
                if e.is_instance_of::<PyValueError>(py) {
                    return Ok(None);
                }
                return Err(e);
            }
        };
        if orders.is_instance_of::<pyo3::types::PyList>() {
            Ok(Some(orders.unbind()))
        } else {
            Ok(None)
        }
    }
}

// ------------------------------------------------------------------------------------------
// HubHintListenerBase
// ------------------------------------------------------------------------------------------

/// The listener's control flow, mirroring `_Stopped` / `WsError` / a classified `OSError`.
enum FlowErr {
    Stopped,
    Ws(&'static str),
    Raise(PyErr),
}

impl From<PyErr> for FlowErr {
    fn from(e: PyErr) -> Self {
        FlowErr::Raise(e)
    }
}

/// One socket plus its pushback buffer (what the handshake read past the header).
struct Conn {
    sock: Py<PyAny>,
    stop: Py<PyAny>,
    pending: Vec<u8>,
}

impl Conn {
    fn fill(&mut self, py: Python<'_>) -> Result<(), FlowErr> {
        let chunk = match self.sock.bind(py).call_method1("recv", (65536usize,)) {
            Ok(c) => c,
            Err(e) => {
                if e.is_instance_of::<PyTimeoutError>(py) {
                    if self.stop.bind(py).call_method0("is_set")?.extract::<bool>()? {
                        return Err(FlowErr::Stopped);
                    }
                    // Yield the GIL so another thread can set the stop event.
                    py.allow_threads(|| std::thread::sleep(Duration::from_micros(200)));
                    return Ok(());
                }
                return Err(FlowErr::Raise(e));
            }
        };
        let bytes: Vec<u8> = chunk.extract()?;
        if bytes.is_empty() {
            return Err(FlowErr::Ws("closed"));
        }
        self.pending.extend_from_slice(&bytes);
        Ok(())
    }

    fn recv_exact(&mut self, py: Python<'_>, n: usize) -> Result<Vec<u8>, FlowErr> {
        while self.pending.len() < n {
            self.fill(py)?;
        }
        let out = self.pending[..n].to_vec();
        self.pending.drain(..n);
        Ok(out)
    }

    fn discard(&mut self, py: Python<'_>, n: usize) -> Result<(), FlowErr> {
        let mut remaining = n;
        while remaining > 0 {
            let take = remaining.min(65536);
            self.recv_exact(py, take)?;
            remaining -= take;
        }
        Ok(())
    }

    fn read_head(&mut self, py: Python<'_>) -> Result<Vec<u8>, FlowErr> {
        let ctx = ctx(py)?;
        let handshake_max: usize = ctx.hub_ws.bind(py).getattr("HANDSHAKE_MAX_BYTES")?.extract()?;
        loop {
            if let Some(pos) = self.pending.windows(4).position(|w| w == b"\r\n\r\n") {
                let end = pos + 4;
                let head = self.pending[..end].to_vec();
                self.pending.drain(..end);
                return Ok(head);
            }
            if self.pending.len() > handshake_max {
                return Err(FlowErr::Ws("handshake"));
            }
            self.fill(py)?;
        }
    }

    fn send_frame(&self, py: Python<'_>, opcode: u8, payload: &[u8]) -> Result<(), FlowErr> {
        if payload.len() >= 126 {
            return Err(FlowErr::Raise(PyValueError::new_err(
                "control frames carry under 126 bytes",
            )));
        }
        let ctx = ctx(py)?;
        let mask: Vec<u8> = ctx.os_urandom.bind(py).call1((4u8,))?.extract()?;
        let masked: Vec<u8> = payload
            .iter()
            .enumerate()
            .map(|(i, &b)| b ^ mask[i % 4])
            .collect();
        let mut frame = vec![0x80 | opcode, 0x80 | (payload.len() as u8)];
        frame.extend_from_slice(&mask);
        frame.extend_from_slice(&masked);
        let _ = self
            .sock
            .bind(py)
            .call_method1("sendall", (PyBytes::new(py, &frame),))
            .map_err(FlowErr::Raise)?;
        Ok(())
    }
}

fn close_sock(py: Python<'_>, conn: &Conn) {
    let _ = conn.sock.bind(py).call_method0("close");
}

fn classify_connect(py: Python<'_>, ctx: &Ctx, e: &PyErr) -> &'static str {
    if e.is_instance(py, ctx.connection_refused.bind(py)) {
        "refused"
    } else if e.is_instance_of::<PyTimeoutError>(py) {
        "timeout"
    } else {
        "unreachable"
    }
}

fn log_ws_warning(py: Python<'_>, kind: &str) {
    let _ = (|| -> PyResult<()> {
        let logging = py.import("logging")?;
        let logger = logging.call_method1("getLogger", ("trade_engine.schwab.hub_ws",))?;
        logger.call_method1("warning", ("hub ws listener failed (%s)", kind))?;
        Ok(())
    })();
}

/// `HubHintListener`: reads the hub `/ws` and calls `on_hint()` per `ACCOUNTS_CHANGED` frame.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct HubHintListenerBase {
    host: String,
    port: u16,
    on_hint: Py<PyAny>,
    connect: Py<PyAny>,
    rand: Py<PyAny>,
    backoff_start: f64,
    backoff_cap: f64,
    consecutive: AtomicI64,
    hints_received: AtomicI64,
    hint_errors: AtomicI64,
    connects: AtomicI64,
    frames_oversized: AtomicI64,
    frames_malformed: AtomicI64,
    failures: Py<PyDict>,
}

impl HubHintListenerBase {
    fn open_conn(&self, py: Python<'_>, stop_event: &Bound<'_, PyAny>) -> Result<Conn, FlowErr> {
        let ctx = ctx(py)?;
        let connect_timeout: f64 = ctx.hub_ws.bind(py).getattr("CONNECT_TIMEOUT_S")?.extract()?;
        let read_timeout: f64 = ctx.hub_ws.bind(py).getattr("READ_TIMEOUT_S")?.extract()?;
        let sock = match self.connect.bind(py).call1((self.host.as_str(), self.port, connect_timeout)) {
            Ok(s) => s,
            Err(e) => return Err(FlowErr::Raise(e)),
        };
        if let Err(e) = sock.call_method1("settimeout", (read_timeout,)) {
            let _ = sock.call_method0("close");
            return Err(FlowErr::Raise(e));
        }
        let mut conn = Conn {
            sock: sock.clone().unbind(),
            stop: stop_event.clone().unbind(),
            pending: Vec::new(),
        };
        // key = base64.b64encode(os.urandom(16)).decode()
        let raw = ctx.os_urandom.bind(py).call1((16u8,)).map_err(FlowErr::Raise)?;
        let b64 = ctx.b64encode.bind(py).call1((raw,)).map_err(FlowErr::Raise)?;
        let key: String = b64.call_method0("decode")?.extract().map_err(FlowErr::Raise)?;
        let ws_path: String = ctx.hub_ws.bind(py).getattr("WS_PATH")?.extract()?;
        let request = format!(
            "GET {ws_path} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n",
            self.host, self.port
        );

        let head = match (|| -> Result<Vec<u8>, FlowErr> {
            sock.call_method1("sendall", (request.as_bytes(),))
                .map_err(FlowErr::Raise)?;
            conn.read_head(py)
        })() {
            Ok(h) => h,
            Err(e) => {
                let _ = sock.call_method0("close");
                return Err(e);
            }
        };
        // latin-1 decode
        let head: String = head.iter().map(|&b| b as char).collect();
        let lines: Vec<&str> = head.split("\r\n").collect();
        if !lines[0].starts_with("HTTP/1.1 101") {
            let _ = sock.call_method0("close");
            return Err(FlowErr::Ws("handshake"));
        }
        let mut accept: Option<String> = None;
        for line in &lines[1..] {
            let (name, _, value) = partition(line);
            if name.trim().to_lowercase() == "sec-websocket-accept" {
                accept = Some(value.trim().to_string());
            }
        }
        let accept_key: String = ctx
            .hub_ws
            .bind(py)
            .getattr("_accept_key")?
            .call1((key.as_str(),))
            .map_err(FlowErr::Raise)?
            .extract()
            .map_err(FlowErr::Raise)?;
        if accept.as_deref() != Some(accept_key.as_str()) {
            let _ = sock.call_method0("close");
            return Err(FlowErr::Ws("handshake"));
        }
        Ok(conn)
    }

    fn read_message(&self, py: Python<'_>, conn: &mut Conn) -> Result<(u8, Option<Vec<u8>>), FlowErr> {
        let ctx = ctx(py)?;
        let max_frame: usize = ctx.hub_ws.bind(py).getattr("MAX_FRAME_BYTES")?.extract()?;
        let mut opcode: Option<u8> = None;
        let mut parts: Vec<u8> = Vec::new();
        let mut total: usize = 0;
        let mut over = false;
        loop {
            let b0b1 = conn.recv_exact(py, 2)?;
            let b0 = b0b1[0];
            let b1 = b0b1[1];
            let fin = b0 & 0x80 != 0;
            let op = b0 & 0x0F;
            if b0 & 0x70 != 0 {
                return Err(FlowErr::Ws("protocol"));
            }
            if b1 & 0x80 != 0 {
                return Err(FlowErr::Ws("protocol"));
            }
            let mut length: usize = (b1 & 0x7F) as usize;
            if length == 126 {
                let ext = conn.recv_exact(py, 2)?;
                length = ((ext[0] as usize) << 8) | (ext[1] as usize);
            } else if length == 127 {
                let ext = conn.recv_exact(py, 8)?;
                let mut n: u64 = 0;
                for &b in &ext {
                    n = (n << 8) | b as u64;
                }
                length = n as usize;
            }
            if op >= 0x8 {
                if !fin || length > 125 {
                    return Err(FlowErr::Ws("protocol"));
                }
                let payload = conn.recv_exact(py, length)?;
                if op == 0x9 {
                    conn.send_frame(py, 0xA, &payload)?;
                } else if op == 0x8 {
                    return Err(FlowErr::Ws("closed"));
                }
                continue;
            }
            if op == 0x0 {
                if opcode.is_none() {
                    return Err(FlowErr::Ws("protocol"));
                }
            } else if opcode.is_none() {
                opcode = Some(op);
            } else {
                return Err(FlowErr::Ws("protocol"));
            }
            total += length;
            if !over && total <= max_frame {
                let data = conn.recv_exact(py, length)?;
                parts.extend_from_slice(&data);
            } else {
                over = true;
                conn.discard(py, length)?;
            }
            if fin {
                if over {
                    return Ok((opcode.unwrap(), None));
                }
                return Ok((opcode.unwrap(), Some(parts)));
            }
        }
    }

    fn read_loop(&self, py: Python<'_>, conn: &mut Conn, stop_event: &Bound<'_, PyAny>) -> Result<(), FlowErr> {
        loop {
            if stop_event.call_method0("is_set")?.extract::<bool>()? {
                return Ok(());
            }
            let (opcode, body) = self.read_message(py, conn)?;
            let body = match body {
                None => {
                    self.frames_oversized.fetch_add(1, Ordering::SeqCst);
                    continue;
                }
                Some(b) => b,
            };
            if opcode == 0x1 {
                self._handle(py, &body)?;
            }
        }
    }

    fn attempt(&self, py: Python<'_>, stop_event: &Bound<'_, PyAny>) -> Result<(), FlowErr> {
        let mut conn = self.open_conn(py, stop_event)?;
        self.connects.fetch_add(1, Ordering::SeqCst);
        self.consecutive.store(0, Ordering::SeqCst);
        let r = self.read_loop(py, &mut conn, stop_event);
        close_sock(py, &conn);
        r
    }

    fn count_failure(&self, py: Python<'_>, kind: &str) -> PyResult<()> {
        let failures = self.failures.bind(py);
        let cur: i64 = failures.get_item(kind)?.expect("kind in WS_KINDS").extract()?;
        failures.set_item(kind, cur + 1)?;
        Ok(())
    }
}

fn partition(line: &str) -> (&str, &str, &str) {
    match line.find(':') {
        Some(i) => (&line[..i], &line[i..i + 1], &line[i + 1..]),
        None => (line, "", ""),
    }
}

#[pymethods]
impl HubHintListenerBase {
    #[new]
    #[pyo3(signature = (base_url, *, on_hint, connect=None, rand=None, backoff_start=5.0, backoff_cap=120.0))]
    fn new(
        py: Python<'_>,
        base_url: &str,
        on_hint: Py<PyAny>,
        connect: Option<Py<PyAny>>,
        rand: Option<Py<PyAny>>,
        backoff_start: f64,
        backoff_cap: f64,
    ) -> PyResult<Self> {
        let ctx = ctx(py)?;
        check_loopback(py, &ctx, base_url)?;
        let parts = ctx.urlsplit.bind(py).call1((base_url.trim(),))?;
        let scheme: String = parts.getattr("scheme")?.extract()?;
        if scheme != "http" {
            return Err(PyValueError::new_err(
                "the hub /ws listener speaks plain ws on loopback only",
            ));
        }
        let hostname = parts.getattr("hostname")?;
        let host: String = if hostname.is_none() {
            "127.0.0.1".to_string()
        } else {
            hostname.extract()?
        };
        let port = parts.getattr("port")?;
        let port: u16 = if port.is_none() { 80 } else { port.extract()? };
        let connect = connect.unwrap_or_else(|| ctx.create_connection.clone_ref(py));
        let rand = rand.unwrap_or_else(|| ctx.random_random.clone_ref(py));
        let ws_kinds = ctx.hub_ws.bind(py).getattr("WS_KINDS")?;
        let failures: Py<PyDict> = py
            .import("builtins")?
            .getattr("dict")?
            .call_method1("fromkeys", (ws_kinds, 0))?
            .downcast_into::<PyDict>()?
            .unbind();
        Ok(Self {
            host,
            port,
            on_hint,
            connect,
            rand,
            backoff_start,
            backoff_cap,
            consecutive: AtomicI64::new(0),
            hints_received: AtomicI64::new(0),
            hint_errors: AtomicI64::new(0),
            connects: AtomicI64::new(0),
            frames_oversized: AtomicI64::new(0),
            frames_malformed: AtomicI64::new(0),
            failures,
        })
    }

    #[getter]
    fn hints_received(&self) -> i64 {
        self.hints_received.load(Ordering::SeqCst)
    }
    #[getter]
    fn hint_errors(&self) -> i64 {
        self.hint_errors.load(Ordering::SeqCst)
    }
    #[getter]
    fn connects(&self) -> i64 {
        self.connects.load(Ordering::SeqCst)
    }
    #[getter]
    fn frames_oversized(&self) -> i64 {
        self.frames_oversized.load(Ordering::SeqCst)
    }
    #[getter]
    fn frames_malformed(&self) -> i64 {
        self.frames_malformed.load(Ordering::SeqCst)
    }
    #[getter]
    fn failures(&self, py: Python<'_>) -> Py<PyDict> {
        self.failures.clone_ref(py)
    }

    #[pyo3(signature = (stop_event, wait=None))]
    fn run(
        &self,
        py: Python<'_>,
        stop_event: &Bound<'_, PyAny>,
        wait: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let wait_obj = match wait {
            Some(w) => w.clone().unbind(),
            None => stop_event.getattr("wait")?.unbind(),
        };
        loop {
            if stop_event.call_method0("is_set")?.extract::<bool>()? {
                return Ok(());
            }
            match self.attempt(py, stop_event) {
                Ok(()) => {}
                Err(FlowErr::Stopped) => return Ok(()),
                Err(FlowErr::Ws(kind)) => {
                    self.count_failure(py, kind)?;
                    log_ws_warning(py, kind);
                }
                Err(FlowErr::Raise(e)) => {
                    if e.is_instance_of::<PyOSError>(py) {
                        let ctx = ctx(py)?;
                        let kind = classify_connect(py, &ctx, &e);
                        self.count_failure(py, kind)?;
                        log_ws_warning(py, kind);
                    } else {
                        return Err(e);
                    }
                }
            }
            if stop_event.call_method0("is_set")?.extract::<bool>()? {
                return Ok(());
            }
            let delay = self._next_delay(py)?;
            if wait_obj.bind(py).call1((delay,))?.extract::<bool>()? {
                return Ok(());
            }
        }
    }

    fn _handle(&self, py: Python<'_>, body: &[u8]) -> PyResult<()> {
        let ctx = ctx(py)?;
        let acct_changed: String = ctx.hub_ws.bind(py).getattr("ACCOUNTS_CHANGED")?.extract()?;
        let prefilter = format!("\"{}\"", acct_changed);
        if !body.windows(prefilter.len()).any(|w| w == prefilter.as_bytes()) {
            return Ok(());
        }
        let obj = match ctx.json.bind(py).call_method1("loads", (PyBytes::new(py, body),)) {
            Ok(o) => o,
            Err(e) => {
                if e.is_instance_of::<PyValueError>(py) {
                    self.frames_malformed.fetch_add(1, Ordering::SeqCst);
                    return Ok(());
                }
                return Err(e);
            }
        };
        if !obj.is_instance_of::<PyDict>() {
            self.frames_malformed.fetch_add(1, Ordering::SeqCst);
            return Ok(());
        }
        let data = obj.call_method1("get", ("data",))?;
        let service = if data.is_instance_of::<PyDict>() {
            data.call_method1("get", ("service",))?
        } else {
            obj.call_method1("get", ("service",))?
        };
        let matched = match service.extract::<String>() {
            Ok(s) => s == acct_changed,
            Err(_) => false,
        };
        if !matched {
            return Ok(());
        }
        self.hints_received.fetch_add(1, Ordering::SeqCst);
        match self.on_hint.bind(py).call0() {
            Ok(_) => {}
            Err(e) => {
                self.hint_errors.fetch_add(1, Ordering::SeqCst);
                let name = e.get_type(py).name().map(|n| n.to_string()).unwrap_or_default();
                let logging = py.import("logging")?;
                let logger = logging.call_method1("getLogger", ("trade_engine.schwab.hub_ws",))?;
                let _ = logger.call_method1("warning", ("accounts hint callback failed (%s)", name));
            }
        }
        Ok(())
    }

    fn _next_delay(&self, py: Python<'_>) -> PyResult<f64> {
        let ctx = ctx(py)?;
        let consecutive = self.consecutive.fetch_add(1, Ordering::SeqCst) + 1;
        let base = (self.backoff_start * 2f64.powi((consecutive - 1) as i32)).min(self.backoff_cap);
        let rand: f64 = self.rand.bind(py).call0()?.extract()?;
        let jitter_const: f64 = ctx.hub_ws.bind(py).getattr("BACKOFF_JITTER")?.extract()?;
        let jitter = 1.0 + ((rand * 2.0) - 1.0) * jitter_const;
        Ok((base * jitter).min(self.backoff_cap))
    }
}
