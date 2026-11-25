//! W7-CORE-C4: `RiskGuardClient`'s one loopback HTTP check, moved into Rust.
//!
//! `riskguard_check(base_url, token, core_account, body_json, timeout_s)` posts one order to
//! `POST /v1/check?account=<quoted core_account>` and maps the outcome to the exact reason
//! strings `trade_engine.oms.precheck` produced: the deadline, a connect/refused/reset or
//! other I/O failure, a non-200 status, a body that will not decode as UTF-8, will not parse
//! as JSON, or fails `te_core::oms::precheck::valid_response`, and finally the allowed or
//! refused answer. The GIL is released for the whole network exchange; std `TcpStream` only,
//! no new crates.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::{Duration, Instant};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::ledger::json::{self, Json};
use te_core::oms::precheck as pc;

/// Python `urllib.parse.quote(value, safe='')`: ASCII letters, digits and `_.-~` stay; every
/// other byte is percent-encoded uppercase (`/` -> `%2F`, ` ` -> `%20`, `&` -> `%26`).
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

/// The host and port `http.client.HTTPConnection` read from the loopback base URL.
fn host_port(base_url: &str) -> PyResult<(String, u16)> {
    let rest = base_url
        .strip_prefix("http://")
        .ok_or_else(|| PyValueError::new_err("riskguard base_url must be an http URL"))?;
    let rest = rest.strip_suffix('/').unwrap_or(rest);
    let (host, port) = rest
        .rsplit_once(':')
        .ok_or_else(|| PyValueError::new_err("riskguard base_url needs an explicit host:port"))?;
    let port: u16 = port
        .parse()
        .map_err(|_| PyValueError::new_err("riskguard base_url has a bad port"))?;
    Ok((host.to_string(), port))
}

fn classify(error: &std::io::Error) -> &'static str {
    match error.kind() {
        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock => "timeout",
        _ => "network",
    }
}

/// One HTTP POST with a total deadline covering connect, write and read. Returns
/// `(status, body)` or the reason string (`"timeout"` / `"network"`).
fn post(
    host: &str,
    port: u16,
    path: &str,
    token: &str,
    body: &[u8],
    timeout: Duration,
) -> Result<(u16, Vec<u8>), &'static str> {
    let deadline = Instant::now() + timeout;
    let remaining = || deadline.saturating_duration_since(Instant::now());

    let mut stream: Option<TcpStream> = None;
    for addr in (host, port).to_socket_addrs().map_err(|_| "network")? {
        match TcpStream::connect_timeout(&addr, remaining()) {
            Ok(socket) => {
                stream = Some(socket);
                break;
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                ) =>
            {
                return Err("timeout");
            }
            Err(_) => continue,
        }
    }
    let mut stream = stream.ok_or("network")?;
    stream.set_read_timeout(Some(remaining())).map_err(|e| classify(&e))?;
    stream.set_write_timeout(Some(remaining())).map_err(|e| classify(&e))?;

    let mut request = format!(
        "POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len(),
    )
    .into_bytes();
    request.extend_from_slice(body);
    stream.write_all(&request).map_err(|e| classify(&e))?;

    let mut buf: Vec<u8> = Vec::with_capacity(4096);
    let mut tmp = [0u8; 2048];
    let header_end = loop {
        if remaining().is_zero() {
            return Err("timeout");
        }
        stream.set_read_timeout(Some(remaining())).map_err(|e| classify(&e))?;
        let n = stream.read(&mut tmp).map_err(|e| classify(&e))?;
        if n == 0 {
            return Err("network");
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(pos) = buf.windows(4).position(|window| window == b"\r\n\r\n") {
            break pos + 4;
        }
    };

    let head = std::str::from_utf8(&buf[..header_end]).map_err(|_| "network")?;
    let mut lines = head.split("\r\n");
    let status: u16 = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse().ok())
        .ok_or("network")?;
    // No Content-Length: the body runs to EOF (the request sent `Connection: close`), as
    // http.client read it. RiskGuard's axum `Json` replies always carry a length.
    let mut content_length: Option<usize> = None;
    for line in lines {
        let lower = line.to_ascii_lowercase();
        if let Some(value) = lower.strip_prefix("content-length:") {
            content_length = Some(value.trim().parse().map_err(|_| "network")?);
        }
    }

    let mut out = buf[header_end..].to_vec();
    while content_length.is_none_or(|length| out.len() < length) {
        if remaining().is_zero() {
            return Err("timeout");
        }
        stream.set_read_timeout(Some(remaining())).map_err(|e| classify(&e))?;
        let n = stream.read(&mut tmp).map_err(|e| classify(&e))?;
        if n == 0 {
            if content_length.is_none() {
                break;
            }
            return Err("network");
        }
        out.extend_from_slice(&tmp[..n]);
    }
    if let Some(length) = content_length {
        out.truncate(length);
    }
    Ok((status, out))
}

/// A Python `int` from a JSON number's decimal text, at full arbitrary precision.
fn python_int(py: Python<'_>, text: &str) -> PyResult<Py<PyAny>> {
    Ok(py.import("builtins")?.getattr("int")?.call1((text,))?.unbind())
}

#[pyfunction]
pub(crate) fn riskguard_check(
    py: Python<'_>,
    base_url: &str,
    token: &str,
    core_account: &str,
    body_json: &str,
    timeout_s: f64,
) -> PyResult<(bool, String, Option<Py<PyAny>>, Option<String>)> {
    let (host, port) = host_port(base_url)?;
    let path = format!("/v1/check?account={}", quote(core_account));
    let timeout = Duration::from_secs_f64(timeout_s.max(0.0));

    let outcome = py.allow_threads(|| post(&host, port, &path, token, body_json.as_bytes(), timeout));

    let (status, body) = match outcome {
        Ok(ok) => ok,
        Err(reason) => return Ok((false, reason.to_string(), None, None)),
    };
    if status != 200 {
        return Ok((false, format!("http_{status}"), None, None));
    }
    let text = match std::str::from_utf8(&body) {
        Ok(text) => text,
        Err(_) => return Ok((false, "invalid".to_string(), None, None)),
    };
    if !pc::valid_response(text) {
        return Ok((false, "invalid".to_string(), None, None));
    }
    let doc = match json::parse(text) {
        Ok(doc) => doc,
        Err(_) => return Ok((false, "invalid".to_string(), None, None)),
    };
    let allowed = matches!(doc.get("allowed"), Some(Json::Bool(true)));
    if allowed {
        let revision = match doc.get("verdictRevision") {
            None => None,
            Some(Json::Int(n)) => Some(python_int(py, &n.to_string())?),
            Some(Json::BigInt(text)) => Some(python_int(py, text)?),
            Some(_) => None,
        };
        let checked_at = match doc.get("checkedAt") {
            Some(Json::Str(text)) => Some(text.clone()),
            _ => None,
        };
        Ok((true, "allowed".to_string(), revision, checked_at))
    } else {
        let reason = match doc.get("reasonCode") {
            Some(Json::Str(text)) => text.clone(),
            _ => String::new(),
        };
        Ok((false, reason, None, None))
    }
}
