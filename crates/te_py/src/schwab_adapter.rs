//! W7-CORE-C18: the Schwab accounts and order adapters' method bodies, moved into Rust.
//!
//! Two `#[pyclass(subclass)]` bases own the sequencing the Python classes did:
//! `SchwabAccountsAdapterBase` (`src/trade_engine/schwab/adapter.py`) maps the hub's two
//! response texts onto the account carriers and feeds the phase-2 `PollAndDiff`; and
//! `SchwabOrderAdapterBase` (`src/trade_engine/schwab/order_adapter.py`) submits, cancels,
//! replaces and resolves orders through the hub's `HubOrdersClient`.
//!
//! Every collaborator — the config dataclasses, the carriers (`AccountRecord`,
//! `AccountSnapshot`, `VenueOrder`, `VenueAck`), the `_Record` order row, the hub clients,
//! the ledger, the clock, the position function, the precheck, the journal and the logger —
//! stays a Python object reached through attribute lookup. The heavy pure helpers
//! (`map_responses`, `build_order_json`, `leg_instrument`, `match_pending`, and the
//! `_Record` helpers `_match_terms` / `_settled` / `_row` / `_replacing_id` / `_mask` /
//! `_message`) stay Python and are imported once. No te_core rule is reached here.
//!
//! The Python modules keep their import path, public names, docstrings and class
//! attributes; the classes become `class X(rs.XBase)` and their `__init__` still sets every
//! instance attribute (`_config`, `_clock`, `_orders`, `_live_ack`, ...) on the `__dict__`,
//! so the existing tests that subclass or monkeypatch instance state keep working. The
//! `_place` / `_replace` callbacks the precheck receives stay Python-accessible methods.

use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

/// The Python objects and constants the two bases reach, imported once.
struct Ctx {
    account_rule_error: Py<PyAny>,
    unsupported: Py<PyAny>,
    venue_ack: Py<PyAny>,
    decimal: Py<PyAny>,
    dataclasses_replace: Py<PyAny>,
    // adapter.py
    map_responses: Py<PyAny>,
    activity_repoll_seconds: i64,
    adapter_logger: Py<PyAny>,
    // orders.py (pure builders, C19 keeps them)
    build_order_json: Py<PyAny>,
    leg_instrument: Py<PyAny>,
    match_pending: Py<PyAny>,
    // order_adapter.py module-level helpers and constants
    record: Py<PyAny>,
    match_terms: Py<PyAny>,
    settled: Py<PyAny>,
    row: Py<PyAny>,
    replacing_id: Py<PyAny>,
    mask: Py<PyAny>,
    message: Py<PyAny>,
    replaced_old: Py<PyAny>,
    live_old: Py<PyAny>,
    order_id_cap: i64,
    order_logger: Py<PyAny>,
    // live ack / precheck constants
    open_suffix: String,
    refusal_code: String,
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let accounts_errors = py.import("trade_engine.accounts.errors")?;
        let broker = py.import("trade_engine.interfaces.broker")?;
        let adapter = py.import("trade_engine.schwab.adapter")?;
        let order_adapter = py.import("trade_engine.schwab.order_adapter")?;
        let orders = py.import("trade_engine.schwab.orders")?;
        let precheck = py.import("trade_engine.oms.precheck")?;
        let live_ack = py.import("trade_engine.live_ack")?;
        let decimal = py.import("decimal")?;
        let dataclasses = py.import("dataclasses")?;
        let logging = py.import("logging")?;
        Ok(Ctx {
            account_rule_error: accounts_errors.getattr("AccountRuleError")?.unbind(),
            unsupported: broker.getattr("UnsupportedCapability")?.unbind(),
            venue_ack: broker.getattr("VenueAck")?.unbind(),
            decimal: decimal.getattr("Decimal")?.unbind(),
            dataclasses_replace: dataclasses.getattr("replace")?.unbind(),
            map_responses: adapter.getattr("map_responses")?.unbind(),
            activity_repoll_seconds: adapter.getattr("ACTIVITY_REPOLL_SECONDS")?.extract()?,
            adapter_logger: logging
                .call_method1("getLogger", ("trade_engine.schwab.adapter",))?
                .unbind(),
            build_order_json: orders.getattr("build_order_json")?.unbind(),
            leg_instrument: orders.getattr("leg_instrument")?.unbind(),
            match_pending: orders.getattr("match_pending")?.unbind(),
            record: order_adapter.getattr("_Record")?.unbind(),
            match_terms: order_adapter.getattr("_match_terms")?.unbind(),
            settled: order_adapter.getattr("_settled")?.unbind(),
            row: order_adapter.getattr("_row")?.unbind(),
            replacing_id: order_adapter.getattr("_replacing_id")?.unbind(),
            mask: order_adapter.getattr("_mask")?.unbind(),
            message: order_adapter.getattr("_message")?.unbind(),
            replaced_old: order_adapter.getattr("_REPLACED_OLD")?.unbind(),
            live_old: order_adapter.getattr("_LIVE_OLD")?.unbind(),
            order_id_cap: order_adapter.getattr("ORDER_ID_CAP")?.extract()?,
            order_logger: logging
                .call_method1("getLogger", ("trade_engine.schwab.order_adapter",))?
                .unbind(),
            open_suffix: precheck.getattr("OPEN_SUFFIX")?.extract()?,
            refusal_code: live_ack.getattr("REFUSAL_CODE")?.extract()?,
        })
    }
}

fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

/// `str(value)` as a `Py<PyAny>`.
fn py_str(py: Python<'_>, value: &str) -> PyResult<Py<PyAny>> {
    Ok(value.into_pyobject(py)?.into_any().unbind())
}

/// A Python `int` from an `i64`.
fn py_int(py: Python<'_>, value: i64) -> PyResult<Py<PyAny>> {
    Ok(value.into_pyobject(py)?.into_any().unbind())
}

/// `datetime.timedelta(seconds=n)`.
fn timedelta_seconds(py: Python<'_>, seconds: i64) -> PyResult<Bound<'_, PyAny>> {
    py.import("datetime")?.getattr("timedelta")?.call1((0, seconds))
}

/// `logger.<level>(fmt, *args)` with the same lazy `%`-style formatting the tests read.
fn log(
    py: Python<'_>,
    logger: &Py<PyAny>,
    level: &str,
    fmt: &str,
    args: Vec<Py<PyAny>>,
) -> PyResult<()> {
    let mut items: Vec<Py<PyAny>> = Vec::with_capacity(args.len() + 1);
    items.push(py_str(py, fmt)?);
    items.extend(args);
    let tuple = PyTuple::new(py, items)?;
    logger.bind(py).call_method1(level, tuple)?;
    Ok(())
}

fn warn(py: Python<'_>, logger: &Py<PyAny>, fmt: &str, args: Vec<Py<PyAny>>) -> PyResult<()> {
    log(py, logger, "warning", fmt, args)
}

fn info(py: Python<'_>, logger: &Py<PyAny>, fmt: &str, args: Vec<Py<PyAny>>) -> PyResult<()> {
    log(py, logger, "info", fmt, args)
}

/// `raise AccountRuleError(kind, message)`.
fn account_rule_error(py: Python<'_>, kind: &str, message: &str) -> PyErr {
    let ctx = match ctx(py) {
        Ok(c) => c,
        Err(e) => return e,
    };
    match ctx.account_rule_error.bind(py).call1((kind, message)) {
        Ok(value) => PyErr::from_value(value),
        Err(e) => e,
    }
}

/// `raise UnsupportedCapability(message)`.
fn unsupported_capability(py: Python<'_>, message: &str) -> PyErr {
    let ctx = match ctx(py) {
        Ok(c) => c,
        Err(e) => return e,
    };
    match ctx.unsupported.bind(py).call1((message,)) {
        Ok(value) => PyErr::from_value(value),
        Err(e) => e,
    }
}

/// `VenueAck(venue_order_id, status, timestamp, message)`.
fn venue_ack(
    py: Python<'_>,
    venue_order_id: &str,
    status: &str,
    timestamp: &Bound<'_, PyAny>,
    message: Py<PyAny>,
) -> PyResult<Py<PyAny>> {
    let ctx = ctx(py)?;
    Ok(ctx
        .venue_ack
        .bind(py)
        .call1((venue_order_id, status, timestamp, message))?
        .unbind())
}

// -----------------------------------------------------------------------------------------
// SchwabAccountsAdapterBase
// -----------------------------------------------------------------------------------------

/// `SchwabAccountsAdapter`'s method bodies. Every piece of state (`_config`, `_clock`,
/// `_ledger`, `_on_poll`, `stats`, `_pending`, `_last`, `_last_attempt_at`, `_last_hint_at`,
/// `_poller`, `env`, `stale_after`, `interval_seconds`, `capabilities`) stays a Python
/// instance attribute, set by the Python `__init__`; the `enabled` / `last_good` / `poller`
/// properties stay Python and are reached through attribute lookup.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct SchwabAccountsAdapterBase;

impl SchwabAccountsAdapterBase {
    /// The `_on()` gate: an off adapter refuses every feed / accounts / poll.
    fn require_on(slf: &Bound<'_, Self>) -> PyResult<()> {
        let enabled: bool = slf.getattr("enabled")?.extract()?;
        if !enabled {
            return Err(account_rule_error(
                slf.py(),
                "switch_off",
                "the schwab accounts adapter is off; set TE_SCHWAB_ACCOUNTS=on to enable it",
            ));
        }
        Ok(())
    }
}

#[pymethods]
impl SchwabAccountsAdapterBase {
    #[new]
    #[pyo3(signature = (*args, **kwargs))]
    fn new(args: &Bound<'_, PyAny>, kwargs: Option<&Bound<'_, PyAny>>) -> Self {
        let _ = (args, kwargs);
        SchwabAccountsAdapterBase
    }

    fn _on(slf: &Bound<'_, Self>) -> PyResult<()> {
        Self::require_on(slf)
    }

    fn feed(
        slf: &Bound<'_, Self>,
        account_numbers_json: &Bound<'_, PyAny>,
        accounts_json: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        Self::require_on(slf)?;
        for (text, what) in [
            (account_numbers_json, "accountNumbers"),
            (accounts_json, "accounts"),
        ] {
            if !text.is_instance_of::<PyString>() {
                return Err(account_rule_error(
                    py,
                    "bad_shape",
                    &format!("{what} is not response text"),
                ));
            }
        }
        if !slf.getattr("_pending")?.is_none() {
            let stats = slf.getattr("stats")?;
            let superseded: i64 = stats.getattr("feed_superseded")?.extract()?;
            stats.setattr("feed_superseded", superseded + 1)?;
        }
        let pair = PyTuple::new(py, [account_numbers_json.clone(), accounts_json.clone()])?;
        slf.setattr("_pending", pair)?;
        Ok(())
    }

    fn next_poll_due(slf: &Bound<'_, Self>, now: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = slf.py();
        let enabled: bool = slf.getattr("enabled")?.extract()?;
        if !enabled {
            return Ok(false);
        }
        let last_good = slf.getattr("_last")?;
        if last_good.is_none() {
            return Ok(true);
        }
        let mut last = last_good.getattr("confirmed_at")?;
        let attempt = slf.getattr("_last_attempt_at")?;
        if !attempt.is_none() {
            let gt: bool = attempt.call_method1("__gt__", (&last,))?.extract()?;
            if gt {
                last = attempt;
            }
        }
        let interval: i64 = slf.getattr("interval_seconds")?.extract()?;
        let td = timedelta_seconds(py, interval)?;
        let since = now.call_method1("__sub__", (&last,))?;
        since.call_method1("__ge__", (&td,))?.extract()
    }

    #[pyo3(signature = (now, message=None))]
    fn on_activity(
        slf: &Bound<'_, Self>,
        now: &Bound<'_, PyAny>,
        message: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let enabled: bool = slf.getattr("enabled")?.extract()?;
        if !enabled {
            return Ok(false);
        }
        if let Some(message) = message {
            if !message.is_none() {
                let stats = slf.getattr("stats")?;
                let ignored: i64 = stats.getattr("activity_bodies_ignored")?.extract()?;
                stats.setattr("activity_bodies_ignored", ignored + 1)?;
            }
        }
        let last_hint = slf.getattr("_last_hint_at")?;
        if !last_hint.is_none() {
            let since = now.call_method1("__sub__", (&last_hint,))?;
            let zero = timedelta_seconds(py, 0)?;
            let ge: bool = since.call_method1("__ge__", (&zero,))?.extract()?;
            let floor = timedelta_seconds(py, ctx.activity_repoll_seconds)?;
            let lt: bool = since.call_method1("__lt__", (&floor,))?.extract()?;
            if ge && lt {
                let stats = slf.getattr("stats")?;
                let suppressed: i64 = stats.getattr("hints_suppressed")?.extract()?;
                stats.setattr("hints_suppressed", suppressed + 1)?;
                return Ok(false);
            }
        }
        slf.setattr("_last_hint_at", now)?;
        let stats = slf.getattr("stats")?;
        let hints: i64 = stats.getattr("hints")?.extract()?;
        stats.setattr("hints", hints + 1)?;
        Ok(true)
    }

    fn accounts(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        Self::require_on(slf)?;
        let pending = slf.getattr("_pending")?;
        if pending.is_none() {
            return Err(account_rule_error(
                py,
                "no_response",
                "no response text was fed; the hub feeds each poll (D3)",
            ));
        }
        slf.setattr("_pending", py.None())?;
        let pair0 = pending.get_item(0)?;
        let pair1 = pending.get_item(1)?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        slf.setattr("_last_attempt_at", &now)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("env", slf.getattr("env")?)?;
        let mapped = match ctx
            .map_responses
            .bind(py)
            .call((&pair0, &pair1, &now), Some(&kwargs))
        {
            Ok(mapped) => mapped,
            Err(e) if e.is_instance(py, ctx.account_rule_error.bind(py)) => {
                let stats = slf.getattr("stats")?;
                let refused: i64 = stats.getattr("polls_refused")?.extract()?;
                stats.setattr("polls_refused", refused + 1)?;
                let kind: String = e.value(py).getattr("kind")?.extract()?;
                warn(
                    py,
                    &ctx.adapter_logger,
                    "schwab accounts snapshot refused (%s)",
                    vec![py_str(py, &kind)?],
                )?;
                return Err(e);
            }
            Err(e) => return Err(e),
        };
        let stats = slf.getattr("stats")?;
        let polls: i64 = stats.getattr("polls")?.extract()?;
        stats.setattr("polls", polls + 1)?;
        let types_unmapped: i64 = mapped.getattr("types_unmapped")?.extract()?;
        let details_unmatched: i64 = mapped.getattr("details_unmatched")?.extract()?;
        let cur_tu: i64 = stats.getattr("types_unmapped")?.extract()?;
        let cur_du: i64 = stats.getattr("details_unmatched")?.extract()?;
        stats.setattr("types_unmapped", cur_tu + types_unmapped)?;
        stats.setattr("details_unmatched", cur_du + details_unmatched)?;
        let snapshot = mapped.getattr("snapshot")?;
        slf.setattr("_last", &snapshot)?;
        Ok(snapshot.unbind())
    }

    fn poll(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        Self::require_on(slf)?;
        Ok(slf.getattr("poller")?.call_method0("poll")?.unbind())
    }

    fn account_events(
        slf: &Bound<'_, Self>,
        since: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Self::require_on(slf)?;
        Ok(slf
            .getattr("poller")?
            .call_method1("account_events", (since,))?
            .unbind())
    }

    fn is_stale(slf: &Bound<'_, Self>, now: &Bound<'_, PyAny>) -> PyResult<bool> {
        let last = slf.getattr("_last")?;
        if last.is_none() {
            return Ok(true);
        }
        let stale_after: i64 = slf.getattr("stale_after")?.extract()?;
        last.call_method1("is_stale", (now, stale_after))?.extract()
    }
}

// -----------------------------------------------------------------------------------------
// SchwabOrderAdapterBase
// -----------------------------------------------------------------------------------------

/// `SchwabOrderAdapter`'s method bodies. All state (`_client`, `_hashes`, `_clock`,
/// `_position`, `_precheck`, `_window`, `_env`, `_live_ack`, `_journal`, `_orders`) is
/// Python instance state set by `__init__`; the `_Record` rows are the Python dataclass in
/// `order_adapter.py`, read and written through attribute lookup.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct SchwabOrderAdapterBase;

impl SchwabOrderAdapterBase {
    /// `_live_refused`: a live adapter without the acknowledgement refuses place and modify.
    fn live_refused(slf: &Bound<'_, Self>) -> PyResult<bool> {
        let env: String = slf.getattr("_env")?.extract()?;
        let live_ack: bool = slf.getattr("_live_ack")?.extract()?;
        Ok(env == "live" && !live_ack)
    }

    /// `_mask(account)` through the module-level helper.
    fn mask_of(py: Python<'_>, account: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let ctx = ctx(py)?;
        Ok(ctx.mask.bind(py).call1((account,))?.unbind())
    }

    /// `_refused`: the local-refusal log line and the `REJECTED` ack.
    fn refused(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        op: &str,
        account: Option<&Bound<'_, PyAny>>,
        code: &str,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let masked = match account {
            Some(account) => Self::mask_of(py, account)?,
            None => Self::mask_of(py, py.None().bind(py))?,
        };
        info(
            py,
            &ctx.order_logger,
            "schwab order %s refused locally: venue_order=%s account=%s code=%s",
            vec![
                py_str(py, op)?,
                py_str(py, venue_order_id)?,
                masked,
                py_str(py, code)?,
            ],
        )?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        venue_ack(py, venue_order_id, "REJECTED", &now, py_str(py, code)?)
    }

    /// `_ack`: the record's status with the optional result's message.
    fn ack(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        record: &Bound<'_, PyAny>,
        result: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let message = match result {
            Some(result) => ctx.message.bind(py).call1((result,))?.unbind(),
            None => py.None(),
        };
        let status = record.getattr("status")?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        venue_ack(py, venue_order_id, &status.str()?.extract::<String>()?, &now, message)
    }

    /// `_result_ack`: the result outcome mapped straight to a status.
    fn result_ack(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        result: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let outcome: String = result.getattr("outcome")?.extract()?;
        let status = match outcome.as_str() {
            "ok" => "ACCEPTED",
            "rejected" | "not_sent" => "REJECTED",
            _ => "PENDING",
        };
        let message = ctx.message.bind(py).call1((result,))?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        venue_ack(py, venue_order_id, status, &now, message.unbind())
    }

    /// `_remember`: insert, then evict settled records until under the cap.
    fn remember(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        record: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let orders = slf.getattr("_orders")?;
        orders.call_method1("__setitem__", (venue_order_id, record))?;
        loop {
            let len: usize = orders.call_method0("__len__")?.extract()?;
            if len <= ctx.order_id_cap as usize {
                break;
            }
            let mut evict: Option<String> = None;
            for item in orders.call_method0("items")?.try_iter()? {
                let (vid, r): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
                let settled: bool = ctx.settled.bind(py).call1((&r,))?.extract()?;
                if settled {
                    evict = Some(vid.extract()?);
                    break;
                }
            }
            match evict {
                Some(vid) => {
                    orders.call_method1("__delitem__", (vid.as_str(),))?;
                }
                None => {
                    warn(
                        py,
                        &ctx.order_logger,
                        "schwab orders: %d unsettled orders over cap %d; none evicted",
                        vec![
                            py_int(py, len as i64)?,
                            py_int(py, ctx.order_id_cap)?,
                        ],
                    )?;
                    return Ok(());
                }
            }
        }
        Ok(())
    }

    /// `_save`: journal the row under the command id.
    fn save(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        record: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        let row = ctx.row.bind(py).call1((record, &now))?;
        slf.getattr("_journal")?
            .call_method1("save", (venue_order_id, row))?;
        Ok(())
    }

    /// `_settle`: fold the hub result into the record and log the outcome.
    fn settle(
        slf: &Bound<'_, Self>,
        record: &Bound<'_, PyAny>,
        result: &Bound<'_, PyAny>,
        op: &str,
    ) -> PyResult<()> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let outcome: String = result.getattr("outcome")?.extract()?;
        if outcome == "ok" {
            let order_id = result.getattr("order_id")?;
            if !order_id.is_none() {
                record.setattr("status", "ACCEPTED")?;
                record.setattr("schwab_id", &order_id)?;
            } else {
                record.setattr("status", "PENDING")?;
                record.setattr("schwab_id", py.None())?;
            }
        } else if outcome == "rejected" || outcome == "not_sent" {
            record.setattr("status", "REJECTED")?;
        } else {
            record.setattr("status", "PENDING")?;
        }
        let account_id = record.getattr("account_id")?;
        let masked = Self::mask_of(py, &account_id)?;
        let http: i64 = result.getattr("http")?.extract()?;
        let status = record.getattr("status")?;
        info(
            py,
            &ctx.order_logger,
            "schwab order %s account=%s outcome=%s http=%s status=%s",
            vec![
                py_str(py, op)?,
                masked,
                py_str(py, &outcome)?,
                py_int(py, http)?,
                py_str(py, &status.str()?.extract::<String>()?)?,
            ],
        )?;
        Ok(())
    }

    /// `_place`: build the body, record, send, settle and ack.
    fn place(slf: &Bound<'_, Self>, order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let allocations = order.getattr("allocations")?;
        let account_id = allocations.get_item(0)?.getattr("account_id")?;
        let account_id_str: String = account_id.extract()?;
        let hashes = slf.getattr("_hashes")?;
        let account_hash = hashes.call_method1("get", (&account_id_str,))?;
        let venue_order_id: String = order.getattr("venue_order_id")?.extract()?;
        if account_hash.is_none() {
            return Self::refused(
                slf,
                &venue_order_id,
                "place",
                Some(&account_id),
                "unmapped_account",
            );
        }
        let opening: bool = order
            .getattr("venue_order_id")?
            .call_method1("endswith", (ctx.open_suffix.as_str(),))?
            .extract()?;
        let position = if opening {
            ctx.decimal.bind(py).call1(("0",))?
        } else {
            let instrument = order.getattr("instrument")?;
            slf.getattr("_position")?
                .call1((&account_id, &instrument))?
        };
        let body = ctx
            .build_order_json
            .bind(py)
            .call1((order, &position))?;
        let instruction = body
            .get_item("orderLegCollection")?
            .get_item(0)?
            .get_item("instruction")?;
        let sent_at = slf.getattr("_clock")?.call_method0("now_utc")?;
        let symbol = ctx
            .leg_instrument
            .bind(py)
            .call1((order,))?
            .get_item("symbol")?;
        let quantity = order.getattr("quantity")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("order", order)?;
        kwargs.set_item("account_id", &account_id)?;
        kwargs.set_item("account_hash", &account_hash)?;
        kwargs.set_item("instruction", &instruction)?;
        kwargs.set_item("sent_at", &sent_at)?;
        kwargs.set_item("status", "PENDING")?;
        kwargs.set_item("symbol", &symbol)?;
        kwargs.set_item("quantity", &quantity)?;
        kwargs.set_item("body", &body)?;
        let record = ctx.record.bind(py).call((), Some(&kwargs))?;
        Self::remember(slf, &venue_order_id, &record)?;
        Self::save(slf, &venue_order_id, &record)?;
        let client = slf.getattr("_client")?;
        let result = client.call_method1("place", (&account_hash, &body))?;
        Self::settle(slf, &record, &result, "place")?;
        Self::save(slf, &venue_order_id, &record)?;
        Self::ack(slf, &venue_order_id, &record, Some(&result))
    }

    /// `_replace`: rebuild the order from the changes and replace at the hub.
    fn replace_impl(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        changes: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let orders = slf.getattr("_orders")?;
        let record = orders.call_method1("get", (venue_order_id,))?;
        if record.is_none() {
            return Self::refused(slf, venue_order_id, "replace", None, "unknown_order");
        }
        if !record.getattr("replace_body")?.is_none() {
            let account_id = record.getattr("account_id")?;
            return Self::refused(
                slf,
                venue_order_id,
                "replace",
                Some(&account_id),
                "unresolved_replace",
            );
        }
        if record.getattr("schwab_id")?.is_none() {
            let account_id = record.getattr("account_id")?;
            return Self::refused(
                slf,
                venue_order_id,
                "replace",
                Some(&account_id),
                "unresolved_order",
            );
        }
        if record.getattr("order")?.is_none() {
            let account_id = record.getattr("account_id")?;
            return Self::refused(
                slf,
                venue_order_id,
                "replace",
                Some(&account_id),
                "restored_order",
            );
        }
        let updates = PyDict::new(py);
        let new_quantity = changes.getattr("new_quantity")?;
        if !new_quantity.is_none() {
            let account = record.getattr("order")?.getattr("allocations")?.get_item(0)?;
            updates.set_item("quantity", &new_quantity)?;
            let alloc_kwargs = PyDict::new(py);
            alloc_kwargs.set_item("quantity", &new_quantity)?;
            let new_account = ctx
                .dataclasses_replace
                .bind(py)
                .call((&account,), Some(&alloc_kwargs))?;
            let allocs = PyTuple::new(py, [new_account])?;
            updates.set_item("allocations", allocs)?;
        }
        let new_limit_price = changes.getattr("new_limit_price")?;
        if !new_limit_price.is_none() {
            updates.set_item("limit_price", &new_limit_price)?;
        }
        let new_stop_price = changes.getattr("new_stop_price")?;
        if !new_stop_price.is_none() {
            updates.set_item("stop_price", &new_stop_price)?;
        }
        let order = record.getattr("order")?;
        let new_order = ctx
            .dataclasses_replace
            .bind(py)
            .call((&order,), Some(&updates))?;
        let body_kwargs = PyDict::new(py);
        body_kwargs.set_item("instruction", record.getattr("instruction")?)?;
        let body = ctx
            .build_order_json
            .bind(py)
            .call((&new_order,), Some(&body_kwargs))?;
        let sent = slf.getattr("_clock")?.call_method0("now_utc")?;
        let client = slf.getattr("_client")?;
        let account_hash = record.getattr("account_hash")?;
        let schwab_id = record.getattr("schwab_id")?;
        let result = client.call_method1("replace", (&account_hash, &schwab_id, &body))?;
        let outcome: String = result.getattr("outcome")?.extract()?;
        if outcome == "ok" {
            record.setattr("order", &new_order)?;
            record.setattr("body", &body)?;
            record.setattr("quantity", new_order.getattr("quantity")?)?;
            let order_id = result.getattr("order_id")?;
            if !order_id.is_none() {
                let cur = record.getattr("schwab_id")?;
                let differs: bool = order_id.call_method1("__ne__", (&cur,))?.extract()?;
                if differs {
                    record.setattr("schwab_id", &order_id)?;
                }
            }
            Self::save(slf, venue_order_id, &record)?;
        } else if outcome == "unknown" {
            record.setattr("replace_pending", &new_order)?;
            record.setattr("replace_body", &body)?;
            record.setattr("replace_sent_at", &sent)?;
            Self::save(slf, venue_order_id, &record)?;
            warn(
                py,
                &ctx.order_logger,
                "schwab replace %s: outcome unknown; old order kept, resolve_pending settles it",
                vec![py_str(py, venue_order_id)?],
            )?;
        }
        Self::result_ack(slf, venue_order_id, &result)
    }

    /// `_resolve_place`: settle a pending place from one list read.
    fn resolve_place(
        slf: &Bound<'_, Self>,
        vid: &str,
        record: &Bound<'_, PyAny>,
        listed: &Bound<'_, PyAny>,
        owned: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let terms = ctx
            .match_terms
            .bind(py)
            .call1((record.getattr("body")?,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("symbol", record.getattr("symbol")?)?;
        kwargs.set_item("instruction", record.getattr("instruction")?)?;
        for key in ["quantity", "order_type", "price", "stop_price"] {
            kwargs.set_item(key, terms.get_item(key)?)?;
        }
        let td5 = timedelta_seconds(py, 5)?;
        let window = slf.getattr("_window")?;
        let sent_at = record.getattr("sent_at")?;
        let w_start = sent_at.call_method1("__sub__", (&td5,))?;
        let w_end = sent_at.call_method1("__add__", (&window,))?;
        kwargs.set_item("window", PyTuple::new(py, [w_start, w_end])?)?;
        kwargs.set_item("exclude", owned)?;
        let result = ctx.match_pending.bind(py).call((listed,), Some(&kwargs))?;
        let (verdict, schwab_id): (String, Option<String>) = result.extract()?;
        if verdict == "accepted" {
            record.setattr("status", "ACCEPTED")?;
            record.setattr("schwab_id", schwab_id)?;
            Self::save(slf, vid, record)?;
            return Self::ack(slf, vid, record, None);
        }
        if verdict == "not_at_venue" {
            record.setattr("status", "REJECTED")?;
            Self::save(slf, vid, record)?;
            let now = slf.getattr("_clock")?.call_method0("now_utc")?;
            return venue_ack(py, vid, "REJECTED", &now, py_str(py, "not_at_venue")?);
        }
        let account_id = record.getattr("account_id")?;
        let masked = Self::mask_of(py, &account_id)?;
        warn(
            py,
            &ctx.order_logger,
            "schwab resolve: %s matches more than one order account=%s; stays PENDING",
            vec![py_str(py, vid)?, masked],
        )?;
        Self::ack(slf, vid, record, None)
    }

    /// `_match_replacing`: match the replacing order from the replace body's terms.
    fn match_replacing(
        slf: &Bound<'_, Self>,
        listed: &Bound<'_, PyAny>,
        record: &Bound<'_, PyAny>,
        skip: &Bound<'_, PyAny>,
    ) -> PyResult<(String, Option<String>)> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let replace_sent = record.getattr("replace_sent_at")?;
        let sent = if replace_sent.is_none() {
            record.getattr("sent_at")?
        } else {
            replace_sent
        };
        let terms = ctx
            .match_terms
            .bind(py)
            .call1((record.getattr("replace_body")?,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("symbol", record.getattr("symbol")?)?;
        kwargs.set_item("instruction", record.getattr("instruction")?)?;
        for key in ["quantity", "order_type", "price", "stop_price"] {
            kwargs.set_item(key, terms.get_item(key)?)?;
        }
        let td5 = timedelta_seconds(py, 5)?;
        let window = slf.getattr("_window")?;
        let w_start = sent.call_method1("__sub__", (&td5,))?;
        let w_end = sent.call_method1("__add__", (&window,))?;
        kwargs.set_item("window", PyTuple::new(py, [w_start, w_end])?)?;
        kwargs.set_item("exclude", skip)?;
        ctx.match_pending
            .bind(py)
            .call((listed,), Some(&kwargs))?
            .extract()
    }

    /// `_clear_replace` (static): drop the unresolved replace fields.
    fn clear_replace(record: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = record.py();
        record.setattr("replace_pending", py.None())?;
        record.setattr("replace_body", py.None())?;
        record.setattr("replace_sent_at", py.None())?;
        Ok(())
    }

    /// `_adopt`: the replace took; the replacing order is the record's order from now on.
    fn adopt(
        slf: &Bound<'_, Self>,
        vid: &str,
        record: &Bound<'_, PyAny>,
        new_id: &str,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let replace_pending = record.getattr("replace_pending")?;
        if !replace_pending.is_none() {
            record.setattr("order", &replace_pending)?;
        }
        let replace_body = record.getattr("replace_body")?;
        let body = record.getattr("body")?;
        let new_body = if replace_body.is_none() {
            body
        } else {
            replace_body
        };
        record.setattr("body", &new_body)?;
        let terms = ctx.match_terms.bind(py).call1((&new_body,))?;
        record.setattr("quantity", terms.get_item("quantity")?)?;
        record.setattr("schwab_id", new_id)?;
        record.setattr("status", "ACCEPTED")?;
        Self::clear_replace(record)?;
        Self::save(slf, vid, record)?;
        Self::ack(slf, vid, record, None)
    }

    /// `_resolve_replace`: settle an unresolved replace from one list read.
    #[allow(clippy::too_many_arguments)]
    fn resolve_replace(
        slf: &Bound<'_, Self>,
        vid: &str,
        record: &Bound<'_, PyAny>,
        listed: &Bound<'_, PyAny>,
        owned: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let old_id = record.getattr("schwab_id")?;
        let old_id_str: String = old_id.str()?.extract()?;
        let mut old: Option<Bound<'_, PyAny>> = None;
        for o in listed.try_iter()? {
            let o = o?;
            let order_id = o.call_method1("get", ("orderId",))?;
            let order_id_str: String = order_id.str()?.extract()?;
            if order_id_str == old_id_str {
                old = Some(o);
                break;
            }
        }
        let status: String = match &old {
            Some(o) => o.call_method1("get", ("status", ""))?.str()?.extract()?,
            None => String::new(),
        };
        let replacing: Option<String> = match &old {
            Some(o) => {
                let r = ctx.replacing_id.bind(py).call1((o,))?;
                if r.is_none() {
                    None
                } else {
                    Some(r.extract()?)
                }
            }
            None => None,
        };
        let in_replaced: bool = ctx
            .replaced_old
            .bind(py)
            .call_method1("__contains__", (&status,))?
            .extract()?;
        let in_live: bool = ctx
            .live_old
            .bind(py)
            .call_method1("__contains__", (&status,))?
            .extract()?;
        if old.is_some() && in_replaced {
            match &replacing {
                Some(new_id) => {
                    return Self::adopt(slf, vid, record, new_id);
                }
                None => {
                    let skip = owned.call_method0("copy")?;
                    skip.call_method1("add", (&old_id,))?;
                    let (verdict, new_id) =
                        Self::match_replacing(slf, listed, record, &skip)?;
                    if verdict == "accepted" {
                        if let Some(new_id) = new_id {
                            return Self::adopt(slf, vid, record, &new_id);
                        }
                    }
                }
            }
        } else if old.is_some() && in_live && replacing.is_none() {
            let skip = owned.call_method0("copy")?;
            skip.call_method1("add", (&old_id,))?;
            let (verdict, _) = Self::match_replacing(slf, listed, record, &skip)?;
            if verdict == "not_at_venue" {
                Self::clear_replace(record)?;
                Self::save(slf, vid, record)?;
                let now = slf.getattr("_clock")?.call_method0("now_utc")?;
                return venue_ack(py, vid, "REJECTED", &now, py_str(py, "replace_not_at_venue")?);
            }
        }
        let status_or_missing = if status.is_empty() {
            "missing".to_string()
        } else {
            status
        };
        let account_id = record.getattr("account_id")?;
        let masked = Self::mask_of(py, &account_id)?;
        warn(
            py,
            &ctx.order_logger,
            "schwab resolve: replace of %s unresolved (old order %s status=%s) account=%s; stays PENDING",
            vec![
                py_str(py, vid)?,
                py_str(py, &old_id_str)?,
                py_str(py, &status_or_missing)?,
                masked,
            ],
        )?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        venue_ack(py, vid, "PENDING", &now, py_str(py, "replace_unresolved")?)
    }
}

#[pymethods]
impl SchwabOrderAdapterBase {
    #[new]
    #[pyo3(signature = (*args, **kwargs))]
    fn new(args: &Bound<'_, PyAny>, kwargs: Option<&Bound<'_, PyAny>>) -> Self {
        let _ = (args, kwargs);
        SchwabOrderAdapterBase
    }

    fn submit(slf: &Bound<'_, Self>, order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let allocations = order.getattr("allocations")?;
        let mut accounts: Vec<String> = Vec::new();
        for item in allocations.try_iter()? {
            let account_id: String = item?.getattr("account_id")?.extract()?;
            if !accounts.contains(&account_id) {
                accounts.push(account_id);
            }
        }
        if accounts.len() != 1 {
            return Err(unsupported_capability(
                py,
                "netted orders not supported on schwab",
            ));
        }
        let venue_order_id: String = order.getattr("venue_order_id")?.extract()?;
        if Self::live_refused(slf)? {
            let account_id = allocations.get_item(0)?.getattr("account_id")?;
            return Self::refused(
                slf,
                &venue_order_id,
                "place",
                Some(&account_id),
                ctx.refusal_code.as_str(),
            );
        }
        let orders = slf.getattr("_orders")?;
        let contains: bool = orders
            .call_method1("__contains__", (&venue_order_id,))?
            .extract()?;
        if contains {
            let record = orders.call_method1("get", (&venue_order_id,))?;
            return Self::ack(slf, &venue_order_id, &record, None);
        }
        let precheck = slf.getattr("_precheck")?;
        if !precheck.is_none() {
            let place = slf.getattr("_place")?;
            return precheck
                .call_method1("submit", (order, place))
                .map(Bound::unbind);
        }
        Self::place(slf, order)
    }

    fn _place(slf: &Bound<'_, Self>, order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Self::place(slf, order)
    }

    fn resolve_pending(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let orders = slf.getattr("_orders")?;
        let mut unsettled: Vec<(String, Bound<'_, PyAny>)> = Vec::new();
        for item in orders.call_method0("items")?.try_iter()? {
            let (vid, record): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
            let status: String = record.getattr("status")?.extract()?;
            if status == "PENDING" || !record.getattr("replace_body")?.is_none() {
                unsettled.push((vid.extract()?, record));
            }
        }
        let mut by_hash: Vec<(String, Vec<(String, Bound<'_, PyAny>)>)> = Vec::new();
        for (vid, record) in unsettled {
            let account_hash: String = record.getattr("account_hash")?.extract()?;
            match by_hash.iter_mut().find(|(h, _)| *h == account_hash) {
                Some((_, items)) => items.push((vid, record)),
                None => by_hash.push((account_hash, vec![(vid, record)])),
            }
        }
        let acks = PyList::empty(py);
        let builtins = py.import("builtins")?;
        for (account_hash, items) in &by_hash {
            let sent_times = PyList::empty(py);
            for (_, record) in items {
                sent_times.append(record.getattr("sent_at")?)?;
            }
            let min_sent = builtins.getattr("min")?.call1((&sent_times,))?;
            let td5 = timedelta_seconds(py, 5)?;
            let start = min_sent.call_method1("__sub__", (&td5,))?;
            let end_times = PyList::empty(py);
            for (_, record) in items {
                let replace_sent = record.getattr("replace_sent_at")?;
                let t = if replace_sent.is_none() {
                    record.getattr("sent_at")?
                } else {
                    replace_sent
                };
                end_times.append(t)?;
            }
            let max_t = builtins.getattr("max")?.call1((&end_times,))?;
            let window = slf.getattr("_window")?;
            let end = max_t.call_method1("__add__", (&window,))?;
            let client = slf.getattr("_client")?;
            let listed =
                client.call_method1("list_orders", (account_hash.as_str(), &start, &end))?;
            if listed.is_none() {
                let first_account = items[0].1.getattr("account_id")?;
                let masked = Self::mask_of(py, &first_account)?;
                warn(
                    py,
                    &ctx.order_logger,
                    "schwab resolve: order list read failed account=%s; %d stay PENDING",
                    vec![masked, py_int(py, items.len() as i64)?],
                )?;
                for (vid, record) in items {
                    if !record.getattr("replace_body")?.is_none() {
                        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
                        let ack = venue_ack(
                            py,
                            vid,
                            "PENDING",
                            &now,
                            py_str(py, "replace_unresolved")?,
                        )?;
                        acks.append(ack)?;
                    }
                }
                continue;
            }
            for (vid, record) in items {
                let owned = builtins.getattr("set")?.call0()?;
                for item in orders.call_method0("items")?.try_iter()? {
                    let (v, r): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
                    let v_str: String = v.extract()?;
                    let sid = r.getattr("schwab_id")?;
                    if !sid.is_none() && v_str != *vid {
                        owned.call_method1("add", (&sid,))?;
                    }
                }
                let ack = if !record.getattr("replace_body")?.is_none() {
                    Self::resolve_replace(slf, vid, record, &listed, &owned)?
                } else {
                    Self::resolve_place(slf, vid, record, &listed, &owned)?
                };
                acks.append(ack)?;
            }
        }
        Ok(acks.into_any().unbind())
    }

    fn cancel(slf: &Bound<'_, Self>, venue_order_id: &str) -> PyResult<Py<PyAny>> {
        let orders = slf.getattr("_orders")?;
        let record = orders.call_method1("get", (venue_order_id,))?;
        if record.is_none() {
            return Self::refused(slf, venue_order_id, "cancel", None, "unknown_order");
        }
        if !record.getattr("replace_body")?.is_none() {
            let account_id = record.getattr("account_id")?;
            return Self::refused(
                slf,
                venue_order_id,
                "cancel",
                Some(&account_id),
                "unresolved_replace",
            );
        }
        if record.getattr("schwab_id")?.is_none() {
            let account_id = record.getattr("account_id")?;
            return Self::refused(
                slf,
                venue_order_id,
                "cancel",
                Some(&account_id),
                "unresolved_order",
            );
        }
        let client = slf.getattr("_client")?;
        let account_hash = record.getattr("account_hash")?;
        let schwab_id = record.getattr("schwab_id")?;
        let result = client.call_method1("cancel", (&account_hash, &schwab_id))?;
        Self::result_ack(slf, venue_order_id, &result)
    }

    fn replace(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        changes: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let ctx = ctx(slf.py())?;
        if Self::live_refused(slf)? {
            return Self::refused(
                slf,
                venue_order_id,
                "replace",
                None,
                ctx.refusal_code.as_str(),
            );
        }
        let precheck = slf.getattr("_precheck")?;
        if !precheck.is_none() {
            let replace_cb = slf.getattr("_replace")?;
            return precheck
                .call_method1("replace", (venue_order_id, changes, replace_cb))
                .map(Bound::unbind);
        }
        Self::replace_impl(slf, venue_order_id, changes)
    }

    fn _replace(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        changes: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Self::replace_impl(slf, venue_order_id, changes)
    }

    fn schwab_order_id(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let orders = slf.getattr("_orders")?;
        let record = orders.call_method1("get", (venue_order_id,))?;
        if record.is_none() {
            return Ok(py.None());
        }
        Ok(record.getattr("schwab_id")?.unbind())
    }
}
