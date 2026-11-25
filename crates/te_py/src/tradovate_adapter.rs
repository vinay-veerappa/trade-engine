//! W7-CORE-C16: `TradovateAdapter` and `AccountWatcher`'s method bodies, moved into Rust.
//!
//! The two `#[pyclass(subclass)]` bases do the sequencing the Python methods did, step for
//! step and in the same order. Every collaborator stays a Python object, reached through
//! Python attribute lookup: the NTX client, the ledger, the clock, the `PollAndDiff`, the
//! registry sink and the injected `trading_day`/`on_poll` callbacks. The carriers
//! (`AccountRecord`, `AccountSnapshot`, `VenueIdentity`, `VenuePosition`, `VenueFill`,
//! `VenueAck`, `VenueOrderState`, `Route`) and the exceptions (`AccountRuleError`,
//! `NtxError`, `UnsupportedCapability`) are imported once and cached in a `GILOnceCell`.
//!
//! All instance state (`_client`, `_bindings`, `_live_ack`, `_orders`, `_resolved`, `_kept`,
//! `_identities`, `stats`, `poller`, …) is set by the Python subclass's `__init__` as
//! ordinary `__dict__` attributes, so the existing tests can still read them and monkeypatch
//! `_live_ack`; the Rust methods read and write them through `getattr`/`setattr`, never as
//! Rust fields. The pure helpers (`_mask`, `_wire_cl_ord_id`, `_price`, `_entry`, `_verdict`,
//! `_decimal`, `_aware`, `_text`, `_bracket_index`, `_figure_as_of`, `_is_hint`) become Rust
//! with their Python names kept; `_bracket_index` is a `#[pyfunction]` so
//! `tests/test_oa5_e2e.py` keeps importing it.

use pyo3::exceptions::{PyAttributeError, PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyString, PyTuple};

type Value<'py> = Bound<'py, PyAny>;

/// The Python classes, constants and exceptions the two bases reach, imported once.
struct Ctx {
    account_rule_error: Py<PyAny>,
    ntx_error: Py<PyAny>,
    unsupported: Py<PyAny>,
    refusal_code: Py<PyAny>,
    decimal: Py<PyAny>,
    invalid_operation: Py<PyAny>,
    poll_and_diff: Py<PyAny>,
    account_record: Py<PyAny>,
    account_snapshot: Py<PyAny>,
    venue_identity: Py<PyAny>,
    venue_position: Py<PyAny>,
    venue_fill: Py<PyAny>,
    venue_ack: Py<PyAny>,
    venue_order_state: Py<PyAny>,
    route: Py<PyAny>,
    account: Py<PyAny>,
    reconcile_report: Py<PyAny>,
    future: Py<PyAny>,
    accounts_rs: Py<PyAny>,
    unresolvable: Py<PyAny>,
    log: Py<PyAny>,
    // enum members
    order_type_market: Py<PyAny>,
    order_type_limit: Py<PyAny>,
    order_type_stop: Py<PyAny>,
    order_state_filled: Py<PyAny>,
    order_state_rejected: Py<PyAny>,
    order_state_accepted: Py<PyAny>,
    order_state_pending_unknown: Py<PyAny>,
    side_buy: Py<PyAny>,
    side_sell: Py<PyAny>,
    platform_tradovate: Py<PyAny>,
    live: Py<PyAny>,
    adopted: Py<PyAny>,
    envs: Py<PyAny>,
    // constants
    account_cap: i64,
    order_id_cap: i64,
    max_qty: i64,
    broker: String,
    source: String,
    figure_fresh_seconds: f64,
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let accounts_errors = py.import("trade_engine.accounts.errors")?;
        let ntx_client = py.import("trade_engine.tradovate.ntx_client")?;
        let broker = py.import("trade_engine.interfaces.broker")?;
        let live_ack = py.import("trade_engine.live_ack")?;
        let decimal = py.import("decimal")?;
        let poll = py.import("trade_engine.accounts.poll")?;
        let adapter = py.import("trade_engine.tradovate.adapter")?;
        let instruments = py.import("trade_engine.domain.instruments")?;
        let orders = py.import("trade_engine.domain.orders")?;
        let accounts = py.import("trade_engine.accounts")?;
        let logging = py.import("logging")?;

        let order_type = orders.getattr("OrderType")?;
        let order_state = orders.getattr("OrderState")?;
        let side = instruments.getattr("Side")?;
        let platform = instruments.getattr("Platform")?;

        let adopted = PyTuple::new(
            py,
            [
                order_state.getattr("SUBMITTED")?,
                order_state.getattr("ACCEPTED")?,
                order_state.getattr("PARTIALLY_FILLED")?,
                order_state.getattr("PENDING_UNKNOWN")?,
            ],
        )?
        .into_any()
        .unbind();

        Ok(Ctx {
            account_rule_error: accounts_errors.getattr("AccountRuleError")?.unbind(),
            ntx_error: ntx_client.getattr("NtxError")?.unbind(),
            unsupported: broker.getattr("UnsupportedCapability")?.unbind(),
            refusal_code: live_ack.getattr("REFUSAL_CODE")?.unbind(),
            decimal: decimal.getattr("Decimal")?.unbind(),
            invalid_operation: decimal.getattr("InvalidOperation")?.unbind(),
            poll_and_diff: poll.getattr("PollAndDiff")?.unbind(),
            account_record: broker.getattr("AccountRecord")?.unbind(),
            account_snapshot: broker.getattr("AccountSnapshot")?.unbind(),
            venue_identity: broker.getattr("VenueIdentity")?.unbind(),
            venue_position: broker.getattr("VenuePosition")?.unbind(),
            venue_fill: broker.getattr("VenueFill")?.unbind(),
            venue_ack: broker.getattr("VenueAck")?.unbind(),
            venue_order_state: broker.getattr("VenueOrderState")?.unbind(),
            route: broker.getattr("Route")?.unbind(),
            account: adapter.getattr("_Account")?.unbind(),
            reconcile_report: adapter.getattr("ReconcileReport")?.unbind(),
            future: instruments.getattr("Future")?.unbind(),
            accounts_rs: accounts.getattr("_rs")?.unbind(),
            unresolvable: instruments.getattr("UnresolvableInstrumentError")?.unbind(),
            log: logging.call_method1("getLogger", ("trade_engine.tradovate.adapter",))?.unbind(),
            order_type_market: order_type.getattr("MARKET")?.unbind(),
            order_type_limit: order_type.getattr("LIMIT")?.unbind(),
            order_type_stop: order_type.getattr("STOP")?.unbind(),
            order_state_filled: order_state.getattr("FILLED")?.unbind(),
            order_state_rejected: order_state.getattr("REJECTED")?.unbind(),
            order_state_accepted: order_state.getattr("ACCEPTED")?.unbind(),
            order_state_pending_unknown: order_state.getattr("PENDING_UNKNOWN")?.unbind(),
            side_buy: side.getattr("BUY")?.unbind(),
            side_sell: side.getattr("SELL")?.unbind(),
            platform_tradovate: platform.getattr("TRADOVATE")?.unbind(),
            live: adapter.getattr("_LIVE")?.unbind(),
            adopted,
            envs: adapter.getattr("_ENVS")?.unbind(),
            account_cap: adapter.getattr("ACCOUNT_CAP")?.extract()?,
            order_id_cap: adapter.getattr("ORDER_ID_CAP")?.extract()?,
            max_qty: adapter.getattr("_MAX_QTY")?.extract()?,
            broker: adapter.getattr("BROKER")?.extract()?,
            source: adapter.getattr("SOURCE")?.extract()?,
            figure_fresh_seconds: adapter.getattr("FIGURE_FRESH_SECONDS")?.extract()?,
        })
    }
}

fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

// --- exceptions -------------------------------------------------------------------------

fn account_rule_error(py: Python<'_>, ctx: &Ctx, kind: &str, message: &str) -> PyErr {
    PyErr::from_value(
        ctx.account_rule_error
            .bind(py)
            .call1((kind, message))
            .expect("AccountRuleError is callable with (kind, message)"),
    )
}

fn unsupported_error(py: Python<'_>, ctx: &Ctx, message: &str) -> PyErr {
    PyErr::from_value(
        ctx.unsupported
            .bind(py)
            .call1((message,))
            .expect("UnsupportedCapability is callable with a message"),
    )
}

/// The `exc.kind` string of a caught `PyErr` (used for `NtxError` and `AccountRuleError`).
fn kind_of(py: Python<'_>, e: &PyErr) -> PyResult<String> {
    e.value(py).getattr("kind")?.extract()
}

/// The `exc.message` string of a caught `NtxError`.
fn message_of(py: Python<'_>, e: &PyErr) -> PyResult<String> {
    e.value(py).getattr("message")?.extract()
}

// --- logging ----------------------------------------------------------------------------

fn log(py: Python<'_>, ctx: &Ctx, level: &str, msg: &str) -> PyResult<()> {
    ctx.log.bind(py).call_method1(level, (msg,))?;
    Ok(())
}

// --- the pure helpers (Python names kept) ----------------------------------------------

/// `_mask`: `"***" + account[-3:]` for a truthy account, else `"***"`.
fn mask(account: Option<&str>) -> String {
    match account {
        None | Some("") => "***".to_string(),
        Some(s) => {
            let start = s.len().saturating_sub(3);
            format!("***{}", &s[start..])
        }
    }
}

/// `_wire_cl_ord_id`: the id with `:` written as `.`, when it matches `^[A-Za-z0-9._-]{1,64}$`.
fn wire_cl_ord_id(text: &str) -> Option<String> {
    let text = text.replace(':', ".");
    let ok = (1..=64).contains(&text.len())
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'_' || b == b'-');
    ok.then_some(text)
}

/// `_price`: `format(value, "f")`.
fn price(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<String> {
    py.import("builtins")?
        .call_method1("format", (value, "f"))?
        .extract()
}

fn str_of(_py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.str()?.extract()
}

/// `_entry`: `{"type": "limit", "price": _price(limit)}` for a LIMIT, else `{"type": "market"}`.
fn entry_dict(py: Python<'_>, ctx: &Ctx, order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let order_type = order.getattr("order_type")?;
    let d = PyDict::new(py);
    if order_type.is(ctx.order_type_limit.bind(py)) {
        let limit = order.getattr("limit_price")?;
        d.set_item("type", "limit")?;
        d.set_item("price", price(py, &limit)?)?;
    } else {
        d.set_item("type", "market")?;
    }
    Ok(d.into_any().unbind())
}

/// `_verdict`: `{"allowed", "verdictRevision", "checkedAt"}`.
fn verdict_dict(py: Python<'_>, verdict: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let d = PyDict::new(py);
    d.set_item("allowed", verdict.getattr("allowed")?)?;
    d.set_item("verdictRevision", verdict.getattr("verdict_revision")?)?;
    d.set_item("checkedAt", verdict.getattr("checked_at")?)?;
    Ok(d.into_any().unbind())
}

/// `_decimal`: an ntx number as a Decimal via the shortest repr; NaN, infinity and
/// non-numbers refuse with `bad_money`.
fn decimal(
    py: Python<'_>,
    ctx: &Ctx,
    value: &Bound<'_, PyAny>,
    what: &str,
) -> PyResult<Py<PyAny>> {
    let decimal = ctx.decimal.bind(py);
    let bad = |what: &str| account_rule_error(py, ctx, "bad_money", &format!("{what} is not a number"));
    if value.is_none() || value.is_instance_of::<PyBool>() {
        return Err(bad(what));
    }
    if value.is_instance_of::<PyFloat>() {
        let finite: bool = py.import("math")?.getattr("isfinite")?.call1((value,))?.extract()?;
        if !finite {
            return Err(account_rule_error(
                py,
                ctx,
                "bad_money",
                &format!("{what} is not finite"),
            ));
        }
        let repr: String = value.repr()?.extract()?;
        return Ok(decimal.call1((repr,))?.unbind());
    }
    if value.is_instance_of::<PyInt>() {
        return Ok(decimal.call1((value,))?.unbind());
    }
    if value.is_instance_of::<PyString>() {
        let out = match decimal.call1((value,)) {
            Ok(out) => out,
            Err(e) if e.is_instance(py, ctx.invalid_operation.bind(py)) => return Err(bad(what)),
            Err(e) => return Err(e),
        };
        let finite: bool = out.getattr("is_finite")?.call0()?.extract()?;
        if !finite {
            return Err(account_rule_error(
                py,
                ctx,
                "bad_money",
                &format!("{what} is not finite"),
            ));
        }
        return Ok(out.unbind());
    }
    Err(bad(what))
}

/// `_aware`: an ISO timestamp (a trailing `Z` becomes `+00:00`) to a UTC-aware datetime.
fn aware(py: Python<'_>, ctx: &Ctx, text: &Bound<'_, PyAny>, what: &str) -> PyResult<Py<PyAny>> {
    if !text.is_instance_of::<PyString>() {
        return Err(account_rule_error(
            py,
            ctx,
            "bad_shape",
            &format!("{what} is not a timestamp"),
        ));
    }
    let raw: String = text.extract()?;
    let datetime = py.import("datetime")?.getattr("datetime")?;
    let value = match datetime.call_method1("fromisoformat", (raw.replace('Z', "+00:00"),)) {
        Ok(v) => v,
        Err(e) if e.is_instance_of::<PyValueError>(py) => {
            return Err(account_rule_error(
                py,
                ctx,
                "bad_shape",
                &format!("{what} is not an ISO timestamp"),
            ));
        }
        Err(e) => return Err(e),
    };
    let tzinfo = value.getattr("tzinfo")?;
    if tzinfo.is_none() || tzinfo.call_method1("utcoffset", (&value,))?.is_none() {
        return Err(account_rule_error(
            py,
            ctx,
            "bad_shape",
            &format!("{what} has no timezone"),
        ));
    }
    let utc = py.import("datetime")?.getattr("timezone")?.getattr("utc")?;
    Ok(value.call_method1("astimezone", (utc,))?.unbind())
}

/// `_text`: a non-empty, stripped string field.
fn text(py: Python<'_>, ctx: &Ctx, row: &Bound<'_, PyAny>, field: &str, what: &str) -> PyResult<String> {
    let value = row.call_method1("get", (field,))?;
    if !value.is_instance_of::<PyString>() {
        return Err(account_rule_error(
            py,
            ctx,
            "bad_shape",
            &format!("{what}.{field} is missing"),
        ));
    }
    let s: String = value.extract()?;
    if s.trim().is_empty() {
        return Err(account_rule_error(
            py,
            ctx,
            "bad_shape",
            &format!("{what}.{field} is missing"),
        ));
    }
    Ok(s)
}

/// `_bracket_index`: `(clOrdId -> ntx ids, authoritative)` from one snapshot account row.
fn bracket_index(py: Python<'_>, row: &Bound<'_, PyAny>) -> PyResult<(Option<Py<PyAny>>, bool)> {
    if row.is_none() {
        return Ok((None, false));
    }
    let out = PyDict::new(py);
    let mut seen = false;
    let orders = row.call_method1("get", ("orders",))?;
    if orders.is_instance_of::<PyList>() {
        for item in orders.try_iter()? {
            let item = item?;
            if item.is_instance_of::<PyDict>() {
                let links = item.call_method1("get", ("brackets",))?;
                let cl = item.call_method1("get", ("clOrdId",))?;
                if cl.is_instance_of::<PyString>()
                    && links.is_instance_of::<PyDict>()
                    && !links.call_method1("get", ("parentId",))?.is_none()
                    && !links.call_method1("get", ("stopId",))?.is_none()
                {
                    seen = true;
                    let parent = links.call_method1("get", ("parentId",))?;
                    let stop = links.call_method1("get", ("stopId",))?;
                    let target = links.call_method1("get", ("targetId",))?;
                    let entry = PyDict::new(py);
                    entry.set_item("clOrdId", &cl)?;
                    entry.set_item("entryId", parent)?;
                    entry.set_item("stopId", stop)?;
                    entry.set_item("targetId", target)?;
                    if !out.contains(&cl)? {
                        out.set_item(&cl, entry)?;
                    }
                }
            }
        }
    }
    let brackets = row.call_method1("get", ("brackets",))?;
    if brackets.is_instance_of::<PyList>() {
        seen = true;
        for item in brackets.try_iter()? {
            let item = item?;
            if item.is_instance_of::<PyDict>() {
                let cl = item.call_method1("get", ("clOrdId",))?;
                if cl.is_instance_of::<PyString>()
                    && !item.call_method1("get", ("entryId",))?.is_none()
                    && !item.call_method1("get", ("stopId",))?.is_none()
                {
                    if !out.contains(&cl)? {
                        out.set_item(&cl, &item)?;
                    }
                }
            }
        }
    }
    if !seen {
        return Ok((None, false));
    }
    let authoritative = row.call_method1("get", ("bracketsAuthoritative",))?;
    // `is not False`: false only when the value is the `False` singleton.
    let is_false = authoritative.is_instance_of::<PyBool>() && !authoritative.is_truthy()?;
    Ok((Some(out.into_any().unbind()), !is_false))
}

/// `_is_hint`: `type == "sync"`, or `type == "props"` and `entity_type == "accounts"`.
fn is_hint(_py: Python<'_>, payload: &Bound<'_, PyAny>) -> PyResult<bool> {
    let kind = payload.call_method1("get", ("type",))?;
    if kind.eq("sync")? {
        return Ok(true);
    }
    if kind.eq("props")? {
        let entity = payload.call_method1("get", ("entity_type",))?;
        return Ok(entity.eq("accounts")?);
    }
    Ok(false)
}

/// `_figure_as_of`: the read time while connected (or fresh), else the daemon's time.
fn figure_as_of(
    py: Python<'_>,
    ctx: &Ctx,
    daemon_as_of: &Bound<'_, PyAny>,
    read_at: &Bound<'_, PyAny>,
    connected: bool,
) -> PyResult<Py<PyAny>> {
    if connected {
        return Ok(read_at.clone().unbind());
    }
    let delta = read_at.call_method1("__sub__", (daemon_as_of,))?;
    let timedelta = py.import("datetime")?.getattr("timedelta")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("seconds", ctx.figure_fresh_seconds)?;
    let window = timedelta.call((), Some(&kwargs))?;
    if delta.le(&window)? {
        Ok(read_at.clone().unbind())
    } else {
        Ok(daemon_as_of.clone().unbind())
    }
}

// --- TradovateAdapter -------------------------------------------------------------------

/// `TradovateAdapter`'s body, as a `#[pyclass(subclass)]` the Python module subclasses.
/// All state is Python `__dict__` (set by `TradovateAdapter.__init__`); every method reads
/// and writes it through `getattr`/`setattr`, so the tests' `_live_ack` monkeypatch and
/// direct reads of `_kept`/`_orders`/`_resolved` keep working.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct TradovateAdapterBase;

#[pymethods]
impl TradovateAdapterBase {
    #[new]
    #[pyo3(signature = (client, *, clock, ledger=None, trading_day=None, on_poll=None, account_bindings=None, live_ack=false))]
    fn new(
        client: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        ledger: Option<&Bound<'_, PyAny>>,
        trading_day: Option<&Bound<'_, PyAny>>,
        on_poll: Option<&Bound<'_, PyAny>>,
        account_bindings: Option<&Bound<'_, PyAny>>,
        live_ack: bool,
    ) -> Self {
        let _ = (client, clock, ledger, trading_day, on_poll, account_bindings, live_ack);
        TradovateAdapterBase
    }

    // -- identity ------------------------------------------------------------------

    fn connect<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        slf.call_method0("take_payload")?;
        slf.call_method0("accounts")?;
        let identities = slf.call_method0("identities")?;
        if !identities.is_truthy()? {
            return Err(account_rule_error(
                py,
                &ctx,
                "no_identity",
                "ntx reports no identity",
            ));
        }
        let first = identities.get_item(0)?;
        let env = first.getattr("env")?;
        slf.setattr("env", &env)?;
        let payload = slf.call_method0("take_payload")?;
        let ledger = slf.getattr("_ledger")?;
        if !ledger.is_none() && !payload.is_none() {
            let kwargs = PyDict::new(py);
            kwargs.set_item("payload", &payload)?;
            slf.call_method("reconcile", (), Some(&kwargs))?;
        }
        Ok(first.unbind())
    }

    fn take_payload<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let payload = slf.getattr("_last_payload")?;
        slf.setattr("_last_payload", py.None())?;
        Ok(payload.unbind())
    }

    fn identities<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let identities = slf.getattr("_identities")?;
        Ok(py
            .import("builtins")?
            .call_method1("list", (identities.call_method0("values")?,))?
            .unbind())
    }

    // -- the account port ------------------------------------------------------------

    fn accounts<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        inc_stat(slf, "reads")?;
        let mapped = (|| -> PyResult<Py<PyAny>> {
            let client = slf.getattr("_client")?;
            let payload = client.call_method0("snapshot")?;
            let clock = slf.getattr("_clock")?;
            let now = clock.call_method0("now_utc")?;
            let mapped = slf.call_method1("_map", (&payload, &now))?;
            slf.setattr("_last_payload", &payload)?;
            Ok(mapped.unbind())
        })();
        let mapped = match mapped {
            Ok(m) => m,
            Err(e) if e.is_instance(py, ctx.ntx_error.bind(py)) => {
                inc_stat(slf, "read_failures")?;
                let kind = kind_of(py, &e)?;
                log(py, &ctx, "warning", &format!("ntx read failed ({kind})"))?;
                return fallback(slf, py, e);
            }
            Err(e) if e.is_instance(py, ctx.account_rule_error.bind(py)) => {
                inc_stat(slf, "refused_snapshots")?;
                let kind = kind_of(py, &e)?;
                if kind == "over_cap" {
                    inc_stat(slf, "over_cap_refused")?;
                }
                log(py, &ctx, "warning", &format!("ntx snapshot refused ({kind})"))?;
                return fallback(slf, py, e);
            }
            Err(e) => return Err(e),
        };
        let snapshot = mapped.bind(py).get_item(0)?;
        let kept = mapped.bind(py).get_item(1)?;
        let identities = mapped.bind(py).get_item(2)?;
        let reference = mapped.bind(py).get_item(3)?;
        slf.setattr("_last_accounts", &snapshot)?;
        slf.setattr("_kept", &kept)?;
        slf.setattr("_identities", &identities)?;
        slf.setattr("_reference", &reference)?;
        Ok(snapshot.unbind())
    }

    fn account_events<'py>(slf: &Bound<'py, Self>, since: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let poller = slf.getattr("poller")?;
        Ok(poller.call_method1("account_events", (since,))?.unbind())
    }

    #[getter]
    fn poller<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let existing = slf.getattr("_poller")?;
        if !existing.is_none() {
            return Ok(existing.unbind());
        }
        let clock = slf.getattr("_clock")?;
        let ledger = slf.getattr("_ledger")?;
        let stale_after = slf.getattr("stale_after")?;
        let on_poll = slf.getattr("_on_poll")?;
        let trading_day = slf.getattr("_trading_day")?;
        let pnl_source = if !trading_day.is_none() {
            slf.getattr("pnl_rows")?
        } else {
            py.None().into_bound(py)
        };
        let kwargs = PyDict::new(py);
        kwargs.set_item("clock", &clock)?;
        kwargs.set_item("ledger", &ledger)?;
        kwargs.set_item("stale_after", &stale_after)?;
        kwargs.set_item("on_poll", &on_poll)?;
        kwargs.set_item("pnl_source", &pnl_source)?;
        kwargs.set_item("broker", ctx.broker.as_str())?;
        let poller = ctx.poll_and_diff.bind(py).call((slf.as_any(),), Some(&kwargs))?;
        slf.setattr("_poller", &poller)?;
        Ok(poller.unbind())
    }

    fn pnl_rows<'py>(slf: &Bound<'py, Self>, snapshot: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let rows = PyList::empty(py);
        let mut oldest: Option<Value<'py>> = None;
        let figures = slf.call_method1("_figures", (snapshot,))?;
        for item in figures.try_iter()? {
            let item = item?;
            let key = item.get_item(0)?;
            let figure = item.get_item(1)?;
            if figure.is_none() {
                let pair = PyTuple::new(py, [key, py.None().into_bound(py)])?;
                rows.append(pair)?;
                continue;
            }
            let value = figure.get_item(0)?;
            let as_of = figure.get_item(1)?;
            let value_str = price(py, &value)?;
            let as_of_str: String = as_of.call_method1("strftime", ("%Y-%m-%dT%H:%M:%SZ",))?.extract()?;
            let inner = PyTuple::new(py, [value_str, as_of_str])?;
            let pair = PyTuple::new(py, [key, inner.into_any()])?;
            rows.append(pair)?;
            let replace = match &oldest {
                None => true,
                Some(o) => as_of.lt(o)?,
            };
            if replace {
                oldest = Some(as_of.clone());
            }
        }
        match oldest {
            Some(o) => slf.setattr("last_figure_as_of", &o)?,
            None => slf.setattr("last_figure_as_of", py.None())?,
        }
        Ok(rows.into_any().unbind())
    }

    fn _figures<'py>(slf: &Bound<'py, Self>, snapshot: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let trading_day = slf.getattr("_trading_day")?;
        let records = snapshot.getattr("records")?;
        let out = PyList::empty(py);
        if trading_day.is_none() {
            for record in records.try_iter()? {
                let record = record?;
                out.append(PyTuple::new(py, [record.getattr("key")?, py.None().into_bound(py)])?)?;
            }
            return Ok(out.into_any().unbind());
        }
        let confirmed = snapshot.getattr("confirmed_at")?;
        let day = trading_day.call1((&confirmed,))?;
        let kept = slf.getattr("_kept")?;
        for record in records.try_iter()? {
            let record = record?;
            let key = record.getattr("key")?;
            let figure = match compute_figure(slf, py, &ctx, &kept, &key, &day, &confirmed) {
                Ok(f) => f,
                Err(e) if e.is_instance(py, ctx.account_rule_error.bind(py)) => {
                    let kind = kind_of(py, &e)?;
                    let display = record.getattr("display")?;
                    let masked = ctx.accounts_rs.bind(py).call_method1("mask", (&display,))?;
                    let masked: String = masked.extract()?;
                    log(py, &ctx, "warning", &format!("ntx realized pnl unreadable for {masked} ({kind})"))?;
                    None
                }
                Err(e) => return Err(e),
            };
            let item = match figure {
                Some((value, as_of)) => {
                    let inner = PyTuple::new(py, [value.bind(py), as_of.bind(py)])?;
                    PyTuple::new(py, [key, inner.into_any()])?
                }
                None => PyTuple::new(py, [key, py.None().into_bound(py)])?,
            };
            out.append(item)?;
        }
        Ok(out.into_any().unbind())
    }

    #[staticmethod]
    fn _figure_as_of(
        daemon_as_of: &Bound<'_, PyAny>,
        read_at: &Bound<'_, PyAny>,
        connected: bool,
    ) -> PyResult<Py<PyAny>> {
        let py = daemon_as_of.py();
        let ctx = ctx(py)?;
        figure_as_of(py, &ctx, daemon_as_of, read_at, connected)
    }

    fn has_open_position<'py>(slf: &Bound<'py, Self>) -> PyResult<bool> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let kept = slf.getattr("_kept")?;
        for account in kept.call_method0("values")?.try_iter()? {
            let account = account?;
            let check = (|| -> PyResult<bool> {
                let raw = account.getattr("raw")?;
                let positions = raw.call_method1("get", ("positions",))?;
                let positions = if positions.is_none() {
                    PyList::empty(py).into_any()
                } else {
                    positions
                };
                for row in positions.try_iter()? {
                    let row = row?;
                    let net = row.call_method1("get", ("net_pos",))?;
                    let dec = decimal(py, &ctx, &net, "position.net_pos")?;
                    let zero = ctx.decimal.bind(py).call1((0,))?;
                    if !dec.bind(py).eq(&zero)? {
                        return Ok(true);
                    }
                }
                Ok(false)
            })();
            match check {
                Ok(true) => return Ok(true),
                Ok(false) => {}
                Err(e)
                    if e.is_instance(py, ctx.account_rule_error.bind(py))
                        || e.is_instance_of::<PyAttributeError>(py) =>
                {
                    return Ok(true);
                }
                Err(e) => return Err(e),
            }
        }
        Ok(false)
    }

    fn is_stale<'py>(slf: &Bound<'py, Self>, now: &Bound<'py, PyAny>) -> PyResult<bool> {
        let last = slf.getattr("_last_accounts")?;
        if last.is_none() {
            return Ok(true);
        }
        let stale_after = slf.getattr("stale_after")?;
        Ok(last.call_method1("is_stale", (now, stale_after))?.is_truthy()?)
    }

    fn stale_keys<'py>(slf: &Bound<'py, Self>, now: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let kept = slf.getattr("_kept")?;
        let stale_after = slf.getattr("stale_after")?;
        let out = PyList::empty(py);
        for item in kept.call_method0("items")?.try_iter()? {
            let item = item?;
            let key = item.get_item(0)?;
            let account = item.get_item(1)?;
            let record = account.getattr("record")?;
            let confirmed = record.getattr("confirmed_at")?;
            let records = PyTuple::new(py, [record])?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("records", records)?;
            kwargs.set_item("confirmed_at", &confirmed)?;
            let one = ctx.account_snapshot.bind(py).call((), Some(&kwargs))?;
            if one.call_method1("is_stale", (now, &stale_after))?.is_truthy()? {
                out.append(&key)?;
            }
        }
        Ok(out.into_any().unbind())
    }

    // -- mapping ------------------------------------------------------------------

    fn _map<'py>(slf: &Bound<'py, Self>, payload: &Bound<'py, PyAny>, now: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let rows = payload.call_method1("get", ("accounts",))?;
        if !rows.is_instance_of::<PyList>() {
            return Err(account_rule_error(py, &ctx, "bad_shape", "snapshot.accounts is not a list"));
        }
        let len = rows.len()?;
        if len > ctx.account_cap as usize {
            return Err(account_rule_error(
                py,
                &ctx,
                "over_cap",
                &format!("snapshot holds more than {} accounts", ctx.account_cap),
            ));
        }
        let fetched = aware(py, &ctx, &payload.call_method1("get", ("fetchedAt",))?, "snapshot.fetchedAt")?;
        let degraded = slf.call_method1("_degraded", (payload,))?;
        let kept = PyDict::new(py);
        let identities = PyDict::new(py);
        let env_of = PyDict::new(py);
        for row in rows.try_iter()? {
            let row = row?;
            if !row.is_instance_of::<PyDict>() {
                return Err(account_rule_error(py, &ctx, "bad_shape", "an account row is not an object"));
            }
            let record_tuple = slf.call_method1("_record", (&row, now))?;
            let record = record_tuple.get_item(0)?;
            let identity = record_tuple.get_item(1)?;
            let env = record_tuple.get_item(2)?;
            if let Some(prev) = env_of.get_item(&identity)? {
                if !prev.eq(&env)? {
                    return Err(account_rule_error(py, &ctx, "env_conflict", "one identity reports two environments"));
                }
            }
            env_of.set_item(&identity, &env)?;
            if identities.get_item(&identity)?.is_none() {
                let kwargs = PyDict::new(py);
                kwargs.set_item("account_id", &identity)?;
                kwargs.set_item("env", &env)?;
                kwargs.set_item("connected_at", now)?;
                kwargs.set_item("broker_name", slf.getattr("name")?)?;
                let venue_identity = ctx.venue_identity.bind(py).call((), Some(&kwargs))?;
                identities.set_item(&identity, &venue_identity)?;
            }
            let record_key = record.getattr("key")?;
            if kept.contains(&record_key)? {
                return Err(account_rule_error(py, &ctx, "bad_shape", "two accounts share a key"));
            }
            let kwargs = PyDict::new(py);
            kwargs.set_item("record", &record)?;
            kwargs.set_item("identity", &identity)?;
            kwargs.set_item("raw", &row)?;
            let account = ctx.account.bind(py).call((), Some(&kwargs))?;
            kept.set_item(&record_key, &account)?;
        }
        let old_kept = slf.getattr("_kept")?;
        for item in old_kept.call_method0("items")?.try_iter()? {
            let item = item?;
            let key = item.get_item(0)?;
            let old = item.get_item(1)?;
            let old_identity = old.getattr("identity")?;
            let in_degraded = degraded.call_method1("__contains__", (&old_identity,))?.is_truthy()?;
            if in_degraded && !kept.contains(&key)? {
                kept.set_item(&key, &old)?;
                inc_stat(slf, "degraded_carried")?;
                if identities.get_item(&old_identity)?.is_none() {
                    let old_identities = slf.getattr("_identities")?;
                    let prev = old_identities.call_method1("get", (&old_identity,))?;
                    if !prev.is_none() {
                        identities.set_item(&old_identity, &prev)?;
                        env_of.set_item(&old_identity, prev.getattr("env")?)?;
                    }
                }
                let masked = ctx.accounts_rs.bind(py).call_method1("mask", (&old_identity,))?;
                let masked: String = masked.extract()?;
                log(py, &ctx, "warning", &format!("ntx identity {masked} is degraded; keeping its accounts, now stale"))?;
            }
        }
        if kept.len() > ctx.account_cap as usize {
            return Err(account_rule_error(
                py,
                &ctx,
                "over_cap",
                &format!("the list would hold more than {} accounts", ctx.account_cap),
            ));
        }
        let mut records_vec = Vec::new();
        for account in kept.call_method0("values")?.try_iter()? {
            records_vec.push(account?.getattr("record")?);
        }
        let records = PyTuple::new(py, records_vec)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("records", records)?;
        kwargs.set_item("confirmed_at", now)?;
        let snapshot = ctx.account_snapshot.bind(py).call((), Some(&kwargs))?;
        let date = fetched.bind(py).call_method0("date")?;
        Ok(PyTuple::new(
            py,
            [snapshot, kept.into_any(), identities.into_any(), date],
        )?
        .into_any()
        .unbind())
    }

    #[staticmethod]
    fn _degraded(payload: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = payload.py();
        let ctx = ctx(py)?;
        let mut errors = payload.call_method1("get", ("identityErrors",))?;
        if errors.is_none() {
            errors = PyList::empty(py).into_any();
        }
        if !errors.is_instance_of::<PyList>() {
            return Err(account_rule_error(py, &ctx, "bad_shape", "snapshot.identityErrors is not a list"));
        }
        let out = PySet::empty(py)?;
        for item in errors.try_iter()? {
            let item = item?;
            if !item.is_instance_of::<PyDict>() {
                return Err(account_rule_error(py, &ctx, "bad_shape", "an identity error is not an object"));
            }
            let ident = text(py, &ctx, &item, "identity", "identityErrors")?;
            out.add(ident)?;
        }
        Ok(py.import("builtins")?.call_method1("frozenset", (out,))?.unbind())
    }

    fn _record<'py>(slf: &Bound<'py, Self>, row: &Bound<'py, PyAny>, now: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let name = text(py, &ctx, row, "name", "account")?;
        let ident = text(py, &ctx, row, "id", "account")?;
        if !ident.bytes().all(|b| b.is_ascii_digit()) {
            return Err(account_rule_error(py, &ctx, "bad_shape", "account.id is not a numeric Tradovate id"));
        }
        let session = row.call_method1("get", ("session",))?;
        if !session.is_instance_of::<PyDict>() {
            return Err(account_rule_error(py, &ctx, "bad_shape", "account.session is missing"));
        }
        let identity = text(py, &ctx, &session, "identity", "account.session")?;
        let env = row.call_method1("get", ("environment",))?;
        let envs = ctx.envs.bind(py);
        if !envs.call_method1("__contains__", (&env,))?.is_truthy()? {
            return Err(account_rule_error(py, &ctx, "bad_env", "account.environment is not sim, paper or live"));
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item("env", &env)?;
        kwargs.set_item("source", ctx.source.as_str())?;
        kwargs.set_item("confirmed_at", now)?;
        kwargs.set_item("api_ref", ident.as_str())?;
        let route1 = ctx.route.bind(py).call1(("nt8", name.as_str()))?;
        let route2 = ctx.route.bind(py).call1(("tradingview", format!("D{ident}")))?;
        let routes = PyTuple::new(py, [route1, route2])?;
        kwargs.set_item("routes", routes)?;
        let record = ctx
            .account_record
            .bind(py)
            .call_method("of", (ctx.broker.as_str(), name.as_str()), Some(&kwargs))?;
        let identity_py = identity.into_pyobject(py)?.into_any();
        Ok(PyTuple::new(py, [record, identity_py, env])?.into_any().unbind())
    }

    // -- positions, fills, realized P&L -------------------------------------------

    fn _account<'py>(slf: &Bound<'py, Self>, account_key: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let kept = slf.getattr("_kept")?;
        if account_key.is_none() {
            if kept.len()? != 1 {
                return Err(account_rule_error(
                    py,
                    &ctx,
                    "account_required",
                    "name an account key: the adapter holds more than one",
                ));
            }
            let values = kept.call_method0("values")?;
            let first = values.call_method0("__iter__")?.call_method0("__next__")?;
            return Ok(first.unbind());
        }
        match kept.get_item(account_key) {
            Ok(account) => Ok(account.unbind()),
            Err(e) if e.is_instance_of::<PyKeyError>(py) => Err(account_rule_error(
                py,
                &ctx,
                "unknown_account",
                "no such account in the last read",
            )),
            Err(e) => Err(e),
        }
    }

    fn _future<'py>(slf: &Bound<'py, Self>, symbol: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let reference = slf.getattr("_reference")?;
        if !symbol.is_instance_of::<PyString>() || reference.is_none() {
            return Err(account_rule_error(py, &ctx, "unresolvable", "the instrument is not text"));
        }
        match ctx.future.bind(py).call_method1(
            "from_platform",
            (ctx.platform_tradovate.bind(py), symbol, &reference),
        ) {
            Ok(f) => Ok(f.unbind()),
            Err(e)
                if e.is_instance(py, ctx.unresolvable.bind(py)) || e.is_instance_of::<PyValueError>(py) =>
            {
                Err(account_rule_error(py, &ctx, "unresolvable", "an unknown root or month in a Tradovate symbol"))
            }
            Err(e) => Err(e),
        }
    }

    #[pyo3(signature = (account_key=None))]
    fn positions<'py>(slf: &Bound<'py, Self>, account_key: Option<&Bound<'py, PyAny>>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let account = match account_key {
            Some(key) => slf.call_method1("_account", (key,))?,
            None => slf.call_method1("_account", (py.None(),))?,
        };
        let raw = account.getattr("raw")?;
        let as_of = aware(py, &ctx, &raw.call_method1("get", ("as_of_utc",))?, "account.as_of_utc")?;
        let rows = raw.call_method1("get", ("positions",))?;
        let rows = if rows.is_none() { PyList::empty(py).into_any() } else { rows };
        let out = PyList::empty(py);
        for row in rows.try_iter()? {
            let row = row?;
            let instrument = slf.call_method1("_future", (row.call_method1("get", ("symbol",))?,))?;
            let quantity = decimal(py, &ctx, &row.call_method1("get", ("net_pos",))?, "position.net_pos")?;
            let avg_price = decimal(py, &ctx, &row.call_method1("get", ("avg_price",))?, "position.avg_price")?;
            out.append(ctx.venue_position.bind(py).call1((instrument, quantity, avg_price, as_of.bind(py)))?)?;
        }
        Ok(out.into_any().unbind())
    }

    #[pyo3(signature = (since, account_key=None))]
    fn fills<'py>(
        slf: &Bound<'py, Self>,
        since: &Bound<'py, PyAny>,
        account_key: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let account = match account_key {
            Some(key) => slf.call_method1("_account", (key,))?,
            None => slf.call_method1("_account", (py.None(),))?,
        };
        let raw = account.getattr("raw")?;
        let rows = raw.call_method1("get", ("trades",))?;
        let rows = if rows.is_none() { PyList::empty(py).into_any() } else { rows };
        let out = PyList::empty(py);
        for row in rows.try_iter()? {
            let row = row?;
            let filled_at = aware(py, &ctx, &row.call_method1("get", ("executedAt",))?, "trade.executedAt")?;
            if filled_at.bind(py).lt(since)? {
                continue;
            }
            let side = row.call_method1("get", ("side",))?;
            if !side.eq("buy")? && !side.eq("sell")? {
                return Err(account_rule_error(py, &ctx, "bad_shape", "trade.side is not buy or sell"));
            }
            let is_buy = side.eq("buy")?;
            let fee = row.call_method1("get", ("fee",))?;
            let fill_id: String = row.call_method1("get", ("id",))?.str()?.extract()?;
            let instrument = slf.call_method1("_future", (row.call_method1("get", ("symbol",))?,))?;
            let quantity = decimal(py, &ctx, &row.call_method1("get", ("quantity",))?, "trade.quantity")?;
            let price = decimal(py, &ctx, &row.call_method1("get", ("price",))?, "trade.price")?;
            let side_enum = if is_buy { ctx.side_buy.bind(py) } else { ctx.side_sell.bind(py) };
            let fee_dec = if fee.is_none() {
                ctx.decimal.bind(py).call1((0,))?.unbind()
            } else {
                decimal(py, &ctx, &fee, "trade.fee")?
            };
            out.append(ctx.venue_fill.bind(py).call1((
                fill_id,
                "",
                instrument,
                quantity,
                price,
                filled_at.bind(py),
                side_enum,
                fee_dec,
            ))?)?;
        }
        Ok(out.into_any().unbind())
    }

    fn realized_pnl<'py>(
        slf: &Bound<'py, Self>,
        account_key: &Bound<'py, PyAny>,
        day: &Bound<'py, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let trading_day = slf.getattr("_trading_day")?;
        if trading_day.is_none() {
            return Ok(py.None().into_bound(py).unbind());
        }
        let kept = slf.getattr("_kept")?;
        let account = kept.call_method1("get", (account_key,))?;
        if account.is_none() {
            return Ok(py.None().into_bound(py).unbind());
        }
        let raw = account.getattr("raw")?;
        let as_of = aware(py, &ctx, &raw.call_method1("get", ("as_of_utc",))?, "account.as_of_utc")?;
        let mapped_day = trading_day.call1((&as_of,))?;
        if !mapped_day.eq(day)? {
            return Ok(py.None().into_bound(py).unbind());
        }
        decimal(py, &ctx, &raw.call_method1("get", ("realized_pnl",))?, "account.realized_pnl")
    }

    fn orders<'py>(slf: &Bound<'py, Self>, since: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let resolved = slf.getattr("_resolved")?;
        let out = PyList::empty(py);
        for item in resolved.call_method0("values")?.try_iter()? {
            let item = item?;
            let updated = item.getattr("updated_at")?;
            if updated.ge(since)? {
                out.append(&item)?;
            }
        }
        Ok(out.into_any().unbind())
    }

    // -- reconcile (OA-5) ------------------------------------------------------------

    #[pyo3(signature = (ledger=None, *, payload=None))]
    fn reconcile<'py>(
        slf: &Bound<'py, Self>,
        ledger: Option<&Bound<'py, PyAny>>,
        payload: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let report = ctx.reconcile_report.bind(py).call0()?;
        let ledger = match ledger {
            Some(l) => l.clone(),
            None => slf.getattr("_ledger")?,
        };
        if ledger.is_none() {
            let logged = slf.getattr("_logged_no_ledger")?;
            if !logged.is_truthy()? {
                slf.setattr("_logged_no_ledger", true)?;
                log(py, &ctx, "info", "ntx reconcile skipped (no ledger)")?;
            }
            return Ok(report.unbind());
        }
        let payload = match payload {
            Some(p) => p.clone(),
            None => match slf.getattr("_client")?.call_method0("snapshot") {
                Ok(p) => p,
                Err(e) if e.is_instance(py, ctx.ntx_error.bind(py)) => {
                    let kind = kind_of(py, &e)?;
                    log(py, &ctx, "warning", &format!("ntx reconcile read failed ({kind})"))?;
                    report.setattr("read_failed", true)?;
                    return Ok(report.unbind());
                }
                Err(e) => return Err(e),
            },
        };
        let rows = if payload.is_instance_of::<PyDict>() {
            payload.call_method1("get", ("accounts",))?
        } else {
            py.None().into_bound(py)
        };
        if !rows.is_instance_of::<PyList>() {
            log(py, &ctx, "warning", "ntx reconcile refused (bad_shape)")?;
            report.setattr("read_failed", true)?;
            return Ok(report.unbind());
        }
        let by_name = PyDict::new(py);
        for row in rows.try_iter()? {
            let row = row?;
            if row.is_instance_of::<PyDict>() {
                let name = row.call_method1("get", ("name",))?;
                if name.is_instance_of::<PyString>() {
                    by_name.set_item(&name, &row)?;
                }
            }
        }
        let clock = slf.getattr("_clock")?;
        let now = clock.call_method0("now_utc")?;
        let ts: f64 = now.call_method0("timestamp")?.extract()?;
        let base: i64 = (ts * 1000.0) as i64;
        let known_accounts = py.import("builtins")?.getattr("set")?.call1((ledger.call_method0("accounts")?,))?;
        let bindings = slf.getattr("_bindings")?;
        for engine_account in bindings.try_iter()? {
            let engine_account = engine_account?;
            let ntx_account = slf.call_method1("_ntx_account", (&engine_account,))?;
            if ntx_account.is_none() || !known_accounts.call_method1("__contains__", (&engine_account,))?.is_truthy()? {
                continue;
            }
            let row = by_name.call_method1("get", (&ntx_account,))?;
            let (index, authoritative) = bracket_index(py, &row)?;
            let state = ledger.call_method1("state", (&engine_account,))?;
            let orders_map = state.getattr("orders")?;
            let venue_ids = state.getattr("venue_order_ids")?;
            let orders_list = py.import("builtins")?.call_method1("list", (orders_map.call_method0("values")?,))?;
            for order in orders_list.try_iter()? {
                let order = order?;
                let parent = order.getattr("parent_order_id")?;
                if !parent.is_none() {
                    continue;
                }
                let order_id = order.getattr("order_id")?;
                let venue_val = venue_ids.call_method1("get", (&order_id,))?;
                let venue = if venue_val.is_none() || !venue_val.is_truthy()? {
                    order_id.clone()
                } else {
                    venue_val
                };
                let kids = PyList::empty(py);
                for item in orders_map.call_method0("values")?.try_iter()? {
                    let item = item?;
                    let item_parent = item.getattr("parent_order_id")?;
                    let in_parents = item_parent.eq(&order_id)? || item_parent.eq(&venue)?;
                    let item_state = item.getattr("state")?;
                    let in_live = ctx.live.bind(py).call_method1("__contains__", (&item_state,))?.is_truthy()?;
                    if in_parents && in_live {
                        kids.append(&item)?;
                    }
                }
                let order_state = order.getattr("state")?;
                let pending = order_state.is(ctx.order_state_pending_unknown.bind(py));
                let in_live = ctx.live.bind(py).call_method1("__contains__", (&order_state,))?.is_truthy()?;
                let is_filled = order_state.is(ctx.order_state_filled.bind(py));
                if !(in_live || (is_filled && kids.len() > 0)) {
                    continue;
                }
                let orders = slf.getattr("_orders")?;
                if orders.contains(&venue)? && !pending {
                    continue;
                }
                let wire = wire_cl_ord_id(&str_of(py, &venue)?);
                let found = match (&index, &wire) {
                    (Some(index), Some(wire)) => index.bind(py).call_method1("get", (wire,))?,
                    _ => py.None().into_bound(py),
                };
                if found.is_none() {
                    if pending && index.is_some() && authoritative {
                        let rejected = ctx.order_state_rejected.bind(py);
                        slf.call_method1("_resolve", (&venue, rejected, order.getattr("quantity")?, &now))?;
                        report.getattr("rejected")?.call_method1("append", (&order_id,))?;
                        log(py, &ctx, "info", &format!(
                            "ntx reconcile: venue_order={} account={} status=REJECTED code=not_at_venue",
                            str_of(py, &venue)?,
                            mask(Some(&str_of(py, &ntx_account)?))
                        ))?;
                        continue;
                    }
                    report.getattr("unmatched")?.call_method1("append", (&order_id,))?;
                    let logged = slf.getattr("_logged_unmatched")?;
                    if !logged.call_method1("__contains__", (&venue,))?.is_truthy()? {
                        logged.call_method1("add", (&venue,))?;
                        log(py, &ctx, "warning", &format!(
                            "ntx reconcile: venue_order={} account={} unmatched; left unmapped",
                            str_of(py, &venue)?,
                            mask(Some(&str_of(py, &ntx_account)?))
                        ))?;
                    }
                    continue;
                }
                let base_py = base.into_pyobject(py)?.into_any();
                slf.call_method1("_map_bracket", (&venue, &order, &kids, &found, &ntx_account, base_py, &venue_ids))?;
                report.getattr("mapped")?.call_method1("append", (&order_id,))?;
                if pending {
                    let accepted_state = ctx.order_state_accepted.bind(py);
                    slf.call_method1("_resolve", (&venue, accepted_state, order.getattr("quantity")?, &now))?;
                    report.getattr("accepted")?.call_method1("append", (&order_id,))?;
                    log(py, &ctx, "info", &format!(
                        "ntx reconcile: venue_order={} account={} status=ACCEPTED",
                        str_of(py, &venue)?,
                        mask(Some(&str_of(py, &ntx_account)?))
                    ))?;
                }
            }
        }
        let mapped = report.getattr("mapped")?.len()?;
        let unmatched = report.getattr("unmatched")?.len()?;
        let accepted = report.getattr("accepted")?.len()?;
        let rejected = report.getattr("rejected")?.len()?;
        log(py, &ctx, "info", &format!(
            "ntx reconcile: mapped={mapped} unmatched={unmatched} accepted={accepted} rejected={rejected}"
        ))?;
        Ok(report.unbind())
    }

    fn _map_bracket<'py>(
        slf: &Bound<'py, Self>,
        venue: &Bound<'py, PyAny>,
        order: &Bound<'py, PyAny>,
        kids: &Bound<'py, PyAny>,
        found: &Bound<'py, PyAny>,
        ntx_account: &Bound<'py, PyAny>,
        base: i64,
        venue_ids: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let mut stops: Vec<Value<'py>> = Vec::new();
        let mut target_pairs: Vec<(String, Value<'py>)> = Vec::new();
        for item in kids.try_iter()? {
            let item = item?;
            let otype = item.getattr("order_type")?;
            if otype.is(ctx.order_type_stop.bind(py)) {
                stops.push(item);
            } else if otype.is(ctx.order_type_limit.bind(py)) {
                let oid: String = item.getattr("order_id")?.extract()?;
                target_pairs.push((oid, item));
            }
        }
        target_pairs.sort_by(|a, b| a.0.cmp(&b.0));
        let stop = stops.into_iter().next();
        let target = target_pairs.into_iter().next().map(|(_, item)| item);
        let stop_id = found.get_item("stopId")?;
        let target_id = found.call_method1("get", ("targetId",))?;
        let qty: i64 = to_int(py, &order.getattr("quantity")?)?;
        let entry_spec = {
            let otype = order.getattr("order_type")?;
            let limit = order.getattr("limit_price")?;
            if otype.is(ctx.order_type_limit.bind(py)) && !limit.is_none() {
                let d = PyDict::new(py);
                d.set_item("type", "limit")?;
                d.set_item("price", price(py, &limit)?)?;
                d.into_any().unbind()
            } else {
                let d = PyDict::new(py);
                d.set_item("type", "market")?;
                d.into_any().unbind()
            }
        };
        let wire_key = |child: &Value<'py>| -> PyResult<Value<'py>> {
            let child_id = child.getattr("order_id")?;
            let v = venue_ids.call_method1("get", (&child_id,))?;
            if v.is_none() || !v.is_truthy()? {
                Ok(child_id)
            } else {
                Ok(v)
            }
        };
        let target_child = match &target {
            Some(t) => {
                let state = t.getattr("state")?;
                if in_adopted(py, &ctx, &state)? {
                    Some(wire_key(t)?)
                } else {
                    None
                }
            }
            None => None,
        };
        // entry record
        {
            let record = PyDict::new(py);
            record.set_item("entryId", found.get_item("entryId")?)?;
            record.set_item("stopId", &stop_id)?;
            record.set_item("targetId", &target_id)?;
            record.set_item("account", ntx_account)?;
            record.set_item("qty", qty)?;
            record.set_item("entry", entry_spec.bind(py))?;
            record.set_item("seq", base)?;
            let stop_qty = match &stop {
                Some(s) => to_int(py, &s.getattr("quantity")?)?,
                None => qty,
            };
            record.set_item("stopQty", stop_qty)?;
            let stop_price = match &stop {
                Some(s) => {
                    let sp = s.getattr("stop_price")?;
                    if sp.is_none() { py.None().into_bound(py) } else { price(py, &sp)?.into_pyobject(py)?.into_any() }
                }
                None => py.None().into_bound(py),
            };
            record.set_item("stopPrice", &stop_price)?;
            let target_qty = match &target {
                Some(t) => to_int(py, &t.getattr("quantity")?)?,
                None => qty,
            };
            record.set_item("targetQty", target_qty)?;
            let target_price = match &target {
                Some(t) => {
                    let lp = t.getattr("limit_price")?;
                    if lp.is_none() { py.None().into_bound(py) } else { price(py, &lp)?.into_pyobject(py)?.into_any() }
                }
                None => py.None().into_bound(py),
            };
            record.set_item("targetPrice", &target_price)?;
            match &target_child {
                Some(tc) => record.set_item("targetChild", tc)?,
                None => record.set_item("targetChild", py.None())?,
            }
            slf.call_method1("_remember", (venue, &record))?;
        }
        // stop child record
        if let Some(stop_ref) = &stop {
            let state = stop_ref.getattr("state")?;
            let stop_price = stop_ref.getattr("stop_price")?;
            if in_adopted(py, &ctx, &state)? && !stop_price.is_none() {
                let record = PyDict::new(py);
                record.set_item("account", ntx_account)?;
                record.set_item("entryId", &stop_id)?;
                record.set_item("stopId", &stop_id)?;
                record.set_item("qty", to_int(py, &stop_ref.getattr("quantity")?)?)?;
                record.set_item("seq", base)?;
                record.set_item("child", "stop")?;
                record.set_item("side", stop_ref.getattr("side")?)?;
                let entry = PyDict::new(py);
                entry.set_item("type", "stop")?;
                entry.set_item("price", price(py, &stop_price)?)?;
                record.set_item("entry", entry)?;
                let key = wire_key(stop_ref)?;
                slf.call_method1("_remember", (&key, &record))?;
            }
        }
        // target child record
        if let (Some(tc), Some(target_ref)) = (&target_child, &target) {
            if !target_id.is_none() {
                let limit_price = target_ref.getattr("limit_price")?;
                if !limit_price.is_none() {
                    let record = PyDict::new(py);
                    record.set_item("account", ntx_account)?;
                    record.set_item("entryId", &target_id)?;
                    record.set_item("stopId", py.None())?;
                    record.set_item("qty", to_int(py, &target_ref.getattr("quantity")?)?)?;
                    record.set_item("seq", base)?;
                    record.set_item("child", "target")?;
                    record.set_item("side", target_ref.getattr("side")?)?;
                    let entry = PyDict::new(py);
                    entry.set_item("type", "limit")?;
                    entry.set_item("price", price(py, &limit_price)?)?;
                    record.set_item("entry", entry)?;
                    slf.call_method1("_remember", (tc, &record))?;
                }
            }
        }
        Ok(())
    }

    fn _resolve<'py>(
        slf: &Bound<'py, Self>,
        venue: &Bound<'py, PyAny>,
        state: &Bound<'py, PyAny>,
        qty: &Bound<'py, PyAny>,
        now: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let resolved = slf.getattr("_resolved")?;
        let zero = ctx.decimal.bind(py).call1((0,))?;
        let vs = ctx.venue_order_state.bind(py).call1((venue, state, zero, qty, now))?;
        resolved.set_item(venue, &vs)?;
        resolved.call_method1("move_to_end", (venue,))?;
        while resolved.len()? > ctx.order_id_cap as usize {
            resolved.call_method1("popitem", (false,))?;
        }
        Ok(())
    }

    fn cash_events<'py>(_slf: &Bound<'py, Self>, _since: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = _slf.py();
        Ok(PyList::empty(py).into_any().unbind())
    }

    // -- the order verbs -------------------------------------------------------------

    fn submit<'py>(slf: &Bound<'py, Self>, order: &Bound<'py, PyAny>) -> PyResult<Value<'py>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let parent = order.getattr("parent_order_id")?;
        let order_type = order.getattr("order_type")?;
        let is_child = order_type.is(ctx.order_type_stop.bind(py)) || order_type.is(ctx.order_type_limit.bind(py));
        if !parent.is_none() && is_child {
            return slf.call_method1("_adopt_child", (order,));
        }
        let is_entry = order_type.is(ctx.order_type_market.bind(py)) || order_type.is(ctx.order_type_limit.bind(py));
        if !is_entry {
            return Err(unsupported_error(py, &ctx, &format!(
                "order type {} not supported on tradovate",
                str_of(py, &order_type)?
            )));
        }
        let tif = order.getattr("tif")?;
        let supported_tifs = slf.getattr("capabilities")?.getattr("supported_tifs")?;
        if !supported_tifs.call_method1("__contains__", (&tif,))?.is_truthy()? {
            return Err(unsupported_error(py, &ctx, &format!(
                "time in force {} not supported on tradovate",
                str_of(py, &tif)?
            )));
        }
        let accounts = py.import("builtins")?.getattr("set")?.call0()?;
        for item in order.getattr("allocations")?.try_iter()? {
            let item = item?;
            accounts.call_method1("add", (item.getattr("account_id")?,))?;
        }
        if accounts.len()? != 1 {
            return Err(unsupported_error(py, &ctx, "netted orders not supported on tradovate"));
        }
        let engine_account = accounts.call_method0("__iter__")?.call_method0("__next__")?;
        let cl = order.getattr("venue_order_id")?;
        let ntx_account = slf.call_method1("_ntx_account", (&engine_account,))?;
        let live_refused = slf.call_method1("_live_refused", (&ntx_account,))?;
        if live_refused.is_truthy()? {
            return slf.call_method1("_refused", (&cl, "place", &engine_account, ctx.refusal_code.bind(py)));
        }
        let stop_price = order.getattr("protective_stop_price")?;
        if stop_price.is_none() {
            return slf.call_method1("_refused", (&cl, "place", &engine_account, "no_stop"));
        }
        let verdict = order.getattr("riskguard_verdict")?;
        let usable = (|| -> PyResult<bool> {
            if verdict.is_none() {
                return Ok(false);
            }
            let checked = verdict.getattr("checked_at")?;
            let revision = verdict.getattr("verdict_revision")?;
            Ok(!checked.is_none() && !revision.is_none())
        })()?;
        if !usable {
            return slf.call_method1("_refused", (&cl, "place", &engine_account, "verdict_required"));
        }
        let ntx_account = slf.call_method1("_ntx_account", (&engine_account,))?;
        if ntx_account.is_none() {
            return slf.call_method1("_refused", (&cl, "place", &engine_account, "unmapped_account"));
        }
        let cl_str = str_of(py, &cl)?;
        let wire_cl = wire_cl_ord_id(&cl_str);
        let wire_cl = match wire_cl {
            Some(w) => w,
            None => return slf.call_method1("_refused", (&cl, "place", &ntx_account, "cl_ord_id_invalid")),
        };
        let qty = order.getattr("quantity")?;
        let integral = qty.call_method0("to_integral_value")?;
        let one = ctx.decimal.bind(py).call1((1,))?;
        let max = ctx.decimal.bind(py).call1((ctx.max_qty,))?;
        if !qty.eq(&integral)? || !(one.le(&qty)? && qty.le(&max)?) {
            return slf.call_method1("_refused", (&cl, "place", &ntx_account, "qty_invalid"));
        }
        let symbol = match order.getattr("instrument")?.call_method1("to_platform", (ctx.platform_tradovate.bind(py),)) {
            Ok(s) => s,
            Err(_) => return slf.call_method1("_refused", (&cl, "place", &ntx_account, "symbol_invalid")),
        };
        let body = PyDict::new(py);
        body.set_item("account", &ntx_account)?;
        body.set_item("clOrdId", wire_cl.as_str())?;
        body.set_item("symbol", &symbol)?;
        body.set_item("side", if order.getattr("side")?.is(ctx.side_buy.bind(py)) { "Buy" } else { "Sell" })?;
        body.set_item("qty", to_int(py, &qty)?)?;
        body.set_item("entry", entry_dict(py, &ctx, order)?)?;
        body.set_item("stop", price(py, &stop_price)?)?;
        let target = order.getattr("protective_target_price")?;
        if !target.is_none() {
            body.set_item("target", price(py, &target)?)?;
        }
        body.set_item("verdict", verdict_dict(py, &verdict)?)?;
        let client = slf.getattr("_client")?;
        let place = client.getattr("place")?;
        let sent = slf.call_method1("_send", ("place", &cl, &ntx_account, &place, &body))?;
        let ack = sent.get_item(0)?;
        let response = sent.get_item(1)?;
        if !response.is_none() {
            let outcome = response.call_method1("get", ("outcome",))?;
            let order_ids = response.call_method1("get", ("orderIds",))?;
            if outcome.eq("SENT")? && !order_ids.is_none() && order_ids.is_truthy()? {
                let ids = py.import("builtins")?.call_method1("dict", (&order_ids,))?;
                ids.set_item("account", &ntx_account)?;
                ids.set_item("qty", to_int(py, &qty)?)?;
                ids.set_item("entry", body.get_item("entry")?)?;
                ids.set_item("seq", 0)?;
                ids.set_item("stopQty", to_int(py, &qty)?)?;
                ids.set_item("targetQty", to_int(py, &qty)?)?;
                ids.set_item("stopPrice", body.get_item("stop")?)?;
                ids.set_item("targetPrice", body.call_method1("get", ("target",))?)?;
                ids.set_item("targetChild", py.None())?;
                slf.call_method1("_remember", (&cl, &ids))?;
            }
        }
        Ok(ack)
    }

    fn _adopt_child<'py>(slf: &Bound<'py, Self>, order: &Bound<'py, PyAny>) -> PyResult<Value<'py>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let cl = order.getattr("venue_order_id")?;
        let accounts = py.import("builtins")?.getattr("set")?.call0()?;
        for item in order.getattr("allocations")?.try_iter()? {
            let item = item?;
            accounts.call_method1("add", (item.getattr("account_id")?,))?;
        }
        if accounts.len()? != 1 {
            return Err(unsupported_error(py, &ctx, "netted orders not supported on tradovate"));
        }
        let engine_account = accounts.call_method0("__iter__")?.call_method0("__next__")?;
        let ntx_account = slf.call_method1("_ntx_account", (&engine_account,))?;
        let live_refused = slf.call_method1("_live_refused", (&ntx_account,))?;
        if live_refused.is_truthy()? {
            return slf.call_method1("_refused", (&cl, "adopt", &ntx_account, ctx.refusal_code.bind(py)));
        }
        let orders = slf.getattr("_orders")?;
        let parent_id = order.getattr("parent_order_id")?;
        let parent = orders.call_method1("get", (&parent_id,))?;
        if parent.is_none() {
            return slf.call_method1("_refused", (&cl, "adopt", &ntx_account, "unknown_parent"));
        }
        if !parent.get_item("account")?.eq(&ntx_account)? {
            return slf.call_method1("_refused", (&cl, "adopt", &ntx_account, "account_mismatch"));
        }
        let is_stop = order.getattr("order_type")?.is(ctx.order_type_stop.bind(py));
        let (ntx_id, held_qty, held_price, order_price) = if is_stop {
            let ntx_id = parent.call_method1("get", ("stopId",))?;
            let held_qty = parent.get_item("stopQty")?;
            let held_price = parent.get_item("stopPrice")?;
            (ntx_id, held_qty, held_price, order.getattr("stop_price")?)
        } else {
            let ntx_id = parent.call_method1("get", ("targetId",))?;
            let held_qty = parent.get_item("targetQty")?;
            let held_price = parent.get_item("targetPrice")?;
            let order_price = order.getattr("limit_price")?;
            if ntx_id.is_none() || held_price.is_none() {
                return slf.call_method1("_refused", (&cl, "adopt", &ntx_account, "no_target"));
            }
            let claimed = parent.call_method1("get", ("targetChild",))?;
            if !claimed.is_none() && !claimed.eq(&cl)? {
                return slf.call_method1("_refused", (&cl, "adopt", &ntx_account, "extra_target"));
            }
            (ntx_id, held_qty, held_price, order_price)
        };
        if ntx_id.is_none() || order_price.is_none() {
            return slf.call_method1("_refused", (&cl, "adopt", &ntx_account, "no_stop"));
        }
        let qty = order.getattr("quantity")?;
        let integral = qty.call_method0("to_integral_value")?;
        let one = ctx.decimal.bind(py).call1((1,))?;
        let max = ctx.decimal.bind(py).call1((ctx.max_qty,))?;
        if !qty.eq(&integral)? || !(one.le(&qty)? && qty.le(&max)?) {
            return slf.call_method1("_refused", (&cl, "adopt", &ntx_account, "qty_invalid"));
        }
        let new_price = price(py, &order_price)?;
        let held_price_str = str_of(py, &held_price)?;
        let price_changed = {
            let np = ctx.decimal.bind(py).call1((&new_price,))?;
            let hp = ctx.decimal.bind(py).call1((&held_price_str,))?;
            !np.eq(&hp)?
        };
        if is_stop && price_changed {
            let np = ctx.decimal.bind(py).call1((&new_price,))?;
            let hp = ctx.decimal.bind(py).call1((&held_price_str,))?;
            let side = order.getattr("side")?;
            let is_buy = side.is(ctx.side_buy.bind(py));
            let loosens = if is_buy { np.gt(&hp)? } else { np.lt(&hp)? };
            if loosens {
                return slf.call_method1("_refused", (&cl, "adopt", &ntx_account, "stop_loosen"));
            }
        }
        let mut seq = 0i64;
        let qty_int = to_int(py, &qty)?;
        let ack = if price_changed || qty_int != to_int(py, &held_qty)? {
            let cl_str = str_of(py, &cl)?;
            let wire_cl = match wire_cl_ord_id(&format!("{cl_str}.m0")) {
                Some(w) => w,
                None => return slf.call_method1("_refused", (&cl, "modify", &ntx_account, "cl_ord_id_invalid")),
            };
            let body = PyDict::new(py);
            body.set_item("account", &ntx_account)?;
            body.set_item("clOrdId", wire_cl.as_str())?;
            body.set_item("orderId", &ntx_id)?;
            body.set_item("qty", qty_int)?;
            let to = PyDict::new(py);
            to.set_item("type", if is_stop { "stop" } else { "limit" })?;
            to.set_item("price", &new_price)?;
            body.set_item("to", to)?;
            let client = slf.getattr("_client")?;
            let modify = client.getattr("modify")?;
            let sent = slf.call_method1("_send", ("modify", &cl, &ntx_account, &modify, &body))?;
            let ack = sent.get_item(0)?;
            let response = sent.get_item(1)?;
            if response.is_none() || !response.call_method1("get", ("outcome",))?.eq("SENT")? {
                return Ok(ack);
            }
            seq = 1;
            ack
        } else {
            let clock = slf.getattr("_clock")?;
            let now = clock.call_method0("now_utc")?;
            let ack = ctx.venue_ack.bind(py).call1((&cl, "ACCEPTED", &now, py.None()))?;
            log(py, &ctx, "info", &format!(
                "ntx order adopt: venue_order={} account={} status=ACCEPTED",
                str_of(py, &cl)?,
                mask(Some(&str_of(py, &ntx_account)?))
            ))?;
            ack
        };
        parent.set_item(if is_stop { "stopQty" } else { "targetQty" }, qty_int)?;
        parent.set_item(if is_stop { "stopPrice" } else { "targetPrice" }, &new_price)?;
        if !is_stop {
            parent.set_item("targetChild", &cl)?;
        }
        let record = PyDict::new(py);
        record.set_item("account", &ntx_account)?;
        record.set_item("entryId", &ntx_id)?;
        record.set_item("stopId", if is_stop { ntx_id.clone() } else { py.None().into_bound(py) })?;
        record.set_item("qty", qty_int)?;
        record.set_item("seq", seq)?;
        record.set_item("child", if is_stop { "stop" } else { "target" })?;
        record.set_item("side", order.getattr("side")?)?;
        let entry = PyDict::new(py);
        entry.set_item("type", if is_stop { "stop" } else { "limit" })?;
        entry.set_item("price", &new_price)?;
        record.set_item("entry", entry)?;
        slf.call_method1("_remember", (&cl, &record))?;
        Ok(ack)
    }

    fn cancel<'py>(slf: &Bound<'py, Self>, venue_order_id: &Bound<'py, PyAny>) -> PyResult<Value<'py>> {
        let py = slf.py();
        let orders = slf.getattr("_orders")?;
        let known = orders.call_method1("get", (venue_order_id,))?;
        if known.is_none() {
            return slf.call_method1("_refused", (venue_order_id, "cancel", py.None(), "unknown_order"));
        }
        let cl_str = str_of(py, venue_order_id)?;
        let seq: i64 = known.get_item("seq")?.extract()?;
        let wire_cl = match wire_cl_ord_id(&format!("{cl_str}.c{seq}")) {
            Some(w) => w,
            None => return slf.call_method1("_refused", (venue_order_id, "cancel", known.get_item("account")?, "cl_ord_id_invalid")),
        };
        let body = PyDict::new(py);
        body.set_item("account", known.get_item("account")?)?;
        body.set_item("clOrdId", wire_cl.as_str())?;
        body.set_item("orderId", known.get_item("entryId")?)?;
        let client = slf.getattr("_client")?;
        let cancel = client.getattr("cancel")?;
        let sent = slf.call_method1("_send", ("cancel", venue_order_id, known.get_item("account")?, &cancel, &body))?;
        let ack = sent.get_item(0)?;
        let response = sent.get_item(1)?;
        if !response.is_none() {
            known.set_item("seq", seq + 1)?;
            if response.call_method1("get", ("outcome",))?.eq("SENT")? {
                orders.call_method1("pop", (venue_order_id, py.None()))?;
            }
        }
        Ok(ack)
    }

    fn replace<'py>(slf: &Bound<'py, Self>, venue_order_id: &Bound<'py, PyAny>, changes: &Bound<'py, PyAny>) -> PyResult<Value<'py>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let orders = slf.getattr("_orders")?;
        let known = orders.call_method1("get", (venue_order_id,))?;
        if known.is_none() {
            return slf.call_method1("_refused", (venue_order_id, "modify", py.None(), "unknown_order"));
        }
        let account = known.get_item("account")?;
        let live_refused = slf.call_method1("_live_refused", (&account,))?;
        if live_refused.is_truthy()? {
            return slf.call_method1("_refused", (venue_order_id, "modify", &account, ctx.refusal_code.bind(py)));
        }
        let new_limit = changes.getattr("new_limit_price")?;
        let new_stop = changes.getattr("new_stop_price")?;
        if !new_limit.is_none() && !new_stop.is_none() {
            return slf.call_method1("_refused", (venue_order_id, "modify", &account, "one_change_only"));
        }
        let new_qty = changes.getattr("new_quantity")?;
        let qty = if new_qty.is_none() {
            ctx.decimal.bind(py).call1((known.get_item("qty")?,))?
        } else {
            new_qty.clone()
        };
        let integral = qty.call_method0("to_integral_value")?;
        let one = ctx.decimal.bind(py).call1((1,))?;
        let max = ctx.decimal.bind(py).call1((ctx.max_qty,))?;
        if !qty.eq(&integral)? || !(one.le(&qty)? && qty.le(&max)?) {
            return slf.call_method1("_refused", (venue_order_id, "modify", &account, "qty_invalid"));
        }
        if !new_stop.is_none() && known.call_method1("get", ("child",))?.eq("stop")? {
            let held = ctx.decimal.bind(py).call1((known.get_item("entry")?.get_item("price")?,))?;
            let side = known.get_item("side")?;
            let is_buy = side.is(ctx.side_buy.bind(py));
            let loosens = if is_buy { new_stop.gt(&held)? } else { new_stop.lt(&held)? };
            if loosens {
                return slf.call_method1("_refused", (venue_order_id, "modify", &account, "stop_loosen"));
            }
        }
        let (order_id, to) = if !new_stop.is_none() {
            let to = PyDict::new(py);
            to.set_item("type", "stop")?;
            to.set_item("price", price(py, &new_stop)?)?;
            (known.get_item("stopId")?, to.into_any())
        } else if !new_limit.is_none() {
            let to = PyDict::new(py);
            to.set_item("type", "limit")?;
            to.set_item("price", price(py, &new_limit)?)?;
            (known.get_item("entryId")?, to.into_any())
        } else if !new_qty.is_none() {
            (known.get_item("entryId")?, known.get_item("entry")?)
        } else {
            return slf.call_method1("_refused", (venue_order_id, "modify", &account, "no_change"));
        };
        let cl_str = str_of(py, venue_order_id)?;
        let seq: i64 = known.get_item("seq")?.extract()?;
        let wire_cl = match wire_cl_ord_id(&format!("{cl_str}.m{seq}")) {
            Some(w) => w,
            None => return slf.call_method1("_refused", (venue_order_id, "modify", &account, "cl_ord_id_invalid")),
        };
        let body = PyDict::new(py);
        body.set_item("account", &account)?;
        body.set_item("clOrdId", wire_cl.as_str())?;
        body.set_item("orderId", &order_id)?;
        body.set_item("qty", to_int(py, &qty)?)?;
        body.set_item("to", &to)?;
        let verdict = changes.getattr("riskguard_verdict")?;
        if !verdict.is_none() {
            let checked = verdict.getattr("checked_at")?;
            let revision = verdict.getattr("verdict_revision")?;
            if checked.is_none() || revision.is_none() {
                return slf.call_method1("_refused", (venue_order_id, "modify", &account, "verdict_required"));
            }
            body.set_item("verdict", verdict_dict(py, &verdict)?)?;
        }
        let client = slf.getattr("_client")?;
        let modify = client.getattr("modify")?;
        let sent = slf.call_method1("_send", ("modify", venue_order_id, &account, &modify, &body))?;
        let ack = sent.get_item(0)?;
        let response = sent.get_item(1)?;
        if !response.is_none() {
            known.set_item("seq", seq + 1)?;
            if response.call_method1("get", ("outcome",))?.eq("SENT")? {
                known.set_item("qty", to_int(py, &qty)?)?;
                let to_type = to.call_method1("get", ("type",))?;
                if !to_type.eq("stop")? || known.call_method1("get", ("child",))?.eq("stop")? {
                    known.set_item("entry", &to)?;
                }
            }
        }
        Ok(ack)
    }

    // -- order helpers -------------------------------------------------------------

    fn _live_refused<'py>(slf: &Bound<'py, Self>, ntx_account: &Bound<'py, PyAny>) -> PyResult<bool> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let live_ack = slf.getattr("_live_ack")?;
        if live_ack.is_truthy()? || ntx_account.is_none() {
            return Ok(false);
        }
        let key = ctx.accounts_rs.bind(py).call_method1("key", (ctx.broker.as_str(), ntx_account))?;
        let kept = slf.getattr("_kept")?;
        let account = kept.call_method1("get", (&key,))?;
        if account.is_none() {
            return Ok(false);
        }
        let identity_name = account.getattr("identity")?;
        let identities = slf.getattr("_identities")?;
        let identity = identities.call_method1("get", (&identity_name,))?;
        let env = if identity.is_none() {
            account.getattr("record")?.getattr("env")?
        } else {
            identity.getattr("env")?
        };
        Ok(env.eq("live")?)
    }

    fn _ntx_account<'py>(slf: &Bound<'py, Self>, engine_account: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let bindings = slf.getattr("_bindings")?;
        let binding = bindings.call_method1("get", (engine_account,))?;
        if binding.is_none() {
            return Ok(py.None().into_bound(py).unbind());
        }
        let account_key = binding.getattr("account_key")?;
        let name = account_key.call_method1("get", ("brokerAccountId",))?;
        if name.is_instance_of::<PyString>() && name.is_truthy()? {
            Ok(name.unbind())
        } else {
            Ok(py.None().into_bound(py).unbind())
        }
    }

    fn _remember<'py>(slf: &Bound<'py, Self>, venue_order_id: &Bound<'py, PyAny>, record: &Bound<'py, PyAny>) -> PyResult<()> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let orders = slf.getattr("_orders")?;
        orders.set_item(venue_order_id, record)?;
        orders.call_method1("move_to_end", (venue_order_id,))?;
        while orders.len()? > ctx.order_id_cap as usize {
            orders.call_method1("popitem", (false,))?;
        }
        Ok(())
    }

    fn _refused<'py>(
        slf: &Bound<'py, Self>,
        venue_order_id: &Bound<'py, PyAny>,
        op: &Bound<'py, PyAny>,
        account: &Bound<'py, PyAny>,
        code: &Bound<'py, PyAny>,
    ) -> PyResult<Value<'py>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let account_str = if account.is_none() {
            None
        } else {
            Some(str_of(py, account)?)
        };
        log(py, &ctx, "info", &format!(
            "ntx order {} refused locally: venue_order={} account={} code={}",
            str_of(py, op)?,
            str_of(py, venue_order_id)?,
            mask(account_str.as_deref()),
            str_of(py, code)?
        ))?;
        let clock = slf.getattr("_clock")?;
        let now = clock.call_method0("now_utc")?;
        Ok(ctx.venue_ack.bind(py).call1((venue_order_id, "REJECTED", now, code))?)
    }

    fn _send<'py>(
        slf: &Bound<'py, Self>,
        op: &Bound<'py, PyAny>,
        venue_order_id: &Bound<'py, PyAny>,
        account: &Bound<'py, PyAny>,
        call: &Bound<'py, PyAny>,
        body: &Bound<'py, PyAny>,
    ) -> PyResult<Value<'py>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let mut response: Option<Value<'py>> = None;
        let (status, message): (String, Option<String>) = match call.call1((body,)) {
            Ok(resp) => {
                response = Some(resp.clone());
                let outcome: String = resp.get_item("outcome")?.extract()?;
                if outcome == "SENT" {
                    ("ACCEPTED".to_string(), None)
                } else if outcome == "PENDING" {
                    let code = resp.call_method1("get", ("code",))?;
                    if code.is_none() || !code.is_truthy()? {
                        ("PENDING".to_string(), Some("outcome_unknown".to_string()))
                    } else {
                        ("PENDING".to_string(), Some(str_of(py, &code)?))
                    }
                } else {
                    let code = resp.call_method1("get", ("code",))?;
                    let reason = resp.call_method1("get", ("reason",))?;
                    if code.is_truthy()? {
                        ("REJECTED".to_string(), Some(str_of(py, &code)?))
                    } else if reason.is_truthy()? {
                        ("REJECTED".to_string(), Some(str_of(py, &reason)?))
                    } else {
                        ("REJECTED".to_string(), Some("refused".to_string()))
                    }
                }
            }
            Err(e) if e.is_instance(py, ctx.ntx_error.bind(py)) => {
                let kind = kind_of(py, &e)?;
                if kind == "pending" || kind == "invalid" {
                    ("PENDING".to_string(), Some("outcome_unknown".to_string()))
                } else if kind == "invalid_body" {
                    ("REJECTED".to_string(), Some(message_of(py, &e)?))
                } else {
                    ("REJECTED".to_string(), Some(kind))
                }
            }
            Err(e) => return Err(e),
        };
        let message_display = match &message {
            Some(m) => m.clone(),
            None => "None".to_string(),
        };
        log(py, &ctx, "info", &format!(
            "ntx order {}: venue_order={} account={} status={} code={}",
            str_of(py, op)?,
            str_of(py, venue_order_id)?,
            mask(Some(&str_of(py, account)?)),
            status,
            message_display
        ))?;
        let clock = slf.getattr("_clock")?;
        let now = clock.call_method0("now_utc")?;
        let msg_py = match message {
            Some(m) => m.into_pyobject(py)?.into_any(),
            None => py.None().into_bound(py),
        };
        let ack = ctx.venue_ack.bind(py).call1((venue_order_id, status, now, msg_py))?;
        let resp_py = match response {
            Some(r) => r,
            None => py.None().into_bound(py),
        };
        Ok(PyTuple::new(py, [ack, resp_py])?.into_any())
    }
}

// --- AccountWatcher --------------------------------------------------------------------

/// `AccountWatcher`'s body, as a `#[pyclass(subclass)]`. State is Python `__dict__`
/// (`_adapter`, `_client`, `_clock`, `_sleep`, `_read_fn`, `limiter`, `backoff`, `snapshots`,
/// `connects`, `_lock`, `_stream`, `_stopping`), set by `AccountWatcher.__init__`.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct AccountWatcherBase;

#[pymethods]
impl AccountWatcherBase {
    #[new]
    #[pyo3(signature = (adapter, client, *, clock, sleep, rand, limiter=None, backoff=None, read=None))]
    fn new(
        adapter: &Bound<'_, PyAny>,
        client: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        sleep: &Bound<'_, PyAny>,
        rand: &Bound<'_, PyAny>,
        limiter: Option<&Bound<'_, PyAny>>,
        backoff: Option<&Bound<'_, PyAny>>,
        read: Option<&Bound<'_, PyAny>>,
    ) -> Self {
        let _ = (adapter, client, clock, sleep, rand, limiter, backoff, read);
        AccountWatcherBase
    }

    fn wake<'py>(slf: &Bound<'py, Self>) -> PyResult<()> {
        let lock = slf.getattr("_lock")?;
        lock.call_method0("acquire")?;
        let result = (|| -> PyResult<Value<'py>> {
            slf.setattr("_stopping", true)?;
            slf.getattr("_stream")
        })();
        lock.call_method0("release")?;
        let stream = result?;
        if !stream.is_none() {
            stream.call_method0("wake")?;
        }
        Ok(())
    }

    #[staticmethod]
    fn _is_hint(payload: &Bound<'_, PyAny>) -> PyResult<bool> {
        is_hint(payload.py(), payload)
    }

    fn _read<'py>(slf: &Bound<'py, Self>) -> PyResult<()> {
        let snapshots: i64 = slf.getattr("snapshots")?.extract()?;
        slf.setattr("snapshots", snapshots + 1)?;
        let read_fn = slf.getattr("_read_fn")?;
        if read_fn.is_none() {
            slf.getattr("_adapter")?.getattr("poller")?.call_method0("poll")?;
        } else {
            read_fn.call0()?;
        }
        Ok(())
    }

    fn _reconcile<'py>(slf: &Bound<'py, Self>) -> PyResult<()> {
        let py = slf.py();
        let adapter = slf.getattr("_adapter")?;
        let ledger = adapter.getattr("_ledger").or_else(|e| {
            if e.is_instance_of::<PyAttributeError>(py) {
                Ok(py.None().into_bound(py))
            } else {
                Err(e)
            }
        })?;
        if ledger.is_none() {
            return Ok(());
        }
        let read_fn = slf.getattr("_read_fn")?;
        if read_fn.is_none() {
            let payload = adapter.call_method0("take_payload")?;
            if payload.is_none() {
                return Ok(());
            }
            let kwargs = PyDict::new(py);
            kwargs.set_item("payload", &payload)?;
            adapter.call_method("reconcile", (), Some(&kwargs))?;
        } else {
            adapter.call_method0("reconcile")?;
        }
        Ok(())
    }

    fn run_once<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let client = slf.getattr("_client")?;
        let stream = match client.call_method0("open_events") {
            Ok(s) => s,
            Err(e) if e.is_instance(py, ctx.ntx_error.bind(py)) => {
                let now = slf.getattr("_clock")?.call_method0("now_utc")?;
                slf.getattr("backoff")?.call_method1("dropped", (&now,))?;
                return Ok(kind_of(py, &e)?.into_pyobject(py)?.into_any().unbind());
            }
            Err(e) => return Err(e),
        };
        // with self._lock: if self._stopping -> close + "stopped"; else _stream = stream
        {
            let lock = slf.getattr("_lock")?;
            lock.call_method0("acquire")?;
            let check = (|| -> PyResult<Option<String>> {
                if slf.getattr("_stopping")?.is_truthy()? {
                    stream.call_method0("close")?;
                    Ok(Some("stopped".to_string()))
                } else {
                    slf.setattr("_stream", &stream)?;
                    Ok(None)
                }
            })();
            lock.call_method0("release")?;
            if let Some(msg) = check? {
                return Ok(msg.into_pyobject(py)?.into_any().unbind());
            }
        }
        let connects: i64 = slf.getattr("connects")?.extract()?;
        slf.setattr("connects", connects + 1)?;
        let outcome = (|| -> PyResult<String> {
            let now = slf.getattr("_clock")?.call_method0("now_utc")?;
            slf.getattr("backoff")?.call_method1("connected", (&now,))?;
            let adapter = slf.getattr("_adapter")?;
            let ledger = adapter.getattr("_ledger").or_else(|e| {
                if e.is_instance_of::<PyAttributeError>(py) {
                    Ok(py.None().into_bound(py))
                } else {
                    Err(e)
                }
            })?;
            if !ledger.is_none() {
                adapter.call_method0("take_payload")?;
            }
            slf.call_method0("_read")?;
            slf.call_method0("_reconcile")?;
            let limiter = slf.getattr("limiter")?;
            let now = slf.getattr("_clock")?.call_method0("now_utc")?;
            limiter.call_method1("note_read", (&now,))?;
            let events = stream.call_method0("events")?;
            let inner = (|| -> PyResult<Option<String>> {
                for payload in events.try_iter()? {
                    let payload = payload?;
                    let now = slf.getattr("_clock")?.call_method0("now_utc")?;
                    if !payload.is_none() && slf.call_method1("_is_hint", (&payload,))?.is_truthy()? {
                        if limiter.call_method1("trigger", (&now,))?.is_truthy()? {
                            slf.call_method0("_read")?;
                        }
                    } else if limiter.call_method1("due", (&now,))?.is_truthy()? {
                        slf.call_method0("_read")?;
                    }
                }
                Ok(None)
            })();
            match inner {
                Ok(_) => Ok("stream_ended".to_string()),
                Err(e) if e.is_instance(py, ctx.ntx_error.bind(py)) => Ok(kind_of(py, &e)?),
                Err(e) => Err(e),
            }
        })();
        // finally
        {
            let lock = slf.getattr("_lock")?;
            lock.call_method0("acquire")?;
            let r = slf.setattr("_stream", py.None());
            lock.call_method0("release")?;
            r?;
        }
        stream.call_method0("close")?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        slf.getattr("backoff")?.call_method1("dropped", (&now,))?;
        let result = outcome?;
        Ok(result.into_pyobject(py)?.into_any().unbind())
    }

    fn run<'py>(slf: &Bound<'py, Self>, stop: &Bound<'py, PyAny>) -> PyResult<()> {
        loop {
            if stop.call0()?.is_truthy()? {
                break;
            }
            slf.call_method0("run_once")?;
            if stop.call0()?.is_truthy()? {
                break;
            }
            let delay = slf.getattr("backoff")?.call_method0("delay")?;
            slf.getattr("_sleep")?.call1((delay,))?;
            let client = slf.getattr("_client")?;
            let stats = client.getattr("stats")?;
            let reconnects: i64 = stats.getattr("reconnects")?.extract()?;
            stats.setattr("reconnects", reconnects + 1)?;
        }
        Ok(())
    }
}

// --- shared helpers --------------------------------------------------------------------

/// The risky half of `_figures` per record: `realized_pnl`, the `as_of` parse and the
/// freshness stamp, as owned `Py<PyAny>` so the caller can catch `AccountRuleError`.
fn compute_figure<'py>(
    slf: &Bound<'py, TradovateAdapterBase>,
    py: Python<'py>,
    ctx: &Ctx,
    kept: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
    day: &Bound<'py, PyAny>,
    confirmed: &Bound<'py, PyAny>,
) -> PyResult<Option<(Py<PyAny>, Py<PyAny>)>> {
    let value = slf.call_method1("realized_pnl", (key, day))?;
    if value.is_none() {
        return Ok(None);
    }
    let kept_account = kept.get_item(key)?;
    let raw = kept_account.getattr("raw")?;
    let daemon_as_of = aware(py, ctx, &raw.call_method1("get", ("as_of_utc",))?, "account.as_of_utc")?;
    let ws = raw.call_method1("get", ("ws_connected",))?;
    // `ws_connected is True`
    let connected: bool = ws.is_instance_of::<PyBool>() && ws.is_truthy()?;
    let as_of = slf.call_method1("_figure_as_of", (daemon_as_of.bind(py), confirmed, connected))?;
    Ok(Some((value.unbind(), as_of.unbind())))
}

fn inc_stat(slf: &Bound<'_, TradovateAdapterBase>, field: &str) -> PyResult<()> {
    let stats = slf.getattr("stats")?;
    let cur: i64 = stats.getattr(field)?.extract()?;
    stats.setattr(field, cur + 1)?;
    Ok(())
}

fn fallback<'py>(
    slf: &Bound<'py, TradovateAdapterBase>,
    _py: Python<'py>,
    e: PyErr,
) -> PyResult<Py<PyAny>> {
    let last = slf.getattr("_last_accounts")?;
    if last.is_none() {
        Err(e)
    } else {
        Ok(last.unbind())
    }
}

fn to_int(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<i64> {
    py.import("builtins")?.getattr("int")?.call1((value,))?.extract()
}

fn in_adopted(py: Python<'_>, ctx: &Ctx, state: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(ctx.adopted.bind(py).call_method1("__contains__", (state,))?.is_truthy()?)
}

/// `_bracket_index` stays importable from `trade_engine.tradovate.adapter` (the test
/// `tests/test_oa5_e2e.py` imports it directly).
#[pyfunction]
pub(crate) fn _bracket_index(row: &Bound<'_, PyAny>) -> PyResult<(Option<Py<PyAny>>, bool)> {
    bracket_index(row.py(), row)
}
