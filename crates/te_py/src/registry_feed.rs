//! W7-CORE-C22: the registry feed host's method bodies, moved into Rust.
//!
//! The host wires parts that already exist and adds no logic of its own:
//! `NtxClient -> TradovateAdapter -> AccountWatcher`, `HubAccountsClient ->
//! SchwabAccountsAdapter -> SchwabPoller`, each with its own `RiskguardRegistrySink`
//! (outbox + pump thread). Four `#[pyclass(subclass)]` bases own the sequencing the Python
//! classes did, step for step and in the same order: `SchwabPollerBase`, `SchwabFeedBase`,
//! `PollPumpBase` and `FeedBase`, plus the module functions `schwab_hint_on`, `cme_trading_day`,
//! `_counts`, `schwab_outbox_path`, `run_once` and `run`.
//!
//! All instance state (`_adapter`, `_client`, `_clock`, `_cond`, `_asked`, `reads_ok`,
//! `failures`, `config`, `adapter`, `sink`, `client`, `clock`, `poller`, `listener`, `polls`,
//! `resnapshots`, `accounts`, `oldest_as_of`, `_open`, `_open_seconds`, `_flat_seconds`, ...)
//! stays Python instance `__dict__`, set by each Python `__init__`; the Rust methods read and
//! write it through `getattr`/`setattr`. Internal calls go through `slf.call_method0(...)` /
//! `slf.call_method1(...)` so a subclass or monkeypatch override still runs.
//!
//! Collaborators (`WallClock`, `SchwabAccountsAdapter`, `HubAccountsClient`, `HubHintListener`,
//! `NtxClient`, `TradovateAdapter`, `AccountWatcher`, `RiskguardRegistrySink`,
//! `RegistrySinkConfig`, `SchwabAccountsConfig`, `SchwabFeed`, `Feed`, `PollPump`,
//! `SchwabPoller`, `live_acknowledged`, `random`, `log`, `monotonic_seconds`,
//! `default_token_path`, `read_token`, `hub_base_url`) are looked up by attribute at CALL TIME
//! from the `trade_engine.tradovate.registry_feed` module, because the tests monkeypatch
//! module attributes (`monkeypatch.setattr(registry_feed, "WallClock", ...)` and
//! `monkeypatch.setattr(registry_feed, "run", fake_run)`).
//!
//! KEPT IN PYTHON: all network/socket/HTTP I/O, the `threading.Thread` / `threading.Condition`
//! / `threading.Event` objects (created in Python `__init__` or via `py.import("threading")`),
//! `_accept_console_ctrl_c` (ctypes/kernel32), the signal-handler wiring in `main_loop`, the
//! `__init__` / `__repr__` methods, the module constants and the dataclasses. When the Rust
//! side waits on a Condition/Event it calls the Python object's `wait(...)`, which releases the
//! GIL itself. The `watch`/`poll`/`poll_schwab`/`listen_hub` thread bodies stay tiny Python
//! helpers (`_watch`, `_poll`, `_poll_schwab`, `_listen_hub`) that the Rust `run` binds with
//! `functools.partial`; the Thread objects stay Python.

use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyTuple, PyType};

/// The Python classes, exceptions and constants the bases reach, imported once.
struct Ctx {
    hub_read_error: Py<PyAny>,
    ws_kinds: Py<PyAny>,
    // constants (never monkeypatched)
    schwab_broker: String,
    env_hint: String,
    schwab_outbox_env: String,
    schwab_backoff_jitter: f64,
    stop_budget_seconds: f64,
    main_wake_seconds: f64,
    sink_off_exit: i64,
    sink_off_line: String,
    et: Py<PyAny>,
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let registry_feed = py.import("trade_engine.tradovate.registry_feed")?;
        let hub_client = py.import("trade_engine.schwab.hub_client")?;
        let hub_ws = py.import("trade_engine.schwab.hub_ws")?;
        let et = py
            .import("zoneinfo")?
            .getattr("ZoneInfo")?
            .call1(("America/New_York",))?;
        Ok(Ctx {
            hub_read_error: hub_client.getattr("HubReadError")?.unbind(),
            ws_kinds: hub_ws.getattr("WS_KINDS")?.unbind(),
            schwab_broker: registry_feed.getattr("SCHWAB_BROKER")?.extract()?,
            env_hint: registry_feed.getattr("ENV_HINT")?.extract()?,
            schwab_outbox_env: registry_feed.getattr("SCHWAB_OUTBOX_ENV")?.extract()?,
            schwab_backoff_jitter: registry_feed.getattr("SCHWAB_BACKOFF_JITTER")?.extract()?,
            stop_budget_seconds: registry_feed.getattr("STOP_BUDGET_SECONDS")?.extract()?,
            main_wake_seconds: registry_feed.getattr("MAIN_WAKE_SECONDS")?.extract()?,
            sink_off_exit: registry_feed.getattr("SINK_OFF_EXIT")?.extract()?,
            sink_off_line: registry_feed.getattr("SINK_OFF_LINE")?.extract()?,
            et: et.unbind(),
        })
    }
}

fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

/// The `trade_engine.tradovate.registry_feed` module (collaborators are read from it).
fn module(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    Ok(py.import("trade_engine.tradovate.registry_feed")?.into_any())
}

/// The module's `log` logger, looked up at call time.
fn logger(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    module(py)?.getattr("log")
}

/// `log.<level>(fmt, *args)` with the same lazy `%`-style formatting the tests read.
fn log_msg(py: Python<'_>, level: &str, fmt: &str, args: Vec<Py<PyAny>>) -> PyResult<()> {
    let mut items: Vec<Py<PyAny>> = Vec::with_capacity(args.len() + 1);
    items.push(fmt.into_pyobject(py)?.into_any().unbind());
    items.extend(args);
    let tuple = PyTuple::new(py, items)?;
    logger(py)?.call_method1(level, tuple)?;
    Ok(())
}

fn str_py(py: Python<'_>, value: &str) -> PyResult<Py<PyAny>> {
    Ok(value.into_pyobject(py)?.into_any().unbind())
}

/// `monotonic_seconds()` (the injected `time.monotonic`), looked up at call time.
fn monotonic(py: Python<'_>) -> PyResult<f64> {
    module(py)?.getattr("monotonic_seconds")?.call0()?.extract()
}

/// `max(0.0, deadline - monotonic_seconds())`: the stop budget's remaining time.
fn remaining(py: Python<'_>, deadline: f64) -> PyResult<f64> {
    Ok((deadline - monotonic(py)?).max(0.0))
}

fn set_opt(d: &Bound<'_, PyDict>, key: &str, v: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
    match v {
        Some(v) => d.set_item(key, v),
        None => d.set_item(key, d.py().None()),
    }
}

// --- the pure module functions ----------------------------------------------------------

fn schwab_hint_on_impl(py: Python<'_>, environ: &Bound<'_, PyAny>) -> PyResult<bool> {
    let ctx = ctx(py)?;
    let raw = environ.call_method1("get", (ctx.env_hint.as_str(),))?;
    let value: String = if raw.is_none() {
        "on".to_string()
    } else {
        let s: String = raw.str()?.extract()?;
        if s.is_empty() {
            "on".to_string()
        } else {
            s
        }
    };
    let value = value.trim().to_lowercase();
    Ok(value != "0" && value != "off")
}

fn cme_trading_day_impl(py: Python<'_>, at: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let ctx = ctx(py)?;
    let astimezone = at.call_method1("astimezone", (ctx.et.bind(py),))?;
    let timedelta = py.import("datetime")?.getattr("timedelta")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("hours", 7)?;
    let seven = timedelta.call((), Some(&kwargs))?;
    let added = astimezone.call_method1("__add__", (&seven,))?;
    Ok(added.call_method0("date")?.unbind())
}

fn counts_impl(py: Python<'_>, stats: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let mapping = py.import("collections.abc")?.getattr("Mapping")?;
    let out = PyDict::new(py);
    for item in stats.call_method0("items")?.try_iter()? {
        let item = item?;
        let key = item.get_item(0)?;
        let value = item.get_item(1)?;
        let scalar = value.is_instance_of::<PyBool>()
            || value.is_instance_of::<PyInt>()
            || value.is_instance_of::<PyFloat>();
        if value.is_instance(&mapping)? {
            for sub_item in value.call_method0("items")?.try_iter()? {
                let sub_item = sub_item?;
                let sub = sub_item.get_item(0)?;
                let inner = sub_item.get_item(1)?;
                if inner.is_instance_of::<PyBool>()
                    || inner.is_instance_of::<PyInt>()
                    || inner.is_instance_of::<PyFloat>()
                {
                    let key_s: String = key.str()?.extract()?;
                    let sub_s: String = sub.str()?.extract()?;
                    out.set_item(format!("{key_s}.{sub_s}"), &inner)?;
                }
            }
        } else if scalar {
            out.set_item(&key, &value)?;
        }
    }
    Ok(out.into_any().unbind())
}

fn schwab_outbox_path_impl(
    py: Python<'_>,
    environ: &Bound<'_, PyAny>,
    tradovate_outbox: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let ctx = ctx(py)?;
    let raw = environ.call_method1("get", (ctx.schwab_outbox_env.as_str(),))?;
    let explicit: String = if raw.is_none() {
        String::new()
    } else {
        raw.str()?.extract()?
    };
    let explicit = explicit.trim().to_string();
    if !explicit.is_empty() {
        return Ok(explicit);
    }
    let tradovate_outbox: String = tradovate_outbox.str()?.extract()?;
    if tradovate_outbox.is_empty() {
        return Ok("registry_outbox_schwab.json".to_string());
    }
    let path = py
        .import("pathlib")?
        .getattr("Path")?
        .call1((&tradovate_outbox,))?;
    let stem: String = path.getattr("stem")?.extract()?;
    let suffix: String = path.getattr("suffix")?.extract()?;
    let with_name = path.call_method1("with_name", (format!("{stem}_schwab{suffix}"),))?;
    with_name.str()?.extract()
}

// --- SchwabPoller -----------------------------------------------------------------------

/// `SchwabPoller`'s body. State is Python `__dict__` set by `SchwabPoller.__init__`
/// (`_adapter`, `_client`, `_clock`, `_rand`, `tick_seconds`, `_backoff_start`,
/// `_backoff_cap`, `reads_ok`, `failures`, `backoff_seconds`, `_consecutive_failures`,
/// `_next_attempt_at`, `_cond`, `_asked`, `asks`).
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct SchwabPollerBase;

#[pymethods]
impl SchwabPollerBase {
    #[new]
    #[pyo3(signature = (adapter, client, *, clock, rand=None, tick_seconds=1.0, backoff_start=30.0, backoff_cap=300.0))]
    fn new(
        adapter: &Bound<'_, PyAny>,
        client: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        rand: Option<&Bound<'_, PyAny>>,
        tick_seconds: f64,
        backoff_start: f64,
        backoff_cap: f64,
    ) -> Self {
        let _ = (adapter, client, clock, rand, tick_seconds, backoff_start, backoff_cap);
        SchwabPollerBase
    }

    fn accounts<'py>(slf: &Bound<'py, Self>) -> PyResult<i64> {
        let snapshot = slf.getattr("_adapter")?.getattr("last_good")?;
        if snapshot.is_none() {
            return Ok(0);
        }
        Ok(snapshot.getattr("records")?.len()? as i64)
    }

    fn ask<'py>(slf: &Bound<'py, Self>) -> PyResult<()> {
        let cond = slf.getattr("_cond")?;
        cond.call_method0("acquire")?;
        let result = (|| -> PyResult<()> {
            slf.setattr("_asked", true)?;
            let asks: i64 = slf.getattr("asks")?.extract()?;
            slf.setattr("asks", asks + 1)?;
            cond.call_method0("notify")?;
            Ok(())
        })();
        cond.call_method0("release")?;
        result
    }

    fn due<'py>(slf: &Bound<'py, Self>, now: &Bound<'py, PyAny>) -> PyResult<bool> {
        let next = slf.getattr("_next_attempt_at")?;
        if !next.is_none() {
            let lt: bool = now.call_method1("__lt__", (&next,))?.extract()?;
            if lt {
                return Ok(false);
            }
        }
        let cond = slf.getattr("_cond")?;
        cond.call_method0("acquire")?;
        let result = (|| -> PyResult<bool> { Ok(slf.getattr("_asked")?.extract()?) })();
        cond.call_method0("release")?;
        let asked: bool = result?;
        if asked {
            return Ok(true);
        }
        slf.getattr("_adapter")?
            .call_method1("next_poll_due", (now,))?
            .extract()
    }

    fn tick<'py>(slf: &Bound<'py, Self>, now: &Bound<'py, PyAny>) -> PyResult<()> {
        let due: bool = slf.call_method1("due", (now,))?.extract()?;
        if due {
            slf.call_method1("attempt", (now,))?;
        }
        Ok(())
    }

    fn _next_backoff<'py>(slf: &Bound<'py, Self>) -> PyResult<f64> {
        let ctx = ctx(slf.py())?;
        let consecutive: i64 = slf.getattr("_consecutive_failures")?.extract()?;
        let consecutive = consecutive + 1;
        slf.setattr("_consecutive_failures", consecutive)?;
        let start: f64 = slf.getattr("_backoff_start")?.extract()?;
        let cap: f64 = slf.getattr("_backoff_cap")?.extract()?;
        let base = cap.min(start * 2f64.powi((consecutive - 1) as i32));
        let rand: f64 = slf.getattr("_rand")?.call0()?.extract()?;
        let jitter = 1.0 + ((rand * 2.0) - 1.0) * ctx.schwab_backoff_jitter;
        Ok(cap.min(base * jitter))
    }

    fn attempt<'py>(slf: &Bound<'py, Self>, now: &Bound<'py, PyAny>) -> PyResult<bool> {
        let py = slf.py();
        let ctx = ctx(py)?;
        // with self._cond: self._asked = False  (a hint during this read asks again next tick)
        {
            let cond = slf.getattr("_cond")?;
            cond.call_method0("acquire")?;
            let r = slf.setattr("_asked", false);
            cond.call_method0("release")?;
            r?;
        }
        let client = slf.getattr("_client")?;
        let read = client.call_method0("read");
        let (numbers, accounts) = match read {
            Ok(value) => (value.get_item(0)?.unbind(), value.get_item(1)?.unbind()),
            Err(e) if e.is_instance(py, ctx.hub_read_error.bind(py)) => {
                let kind: String = e.value(py).getattr("kind")?.extract()?;
                let failures = slf.getattr("failures")?;
                let cur: i64 = failures.call_method1("get", (&kind, 0))?.extract()?;
                failures.set_item(&kind, cur + 1)?;
                let backoff = slf.call_method0("_next_backoff")?;
                slf.setattr("backoff_seconds", &backoff)?;
                let backoff_f: f64 = backoff.extract()?;
                let timedelta = py.import("datetime")?.getattr("timedelta")?;
                let kwargs = PyDict::new(py);
                kwargs.set_item("seconds", backoff_f)?;
                let td = timedelta.call((), Some(&kwargs))?;
                let next_attempt = now.call_method1("__add__", (&td,))?;
                slf.setattr("_next_attempt_at", next_attempt)?;
                let rounded = py.import("builtins")?.call_method1("round", (backoff_f, 1))?;
                log_msg(
                    py,
                    "warning",
                    "schwab hub read failed (%s); next read in %ss",
                    vec![kind.into_pyobject(py)?.into_any().unbind(), rounded.unbind()],
                )?;
                return Ok(false);
            }
            Err(e) => return Err(e),
        };
        slf.setattr("_consecutive_failures", 0)?;
        slf.setattr("backoff_seconds", 0.0)?;
        slf.setattr("_next_attempt_at", py.None())?;
        let adapter = slf.getattr("_adapter")?;
        adapter.call_method1("feed", (numbers.bind(py), accounts.bind(py)))?;
        adapter.getattr("poller")?.call_method0("poll")?;
        let reads_ok: i64 = slf.getattr("reads_ok")?.extract()?;
        slf.setattr("reads_ok", reads_ok + 1)?;
        Ok(true)
    }

    fn _wait_for_tick<'py>(slf: &Bound<'py, Self>, stop_event: &Bound<'py, PyAny>) -> PyResult<()> {
        let cond = slf.getattr("_cond")?;
        cond.call_method0("acquire")?;
        let result = (|| -> PyResult<()> {
            let is_set: bool = stop_event.call_method0("is_set")?.extract()?;
            if is_set {
                return Ok(());
            }
            let asked: bool = slf.getattr("_asked")?.extract()?;
            if asked {
                let next = slf.getattr("_next_attempt_at")?;
                if next.is_none() {
                    return Ok(());
                }
                let now = slf.getattr("_clock")?.call_method0("now_utc")?;
                let ge: bool = now.call_method1("__ge__", (&next,))?.extract()?;
                if ge {
                    return Ok(());
                }
            }
            let tick_seconds = slf.getattr("tick_seconds")?;
            cond.call_method1("wait", (&tick_seconds,))?;
            Ok(())
        })();
        cond.call_method0("release")?;
        result
    }

    #[pyo3(signature = (stop_event, wait=None))]
    fn run<'py>(
        slf: &Bound<'py, Self>,
        stop_event: &Bound<'py, PyAny>,
        wait: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<()> {
        loop {
            let is_set: bool = stop_event.call_method0("is_set")?.extract()?;
            if is_set {
                return Ok(());
            }
            match wait {
                Some(w) => {
                    let tick_seconds = slf.getattr("tick_seconds")?;
                    w.call1((&tick_seconds,))?;
                }
                None => {
                    slf.call_method1("_wait_for_tick", (stop_event,))?;
                }
            }
            let is_set: bool = stop_event.call_method0("is_set")?.extract()?;
            if is_set {
                return Ok(());
            }
            let now = slf.getattr("_clock")?.call_method0("now_utc")?;
            slf.call_method1("tick", (&now,))?;
        }
    }

    fn once<'py>(slf: &Bound<'py, Self>) -> PyResult<bool> {
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        slf.call_method1("attempt", (&now,))?.extract()
    }
}

// --- SchwabFeed --------------------------------------------------------------------------

/// `SchwabFeed`'s body. State is Python `__dict__` set by `SchwabFeed.__init__`
/// (`config`, `adapter`, `sink`, `client`, `clock`, `poller`, `listener`).
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct SchwabFeedBase;

#[pymethods]
impl SchwabFeedBase {
    #[new]
    #[pyo3(signature = (config, adapter, sink, client, *, clock, rand=None, listener=None))]
    fn new(
        config: &Bound<'_, PyAny>,
        adapter: &Bound<'_, PyAny>,
        sink: &Bound<'_, PyAny>,
        client: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        rand: Option<&Bound<'_, PyAny>>,
        listener: Option<&Bound<'_, PyAny>>,
    ) -> Self {
        let _ = (config, adapter, sink, client, clock, rand, listener);
        SchwabFeedBase
    }

    fn on_accounts_hint<'py>(slf: &Bound<'py, Self>) -> PyResult<bool> {
        let py = slf.py();
        let adapter = slf.getattr("adapter")?;
        let now = slf.getattr("clock")?.call_method0("now_utc")?;
        let acted: bool = adapter
            .call_method1("on_activity", (&now, py.None()))?
            .extract()?;
        if acted {
            slf.getattr("poller")?.call_method0("ask")?;
        }
        Ok(acted)
    }

    #[classmethod]
    #[pyo3(signature = (environ, *, clock, tradovate_outbox, http_post=None, hub_client=None, rand=None, ws_connect=None))]
    fn build(
        cls: &Bound<'_, PyType>,
        environ: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        tradovate_outbox: &Bound<'_, PyAny>,
        http_post: Option<&Bound<'_, PyAny>>,
        hub_client: Option<&Bound<'_, PyAny>>,
        rand: Option<&Bound<'_, PyAny>>,
        ws_connect: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let py = cls.py();
        let ctx = ctx(py)?;
        let m = module(py)?;
        let env_dict = py.import("builtins")?.getattr("dict")?.call1((environ,))?;
        let config_cls = m.getattr("SchwabAccountsConfig")?;
        let config = config_cls.call_method1("from_env", (&env_dict,))?;
        let enabled: bool = config.getattr("enabled")?.extract()?;
        if !enabled {
            return Ok(py.None());
        }
        let registry_config_cls = m.getattr("RegistrySinkConfig")?;
        let base = registry_config_cls.call_method1("from_env", (&env_dict,))?;
        let sink_config = {
            let replace = py.import("dataclasses")?.getattr("replace")?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("broker", ctx.schwab_broker.as_str())?;
            kwargs.set_item(
                "outbox_path",
                schwab_outbox_path_impl(py, environ, tradovate_outbox)?,
            )?;
            kwargs.set_item("pnl_enabled", false)?;
            replace.call((&base,), Some(&kwargs))?
        };
        let sink_cls = m.getattr("RiskguardRegistrySink")?;
        let sk = PyDict::new(py);
        sk.set_item("clock", clock)?;
        if let Some(hp) = http_post {
            sk.set_item("http_post", hp)?;
        }
        let sink = sink_cls.call((&sink_config,), Some(&sk))?;
        let client = match hub_client {
            Some(hc) => hc.clone(),
            None => m.getattr("HubAccountsClient")?.call_method1("from_env", (environ,))?,
        };
        let adapter_cls = m.getattr("SchwabAccountsAdapter")?;
        let ak = PyDict::new(py);
        ak.set_item("clock", clock)?;
        ak.set_item("on_poll", sink.getattr("observe")?)?;
        let adapter = adapter_cls.call((&config,), Some(&ak))?;
        let fk = PyDict::new(py);
        fk.set_item("clock", clock)?;
        if let Some(r) = rand {
            fk.set_item("rand", r)?;
        }
        let feed = cls.call((&config, &adapter, &sink, &client), Some(&fk))?;
        if schwab_hint_on_impl(py, environ)? {
            let hub_url = m.getattr("hub_base_url")?.call1((environ,))?;
            let lk = PyDict::new(py);
            lk.set_item("on_hint", feed.getattr("on_accounts_hint")?)?;
            if let Some(ws) = ws_connect {
                lk.set_item("connect", ws)?;
            }
            if let Some(r) = rand {
                lk.set_item("rand", r)?;
            }
            let listener = m.getattr("HubHintListener")?.call((&hub_url,), Some(&lk))?;
            feed.setattr("listener", &listener)?;
        }
        Ok(feed.unbind())
    }

    fn once<'py>(slf: &Bound<'py, Self>) -> PyResult<i64> {
        let poller = slf.getattr("poller")?;
        let good: bool = poller.call_method0("once")?.extract()?;
        if !good {
            return Ok(0);
        }
        slf.getattr("sink")?.call_method0("pump")?.extract()
    }

    fn counts<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let poller = slf.getattr("poller")?;
        let adapter = slf.getattr("adapter")?;
        let out = PyDict::new(py);
        out.set_item("schwab_accounts", poller.call_method0("accounts")?)?;
        out.set_item("schwab_reads_ok", poller.getattr("reads_ok")?)?;
        out.set_item("schwab_hints_acted", adapter.getattr("stats")?.getattr("hints")?)?;
        out.set_item(
            "schwab_hints_suppressed",
            adapter.getattr("stats")?.getattr("hints_suppressed")?,
        )?;
        for item in poller.getattr("failures")?.call_method0("items")?.try_iter()? {
            let item = item?;
            let kind = item.get_item(0)?;
            let count = item.get_item(1)?;
            let kind_s: String = kind.str()?.extract()?;
            out.set_item(format!("schwab_read_fail_{kind_s}"), &count)?;
        }
        let listener = slf.getattr("listener")?;
        if !listener.is_none() {
            out.set_item("schwab_hints_received", listener.getattr("hints_received")?)?;
            out.set_item("schwab_hint_errors", listener.getattr("hint_errors")?)?;
            out.set_item("schwab_ws_connects", listener.getattr("connects")?)?;
            out.set_item(
                "schwab_ws_frames_oversized",
                listener.getattr("frames_oversized")?,
            )?;
            out.set_item(
                "schwab_ws_frames_malformed",
                listener.getattr("frames_malformed")?,
            )?;
            let failures = listener.getattr("failures")?;
            for kind in ctx.ws_kinds.bind(py).try_iter()? {
                let kind = kind?;
                let kind_s: String = kind.str()?.extract()?;
                out.set_item(format!("schwab_ws_fail_{kind_s}"), failures.get_item(&kind)?)?;
            }
        }
        let sink = slf.getattr("sink")?;
        for item in counts_impl(py, &sink.call_method0("stats")?)?
            .bind(py)
            .call_method0("items")?
            .try_iter()?
        {
            let item = item?;
            let key = item.get_item(0)?;
            let value = item.get_item(1)?;
            let key_s: String = key.str()?.extract()?;
            out.set_item(format!("schwab_sink.{key_s}"), &value)?;
        }
        Ok(out.into_any().unbind())
    }
}

// --- PollPump ----------------------------------------------------------------------------

/// `PollPump`'s body. State is Python `__dict__` set by `PollPump.__init__`
/// (`_adapter`, `_open_seconds`, `_flat_seconds`, `_cond`, `_asked`, `_stopped`, `_open`,
/// `polls`, `resnapshots`, `accounts`, `oldest_as_of`).
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct PollPumpBase;

#[pymethods]
impl PollPumpBase {
    #[new]
    #[pyo3(signature = (adapter, *, open_seconds=2.0, flat_seconds=30.0))]
    fn new(adapter: &Bound<'_, PyAny>, open_seconds: f64, flat_seconds: f64) -> Self {
        let _ = (adapter, open_seconds, flat_seconds);
        PollPumpBase
    }

    fn ask<'py>(slf: &Bound<'py, Self>) -> PyResult<()> {
        let cond = slf.getattr("_cond")?;
        cond.call_method0("acquire")?;
        let result = (|| -> PyResult<()> {
            slf.setattr("_asked", true)?;
            cond.call_method0("notify")?;
            Ok(())
        })();
        cond.call_method0("release")?;
        result
    }

    fn stop<'py>(slf: &Bound<'py, Self>) -> PyResult<()> {
        let cond = slf.getattr("_cond")?;
        cond.call_method0("acquire")?;
        let result = (|| -> PyResult<()> {
            slf.setattr("_stopped", true)?;
            cond.call_method0("notify")?;
            Ok(())
        })();
        cond.call_method0("release")?;
        result
    }

    fn interval<'py>(slf: &Bound<'py, Self>) -> PyResult<f64> {
        let open: bool = slf.getattr("_open")?.extract()?;
        if open {
            slf.getattr("_open_seconds")?.extract()
        } else {
            slf.getattr("_flat_seconds")?.extract()
        }
    }

    fn _wait_real<'py>(slf: &Bound<'py, Self>, seconds: f64) -> PyResult<()> {
        let cond = slf.getattr("_cond")?;
        cond.call_method0("acquire")?;
        let result = (|| -> PyResult<()> {
            let asked: bool = slf.getattr("_asked")?.extract()?;
            let stopped: bool = slf.getattr("_stopped")?.extract()?;
            if !(asked || stopped) {
                cond.call_method1("wait", (seconds,))?;
            }
            Ok(())
        })();
        cond.call_method0("release")?;
        result
    }

    #[pyo3(signature = (wait=None))]
    fn run<'py>(slf: &Bound<'py, Self>, wait: Option<&Bound<'py, PyAny>>) -> PyResult<()> {
        let py = slf.py();
        let wait = match wait {
            Some(w) => w.clone(),
            None => slf.getattr("_wait_real")?,
        };
        loop {
            let interval: f64 = slf.call_method0("interval")?.extract()?;
            wait.call1((interval,))?;
            let cond = slf.getattr("_cond")?;
            cond.call_method0("acquire")?;
            let asked = (|| -> PyResult<Option<bool>> {
                let stopped: bool = slf.getattr("_stopped")?.extract()?;
                if stopped {
                    return Ok(None);
                }
                let asked: bool = slf.getattr("_asked")?.extract()?;
                slf.setattr("_asked", false)?;
                Ok(Some(asked))
            })();
            cond.call_method0("release")?;
            match asked? {
                None => return Ok(()),
                Some(asked) => {
                    let kwargs = PyDict::new(py);
                    kwargs.set_item("timed", !asked)?;
                    slf.call_method("_poll", (), Some(&kwargs))?;
                }
            }
        }
    }

    #[pyo3(signature = (*, timed))]
    fn _poll<'py>(slf: &Bound<'py, Self>, timed: bool) -> PyResult<()> {
        let adapter = slf.getattr("_adapter")?;
        adapter.getattr("poller")?.call_method0("poll")?;
        let polls: i64 = slf.getattr("polls")?.extract()?;
        slf.setattr("polls", polls + 1)?;
        if timed {
            let resnapshots: i64 = slf.getattr("resnapshots")?.extract()?;
            slf.setattr("resnapshots", resnapshots + 1)?;
        }
        let snapshot = adapter.getattr("poller")?.getattr("last_good")?;
        let accounts = if snapshot.is_none() {
            0i64
        } else {
            snapshot.getattr("records")?.len()? as i64
        };
        slf.setattr("accounts", accounts)?;
        let open: bool = adapter.call_method0("has_open_position")?.extract()?;
        slf.setattr("_open", open)?;
        let as_of = adapter.getattr("last_figure_as_of")?;
        slf.setattr("oldest_as_of", &as_of)?;
        Ok(())
    }
}

// --- Feed --------------------------------------------------------------------------------

/// `Feed`'s body. State is Python `__dict__` set by `Feed.__init__`
/// (`config`, `sink`, `client`, `adapter`, `schwab`).
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct FeedBase;

#[pymethods]
impl FeedBase {
    #[new]
    #[pyo3(signature = (config, sink, client, adapter, *, schwab=None))]
    fn new(
        config: &Bound<'_, PyAny>,
        sink: &Bound<'_, PyAny>,
        client: &Bound<'_, PyAny>,
        adapter: &Bound<'_, PyAny>,
        schwab: Option<&Bound<'_, PyAny>>,
    ) -> Self {
        let _ = (config, sink, client, adapter, schwab);
        FeedBase
    }

    #[classmethod]
    #[pyo3(signature = (environ, *, clock, make_client=None, http_post=None, hub_client=None, rand=None, ws_connect=None, ledger=None))]
    #[allow(clippy::too_many_arguments)]
    fn build(
        cls: &Bound<'_, PyType>,
        environ: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        make_client: Option<&Bound<'_, PyAny>>,
        http_post: Option<&Bound<'_, PyAny>>,
        hub_client: Option<&Bound<'_, PyAny>>,
        rand: Option<&Bound<'_, PyAny>>,
        ws_connect: Option<&Bound<'_, PyAny>>,
        ledger: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let py = cls.py();
        let m = module(py)?;
        let env_dict = py.import("builtins")?.getattr("dict")?.call1((environ,))?;
        let config_cls = m.getattr("RegistrySinkConfig")?;
        let config = config_cls.call_method1("from_env", (&env_dict,))?;
        let enabled: bool = config.getattr("enabled")?.extract()?;
        if !enabled {
            return Ok(py.None());
        }
        let sink_cls = m.getattr("RiskguardRegistrySink")?;
        let sk = PyDict::new(py);
        sk.set_item("clock", clock)?;
        if let Some(hp) = http_post {
            sk.set_item("http_post", hp)?;
        }
        let sink = sink_cls.call((&config,), Some(&sk))?;
        let client = match make_client {
            Some(mc) => mc.call0()?,
            None => {
                let read_token = m.getattr("read_token")?;
                let default_token_path = m.getattr("default_token_path")?;
                let token = read_token.call1((default_token_path.call1((&env_dict,))?,))?;
                m.getattr("NtxClient")?.call1((token,))?
            }
        };
        let adapter_cls = m.getattr("TradovateAdapter")?;
        let ak = PyDict::new(py);
        ak.set_item("clock", clock)?;
        ak.set_item("on_poll", sink.getattr("observe")?)?;
        let pnl_enabled = config.getattr("pnl_enabled")?;
        let trading_day = if pnl_enabled.is_truthy()? {
            m.getattr("cme_trading_day")?
        } else {
            py.None().into_bound(py)
        };
        ak.set_item("trading_day", &trading_day)?;
        set_opt(&ak, "ledger", ledger)?;
        ak.set_item("live_ack", m.getattr("live_acknowledged")?.call1((environ,))?)?;
        let adapter = adapter_cls.call((&client,), Some(&ak))?;
        let schwab_cls = m.getattr("SchwabFeed")?;
        let sk2 = PyDict::new(py);
        sk2.set_item("clock", clock)?;
        sk2.set_item("tradovate_outbox", config.getattr("outbox_path")?)?;
        if let Some(hp) = http_post {
            sk2.set_item("http_post", hp)?;
        }
        if let Some(hc) = hub_client {
            sk2.set_item("hub_client", hc)?;
        }
        if let Some(r) = rand {
            sk2.set_item("rand", r)?;
        }
        if let Some(ws) = ws_connect {
            sk2.set_item("ws_connect", ws)?;
        }
        let schwab = schwab_cls.call_method("build", (environ,), Some(&sk2))?;
        let fk = PyDict::new(py);
        if !schwab.is_none() {
            fk.set_item("schwab", &schwab)?;
        }
        Ok(cls.call((&config, &sink, &client, &adapter), Some(&fk))?.unbind())
    }

    fn accounts<'py>(slf: &Bound<'py, Self>) -> PyResult<i64> {
        let snapshot = slf
            .getattr("adapter")?
            .getattr("poller")?
            .getattr("last_good")?;
        if snapshot.is_none() {
            return Ok(0);
        }
        Ok(snapshot.getattr("records")?.len()? as i64)
    }

    fn pnl_rows<'py>(slf: &Bound<'py, Self>) -> PyResult<i64> {
        let adapter = slf.getattr("adapter")?;
        let snapshot = adapter.getattr("poller")?.getattr("last_good")?;
        if snapshot.is_none() {
            return Ok(0);
        }
        let pnl_enabled: bool = slf.getattr("config")?.getattr("pnl_enabled")?.extract()?;
        if !pnl_enabled {
            return Ok(0);
        }
        let rows = adapter.call_method1("pnl_rows", (&snapshot,))?;
        let mut count = 0i64;
        for item in rows.try_iter()? {
            let item = item?;
            let figure = item.get_item(1)?;
            if !figure.is_none() {
                count += 1;
            }
        }
        Ok(count)
    }
}

// --- the module functions ----------------------------------------------------------------

#[pyfunction]
pub(crate) fn schwab_hint_on(py: Python<'_>, environ: &Bound<'_, PyAny>) -> PyResult<bool> {
    schwab_hint_on_impl(py, environ)
}

#[pyfunction]
pub(crate) fn cme_trading_day(py: Python<'_>, at: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    cme_trading_day_impl(py, at)
}

#[pyfunction]
pub(crate) fn _counts(py: Python<'_>, stats: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    counts_impl(py, stats)
}

#[pyfunction]
pub(crate) fn schwab_outbox_path(
    py: Python<'_>,
    environ: &Bound<'_, PyAny>,
    tradovate_outbox: &Bound<'_, PyAny>,
) -> PyResult<String> {
    schwab_outbox_path_impl(py, environ, tradovate_outbox)
}

#[pyfunction]
#[pyo3(signature = (environ, *, clock=None, make_client=None, http_post=None, hub_client=None))]
pub(crate) fn run_once(
    py: Python<'_>,
    environ: &Bound<'_, PyAny>,
    clock: Option<&Bound<'_, PyAny>>,
    make_client: Option<&Bound<'_, PyAny>>,
    http_post: Option<&Bound<'_, PyAny>>,
    hub_client: Option<&Bound<'_, PyAny>>,
) -> PyResult<(i64, Option<Py<PyAny>>)> {
    let ctx = ctx(py)?;
    let m = module(py)?;
    let clock = match clock {
        Some(c) if !c.is_none() => c.clone(),
        _ => m.getattr("WallClock")?.call0()?,
    };
    let feed_cls = m.getattr("Feed")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("clock", &clock)?;
    set_opt(&kwargs, "make_client", make_client)?;
    set_opt(&kwargs, "http_post", http_post)?;
    set_opt(&kwargs, "hub_client", hub_client)?;
    let feed = feed_cls.call_method("build", (environ,), Some(&kwargs))?;
    if feed.is_none() {
        log_msg(py, "warning", &ctx.sink_off_line, vec![])?;
        return Ok((ctx.sink_off_exit, None));
    }
    let adapter = feed.getattr("adapter")?;
    adapter.getattr("poller")?.call_method0("poll")?;
    let sink = feed.getattr("sink")?;
    let sent: i64 = sink.call_method0("pump")?.extract()?;
    let summary = PyDict::new(py);
    summary.set_item("accounts", feed.call_method0("accounts")?)?;
    summary.set_item("pnl_rows", feed.call_method0("pnl_rows")?)?;
    summary.set_item("sent", sent)?;
    summary.set_item("halted", sink.getattr("halted")?)?;
    let schwab = feed.getattr("schwab")?;
    if !schwab.is_none() {
        let schwab_sent: i64 = schwab.call_method0("once")?.extract()?;
        summary.set_item(
            "schwab_accounts",
            schwab.getattr("poller")?.call_method0("accounts")?,
        )?;
        summary.set_item("schwab_sent", schwab_sent)?;
    }
    Ok((0, Some(summary.into_any().unbind())))
}

#[pyfunction]
#[pyo3(signature = (environ, *, clock=None, sleep=None, rand=None, stop_event, make_client=None, http_post=None, hub_client=None, heartbeat_seconds=60.0, pump_tick_seconds=5.0, ws_connect=None, ledger=None))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn run(
    py: Python<'_>,
    environ: &Bound<'_, PyAny>,
    clock: Option<&Bound<'_, PyAny>>,
    sleep: Option<&Bound<'_, PyAny>>,
    rand: Option<&Bound<'_, PyAny>>,
    stop_event: &Bound<'_, PyAny>,
    make_client: Option<&Bound<'_, PyAny>>,
    http_post: Option<&Bound<'_, PyAny>>,
    hub_client: Option<&Bound<'_, PyAny>>,
    heartbeat_seconds: f64,
    pump_tick_seconds: f64,
    ws_connect: Option<&Bound<'_, PyAny>>,
    ledger: Option<&Bound<'_, PyAny>>,
) -> PyResult<i64> {
    let ctx = ctx(py)?;
    let m = module(py)?;
    let clock = match clock {
        Some(c) if !c.is_none() => c.clone(),
        _ => m.getattr("WallClock")?.call0()?,
    };
    let feed_cls = m.getattr("Feed")?;
    let fk = PyDict::new(py);
    fk.set_item("clock", &clock)?;
    set_opt(&fk, "make_client", make_client)?;
    set_opt(&fk, "http_post", http_post)?;
    set_opt(&fk, "hub_client", hub_client)?;
    set_opt(&fk, "rand", rand)?;
    set_opt(&fk, "ws_connect", ws_connect)?;
    set_opt(&fk, "ledger", ledger)?;
    let feed = feed_cls.call_method("build", (environ,), Some(&fk))?;
    if feed.is_none() {
        log_msg(py, "warning", &ctx.sink_off_line, vec![])?;
        return Ok(ctx.sink_off_exit);
    }
    let adapter = feed.getattr("adapter")?;
    let pump_cls = m.getattr("PollPump")?;
    let pump = pump_cls.call1((&adapter,))?;
    let watcher_cls = m.getattr("AccountWatcher")?;
    let sleep_val = match sleep {
        Some(s) if !s.is_none() => s.clone(),
        _ => stop_event.getattr("wait")?,
    };
    let rand_val = match rand {
        Some(r) if !r.is_none() => r.clone(),
        _ => m.getattr("random")?.getattr("random")?,
    };
    let wk = PyDict::new(py);
    wk.set_item("clock", &clock)?;
    wk.set_item("sleep", &sleep_val)?;
    wk.set_item("rand", &rand_val)?;
    wk.set_item("read", pump.getattr("ask")?)?;
    let watcher = watcher_cls.call((&adapter, feed.getattr("client")?), Some(&wk))?;

    let sink = feed.getattr("sink")?;
    let sk = PyDict::new(py);
    sk.set_item("tick_seconds", pump_tick_seconds)?;
    sink.call_method("start", (), Some(&sk))?;
    let pnl_on: bool = feed.getattr("config")?.getattr("pnl_enabled")?.extract()?;
    let schwab = feed.getattr("schwab")?;
    log_msg(
        py,
        "info",
        "registry feed started (pnl=%s, schwab=%s)",
        vec![
            str_py(py, if pnl_on { "on" } else { "off" })?,
            str_py(py, if schwab.is_none() { "off" } else { "on" })?,
        ],
    )?;

    let partial = py.import("functools")?.getattr("partial")?;
    let thread_cls = py.import("threading")?.getattr("Thread")?;
    let watch_target = partial.call1((m.getattr("_watch")?, &watcher, stop_event))?;
    let poll_target = partial.call1((m.getattr("_poll")?, &pump, stop_event))?;
    let thread = make_thread(py, &thread_cls, &watch_target, "registry-feed-watcher")?;
    let pump_thread = make_thread(py, &thread_cls, &poll_target, "registry-feed-poll")?;
    pump_thread.bind(py).call_method0("start")?;
    thread.bind(py).call_method0("start")?;

    let (schwab_thread, hint_thread): (Py<PyAny>, Py<PyAny>) = if !schwab.is_none() {
        let schwab_sink = schwab.getattr("sink")?;
        schwab_sink.call_method("start", (), Some(&sk))?;
        let target = partial.call1((m.getattr("_poll_schwab")?, &feed, stop_event))?;
        let st = make_thread(py, &thread_cls, &target, "registry-feed-schwab")?;
        st.bind(py).call_method0("start")?;
        let listener = schwab.getattr("listener")?;
        let ht = if !listener.is_none() {
            let target = partial.call1((m.getattr("_listen_hub")?, &listener, stop_event))?;
            let ht = make_thread(py, &thread_cls, &target, "registry-feed-hub-ws")?;
            ht.bind(py).call_method0("start")?;
            ht
        } else {
            py.None()
        };
        (st, ht)
    } else {
        (py.None(), py.None())
    };

    // main loop (a closure so the finally-block cleanup always runs)
    let loop_result: PyResult<()> = (|| -> PyResult<()> {
        let mut next_beat = monotonic(py)? + heartbeat_seconds;
        let wait_min = ctx.main_wake_seconds.min(heartbeat_seconds);
        loop {
            let should_stop: bool = stop_event.call_method1("wait", (wait_min,))?.extract()?;
            if should_stop {
                break;
            }
            if monotonic(py)? < next_beat {
                continue;
            }
            next_beat = monotonic(py)? + heartbeat_seconds;
            let oldest = pump.getattr("oldest_as_of")?;
            let now = clock.call_method0("now_utc")?;
            let payload = PyDict::new(py);
            payload.set_item("snapshots", watcher.getattr("snapshots")?)?;
            payload.set_item("connects", watcher.getattr("connects")?)?;
            payload.set_item("accounts", pump.getattr("accounts")?)?;
            payload.set_item("resnapshots", pump.getattr("resnapshots")?)?;
            let max_age = if oldest.is_none() {
                py.None().into_bound(py).into_any()
            } else {
                let diff = now.call_method1("__sub__", (&oldest,))?;
                let total: f64 = diff.call_method0("total_seconds")?.extract()?;
                py.import("builtins")?
                    .call_method1("round", (total, 1))?
                    .into_any()
            };
            payload.set_item("max_as_of_age_s", max_age)?;
            for item in counts_impl(py, &sink.call_method0("stats")?)?
                .bind(py)
                .call_method0("items")?
                .try_iter()?
            {
                let item = item?;
                payload.set_item(item.get_item(0)?, item.get_item(1)?)?;
            }
            if !schwab.is_none() {
                for item in schwab.call_method0("counts")?.call_method0("items")?.try_iter()? {
                    let item = item?;
                    payload.set_item(item.get_item(0)?, item.get_item(1)?)?;
                }
            }
            log_msg(py, "info", "registry feed heartbeat %s", vec![payload.into_any().unbind()])?;
        }
        Ok(())
    })();

    // REGFEED-STOP-1: every stop wait shares one deadline.
    let deadline = monotonic(py)? + ctx.stop_budget_seconds;
    watcher.call_method0("wake")?;
    pump.call_method0("stop")?;
    let stop_kwargs = PyDict::new(py);
    stop_kwargs.set_item("timeout", remaining(py, deadline)?)?;
    sink.call_method("stop", (), Some(&stop_kwargs))?;
    if !schwab.is_none() {
        let sk2 = PyDict::new(py);
        sk2.set_item("timeout", remaining(py, deadline)?)?;
        schwab.getattr("sink")?.call_method("stop", (), Some(&sk2))?;
    }
    join_worker(py, &thread, deadline)?;
    join_worker(py, &pump_thread, deadline)?;
    join_worker(py, &schwab_thread, deadline)?;
    join_worker(py, &hint_thread, deadline)?;
    log_msg(py, "info", "registry feed stopped", vec![])?;
    loop_result?;
    Ok(0)
}

fn make_thread(
    py: Python<'_>,
    thread_cls: &Bound<'_, PyAny>,
    target: &Bound<'_, PyAny>,
    name: &str,
) -> PyResult<Py<PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("target", target)?;
    kwargs.set_item("name", name)?;
    kwargs.set_item("daemon", true)?;
    Ok(thread_cls.call((), Some(&kwargs))?.unbind())
}

fn join_worker(py: Python<'_>, worker: &Py<PyAny>, deadline: f64) -> PyResult<()> {
    let bound = worker.bind(py);
    if bound.is_none() {
        return Ok(());
    }
    let kwargs = PyDict::new(py);
    kwargs.set_item("timeout", remaining(py, deadline)?)?;
    bound.call_method("join", (), Some(&kwargs))?;
    Ok(())
}
