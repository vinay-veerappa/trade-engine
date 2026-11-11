//! BROKER_PLATFORM phase 2: the account port's doors into `te_core::accounts`.
//!
//! Python carries the data; every rule (key, masking, diff, freshness, interval, ring)
//! lives in `te_core`. A refusal crosses as `ValueError((kind, message))`, which
//! `trade_engine/accounts/_rs.py` raises as `AccountRuleError`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::accounts as ac;
use te_core::ledger::json::Json;
use te_core::ledger::model::{err, DateTime, LErr, R as LR, Val};

fn refuse(e: LErr) -> PyErr {
    PyValueError::new_err((e.kind.to_string(), e.msg))
}

fn when(text: &str, what: &str) -> PyResult<DateTime> {
    ac::parse_at(text, what).map_err(refuse)
}

#[pyfunction]
fn accounts_key(broker: &str, account_id: &str) -> PyResult<String> {
    ac::key(broker, account_id).map_err(refuse)
}

#[pyfunction]
fn accounts_display(account_id: &str) -> String {
    ac::display(account_id)
}

#[pyfunction]
fn accounts_interval(seconds: i64) -> PyResult<i64> {
    ac::interval_seconds(seconds as i128).map(|v| v as i64).map_err(refuse)
}

#[pyfunction]
fn accounts_snapshot_check(snapshot: &str) -> PyResult<()> {
    ac::snapshot_from_json(snapshot).map(|_| ()).map_err(refuse)
}

#[pyfunction]
#[pyo3(signature = (old, new, now, first_seq))]
fn accounts_diff(old: Option<&str>, new: &str, now: &str, first_seq: u64) -> PyResult<String> {
    let new = ac::snapshot_from_json(new).map_err(refuse)?;
    let old = match old {
        Some(text) if !text.is_empty() => Some(ac::snapshot_from_json(text).map_err(refuse)?),
        _ => None,
    };
    let at = when(now, "now")?;
    Ok(ac::events_to_json(&ac::diff(old.as_ref(), &new, &at, first_seq)))
}

#[pyfunction]
fn accounts_is_stale(snapshot: &str, now: &str, stale_after: i64) -> PyResult<bool> {
    let snap = ac::snapshot_from_json(snapshot).map_err(refuse)?;
    let at = when(now, "now")?;
    ac::is_stale(&snap, &at, stale_after as i128).map_err(refuse)
}

#[pyfunction]
fn accounts_replay(events: &str) -> PyResult<String> {
    ac::replay(events).map_err(refuse)
}

/// The bounded account-event ring (10,000 events or 24 h, oldest dropped and counted).
#[pyclass(unsendable)]
struct AccountEventRing {
    inner: ac::EventRing,
}

#[pymethods]
impl AccountEventRing {
    #[new]
    fn new() -> AccountEventRing {
        AccountEventRing { inner: ac::EventRing::new() }
    }

    /// Append the events in `events_json` (the internal event JSON the diff produced).
    fn push(&mut self, events_json: &str, now: &str) -> PyResult<()> {
        let at = when(now, "now")?;
        let events = events_from_json(events_json).map_err(refuse)?;
        self.inner.push(events, &at);
        Ok(())
    }

    /// The events at or after `since`, oldest first.
    fn since(&mut self, since: &str, now: &str) -> PyResult<String> {
        let from = when(since, "since")?;
        let at = when(now, "now")?;
        Ok(ac::events_to_json(&self.inner.since(&from, &at)))
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    #[getter]
    fn dropped(&self) -> usize {
        self.inner.dropped()
    }

    /// The oldest held event's ISO instant, or None when empty.
    fn oldest(&self) -> Option<String> {
        self.inner.oldest().map(|e| ac::iso_z(&e.at))
    }
}

/// The diff's internal event JSON back into `Event`s (the ring stores what the diff made).
fn events_from_json(text: &str) -> LR<Vec<ac::Event>> {
    let node = te_core::ledger::json::parse(text).map_err(|e| LErr { kind: "bad_shape", msg: e.message })?;
    let items = match node {
        Json::Arr(items) => items,
        other => return err("bad_shape", format!("events must be a list, got {}", other.type_name())),
    };
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let kind = match item.get("kind") {
            Some(Json::Str(s)) => match s.as_str() {
                "added" => "added",
                "removed" => "removed",
                "changed" => "changed",
                other => return err("bad_shape", format!("not an account event kind: {other:?}")),
            },
            _ => return err("bad_shape", "event.kind must be a string"),
        };

        let key = match item.get("key") {
            Some(Json::Str(s)) => s.clone(),
            _ => return err("bad_shape", "event.key must be a string"),
        };
        let display = match item.get("display") {
            Some(Json::Str(s)) => s.clone(),
            _ => return err("bad_shape", "event.display must be a string"),
        };
        let at = match item.get("at") {
            Some(Json::Str(s)) => ac::parse_at(s, "event.at")?,
            _ => return err("bad_shape", "event.at must be a string"),
        };
        let seq = match item.get("seq") {
            Some(Json::Int(n)) if *n > 0 => *n as u64,
            _ => return err("bad_shape", "event.seq must be a positive integer"),
        };
        let record = match item.get("record") {
            Some(Json::Null) | None => None,
            Some(node) => Some(record_from_json(node)?),
        };
        let mut changes = Vec::new();
        if let Some(Json::Arr(rows)) = item.get("changes") {
            for row in rows {
                let Json::Arr(triple) = row else {
                    return err("bad_shape", "event.changes entries must be [name, old, new]");
                };
                if triple.len() != 3 {
                    return err("bad_shape", "event.changes entries must be [name, old, new]");
                }
                let name = match &triple[0] {
                    Json::Str(s) => s.clone(),
                    _ => return err("bad_shape", "event.changes names must be strings"),
                };
                changes.push((name, change_val(&triple[1]), change_val(&triple[2])));
            }
        }
        out.push(ac::Event { kind, key, display, at, seq, record, changes });
    }
    Ok(out)
}

/// The internal record JSON back into `accounts::Record` (same shape as a snapshot record).
fn record_from_json(node: &Json) -> LR<ac::Record> {
    ac::snapshot_from_json(&format!("{{\"records\":[{}],\"confirmed_at\":null}}", te_core::ledger::json::dumps(node)))
        .map(|s| s.records.into_iter().next().expect("one record"))
}

fn change_val(node: &Json) -> ac::ChangeVal {
    match node {
        Json::Str(s) => ac::ChangeVal::Str(s.clone()),
        Json::Arr(items) => ac::ChangeVal::Routes(
            items
                .iter()
                .filter_map(|item| {
                    let platform = match item.get("platform") {
                        Some(Json::Str(s)) => s.clone(),
                        _ => return None,
                    };
                    let local_name = match item.get("local_name") {
                        Some(Json::Str(s)) => s.clone(),
                        _ => return None,
                    };
                    Some(ac::Route { platform, local_name })
                })
                .collect(),
        ),
        _ => ac::ChangeVal::None,
    }
}

#[allow(dead_code)]
fn field_val(node: &Json) -> Val {
    match node {
        Json::Str(s) => Val::Str(s.clone()),
        Json::Arr(items) => Val::Tuple(items.iter().map(field_val).collect()),
        _ => Val::None,
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(accounts_key, m)?)?;
    m.add_function(wrap_pyfunction!(accounts_display, m)?)?;
    m.add_function(wrap_pyfunction!(accounts_interval, m)?)?;
    m.add_function(wrap_pyfunction!(accounts_snapshot_check, m)?)?;
    m.add_function(wrap_pyfunction!(accounts_diff, m)?)?;
    m.add_function(wrap_pyfunction!(accounts_is_stale, m)?)?;
    m.add_function(wrap_pyfunction!(accounts_replay, m)?)?;
    m.add_class::<AccountEventRing>()?;
    Ok(())
}
