//! W7-CORE-C21: the journal and RiskGuard-registry sinks' method bodies and HTTP I/O.
//!
//! Both Python modules keep their import path and public surface; their classes become thin
//! subclasses of the two bases here (`HttpJournalSinkBase`, `RiskguardRegistrySinkBase`). The
//! bases own the sequencing the Python methods did, in the same order. The HTTP network I/O stays
//! on stdlib urllib (`_default_http_request` / `_default_post`), reached through the `_http` /
//! `_post` attributes: chunked replies, https, redirects and IPv6 hosts are urllib's job.
//!
//! State lives in Python instance attributes set by each subclass's `__init__` (`_http`,
//! `_post`, `_outbox`, `_dirty`, `_seq_base`, `counters`, `last_error_kind`, `_clock`, `_config`,
//! the locks, events and worker thread), so the bases read and write them through `getattr` /
//! `setattr`. A collaborator a test hands in or reassigns (`http_client`, `_post`) is read at
//! call time through attribute lookup; a method a test monkeypatches (`RiskguardRegistrySink.stop`)
//! is only ever reached through Python attribute lookup, never a direct Rust call.
//!
//! `raise X from err` is preserved: `_send_one` maps the outbox's `ValueError((kind, message))`
//! refusals to `AccountRuleError` exactly as `trade_engine.accounts._rs._call` does.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

/// `trade_engine.accounts._rs._call`'s refusal mapping: an exact `ValueError((kind, message))`
/// becomes `AccountRuleError(kind, message)`; anything else passes through unchanged.
fn map_refusal<'py>(
    py: Python<'py>,
    result: PyResult<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    match result {
        Ok(value) => Ok(value),
        Err(error) => {
            if error.is_instance_of::<PyValueError>(py) {
                let value = error.value(py);
                if let Ok(args) = value.getattr("args") {
                    if args.len()? == 2 {
                        if let (Ok(kind), Ok(message)) = (args.get_item(0), args.get_item(1)) {
                            if kind.is_instance_of::<PyString>()
                                && message.is_instance_of::<PyString>()
                            {
                                if let Ok(cls) = py
                                    .import("trade_engine.accounts.errors")
                                    .and_then(|m| m.getattr("AccountRuleError"))
                                {
                                    if let Ok(instance) = cls.call1((&kind, &message)) {
                                        return Err(PyErr::from_value(instance));
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Err(error)
        }
    }
}

fn empty_list(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    py.import("builtins")?.getattr("list")?.call0()
}

fn get_logger(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    py.import("logging")?
        .getattr("getLogger")?
        .call1(("trade_engine.sinks.riskguard_registry",))
}

fn incr(slf: &Bound<'_, PyAny>, key: &str, delta: i64) -> PyResult<()> {
    let counters = slf.getattr("counters")?;
    let current = counters.get_item(key)?;
    counters.set_item(key, current.call_method1("__add__", (delta,))?)?;
    Ok(())
}

fn journal_auth(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    py.import("trade_engine.sinks.journal")?
        .getattr("JournalAuthError")
}

// ---------------------------------------------------------------------------------------------
// HttpJournalSink
// ---------------------------------------------------------------------------------------------

#[pyclass(subclass, module = "trade_engine_rs")]
pub struct HttpJournalSinkBase;

#[pymethods]
impl HttpJournalSinkBase {
    #[new]
    #[pyo3(signature = (base_url, account_id, *, http_client=None, name="journal"))]
    fn new(
        base_url: &Bound<'_, PyAny>,
        account_id: &Bound<'_, PyAny>,
        http_client: Option<&Bound<'_, PyAny>>,
        name: &str,
    ) -> Self {
        // The Python subclass's __init__ sets `base_url`, `account_id`, `name` and `_http`;
        // these are accepted only so __new__ agrees with __init__'s signature.
        let _ = (base_url, account_id, http_client, name);
        HttpJournalSinkBase
    }

    fn publish<'py>(
        slf: &Bound<'py, Self>,
        event_seq: &Bound<'py, PyAny>,
        event: &Bound<'py, PyAny>,
    ) -> PyResult<bool> {
        let py = slf.py();
        let execution_cls = py
            .import("trade_engine.interfaces.sinks")?
            .getattr("JournalExecution")?;
        if event.is_instance(&execution_cls)? {
            return slf
                .call_method1("publish_execution", (event_seq, event))?
                .extract();
        }
        if event.is_instance_of::<PyDict>() {
            let execution = slf.call_method1("_dict_to_execution", (event,))?;
            return slf
                .call_method1("publish_execution", (event_seq, execution))?
                .extract();
        }
        Ok(false)
    }

    fn publish_execution<'py>(
        slf: &Bound<'py, Self>,
        event_seq: &Bound<'py, PyAny>,
        execution: &Bound<'py, PyAny>,
    ) -> PyResult<bool> {
        let py = slf.py();
        let rs = py.import("trade_engine_rs")?;
        let auth = journal_auth(py)?;
        let account_id = slf.getattr("account_id")?;

        // 0. Invariant I8: refuse cross-account delivery.
        let same: bool = rs
            .getattr("journal_account")?
            .call1((execution.getattr("account_id")?, &account_id))?
            .extract()?;
        if !same {
            return Ok(false);
        }

        // 1. Multiplier first, so the journal's rebuild on insert already prices the fill.
        let derivative: bool = rs
            .getattr("journal_derivative")?
            .call1((execution.getattr("multiplier")?,))?
            .extract()?;
        if derivative {
            let ensured: bool = slf.call_method1("_ensure_multiplier", (execution,))?.extract()?;
            if !ensured {
                return Ok(false);
            }
        }

        // 2. Post the execution.
        let payload = rs
            .getattr("journal_payload")?
            .call1((&account_id, event_seq, execution))?;
        let (status, body) = match slf.call_method1("_request", ("/api/executions", "POST", &payload)) {
            Ok(value) => (value.get_item(0)?.extract::<u16>()?, value.get_item(1)?),
            Err(error) if error.is_instance(py, &auth) => return Err(error),
            Err(_) => return Ok(false),
        };
        let accepted: bool = rs.getattr("journal_accepted")?.call1((status,))?.extract()?;
        if !accepted {
            return Ok(false);
        }
        let stored: bool = rs.getattr("journal_stored")?.call1((&body,))?.extract()?;
        if !stored {
            return Ok(false);
        }

        // 3. Patch trade annotations (stopLoss, profitTarget, strategy tag).
        slf.call_method1("_patch_trade_annotations", (execution,))?;

        // 4. Invariant I12: confirm delivery by reading back, not by HTTP 200.
        slf.call_method1("confirm_delivery", (event_seq, execution))?
            .extract()
    }

    fn _dict_to_execution<'py>(
        slf: &Bound<'py, Self>,
        d: &Bound<'py, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let converted = py
            .import("trade_engine_rs")?
            .getattr("journal_execution")?
            .call1((d,))?;
        let cls = py
            .import("trade_engine.interfaces.sinks")?
            .getattr("JournalExecution")?;
        Ok(cls
            .call((), Some(converted.downcast::<PyDict>()?))?
            .unbind())
    }

    fn _request<'py>(
        slf: &Bound<'py, Self>,
        path: &str,
        method: &str,
        body: &Bound<'py, PyAny>,
    ) -> PyResult<(u16, Py<PyAny>)> {
        let py = slf.py();
        let base: String = slf.getattr("base_url")?.extract()?;
        let url = format!("{base}{path}");
        let http = slf.getattr("_http")?;
        let result = http.call1((url, method, body))?;
        let status: u16 = result.get_item(0)?.extract()?;
        let parsed = result.get_item(1)?;
        if status == 401 || status == 403 {
            let msg = format!(
                "Journal at {base} refused {method} {path} with {status}: it has a \
                 password configured and this sink does not authenticate"
            );
            return Err(PyErr::from_value(journal_auth(py)?.call1((msg,))?));
        }
        let parsed = if parsed.is_instance_of::<PyDict>() {
            parsed
        } else {
            PyDict::new(py).into_any()
        };
        Ok((status, parsed.unbind()))
    }

    fn _read_multipliers<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let value = slf.call_method1("_request", ("/api/settings", "GET", py.None()))?;
        let status: u16 = value.get_item(0)?.extract()?;
        let body = value.get_item(1)?;
        let multipliers = body.call_method1("get", ("multipliers",))?;
        if status != 200 || !multipliers.is_instance_of::<PyDict>() {
            return Ok(py.None().into_bound(py).unbind());
        }
        Ok(multipliers.unbind())
    }

    fn _ensure_multiplier<'py>(
        slf: &Bound<'py, Self>,
        execution: &Bound<'py, PyAny>,
    ) -> PyResult<bool> {
        let py = slf.py();
        let rs = py.import("trade_engine_rs")?;
        let auth = journal_auth(py)?;
        let symbol = rs
            .getattr("journal_symbol")?
            .call1((execution.getattr("symbol")?,))?;
        let result = (|| -> PyResult<bool> {
            let existing = slf.call_method0("_read_multipliers")?;
            let plan = rs.getattr("journal_multiplier")?.call1((
                &existing,
                &symbol,
                execution.getattr("multiplier")?,
            ))?;
            let action: String = plan.get_item("action")?.extract()?;
            if action == "refuse" {
                return Ok(false);
            }
            if action == "done" {
                return Ok(true);
            }
            let merged = plan.get_item("multipliers")?;
            let patch_body = PyDict::new(py);
            patch_body.set_item("multipliers", &merged)?;
            let status: u16 = match slf.call_method1("_request", ("/api/settings", "PATCH", &patch_body)) {
                Ok(value) => value.get_item(0)?.extract()?,
                Err(error) => return Err(error),
            };
            let accepted: bool = rs.getattr("journal_accepted")?.call1((status,))?.extract()?;
            if !accepted {
                return Ok(false);
            }
            let read_back = slf.call_method0("_read_multipliers")?;
            Ok(read_back.eq(&merged)?)
        })();
        match result {
            Ok(value) => Ok(value),
            Err(error) if error.is_instance(py, &auth) => Err(error),
            Err(_) => Ok(false),
        }
    }

    fn _patch_trade_annotations<'py>(
        slf: &Bound<'py, Self>,
        execution: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let rs = py.import("trade_engine_rs")?;
        let auth = journal_auth(py)?;
        let has: bool = rs
            .getattr("journal_annotations")?
            .call1((
                execution.getattr("stop_loss")?,
                execution.getattr("profit_target")?,
                execution.getattr("strategy_tag")?,
            ))?
            .extract()?;
        if !has {
            return Ok(());
        }
        let symbol = rs
            .getattr("journal_symbol")?
            .call1((execution.getattr("symbol")?,))?;
        let result = (|| -> PyResult<()> {
            let account_id = slf.getattr("account_id")?;
            let query_dict = PyDict::new(py);
            query_dict.set_item("accounts", &account_id)?;
            let query: String = py
                .import("urllib.parse")?
                .getattr("urlencode")?
                .call1((query_dict,))?
                .extract()?;
            let list_path = format!("/api/trades?{query}");
            let (status, body) = match slf.call_method1("_request", (list_path.as_str(), "GET", py.None())) {
                Ok(value) => (value.get_item(0)?.extract::<u16>()?, value.get_item(1)?),
                Err(error) => return Err(error),
            };
            if status != 200 {
                return Ok(());
            }
            let trades = body.call_method1("get", ("trades", empty_list(py)?))?;
            let matching = rs
                .getattr("journal_annotation_trade")?
                .call1((&trades, &symbol))?;
            if !matching.is_truthy()? {
                return Ok(());
            }
            let trade_key = matching.call_method1("get", ("key",))?;
            if !trade_key.is_truthy()? {
                return Ok(());
            }
            let patch_data = rs.getattr("journal_patch")?.call1((execution, &matching))?;
            if patch_data.is_truthy()? {
                let key_encoded: String = py
                    .import("urllib.parse")?
                    .getattr("quote")?
                    .call1((&trade_key, ""))?
                    .extract()?;
                let detail_path = format!("/api/trades/{key_encoded}");
                slf.call_method1("_request", (detail_path.as_str(), "PATCH", &patch_data))?;
            }
            Ok(())
        })();
        match result {
            Ok(()) => Ok(()),
            Err(error) if error.is_instance(py, &auth) => Err(error),
            Err(_) => Ok(()),
        }
    }

    #[pyo3(signature = (event_seq, execution=None))]
    fn confirm_delivery<'py>(
        slf: &Bound<'py, Self>,
        event_seq: &Bound<'py, PyAny>,
        execution: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<bool> {
        let execution = match execution {
            Some(value) if !value.is_none() => value,
            _ => return Ok(false),
        };
        let py = slf.py();
        let journal_mod = py.import("trade_engine.sinks.journal")?;
        let auth = journal_mod.getattr("JournalAuthError")?;
        let rs = py.import("trade_engine_rs")?;
        let symbol = rs
            .getattr("journal_symbol")?
            .call1((execution.getattr("symbol")?,))?;
        let executed_at = execution.getattr("executed_at")?;
        let sent = journal_mod
            .getattr("journal_executed_at")?
            .call1((executed_at, event_seq))?;
        let sent_at = journal_mod.getattr("_parse_instant")?.call1((sent,))?;

        let result = (|| -> PyResult<bool> {
            let account_id = slf.getattr("account_id")?;
            let query_dict = PyDict::new(py);
            query_dict.set_item("accounts", &account_id)?;
            let query: String = py
                .import("urllib.parse")?
                .getattr("urlencode")?
                .call1((query_dict,))?
                .extract()?;
            let list_path = format!("/api/trades?{query}");
            let (status, body) = match slf.call_method1("_request", (list_path.as_str(), "GET", py.None())) {
                Ok(value) => (value.get_item(0)?.extract::<u16>()?, value.get_item(1)?),
                Err(error) => return Err(error),
            };
            if status != 200 {
                return Ok(false);
            }
            let trades = body.call_method1("get", ("trades", empty_list(py)?))?;
            let candidates = rs.getattr("journal_candidates")?.call1((&trades, &symbol))?;
            let quote = py.import("urllib.parse")?.getattr("quote")?;
            for trade in candidates.try_iter()? {
                let trade = trade?;
                let trade_key = trade.call_method1("get", ("key",))?;
                if !trade_key.is_truthy()? {
                    continue;
                }
                let key_encoded: String = quote.call1((&trade_key, ""))?.extract()?;
                let detail_path = format!("/api/trades/{key_encoded}");
                let (dt_status, dt_body) =
                    match slf.call_method1("_request", (detail_path.as_str(), "GET", py.None())) {
                        Ok(value) => (value.get_item(0)?.extract::<u16>()?, value.get_item(1)?),
                        Err(error) => return Err(error),
                    };
                if dt_status != 200 {
                    continue;
                }
                let fills = dt_body.call_method1("get", ("executions", empty_list(py)?))?;
                for fill in fills.try_iter()? {
                    let fill = fill?;
                    let matched: bool = rs
                        .getattr("journal_match")?
                        .call1((execution, &fill, &dt_body, &sent_at))?
                        .extract()?;
                    if matched {
                        return Ok(true);
                    }
                }
            }
            Ok(false)
        })();
        match result {
            Ok(value) => Ok(value),
            Err(error) if error.is_instance(py, &auth) => Err(error),
            Err(_) => Ok(false),
        }
    }
}

// ---------------------------------------------------------------------------------------------
// RiskguardRegistrySink
// ---------------------------------------------------------------------------------------------

#[pyclass(subclass, module = "trade_engine_rs")]
pub struct RiskguardRegistrySinkBase;

#[pymethods]
impl RiskguardRegistrySinkBase {
    #[new]
    #[pyo3(signature = (config, *, clock, http_post=None))]
    fn new(
        config: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        http_post: Option<&Bound<'_, PyAny>>,
    ) -> Self {
        // The Python subclass's __init__ builds all state; these are accepted only so
        // __new__ agrees with __init__'s signature.
        let _ = (config, clock, http_post);
        RiskguardRegistrySinkBase
    }

    #[pyo3(signature = (snapshot, events, seq, pnl=None))]
    fn observe<'py>(
        slf: &Bound<'py, Self>,
        snapshot: &Bound<'py, PyAny>,
        events: &Bound<'py, PyAny>,
        seq: &Bound<'py, PyAny>,
        pnl: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<()> {
        let py = slf.py();
        let outbox = slf.getattr("_outbox")?;
        if outbox.is_none() {
            return Ok(());
        }

        let json = py.import("json")?;
        let separators = PyDict::new(py);
        separators.set_item("separators", (",", ":"))?;
        let contract = snapshot.call_method0("as_contract")?;
        let snap_text: String = json
            .getattr("dumps")?
            .call((contract,), Some(&separators))?
            .extract()?;
        let confirmed = py
            .import("trade_engine.interfaces.broker")?
            .getattr("_iso_z")?
            .call1((snapshot.getattr("confirmed_at")?,))?;
        let account_rule = py
            .import("trade_engine.accounts.errors")?
            .getattr("AccountRuleError")?;

        let success = {
            let lock = slf.getattr("_lock")?;
            lock.call_method0("__enter__")?;
            let guarded = (|| -> PyResult<bool> {
                let rebased = slf.call_method1("_rebased_events", (events,))?;
                let seq_base = slf.getattr("_seq_base")?;
                let seq_sum = seq.call_method1("__add__", (&seq_base,))?;
                if let Err(error) =
                    slf.call_method1("_observe_locked", (&snap_text, &rebased, &seq_sum, &confirmed))
                {
                    if !error.is_instance(py, &account_rule) {
                        return Err(error);
                    }
                    let kind: String = error.value(py).getattr("kind")?.extract()?;
                    if kind != "seq_gap" {
                        slf.call_method1("_refused", (kind,))?;
                        return Ok(false);
                    }
                    // The poller and the outbox disagree about the head: re-seed riskguard.
                    incr(slf, "reseeds", 1)?;
                    let stats_text: String = outbox.call_method0("stats")?.extract()?;
                    let stats = json.getattr("loads")?.call1((stats_text,))?;
                    let old_head: i64 = stats.call_method1("get", ("head_seq", 0))?.extract()?;
                    let current_base: i64 = slf.getattr("_seq_base")?.extract()?;
                    if old_head > current_base {
                        slf.setattr("_seq_base", old_head)?;
                        slf.call_method0("_persist_base")?;
                    }
                    outbox.call_method0("reset")?;
                    let rebased_again = slf.call_method1("_rebased_events", (events,))?;
                    let seq_base_again = slf.getattr("_seq_base")?;
                    let seq_sum_again = seq.call_method1("__add__", (&seq_base_again,))?;
                    if let Err(again) = slf.call_method1(
                        "_observe_locked",
                        (&snap_text, &rebased_again, &seq_sum_again, &confirmed),
                    ) {
                        if !again.is_instance(py, &account_rule) {
                            return Err(again);
                        }
                        let kind_again: String = again.value(py).getattr("kind")?.extract()?;
                        slf.call_method1("_refused", (kind_again,))?;
                        return Ok(false);
                    }
                }
                incr(slf, "observed", 1)?;
                slf.setattr("_dirty", true)?;
                let pnl_enabled: bool = slf.getattr("_config")?.getattr("pnl_enabled")?.extract()?;
                if pnl_enabled {
                    if let Some(rows) = pnl {
                        if !rows.is_none() {
                            slf.call_method1("_forward_pnl", (rows,))?;
                        }
                    }
                }
                Ok(true)
            })();
            lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
            guarded?
        };
        if success {
            slf.getattr("_wake")?.call_method0("set")?;
        }
        Ok(())
    }

    fn pump<'py>(slf: &Bound<'py, Self>) -> PyResult<i64> {
        let py = slf.py();
        if slf.getattr("_outbox")?.is_none() {
            return Ok(0);
        }
        let max: i64 = py
            .import("trade_engine.sinks.riskguard_registry")?
            .getattr("MAX_ENVELOPES_PER_PUMP")?
            .extract()?;
        let lock = slf.getattr("_pump_lock")?;
        lock.call_method0("__enter__")?;
        let looped = (|| -> PyResult<i64> {
            let mut acked = 0i64;
            for _ in 0..max {
                if slf.getattr("_stop")?.call_method0("is_set")?.is_truthy()? {
                    break;
                }
                let outcome: String = slf.call_method0("_send_one")?.extract()?;
                match outcome.as_str() {
                    "idle" => break,
                    "acked" => {
                        acked += 1;
                        continue;
                    }
                    "need_snapshot" => continue,
                    _ => break,
                }
            }
            Ok(acked)
        })();
        let persist = slf.call_method0("_persist");
        lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
        let acked = looped?;
        persist?;
        Ok(acked)
    }

    fn _send_one<'py>(slf: &Bound<'py, Self>) -> PyResult<String> {
        let py = slf.py();
        let module = py.import("trade_engine.sinks.riskguard_registry")?;
        let logger = get_logger(py)?;
        let clock = slf.getattr("_clock")?;
        let now_utc = clock.call_method0("now_utc")?;
        let now = py
            .import("trade_engine.interfaces.broker")?
            .getattr("_iso_z")?
            .call1((now_utc,))?;
        let lock = slf.getattr("_lock")?;
        let outbox = slf.getattr("_outbox")?;

        lock.call_method0("__enter__")?;
        let body = match map_refusal(py, outbox.call_method1("next_envelope", (&now,))) {
            Ok(value) => value,
            Err(error) => {
                lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
                let kind: String = error.value(py).getattr("kind")?.extract()?;
                slf.call_method1("_halted", (kind,))?;
                return Ok("halted".to_string());
            }
        };
        if body.is_none() {
            lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
            return Ok("idle".to_string());
        }
        slf.setattr("_dirty", true)?;
        lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;

        let config = slf.getattr("_config")?;
        let token_file = config.getattr("token_file")?;
        let token = match module.getattr("read_token")?.call1((token_file,)) {
            Ok(value) => value,
            Err(_) => {
                incr(slf, "token_failures", 1)?;
                slf.setattr("last_error_kind", "token_unavailable")?;
                logger.call_method1("warning", ("registry sink cannot read the connector token file",))?;
                return Ok("retry".to_string());
            }
        };
        let token_text: String = token.extract()?;
        let headers = PyDict::new(py);
        headers.set_item("Authorization", format!("Bearer {token_text}"))?;
        headers.set_item("Content-Type", "application/json")?;
        incr(slf, "posts", 1)?;
        let base: String = config.getattr("base_url")?.extract()?;
        let url = format!("{}/v1/registry/ingest", base.trim_end_matches('/'));
        let body_bytes = body.call_method1("encode", ("utf-8",))?;
        let timeout: f64 = module.getattr("REQUEST_TIMEOUT_S")?.extract()?;
        let post = slf.getattr("_post")?;
        let answer = post.call1((url, body_bytes, headers, timeout))?;
        let status: u16 = answer.get_item(0)?.extract()?;
        let text: String = answer.get_item(1)?.extract()?;

        lock.call_method0("__enter__")?;
        let response = map_refusal(py, outbox.call_method1("on_response", (status, text.as_str(), &now)));
        match response {
            Ok(result) => {
                slf.setattr("_dirty", true)?;
                lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
                let outcome: String = result.extract()?;
                if outcome == "retry" {
                    incr(slf, "post_failures", 1)?;
                    let kind = if status != 0 {
                        format!("http_{status}")
                    } else {
                        "no_answer".to_string()
                    };
                    slf.setattr("last_error_kind", kind)?;
                    return Ok("retry".to_string());
                }
                if outcome == "need_snapshot" {
                    incr(slf, "snapshots_requested", 1)?;
                    return Ok("need_snapshot".to_string());
                }
                incr(slf, "acks", 1)?;
                Ok("acked".to_string())
            }
            Err(error) => {
                let kind: String = error.value(py).getattr("kind")?.extract()?;
                if kind == "ack_ahead" {
                    let rebased: bool = slf
                        .call_method1("_rebase_on_ack_ahead", (status, text.as_str()))?
                        .extract()?;
                    lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
                    if rebased {
                        return Ok("need_snapshot".to_string());
                    }
                    slf.call_method1("_halted", (kind,))?;
                    return Ok("halted".to_string());
                }
                if matches!(kind.as_str(), "ack_missing" | "ack_short" | "bad_response") {
                    incr(slf, "ack_refused", 1)?;
                    slf.setattr("last_error_kind", kind.clone())?;
                    logger.call_method1(
                        "warning",
                        (format!("registry sink: delivery not confirmed ({kind})"),),
                    )?;
                    lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
                    return Ok("retry".to_string());
                }
                slf.call_method1("_halted", (kind,))?;
                lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
                Ok("halted".to_string())
            }
        }
    }

    fn _rebase_on_ack_ahead<'py>(
        slf: &Bound<'py, Self>,
        status: u16,
        text: &str,
    ) -> PyResult<bool> {
        let py = slf.py();
        let json = py.import("json")?;
        let applied: Option<i64> = if status == 200 {
            match json.getattr("loads")?.call1((text,)) {
                Ok(doc) => match doc.call_method1("get", ("applied_seq",)) {
                    Ok(value) => {
                        let bound = value;
                        let int_type = py.get_type::<pyo3::types::PyInt>();
                        let bool_type = py.get_type::<pyo3::types::PyBool>();
                        if bound.is_instance(&int_type)? && !bound.is_instance(&bool_type)? {
                            bound.extract::<i64>().ok().filter(|n| *n >= 0)
                        } else {
                            None
                        }
                    }
                    Err(_) => None,
                },
                Err(_) => None,
            }
        } else {
            None
        };
        let Some(applied_value) = applied else {
            return Ok(false);
        };

        let clock = slf.getattr("_clock")?;
        let now = clock.call_method0("now_utc")?;
        let last = slf.getattr("_last_rebase")?;
        let min_interval: f64 = py
            .import("trade_engine.sinks.riskguard_registry")?
            .getattr("REBASE_MIN_INTERVAL_S")?
            .extract()?;
        if !last.is_none() {
            let diff = now.call_method1("__sub__", (&last,))?;
            let seconds: f64 = diff.call_method0("total_seconds")?.extract()?;
            if seconds < min_interval {
                return Ok(false);
            }
        }

        let outbox = slf.getattr("_outbox")?;
        let stats_text: String = outbox.call_method0("stats")?.extract()?;
        let stats = json.getattr("loads")?.call1((stats_text,))?;
        let head: i64 = stats.call_method1("get", ("head_seq", 0))?.extract()?;
        let old_base: i64 = slf.getattr("_seq_base")?.extract()?;
        let delta = (applied_value - head + 1).max(0);
        let new_base = old_base + delta;
        slf.setattr("_seq_base", new_base)?;
        outbox.call_method0("reset")?;
        outbox.call_method0("resume")?;
        slf.setattr("_last_rebase", now)?;
        slf.setattr("_dirty", true)?;
        incr(slf, "rebases", 1)?;
        incr(slf, "snapshots_requested", 1)?;
        slf.setattr("last_error_kind", "ack_ahead")?;
        slf.call_method0("_persist_base")?;
        get_logger(py)?.call_method1(
            "warning",
            (format!(
                "registry sink rebased: riskguard applied_seq={applied_value} is ahead of \
                 outbox head={head}; seq_base {old_base} -> {new_base}"
            ),),
        )?;
        Ok(true)
    }

    fn resume<'py>(slf: &Bound<'py, Self>) -> PyResult<()> {
        let py = slf.py();
        let outbox = slf.getattr("_outbox")?;
        if outbox.is_none() {
            return Ok(());
        }
        let lock = slf.getattr("_lock")?;
        lock.call_method0("__enter__")?;
        let inner = (|| -> PyResult<()> {
            outbox.call_method0("resume")?;
            slf.setattr("_dirty", true)?;
            Ok(())
        })();
        lock.call_method1("__exit__", (py.None(), py.None(), py.None()))?;
        inner?;
        slf.getattr("_wake")?.call_method0("set")?;
        Ok(())
    }

    #[pyo3(signature = (tick_seconds=5.0))]
    fn start<'py>(slf: &Bound<'py, Self>, tick_seconds: f64) -> PyResult<()> {
        let py = slf.py();
        if slf.getattr("_outbox")?.is_none() || !slf.getattr("_thread")?.is_none() {
            return Ok(());
        }
        slf.getattr("_stop")?.call_method0("clear")?;
        let worker = py
            .import("trade_engine.sinks.riskguard_registry")?
            .getattr("_worker_run")?;
        let target = py
            .import("functools")?
            .getattr("partial")?
            .call1((&worker, slf.as_any(), tick_seconds))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("target", target)?;
        kwargs.set_item("name", "registry-sink")?;
        kwargs.set_item("daemon", true)?;
        let thread = py
            .import("threading")?
            .getattr("Thread")?
            .call((), Some(&kwargs))?;
        slf.setattr("_thread", thread.clone())?;
        thread.call_method0("start")?;
        Ok(())
    }

    #[pyo3(signature = (timeout=1.0))]
    fn stop<'py>(slf: &Bound<'py, Self>, timeout: f64) -> PyResult<()> {
        let py = slf.py();
        slf.getattr("_stop")?.call_method0("set")?;
        slf.getattr("_wake")?.call_method0("set")?;
        let thread = slf.getattr("_thread")?;
        slf.setattr("_thread", py.None())?;
        if !thread.is_none() {
            thread.call_method1("join", (timeout,))?;
            let alive: bool = thread.call_method0("is_alive")?.extract()?;
            if alive {
                get_logger(py)?.call_method1(
                    "warning",
                    ("registry sink still delivering at stop; final flush skipped",),
                )?;
                return Ok(());
            }
        }
        slf.call_method0("_persist")?;
        Ok(())
    }
}
