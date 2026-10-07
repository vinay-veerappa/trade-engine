//! Native transport with the legacy request grammar ahead of Axum routing.
//! Hyper's parser cannot preserve stdlib's version/header/error outcomes.
use axum::{body::Body, extract::State, http::Request, response::Response, Extension, Router};
use chrono::{DateTime, Utc};
use regex::Regex;
use std::{
    collections::BTreeMap,
    io,
    sync::{Arc, Mutex},
    thread::{self, JoinHandle},
    time::Duration,
};
use te_core::ledger::{
    bridge, codec,
    fold::{self, AccountState},
    json::{self, Json},
    model::{Event, LErr},
};
use te_core::money::Money;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    sync::{mpsc, watch},
    task::JoinSet,
};
use tower::ServiceExt;

pub const AFTER_ERROR: &str = "Bad Request: 'after' parameter must be a non-negative integer";
pub const HEADER_ERROR: &str = "Bad Request: 'Last-Event-ID' header must be a non-negative integer";

#[derive(Clone, Debug)]
pub enum Cursor {
    Sequence(i64),
    Overflow,
}

pub type Target = Result<(String, Option<String>), String>;
pub struct RequestText {
    pub headers: Vec<(String, String)>,
    pub target: Target,
}

/// Standard-library text conversions are adapters, not request/stream owners.
pub trait Text: Send + Sync + 'static {
    fn request(&self, bytes: &[u8], target: &str) -> Result<RequestText, String>;
    fn integer(&self, text: &str) -> Result<Option<Cursor>, String>;
}

pub fn cursor(
    text: &dyn Text,
    query: Option<&str>,
    header: Option<&str>,
) -> Result<Result<Cursor, &'static str>, String> {
    let mut after = Cursor::Sequence(0);
    if let Some(value) = query {
        let Some(value) = text.integer(value)? else {
            return Ok(Err(AFTER_ERROR));
        };
        after = value;
    }
    if let Some(value) = header {
        let Some(value) = text.integer(value)? else {
            return Ok(Err(HEADER_ERROR));
        };
        after = value;
    }
    Ok(Ok(after))
}

fn ledger_error(error: LErr) -> String {
    format!("{}: {}", error.kind, error.msg)
}

fn string(value: &str) -> Json {
    Json::Str(value.to_owned())
}
fn object(fields: Vec<(&str, Json)>) -> Json {
    Json::Obj(fields.into_iter().map(|(k, v)| (k.to_owned(), v)).collect())
}
fn decimal(value: &Money) -> Json {
    string(&value.canon())
}
fn optional(value: &Option<Money>) -> Json {
    value.as_ref().map_or(Json::Null, decimal)
}

pub fn account(state: &AccountState) -> Result<Json, String> {
    let mut positions: Vec<(String, Json)> = Vec::new();
    for (instrument, position) in state.positions.iter() {
        let symbol = instrument.symbol().map_err(ledger_error)?;
        let value = object(vec![
            ("symbol", string(&symbol)),
            ("quantity", decimal(&position.quantity)),
            ("avg_cost", decimal(&position.avg_cost)),
            ("realized_pnl", decimal(&position.realized_pnl)),
        ]);
        // Python's symbol-keyed dict keeps the last value for equal symbols.
        if let Some((_, old)) = positions.iter_mut().find(|(key, _)| key == &symbol) {
            *old = value;
        } else {
            positions.push((symbol, value));
        }
    }
    let mut orders = Vec::new();
    for (id, order) in state.orders.iter() {
        orders.push((
            id.clone(),
            object(vec![
                ("order_id", string(&order.order_id)),
                (
                    "instrument",
                    string(&order.instrument.symbol().map_err(ledger_error)?),
                ),
                ("side", string(order.side.value())),
                ("state", string(order.state.value())),
                ("quantity", decimal(&order.quantity)),
                ("limit_price", optional(&order.limit_price)),
                ("stop_price", optional(&order.stop_price)),
            ]),
        ));
    }
    Ok(object(vec![
        ("account_id", string(&state.account_id)),
        ("cash", decimal(&state.cash)),
        ("realized_pnl", decimal(&state.realized_pnl)),
        ("last_seq", Json::Int(state.last_seq)),
        ("positions", Json::Obj(positions)),
        ("orders", Json::Obj(orders)),
    ]))
}

fn events(path: &str, after: Option<i64>) -> Result<Vec<Event>, String> {
    let reader = crate::store::Store::reader(path).map_err(|e| e.to_string())?;
    let connection = reader.connection.lock().expect("reader mutex poisoned");
    let connection = connection.as_ref().ok_or("reader closed")?;
    let query = if after.is_some() {
        "SELECT * FROM events WHERE seq > ? ORDER BY seq ASC"
    } else {
        "SELECT * FROM events ORDER BY seq ASC"
    };
    let mut statement = connection.prepare(query).map_err(|e| e.to_string())?;
    let mut rows = statement
        .query(rusqlite::params_from_iter(after))
        .map_err(|e| e.to_string())?;
    let mut output = Vec::new();
    while let Some(row) = rows.next().map_err(|e| e.to_string())? {
        let result = (|| -> rusqlite::Result<_> {
            Ok((
                row.get::<_, String>("account")?,
                row.get::<_, String>("kind")?,
                row.get::<_, String>("payload_json")?,
                row.get::<_, String>("ts_utc")?,
                row.get::<_, Option<String>>("command_id")?,
                row.get::<_, i64>("schema_version")?,
                row.get::<_, i64>("seq")?,
            ))
        })()
        .map_err(|e| e.to_string())?;
        output.push(
            bridge::event_from_row(
                &result.0,
                &result.1,
                &result.2,
                &result.3,
                result.4.as_deref(),
                result.5 as i128,
                Some(result.6 as i128),
            )
            .map_err(ledger_error)?,
        );
    }
    Ok(output)
}

pub fn snapshot(path: &str) -> Result<Vec<u8>, String> {
    let events = events(path, None)?;
    let seq = events.last().and_then(|e| e.seq).unwrap_or(0);
    let states = fold::fold(&events).map_err(ledger_error)?;
    let accounts = states
        .iter()
        .map(|(key, state)| Ok((key.clone(), account(state)?)))
        .collect::<Result<Vec<_>, String>>()?;
    Ok(json::dumps(&object(vec![
        ("seq", Json::Int(seq)),
        ("accounts", Json::Obj(accounts)),
    ]))
    .into_bytes())
}

pub fn frame(event: &Event) -> Result<Frame, String> {
    let payload = codec::encode_event(event).map_err(ledger_error)?;
    let id = event.seq.map_or("None".into(), |id| id.to_string());
    Ok(Frame {
        seq: event.seq,
        bytes: format!("id: {id}\ndata: {}\n\n", json::dumps(&payload)).into_bytes(),
    })
}

#[derive(Clone)]
pub struct Frame {
    pub seq: Option<i128>,
    pub bytes: Vec<u8>,
}

struct Api {
    path: String,
    identity: String,
    text: Arc<dyn Text>,
    origin: Regex,
    ping: f64,
    subscribers: Mutex<BTreeMap<u64, mpsc::UnboundedSender<Frame>>>,
    serial: Mutex<u64>,
    errors: Mutex<Vec<String>>,
    stop: watch::Sender<bool>,
    backlog_paused: watch::Sender<bool>,
    control: Mutex<Option<Arc<dyn Control>>>,
}

/// The versioned runtime control surface, separate from the legacy GET API.
/// Implementations authorize capability/generation and never self-RPC.
pub trait Control: Send + Sync + 'static {
    /// POST /v1/runtime/jobs: authorize then admit. Returns the record JSON
    /// plus whether this admission inserted a new record.
    fn submit(&self, body: &[u8], capability: Option<&str>) -> Result<(Json, bool), ControlError>;
    /// GET /v1/runtime/jobs/<id>
    fn job(&self, id: &str) -> Result<Json, ControlError>;
    /// GET /v1/runtime/status
    fn status(&self) -> Result<Json, ControlError>;
}

/// Exact control refusals; the wire layer preserves type and message.
#[derive(Clone, Debug)]
pub struct ControlError {
    pub kind: &'static str,
    pub message: String,
}
impl ControlError {
    pub fn new(kind: &'static str, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }
}
impl Api {
    fn failure(&self, error: impl Into<String>) {
        let error = error.into();
        eprintln!("Engine HTTP request failed: {error}");
        self.errors
            .lock()
            .expect("errors mutex poisoned")
            .push(error);
    }
    fn subscribe(self: &Arc<Self>) -> (Subscription, mpsc::UnboundedReceiver<Frame>) {
        let (sender, receiver) = mpsc::unbounded_channel();
        let mut serial = self.serial.lock().expect("serial mutex poisoned");
        *serial += 1;
        let id = *serial;
        self.subscribers
            .lock()
            .expect("subscribers mutex poisoned")
            .insert(id, sender);
        (
            Subscription {
                api: self.clone(),
                id,
            },
            receiver,
        )
    }
}
struct Subscription {
    api: Arc<Api>,
    id: u64,
}
impl Drop for Subscription {
    fn drop(&mut self) {
        self.api
            .subscribers
            .lock()
            .expect("subscribers mutex poisoned")
            .remove(&self.id);
    }
}

pub struct Server {
    api: Arc<Api>,
    pub port: u16,
    thread: Option<JoinHandle<()>>,
}
impl Server {
    pub fn start(
        path: String,
        host: &str,
        port: u16,
        ping: f64,
        identity: String,
        text: Arc<dyn Text>,
    ) -> io::Result<Self> {
        if !matches!(host, "127.0.0.1" | "localhost") {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("Engine server must bind to localhost only (127.0.0.1), got {host}"),
            ));
        }
        let listener = std::net::TcpListener::bind(("127.0.0.1", port))?;
        listener.set_nonblocking(true)?;
        let port = listener.local_addr()?.port();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        let (stop, _) = watch::channel(false);
        let (backlog_paused, _) = watch::channel(false);
        let api = Arc::new(Api {
            path,
            identity,
            text,
            origin: Regex::new(r"(?i)^https?://(localhost|127\.0\.0\.1)(:\d+)?$")
                .expect("origin regex"),
            ping,
            subscribers: Mutex::new(BTreeMap::new()),
            serial: Mutex::new(0),
            errors: Mutex::new(Vec::new()),
            stop,
            backlog_paused,
            control: Mutex::new(None),
        });
        let owner = api.clone();
        let thread = thread::Builder::new().name("engine-http".into()).spawn(move || {
            runtime.block_on(async move {
                let listener = match TcpListener::from_std(listener) {
                    Ok(listener) => listener,
                    Err(error) => { owner.failure(error.to_string()); return; }
                };
                let router = Router::new().fallback(route).with_state(owner.clone());
                let mut stopped = owner.stop.subscribe();
                let mut tasks = JoinSet::new();
                loop {
                    if *stopped.borrow_and_update() { break; }
                    tokio::select! {
                        biased;
                        _ = stopped.changed() => break,
                        result = listener.accept() => match result {
                            Ok((socket, _)) => {
                                let api = owner.clone();
                                let service = router.clone();
                                let mut stop = api.stop.subscribe();
                                tasks.spawn(async move {
                                    if *stop.borrow_and_update() { return; }
                                    tokio::select! {
                                        biased;
                                        _ = stop.changed() => {},
                                        result = connection(socket, service, api.clone()) => {
                                            if let Err(error) = result {
                                                if !matches!(error.kind(), io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset | io::ErrorKind::ConnectionAborted) {
                                                    api.failure(error.to_string());
                                                }
                                            }
                                        }
                                    }
                                });
                            }
                            Err(error) => { owner.failure(error.to_string()); break; }
                        },
                        result = tasks.join_next(), if !tasks.is_empty() => {
                            if let Some(Err(error)) = result { owner.failure(error.to_string()); }
                        }
                    }
                }
                drop(listener);
                while let Some(result) = tasks.join_next().await {
                    if let Err(error) = result { owner.failure(error.to_string()); }
                }
            });
        })?;
        Ok(Self {
            api,
            port,
            thread: Some(thread),
        })
    }
    pub fn broadcast(&self, frame: Frame) {
        self.api
            .subscribers
            .lock()
            .expect("subscribers mutex poisoned")
            .retain(|_, sender| sender.send(frame.clone()).is_ok());
    }
    pub fn subscriber_count(&self) -> usize {
        self.api
            .subscribers
            .lock()
            .expect("subscribers mutex poisoned")
            .len()
    }
    pub fn errors(&self) -> Vec<String> {
        self.api
            .errors
            .lock()
            .expect("errors mutex poisoned")
            .clone()
    }
    /// Attach the versioned runtime control surface. Without it every
    /// /v1/runtime route keeps the legacy 404/501 outcomes.
    pub fn attach_control(&self, control: Arc<dyn Control>) {
        *self.api.control.lock().expect("control mutex poisoned") = Some(control);
    }
    pub fn pause_backlog(&self, paused: bool) {
        self.api.backlog_paused.send_replace(paused);
    }
    pub fn stop(&mut self) -> Result<(), String> {
        self.api.stop.send_replace(true);
        if let Some(thread) = self.thread.take() {
            thread
                .join()
                .map_err(|_| "HTTP owner thread panicked".to_owned())?;
        }
        Ok(())
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        if let Err(error) = self.stop() {
            self.api.failure(error);
        }
    }
}

#[derive(Clone)]
struct Raw {
    method: String,
    target: Target,
    headers: Vec<(String, String)>,
    http09: bool,
    body: Vec<u8>,
}
impl Raw {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }
}
#[derive(Clone)]
struct Reply {
    code: u16,
    reason: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
    stream: Option<Cursor>,
}
fn error(code: u16, message: String, explanation: &str, head: bool) -> Reply {
    let escape = |s: &str| {
        s.replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
    };
    let body = format!("<!DOCTYPE HTML>\n<html lang=\"en\">\n    <head>\n        <meta charset=\"utf-8\">\n        <title>Error response</title>\n    </head>\n    <body>\n        <h1>Error response</h1>\n        <p>Error code: {code}</p>\n        <p>Message: {}.</p>\n        <p>Error code explanation: {code} - {}.</p>\n    </body>\n</html>\n", escape(&message), escape(explanation)).into_bytes();
    Reply {
        code,
        reason: message,
        headers: vec![
            ("Connection".into(), "close".into()),
            ("Content-Type".into(), "text/html;charset=utf-8".into()),
            ("Content-Length".into(), body.len().to_string()),
        ],
        body: if head { Vec::new() } else { body },
        stream: None,
    }
}
fn cors(api: &Api, raw: &Raw) -> Option<String> {
    raw.header("Origin")
        .map(|origin| origin.trim_matches(whitespace))
        .filter(|origin| api.origin.is_match(origin))
        .map(str::to_owned)
}

/// Control replies preserve the exact refusal type and message as JSON.
fn control_reply(code: u16, failure: &ControlError) -> Reply {
    let body = object(vec![(
        "error",
        object(vec![
            ("type", string(failure.kind)),
            ("message", string(&failure.message)),
        ]),
    )]);
    let body = json::dumps(&body).into_bytes();
    Reply {
        code,
        reason: if code == 400 { "Bad Request" } else { "Forbidden" }.into(),
        headers: vec![
            ("Connection".into(), "close".into()),
            ("Content-Type".into(), "application/json".into()),
            ("Content-Length".into(), body.len().to_string()),
        ],
        body,
        stream: None,
    }
}
async fn route(State(api): State<Arc<Api>>, Extension(raw): Extension<Raw>) -> Response {
    let result = handle(&api, &raw);
    let mut response = Response::new(Body::empty());
    match result {
        Ok(reply) => {
            response.extensions_mut().insert(reply);
        }
        Err(error) => {
            api.failure(error);
        }
    }
    response
}
fn handle(api: &Api, raw: &Raw) -> Result<Reply, String> {
    // The versioned control surface is separate from the legacy GET API:
    // it accepts POST, refuses cross-origin and never serves legacy bytes.
    if let Some(control) = api.control.lock().expect("control mutex poisoned").clone() {
        if let Some(target) = raw.target.as_ref().ok() {
            if target.0.starts_with("/v1/runtime") {
                let host = raw
                    .header("Host")
                    .unwrap_or("")
                    .split(':')
                    .next()
                    .unwrap_or("")
                    .trim_matches(whitespace)
                    .to_lowercase();
                if !matches!(host.as_str(), "127.0.0.1" | "localhost") {
                    return Ok(error(
                        403,
                        "Forbidden: Invalid Host header".into(),
                        "Request forbidden -- authorization will not help",
                        false,
                    ));
                }
                if let Some(origin) = raw.header("Origin") {
                    if !api.origin.is_match(origin.trim_matches(whitespace)) {
                        return Ok(control_reply(
                            403,
                            &ControlError::new(
                                "RuntimeOriginError",
                                "control requests cannot be cross-origin",
                            ),
                        ));
                    }
                }
                let path = target.0.as_str();
                let path = path.strip_prefix("/v1/runtime").unwrap_or(path);
                let outcome = match (raw.method.as_str(), path) {
                    ("POST", "/jobs") => control
                        .submit(&raw.body, raw.header("X-TE-Capability"))
                        .map(|(record, inserted)| (201u16, inserted, record)),
                    ("GET", path) if path == "/status" => {
                        control.status().map(|status| (200u16, false, status))
                    }
                    ("GET", path) if let Some(id) = path.strip_prefix("/jobs/") => {
                        control.job(id).map(|job| (200u16, false, job))
                    }
                    _ => Err(ControlError::new(
                        "RuntimeRouteError",
                        "unsupported runtime route or method",
                    )),
                };
                return match outcome {
                    Ok((code, _inserted, body)) => Ok(Reply {
                        code,
                        reason: if code == 201 { "Created" } else { "OK" }.into(),
                        headers: vec![("Content-Type".into(), "application/json".into())],
                        body: json::dumps(&body).into_bytes(),
                        stream: None,
                    }),
                    Err(failure) => Ok(control_reply(400, &failure)),
                };
            }
        }
    }
    if !matches!(raw.method.as_str(), "GET" | "OPTIONS") {
        return Ok(error(
            501,
            format!("Unsupported method ({})", codec::py_repr(&raw.method)),
            "Server does not support this operation",
            raw.method == "HEAD",
        ));
    }
    let host = raw
        .header("Host")
        .unwrap_or("")
        .split(':')
        .next()
        .unwrap_or("")
        .trim_matches(whitespace)
        .to_lowercase();
    if !matches!(host.as_str(), "127.0.0.1" | "localhost") {
        return Ok(error(
            403,
            "Forbidden: Invalid Host header".into(),
            "Request forbidden -- authorization will not help",
            false,
        ));
    }
    let origin = cors(api, raw);
    if raw.method == "OPTIONS" {
        let mut headers = Vec::new();
        if let Some(origin) = origin {
            headers.extend([
                ("Access-Control-Allow-Origin".into(), origin),
                (
                    "Access-Control-Allow-Methods".into(),
                    "GET, POST, OPTIONS".into(),
                ),
                (
                    "Access-Control-Allow-Headers".into(),
                    "Content-Type, Last-Event-ID".into(),
                ),
            ]);
        }
        return Ok(Reply {
            code: 204,
            reason: "No Content".into(),
            headers,
            body: Vec::new(),
            stream: None,
        });
    }
    let (path, query) = raw.target.clone()?;
    let mut reply = Reply {
        code: 200,
        reason: "OK".into(),
        headers: Vec::new(),
        body: Vec::new(),
        stream: None,
    };
    match path.as_str() {
        "/events" => {
            let after = match cursor(
                api.text.as_ref(),
                query.as_deref(),
                raw.header("Last-Event-ID"),
            )? {
                Ok(after) => after,
                Err(reason) => {
                    return Ok(error(
                        400,
                        reason.into(),
                        "Bad request syntax or unsupported method",
                        false,
                    ))
                }
            };
            reply.headers.extend([
                ("Content-Type".into(), "text/event-stream".into()),
                ("Cache-Control".into(), "no-cache".into()),
                ("Connection".into(), "keep-alive".into()),
            ]);
            reply.stream = Some(after);
        }
        "/health" => {
            let reader = crate::store::Store::reader(&api.path).map_err(|e| e.to_string())?;
            let connection = reader.connection.lock().expect("reader mutex poisoned");
            let connection = connection.as_ref().ok_or("reader closed")?;
            let (seq, count): (i64, i64) = connection
                .query_row(
                    "SELECT COALESCE(MAX(seq), 0), COUNT(*) FROM events",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .map_err(|e| e.to_string())?;
            reply.body = json::dumps(&object(vec![
                ("count", Json::Int(count as i128)),
                ("ok", Json::Bool(true)),
                ("seq", Json::Int(seq as i128)),
            ]))
            .into_bytes();
        }
        "/snapshot" => reply.body = snapshot(&api.path)?,
        _ => {
            return Ok(error(
                404,
                "Not Found".into(),
                "Nothing matches the given URI",
                false,
            ))
        }
    }
    if reply.stream.is_none() {
        reply
            .headers
            .push(("Content-Type".into(), "application/json".into()));
        reply
            .headers
            .push(("Content-Length".into(), reply.body.len().to_string()));
    }
    if let Some(origin) = origin {
        reply
            .headers
            .push(("Access-Control-Allow-Origin".into(), origin));
    }
    Ok(reply)
}

fn whitespace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{1f}' | '\u{85}')
}
fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|b| char::from(*b)).collect()
}

fn simple_headers(bytes: &[u8]) -> Option<Vec<(String, String)>> {
    if !bytes.is_ascii() || !bytes.ends_with(b"\r\n\r\n") {
        return None;
    }
    let mut headers = Vec::new();
    for line in std::str::from_utf8(bytes).ok()?.split("\r\n") {
        if line.is_empty() {
            break;
        }
        let (name, value) = line.split_once(':')?;
        if name.is_empty()
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
            || !value
                .bytes()
                .all(|b| b == b'\t' || (b' '..=b'~').contains(&b))
        {
            return None;
        }
        headers.push((
            name.to_owned(),
            value.trim_start_matches([' ', '\t']).to_owned(),
        ));
    }
    Some(headers)
}

fn request_text(text: &dyn Text, bytes: &[u8], target: &str) -> Result<RequestText, String> {
    // This disjoint canonical subset needs no interpreter observation. All email
    // folding, unusual fields, URL/query and Unicode cases use the stdlib adapter.
    if matches!(target, "/health" | "/snapshot" | "/events") {
        if let Some(headers) = simple_headers(bytes) {
            return Ok(RequestText {
                headers,
                target: Ok((target.to_owned(), None)),
            });
        }
    }
    text.request(bytes, target)
}

async fn line(reader: &mut BufReader<TcpStream>) -> io::Result<Vec<u8>> {
    let mut line = Vec::new();
    loop {
        let buffer = reader.fill_buf().await?;
        if buffer.is_empty() {
            return Ok(line);
        }
        let count = buffer
            .iter()
            .position(|b| *b == b'\n')
            .map_or(buffer.len(), |i| i + 1)
            .min(65537 - line.len());
        let complete = buffer[count - 1] == b'\n';
        line.extend_from_slice(&buffer[..count]);
        reader.consume(count);
        if complete || line.len() == 65537 {
            return Ok(line);
        }
    }
}
enum Parsed {
    Empty,
    Error(Reply),
    LegacyError(Reply),
    Request(Raw),
}
async fn parse(reader: &mut BufReader<TcpStream>, text: &dyn Text) -> io::Result<Parsed> {
    let bytes = line(reader).await?;
    if bytes.len() > 65536 {
        return Ok(Parsed::Error(error(
            414,
            "URI Too Long".into(),
            "URI is too long",
            false,
        )));
    }
    let request = latin1(&bytes);
    let request = request.trim_end_matches(['\r', '\n']);
    let words: Vec<_> = request
        .split(whitespace)
        .filter(|word| !word.is_empty())
        .collect();
    if words.is_empty() {
        return Ok(Parsed::Empty);
    }
    let mut http09 = false;
    if words.len() >= 3 {
        let version = words[words.len() - 1];
        let parsed = version
            .strip_prefix("HTTP/")
            .and_then(|v| v.split_once('.'))
            .and_then(|(major, minor)| {
                if major.is_empty()
                    || minor.is_empty()
                    || major.len() > 10
                    || minor.len() > 10
                    || !major.bytes().all(|c| c.is_ascii_digit())
                    || !minor.bytes().all(|c| c.is_ascii_digit())
                {
                    return None;
                }
                Some((major.parse::<u64>().ok()?, minor.parse::<u64>().ok()?))
            });
        let Some((major, _)) = parsed else {
            return Ok(Parsed::Error(error(
                400,
                format!("Bad request version ({})", codec::py_repr(version)),
                "Bad request syntax or unsupported method",
                false,
            )));
        };
        if major >= 2 {
            return Ok(Parsed::Error(error(
                505,
                format!("Invalid HTTP version ({})", &version[5..]),
                "Cannot fulfill request",
                false,
            )));
        }
        http09 = version == "HTTP/0.9";
    }
    if !(2..=3).contains(&words.len()) {
        return Ok(Parsed::Error(error(
            400,
            format!("Bad request syntax ({})", codec::py_repr(request)),
            "Bad request syntax or unsupported method",
            false,
        )));
    }
    if words.len() == 2 && words[0] != "GET" {
        return Ok(Parsed::Error(error(
            400,
            format!("Bad HTTP/0.9 request type ({})", codec::py_repr(words[0])),
            "Bad request syntax or unsupported method",
            false,
        )));
    }
    let target = if words[1].starts_with("//") {
        format!("/{}", words[1].trim_start_matches('/'))
    } else {
        words[1].to_owned()
    };
    let mut converted = RequestText {
        headers: Vec::new(),
        target: Err("HTTP/0.9 has no Host header".into()),
    };
    if words.len() != 2 {
        let mut bytes = Vec::new();
        for index in 0.. {
            let header = line(reader).await?;
            if header.len() > 65536 {
                let reply = error(
                    431,
                    "Line too long".into(),
                    "got more than 65536 bytes when reading header line",
                    words[0] == "HEAD",
                );
                return Ok(if http09 {
                    Parsed::LegacyError(reply)
                } else {
                    Parsed::Error(reply)
                });
            }
            if index >= 100 {
                let reply = error(
                    431,
                    "Too many headers".into(),
                    "got more than 100 headers",
                    words[0] == "HEAD",
                );
                return Ok(if http09 {
                    Parsed::LegacyError(reply)
                } else {
                    Parsed::Error(reply)
                });
            }
            let end = header.is_empty() || header == b"\r\n" || header == b"\n";
            bytes.extend(header);
            if end {
                break;
            }
        }
        converted = request_text(text, &bytes, &target).map_err(io::Error::other)?;
    } else {
        http09 = true;
    }
    Ok(Parsed::Request(Raw {
        method: words[0].to_owned(),
        target: converted.target,
        headers: converted.headers,
        http09,
        body: Vec::new(),
    }))
}
fn date() -> io::Result<String> {
    let micros =
        i64::try_from(crate::clock::system_utc_microseconds()).map_err(io::Error::other)?;
    let instant = DateTime::<Utc>::from_timestamp_micros(micros)
        .ok_or_else(|| io::Error::other("transport date out of range"))?;
    Ok(instant.format("%a, %d %b %Y %H:%M:%S GMT").to_string())
}
async fn write_reply(
    reader: &mut BufReader<TcpStream>,
    api: &Api,
    reply: &Reply,
    http09: bool,
) -> io::Result<()> {
    let mut output = Vec::new();
    if !http09 {
        let mut headers = format!(
            "HTTP/1.0 {} {}\r\nServer: {}\r\nDate: {}\r\n",
            reply.code,
            reply.reason,
            api.identity,
            date()?
        );
        for (key, value) in &reply.headers {
            headers.push_str(&format!("{key}: {value}\r\n"));
        }
        headers.push_str("\r\n");
        let bytes: Result<Vec<u8>, _> = headers.chars().map(|c| u8::try_from(c as u32)).collect();
        output = bytes.map_err(io::Error::other)?;
    }
    output.extend_from_slice(&reply.body);
    reader.get_mut().write_all(&output).await
}
async fn connection(socket: TcpStream, router: Router, api: Arc<Api>) -> io::Result<()> {
    socket.set_nodelay(true)?;
    let mut reader = BufReader::new(socket);
    let raw = match parse(&mut reader, api.text.as_ref()).await? {
        Parsed::Empty => return Ok(()),
        Parsed::Error(reply) => {
            write_reply(&mut reader, &api, &reply, false).await?;
            return Ok(());
        }
        Parsed::LegacyError(reply) => {
            write_reply(&mut reader, &api, &reply, true).await?;
            return Ok(());
        }
        Parsed::Request(mut raw) => {
            // The control surface is the only POST consumer; read its body.
            // A bounded length refuses oversize submissions instead of
            // buffering unbounded input.
            if raw.method == "POST" {
                const MAX_CONTROL_BODY: usize = 1 << 20;
                if let Some(length) = raw.header("Content-Length") {
                    let length: usize = length.trim().parse().map_err(|_| {
                        io::Error::other("RuntimeRequestError: invalid Content-Length")
                    })?;
                    if length > MAX_CONTROL_BODY {
                        return Err(io::Error::other(
                            "RuntimeRequestError: control request body exceeds 1 MiB",
                        ));
                    }
                    let mut body = vec![0u8; length];
                    reader.read_exact(&mut body).await?;
                    raw.body = body;
                }
            }
            raw
        }
    };
    let http09 = raw.http09;
    let request = Request::builder()
        .uri("/")
        .extension(raw)
        .body(Body::empty())
        .map_err(io::Error::other)?;
    let mut response = router
        .oneshot(request)
        .await
        .expect("Axum router is infallible");
    let Some(reply) = response.extensions_mut().remove::<Reply>() else {
        return Ok(());
    };
    write_reply(&mut reader, &api, &reply, http09).await?;
    let Some(cursor) = reply.stream else {
        return Ok(());
    };
    reader.get_mut().write_all(b"retry: 1000\n\n").await?;
    let (_subscription, mut queue) = api.subscribe();
    let mut paused = api.backlog_paused.subscribe();
    while *paused.borrow_and_update() {
        paused.changed().await.map_err(io::Error::other)?;
    }
    let after = match cursor {
        Cursor::Sequence(after) => after,
        Cursor::Overflow => {
            return Err(io::Error::other(
                "OverflowError: Python int too large to convert to SQLite INTEGER",
            ))
        }
    };
    let mut max_sent = after as i128;
    for event in events(&api.path, Some(after)).map_err(io::Error::other)? {
        let event = frame(&event).map_err(io::Error::other)?;
        reader.get_mut().write_all(&event.bytes).await?;
        if let Some(seq) = event.seq {
            max_sent = max_sent.max(seq);
        }
    }
    let ping = Duration::try_from_secs_f64(api.ping).map_err(|_| {
        io::Error::other(if api.ping < 0.0 {
            "ValueError: 'timeout' must be a non-negative number"
        } else {
            "OverflowError: timestamp out of range for platform time_t"
        })
    })?;
    loop {
        tokio::select! {
            event = queue.recv() => {
                let Some(event) = event else { break; };
                if event.seq.is_some_and(|seq| seq <= max_sent) { continue; }
                reader.get_mut().write_all(&event.bytes).await?;
                if let Some(seq) = event.seq { max_sent = max_sent.max(seq); }
            },
            _ = tokio::time::sleep(ping) => {
                reader.get_mut().write_all(b": ping\n\n").await?;
            }

        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    struct DecimalText;
    impl Text for DecimalText {
        fn request(&self, _: &[u8], _: &str) -> Result<RequestText, String> {
            unreachable!()
        }
        fn integer(&self, text: &str) -> Result<Option<Cursor>, String> {
            Ok(text.parse().ok().map(Cursor::Sequence))
        }
    }
    #[test]
    fn query_is_validated_before_header_and_header_wins() {
        assert!(matches!(
            cursor(&DecimalText, Some("1"), Some("2")),
            Ok(Ok(Cursor::Sequence(2)))
        ));
        assert!(matches!(
            cursor(&DecimalText, Some("bad"), Some("2")),
            Ok(Err(AFTER_ERROR))
        ));
        assert!(matches!(
            cursor(&DecimalText, Some("1"), Some("bad")),
            Ok(Err(HEADER_ERROR))
        ));
    }
    #[test]
    fn reduced_projection_uses_the_canonical_decimal_spelling_and_no_internal_fields() {
        let mut state = AccountState::new("synthetic");
        state.cash = Money::parse("-0.00").unwrap();
        assert_eq!(json::dumps(&account(&state).unwrap()), "{\"account_id\":\"synthetic\",\"cash\":\"0\",\"last_seq\":0,\"orders\":{},\"positions\":{},\"realized_pnl\":\"0\"}");
    }
    #[test]
    fn legacy_error_escaping_and_head_length_are_exact() {
        let reply = error(
            501,
            "Unsupported method ('<x>')".into(),
            "Server does not support this operation",
            true,
        );
        assert!(reply.body.is_empty());
        assert_eq!(reply.headers[2].0, "Content-Length");
        let reply = error(
            501,
            "Unsupported method ('<x>')".into(),
            "Server does not support this operation",
            false,
        );
        assert!(String::from_utf8(reply.body)
            .unwrap()
            .contains("Unsupported method ('&lt;x&gt;')"));
    }

    struct SyntheticControl {
        expected: &'static str,
        refuse: bool,
    }
    impl Control for SyntheticControl {
        fn submit(&self, body: &[u8], capability: Option<&str>) -> Result<(Json, bool), ControlError> {
            if capability != Some(self.expected) {
                return Err(ControlError::new(
                    "RuntimeCapabilityError",
                    "control capability does not match the owner",
                ));
            }
            if self.refuse {
                return Err(ControlError::new("RuntimeJobError", "configured refusal"));
            }
            let payload: Json = json::parse(std::str::from_utf8(body).unwrap()).unwrap();
            Ok((payload, true))
        }
        fn job(&self, id: &str) -> Result<Json, ControlError> {
            Ok(object(vec![("request_id", string(id))]))
        }
        fn status(&self) -> Result<Json, ControlError> {
            Ok(object(vec![("role", string("eod"))]))
        }
    }
    fn raw_request(method: &str, target: &str, headers: &[(&str, &str)], body: Vec<u8>) -> Raw {
        Raw {
            method: method.to_owned(),
            target: Ok((target.to_owned(), None)),
            headers: headers
                .iter()
                .map(|(key, value)| (key.to_string(), value.to_string()))
                .collect(),
            http09: false,
            body,
        }
    }
    fn test_api(control: Option<SyntheticControl>) -> Api {
        Api {
            path: "unused".into(),
            identity: "test".into(),
            text: Arc::new(DecimalText),
            origin: Regex::new(r"(?i)^https?://(localhost|127\.0\.0\.1)(:\d+)?$").unwrap(),
            ping: 15.0,
            subscribers: Mutex::new(BTreeMap::new()),
            serial: Mutex::new(0),
            errors: Mutex::new(Vec::new()),
            stop: watch::channel(false).0,
            backlog_paused: watch::channel(false).0,
            control: Mutex::new(control.map(|c| Arc::new(c) as Arc<dyn Control>)),
        }
    }

    #[test]
    fn control_routes_authorize_and_preserve_exact_refusals() {
        let api = test_api(Some(SyntheticControl {
            expected: "synthetic",
            refuse: false,
        }));
        // POST /v1/runtime/jobs with the matching capability is admitted.
        let reply = handle(
            &api,
            &raw_request(
                "POST",
                "/v1/runtime/jobs",
                &[("Host", "localhost"), ("X-TE-Capability", "synthetic")],
                br#"{"request_id":"one"}"#.to_vec(),
            ),
        )
        .unwrap();
        assert_eq!(reply.code, 201);
        assert_eq!(String::from_utf8(reply.body).unwrap(), r#"{"request_id":"one"}"#);
        // A wrong capability refuses with the exact type and message.
        let reply = handle(
            &api,
            &raw_request(
                "POST",
                "/v1/runtime/jobs",
                &[("Host", "localhost"), ("X-TE-Capability", "wrong")],
                b"{}".to_vec(),
            ),
        )
        .unwrap();
        assert_eq!(reply.code, 400);
        // The reduced codec preserves Python's json.dumps(sort_keys=True) order.
        assert_eq!(
            String::from_utf8(reply.body).unwrap(),
            r#"{"error":{"message":"control capability does not match the owner","type":"RuntimeCapabilityError"}}"#
        );
        // GET status and job lookups are authorized reads.
        let reply = handle(
            &api,
            &raw_request("GET", "/v1/runtime/status", &[("Host", "127.0.0.1")], Vec::new()),
        )
        .unwrap();
        assert_eq!(reply.code, 200);
        assert_eq!(String::from_utf8(reply.body).unwrap(), r#"{"role":"eod"}"#);
        let reply = handle(
            &api,
            &raw_request("GET", "/v1/runtime/jobs/one", &[("Host", "localhost")], Vec::new()),
        )
        .unwrap();
        assert_eq!(
            String::from_utf8(reply.body).unwrap(),
            r#"{"request_id":"one"}"#
        );
        // Unsupported control routes/methods refuse explicitly.
        let reply = handle(
            &api,
            &raw_request("POST", "/v1/runtime/status", &[("Host", "localhost")], Vec::new()),
        )
        .unwrap();
        assert_eq!(reply.code, 400);
        assert!(String::from_utf8(reply.body)
            .unwrap()
            .contains("RuntimeRouteError"));
        // A cross-origin control request never reaches the control surface.
        let reply = handle(
            &api,
            &raw_request(
                "GET",
                "/v1/runtime/status",
                &[("Host", "localhost"), ("Origin", "http://attacker.test")],
                Vec::new(),
            ),
        )
        .unwrap();
        assert_eq!(reply.code, 403);
        assert!(String::from_utf8(reply.body)
            .unwrap()
            .contains("RuntimeOriginError"));
        // A non-local Host keeps the legacy 403 outcome.
        let reply = handle(
            &api,
            &raw_request("GET", "/v1/runtime/status", &[("Host", "attacker.test")], Vec::new()),
        )
        .unwrap();
        assert_eq!(reply.code, 403);
        assert!(String::from_utf8(reply.body).unwrap().contains("Invalid Host"));
    }

    #[test]
    fn without_control_the_legacy_outcomes_are_unchanged() {
        let api = test_api(None);
        // POST /v1/runtime/jobs without an attached control keeps 501.
        let reply = handle(
            &api,
            &raw_request("POST", "/v1/runtime/jobs", &[("Host", "localhost")], Vec::new()),
        )
        .unwrap();
        assert_eq!(reply.code, 501);
        // GET /v1/runtime/status keeps the legacy 404.
        let reply = handle(
            &api,
            &raw_request("GET", "/v1/runtime/status", &[("Host", "localhost")], Vec::new()),
        )
        .unwrap();
        assert_eq!(reply.code, 404);
    }
}
