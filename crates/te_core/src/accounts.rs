//! The broker account port (BROKER_PLATFORM phase 2, `docs/design/BROKER-PLATFORM.md`
//! §6.2-6.3 and `docs/BROKER_CONTRACT.md` sections 2-3, decision D-13).
//!
//! One implementation for every adapter (D3). Time always arrives as an argument (I7):
//! nothing here reads a clock. Values cross the Python boundary as JSON text; a refusal
//! carries a `kind` (I5) and is raised by the pyo3 wrapper as `ValueError((kind, msg))`.
//!
//! - **Key rule:** `broker:accountId`, the id's case kept; an empty id, an empty broker,
//!   any whitespace or the riskguard-only `local:` prefix is refused (`bad_key`).
//! - **Masking rule:** `display` is `*` plus the last three characters; a shorter id shows
//!   `*` plus what exists. Nothing else about the id is ever produced by this module.
//! - **Record:** contract section 2. `class` and `firm` are always null from the engine
//!   (`overlay_from_engine` otherwise); `routes` are up to 8 `{platform, local_name}`
//!   objects; `confirmed_at` is RFC 3339 with a `Z`. `api_ref` never reaches this module.
//! - **Diff rule:** added = new only, removed = old only, changed = same key and any of
//!   `status`, `env`, `account_type`, `routes` differs (`confirmed_at` is ignored, and
//!   `routes` compare as a set sorted by `(platform, local_name)`). Events are sorted by
//!   key, carry a per-broker strictly rising `seq` (the caller supplies the first one),
//!   `key`, `display` and `at`; `added` carries the record, `changed` the old and new
//!   values of each changed field.
//! - **Freshness:** `is_stale(snapshot, now, stale_after)` is true at exactly
//!   `stale_after` and after it, false just under it.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use super::ledger::json::{self, Json};
use super::ledger::model::{err, parse_datetime, DateTime, LErr, R};

/// The design's hard cap for one broker's account list (§6.8a). Above it a snapshot is
/// refused and alerted, never grown silently (B7).
pub const MAX_RECORDS: usize = 2000;
/// The contract's cap on one record's routes.
pub const MAX_ROUTES: usize = 8;
/// The poll floor and default, in seconds (§6.3).
pub const MIN_INTERVAL_SECONDS: i128 = 30;
pub const DEFAULT_INTERVAL_SECONDS: i128 = 60;
/// The per-broker freshness default (§6.3).
pub const DEFAULT_STALE_AFTER_SECONDS: i128 = 120;
/// The account event ring: 10,000 events or 24 h, whichever comes first (§6.8a).
pub const RING_CAPACITY: usize = 10_000;
pub const RING_MAX_AGE_SECONDS: i128 = 24 * 60 * 60;

const ENVS: [&str; 3] = ["sim", "paper", "live"];
const STATUSES: [&str; 2] = ["active", "closed"];
const ACCOUNT_TYPES: [&str; 3] = ["margin", "ira", "cash"];
const PLATFORMS: [&str; 5] = ["nt8", "tradingview", "tradovate", "tos", "ibkr"];
/// Fields a `changed` event may name: exactly the diff-relevant, non-id fields.
const CHANGE_FIELDS: [&str; 4] = ["account_type", "env", "routes", "status"];

fn bad_key<T>(msg: impl Into<String>) -> R<T> {
    err("bad_key", msg)
}

fn bad_shape<T>(msg: impl Into<String>) -> R<T> {
    err("bad_shape", msg)
}

// --- the key and masking rules -------------------------------------------------------------

/// `broker:accountId`. The id's case is kept; an empty broker, empty id, any whitespace in
/// either, or the riskguard-only `local:` prefix is refused (contract section 2).
pub fn key(broker: &str, account_id: &str) -> R<String> {
    if broker.is_empty() {
        return bad_key("a broker name must be non-empty");
    }
    if broker.chars().any(char::is_whitespace) {
        return bad_key("a broker name must not contain whitespace");
    }
    if broker == "local" {
        return bad_key("the 'local:' prefix is riskguard-only and is refused from the engine");
    }
    if account_id.is_empty() {
        return bad_key("an account id must be non-empty");
    }
    if account_id.chars().any(char::is_whitespace) {
        return bad_key("an account id must not contain whitespace");
    }
    if account_id.contains(':') {
        return bad_key("an account id must not contain ':' (the key is broker:accountId)");
    }
    Ok(format!("{broker}:{account_id}"))
}

/// The account key a ledger membership payload names, checked by the one key rule.
pub fn ledger_key(key: &str) -> R<()> {
    split_key(key).map(|_| ())
}

/// `*` plus the last three characters; a shorter id shows `*` plus what exists.
pub fn display(account_id: &str) -> String {
    let mut tail: Vec<char> = account_id.chars().rev().take(3).collect();
    tail.reverse();
    format!("*{}", tail.into_iter().collect::<String>())
}

/// Split a key into `(broker, accountId)`, refusing what [`key`] refuses.
fn split_key(text: &str) -> R<(&str, &str)> {
    let Some((broker, id)) = text.split_once(':') else {
        return bad_key("an account key must be 'broker:accountId'");
    };
    // Re-run the one key rule so a replayed or wire key is never trusted by shape alone.
    key(broker, id)?;
    Ok((broker, id))
}

fn check_display(key: &str, shown: &str) -> R<()> {
    let (_, id) = split_key(key)?;
    let want = display(id);
    if shown != want {
        return bad_shape(format!(
            "display must be the last three characters of the account id ({want}), got {shown:?}"
        ));
    }
    Ok(())
}

// --- the poll interval ---------------------------------------------------------------------

/// A poll interval: 30 s minimum, 60 s by default (the caller applies the default).
pub fn interval_seconds(seconds: i128) -> R<i128> {
    if seconds < MIN_INTERVAL_SECONDS {
        return bad_shape(format!(
            "a poll interval of {seconds}s is under the {MIN_INTERVAL_SECONDS}s minimum"
        ));
    }
    Ok(seconds)
}

// --- the record and the snapshot -----------------------------------------------------------

/// One route to a broker account: the platform and the name that platform uses.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Route {
    pub platform: String,
    pub local_name: String,
}

#[derive(Debug, Clone)]
pub struct Record {
    pub key: String,
    pub display: String,
    pub env: String,
    pub status: String,
    pub class_: Option<String>,
    pub firm: Option<String>,
    pub account_type: Option<String>,
    /// Sorted by `(platform, local_name)`: the canonical, diff-stable order.
    pub routes: Vec<Route>,
    pub source: String,
    pub confirmed_at: DateTime,
}

#[derive(Debug, Clone)]
pub struct Snapshot {
    pub records: Vec<Record>,
    /// Absent only for a replayed snapshot (diff ignores it either way).
    pub confirmed_at: Option<DateTime>,
}

fn as_str(j: Option<&Json>, what: &str) -> R<String> {
    match j {
        Some(Json::Str(s)) => Ok(s.clone()),
        Some(other) => bad_shape(format!("{what} must be a string, got {}", other.type_name())),
        None => bad_shape(format!("{what} is required")),
    }
}

fn as_opt_str(j: Option<&Json>, what: &str) -> R<Option<String>> {
    match j {
        None | Some(Json::Null) => Ok(None),
        Some(Json::Str(s)) => Ok(Some(s.clone())),
        Some(other) => bad_shape(format!("{what} must be a string or null, got {}", other.type_name())),
    }
}

fn parse_time(j: Option<&Json>, what: &str) -> R<DateTime> {
    parse_at(&as_str(j, what)?, what)
}

/// An aware ISO instant, with a `kind` when it is not (I7).
pub fn parse_at(text: &str, what: &str) -> R<DateTime> {
    let (dt, aware) = parse_datetime(text)?;
    if !aware {
        return bad_shape(format!("{what} must carry a UTC offset (I7)"));
    }
    Ok(dt)
}

fn check_one_of(value: &str, allowed: &[&str], what: &str) -> R<()> {
    if !allowed.contains(&value) {
        return bad_shape(format!("{what} must be one of {allowed:?}, got {value:?}"));
    }
    Ok(())
}

fn parse_routes(j: Option<&Json>) -> R<Vec<Route>> {
    let items = match j {
        Some(Json::Arr(items)) => items,
        Some(other) => return bad_shape(format!("record.routes must be a list, got {}", other.type_name())),
        None => return bad_shape("record.routes is required"),
    };
    if items.len() > MAX_ROUTES {
        return bad_shape(format!(
            "{} routes is over the {MAX_ROUTES}-route cap (contract section 5)",
            items.len()
        ));
    }
    let mut routes = Vec::with_capacity(items.len());
    for item in items {
        if !matches!(item, Json::Obj(_)) {
            return bad_shape(format!("a route must be an object, got {}", item.type_name()));
        }
        let platform = as_str(item.get("platform"), "route.platform")?;
        let local_name = as_str(item.get("local_name"), "route.local_name")?;
        check_one_of(&platform, &PLATFORMS, "route.platform")?;
        if local_name.is_empty() {
            return bad_shape("route.local_name must be non-empty");
        }
        routes.push(Route { platform, local_name });
    }
    normalize_routes(routes)
}

/// The one route rule: at most 8, known platforms, non-empty local names, canonical order.
pub fn normalize_routes(mut routes: Vec<Route>) -> R<Vec<Route>> {
    if routes.len() > MAX_ROUTES {
        return bad_shape(format!(
            "{} routes is over the {MAX_ROUTES}-route cap (contract section 5)",
            routes.len()
        ));
    }
    for route in &routes {
        check_one_of(&route.platform, &PLATFORMS, "route.platform")?;
        if route.local_name.is_empty() {
            return bad_shape("route.local_name must be non-empty");
        }
    }
    routes.sort_by(|a, b| (&a.platform, &a.local_name).cmp(&(&b.platform, &b.local_name)));
    routes.dedup();
    Ok(routes)
}

/// The one ledger-record rule: contract section 2 with `api_ref` absent (D-13). Used by the
/// ledger payload's constructor, so the stored record and the wire record cannot drift.
pub fn ledger_record(
    key: String,
    display: String,
    env: String,
    status: String,
    class_: Option<String>,
    firm: Option<String>,
    account_type: Option<String>,
    routes: Vec<Route>,
    source: String,
    confirmed_at: DateTime,
) -> R<super::ledger::model::AccountLedgerRecord> {
    split_key(&key)?;
    check_display(&key, &display)?;
    check_one_of(&env, &ENVS, "record.env")?;
    check_one_of(&status, &STATUSES, "record.status")?;
    if class_.is_some() || firm.is_some() {
        return err(
            "overlay_from_engine",
            "record.class and record.firm are riskguard overlays; this side sends null",
        );
    }
    if let Some(t) = &account_type {
        check_one_of(t, &ACCOUNT_TYPES, "record.account_type")?;
    }
    if source.is_empty() {
        return bad_shape("record.source must be non-empty");
    }
    Ok(super::ledger::model::AccountLedgerRecord {
        key,
        display,
        env,
        status,
        class_,
        firm,
        account_type,
        routes: normalize_routes(routes)?,
        source,
        confirmed_at,
    })
}

fn record_json(r: &Record) -> Json {
    let opt = |v: &Option<String>| v.as_ref().map_or(Json::Null, |s| Json::Str(s.clone()));
    Json::Obj(vec![
        ("key".into(), Json::Str(r.key.clone())),
        ("display".into(), Json::Str(r.display.clone())),
        ("env".into(), Json::Str(r.env.clone())),
        ("status".into(), Json::Str(r.status.clone())),
        ("class".into(), opt(&r.class_)),
        ("firm".into(), opt(&r.firm)),
        ("account_type".into(), opt(&r.account_type)),
        ("routes".into(), Json::Arr(r.routes.iter().map(route_json).collect())),
        ("source".into(), Json::Str(r.source.clone())),
        ("confirmed_at".into(), Json::Str(iso_z(&r.confirmed_at))),
    ])
}

fn route_json(r: &Route) -> Json {
    Json::Obj(vec![
        ("platform".into(), Json::Str(r.platform.clone())),
        ("local_name".into(), Json::Str(r.local_name.clone())),
    ])
}

/// RFC 3339 UTC with a `Z` (contract section 2), fractional seconds kept when present.
pub fn iso_z(dt: &DateTime) -> String {
    let text = dt.iso();
    match text.strip_suffix("+00:00") {
        Some(head) => format!("{head}Z"),
        None => text,
    }
}

fn parse_record(node: &Json) -> R<Record> {
    if !matches!(node, Json::Obj(_)) {
        return bad_shape(format!("an account record must be an object, got {}", node.type_name()));
    }
    let key = as_str(node.get("key"), "record.key")?;
    split_key(&key)?;
    let shown = as_str(node.get("display"), "record.display")?;
    check_display(&key, &shown)?;
    let env = as_str(node.get("env"), "record.env")?;
    check_one_of(&env, &ENVS, "record.env")?;
    let status = as_str(node.get("status"), "record.status")?;
    check_one_of(&status, &STATUSES, "record.status")?;
    // `class` and `firm` are riskguard's overlays: this side always sends null and never
    // guesses (contract section 2).
    let class_ = as_opt_str(node.get("class"), "record.class")?;
    let firm = as_opt_str(node.get("firm"), "record.firm")?;
    if class_.is_some() || firm.is_some() {
        return err(
            "overlay_from_engine",
            "record.class and record.firm are riskguard overlays; this side sends null",
        );
    }
    let account_type = as_opt_str(node.get("account_type"), "record.account_type")?;
    if let Some(t) = &account_type {
        check_one_of(t, &ACCOUNT_TYPES, "record.account_type")?;
    }
    let routes = parse_routes(node.get("routes"))?;
    let source = as_str(node.get("source"), "record.source")?;
    if source.is_empty() {
        return bad_shape("record.source must be non-empty");
    }
    let confirmed_at = parse_time(node.get("confirmed_at"), "record.confirmed_at")?;
    Ok(Record { key, display: shown, env, status, class_, firm, account_type, routes, source, confirmed_at })
}

/// Parse and validate a snapshot's JSON (`{"records": [...], "confirmed_at": ...}`).
/// `api_ref` and any other unknown field are refused (contract section 2, D-13).
pub fn snapshot_from_json(text: &str) -> R<Snapshot> {
    let node = json::parse(text).map_err(|e| LErr { kind: "bad_shape", msg: e.message })?;
    let items = match node.get("records") {
        Some(Json::Arr(items)) => items,
        Some(other) => return bad_shape(format!("records must be a list, got {}", other.type_name())),
        None => return bad_shape("records is required"),
    };
    if items.len() > MAX_RECORDS {
        return err(
            "cap_exceeded",
            format!(
                "{} records is over the {MAX_RECORDS}-record cap; the snapshot is refused (B7)",
                items.len()
            ),
        );
    }
    let confirmed_at = match node.get("confirmed_at") {
        None | Some(Json::Null) => None,
        Some(_) => Some(parse_time(node.get("confirmed_at"), "confirmed_at")?),
    };
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut records = Vec::with_capacity(items.len());
    for item in items {
        if item.get("api_ref").is_some() {
            return bad_shape("api_ref is not part of the contract and must not be serialized (D-13)");
        }
        let record = parse_record(item)?;
        if !seen.insert(record.key.clone()) {
            return bad_shape(format!("duplicate account key {} in one snapshot", record.key));
        }
        records.push(record);
    }
    records.sort_by(|a, b| a.key.cmp(&b.key));
    Ok(Snapshot { records, confirmed_at })
}

/// The snapshot's JSON, in contract section 2's shape (no `api_ref`).
pub fn snapshot_to_json(s: &Snapshot) -> String {
    let records = Json::Arr(s.records.iter().map(record_json).collect());
    let fields = vec![
        ("records".to_string(), records),
        (
            "confirmed_at".to_string(),
            s.confirmed_at.as_ref().map_or(Json::Null, |d| Json::Str(iso_z(d))),
        ),
    ];
    json::dumps(&Json::Obj(fields))
}

// --- the diff rule -------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum ChangeVal {
    Str(String),
    None,
    Routes(Vec<Route>),
}

#[derive(Debug, Clone)]
pub struct Event {
    pub kind: &'static str,
    pub key: String,
    pub display: String,
    pub at: DateTime,
    /// Per broker, strictly rising (the caller supplies the first one).
    pub seq: u64,
    /// `added` only: the whole record, without `api_ref`.
    pub record: Option<Record>,
    /// `changed` only: `(field, old, new)`; the wire sends the new values only.
    pub changes: Vec<(String, ChangeVal, ChangeVal)>,
}

fn str_val(s: &str) -> ChangeVal {
    ChangeVal::Str(s.to_string())
}

fn opt_val(v: &Option<String>) -> ChangeVal {
    v.as_ref().map_or(ChangeVal::None, |s| ChangeVal::Str(s.clone()))
}

fn routes_val(routes: &[Route]) -> ChangeVal {
    ChangeVal::Routes(routes.to_vec())
}

fn changed_fields(old: &Record, new: &Record) -> Vec<(String, ChangeVal, ChangeVal)> {
    let mut out = Vec::new();
    if old.env != new.env {
        out.push(("env".to_string(), str_val(&old.env), str_val(&new.env)));
    }
    if old.status != new.status {
        out.push(("status".to_string(), str_val(&old.status), str_val(&new.status)));
    }
    if old.account_type != new.account_type {
        out.push(("account_type".to_string(), opt_val(&old.account_type), opt_val(&new.account_type)));
    }
    if old.routes != new.routes {
        out.push(("routes".to_string(), routes_val(&old.routes), routes_val(&new.routes)));
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
}

/// `diff(old, new, now, first_seq)`: added, removed and changed events, sorted by key, with
/// `seq` running from `first_seq` (inclusive). `old = None` is the empty membership.
pub fn diff(old: Option<&Snapshot>, new: &Snapshot, now: &DateTime, first_seq: u64) -> Vec<Event> {
    let empty = Snapshot { records: Vec::new(), confirmed_at: None };
    let old = old.unwrap_or(&empty);
    let before: BTreeMap<&str, &Record> = old.records.iter().map(|r| (r.key.as_str(), r)).collect();
    let after: BTreeMap<&str, &Record> = new.records.iter().map(|r| (r.key.as_str(), r)).collect();
    let mut events = Vec::new();
    for (key, record) in &after {
        match before.get(key) {
            None => events.push(Event {
                kind: "added",
                key: (*key).to_string(),
                display: record.display.clone(),
                at: now.clone(),
                seq: 0,
                record: Some((*record).clone()),
                changes: Vec::new(),
            }),
            Some(old_record) => {
                let changes = changed_fields(old_record, record);
                if !changes.is_empty() {
                    events.push(Event {
                        kind: "changed",
                        key: (*key).to_string(),
                        display: record.display.clone(),
                        at: now.clone(),
                        seq: 0,
                        record: None,
                        changes,
                    });
                }
            }
        }
    }
    for (key, record) in &before {
        if !after.contains_key(key) {
            events.push(Event {
                kind: "removed",
                key: (*key).to_string(),
                display: record.display.clone(),
                at: now.clone(),
                seq: 0,
                record: None,
                changes: Vec::new(),
            });
        }
    }
    events.sort_by(|a, b| a.key.cmp(&b.key).then(a.kind.cmp(b.kind)));
    for (offset, event) in events.iter_mut().enumerate() {
        event.seq = first_seq + offset as u64;
    }
    events
}

fn change_json(v: &ChangeVal) -> Json {
    match v {
        ChangeVal::None => Json::Null,
        ChangeVal::Str(s) => Json::Str(s.clone()),
        ChangeVal::Routes(routes) => Json::Arr(routes.iter().map(route_json).collect()),
    }
}

/// Events as the internal JSON the poll helper and the ledger read: `record` for added,
/// `changes` with old and new for changed. The contract's wire form is the carrier's job.
pub fn events_to_json(events: &[Event]) -> String {
    let body = Json::Arr(
        events
            .iter()
            .map(|e| {
                let changes = Json::Arr(
                    e.changes
                        .iter()
                        .map(|(name, old, new)| {
                            Json::Arr(vec![Json::Str(name.clone()), change_json(old), change_json(new)])
                        })
                        .collect(),
                );
                Json::Obj(vec![
                    ("kind".into(), Json::Str(e.kind.to_string())),
                    ("key".into(), Json::Str(e.key.clone())),
                    ("display".into(), Json::Str(e.display.clone())),
                    ("at".into(), Json::Str(iso_z(&e.at))),
                    ("seq".into(), Json::Int(e.seq as i128)),
                    ("record".into(), e.record.as_ref().map_or(Json::Null, record_json)),
                    ("changes".into(), changes),
                ])
            })
            .collect(),
    );
    json::dumps(&body)
}

/// Rebuild the full snapshot from the ledger's account events (in ledger order). The input
/// rows are the internal event JSON above; `added` installs a record, `changed` applies the
/// new values, `removed` drops it. The result is a snapshot JSON (no `confirmed_at`).
pub fn replay(events_json: &str) -> R<String> {
    let node = json::parse(events_json).map_err(|e| LErr { kind: "bad_shape", msg: e.message })?;
    let items = match &node {
        Json::Arr(items) => items,
        other => return bad_shape(format!("replay input must be a list, got {}", other.type_name())),
    };
    let mut records: BTreeMap<String, Record> = BTreeMap::new();
    for item in items {
        let kind = as_str(item.get("kind"), "event.kind")?;
        let key = as_str(item.get("key"), "event.key")?;
        let shown = as_str(item.get("display"), "event.display")?;
        check_display(&key, &shown)?;
        match kind.as_str() {
            "added" => {
                let record_node = item.get("record").ok_or_else(|| LErr {
                    kind: "bad_shape".into(),
                    msg: "an added ledger event needs its record".into(),
                })?;
                let record = parse_record(record_node)?;
                records.insert(key, record);
            }
            "changed" => {
                let Some(Json::Arr(rows)) = item.get("changes") else {
                    return bad_shape("a changed ledger event needs its old/new changes");
                };
                let Some(record) = records.get_mut(&key) else {
                    return bad_shape(format!("changed event for {key}, which the ledger never added"));
                };
                for row in rows {
                    let Json::Arr(triple) = row else {
                        return bad_shape("a change must be [name, old, new]");
                    };
                    if triple.len() != 3 {
                        return bad_shape("a change must be [name, old, new]");
                    }
                    let name = match &triple[0] {
                        Json::Str(s) => s.as_str(),
                        _ => return bad_shape("a change name must be a string"),
                    };
                    apply_change(record, name, &triple[2])?;
                }
            }
            "removed" => {
                records.remove(&key);
            }
            other => return bad_shape(format!("not an account event kind: {other:?}")),
        }
    }
    let snapshot = Snapshot { records: records.into_values().collect(), confirmed_at: None };
    Ok(snapshot_to_json(&snapshot))
}

fn apply_change(record: &mut Record, name: &str, new: &Json) -> R<()> {
    match name {
        "env" => {
            let value = as_str(Some(new), "change.env")?;
            check_one_of(&value, &ENVS, "change.env")?;
            record.env = value;
        }
        "status" => {
            let value = as_str(Some(new), "change.status")?;
            check_one_of(&value, &STATUSES, "change.status")?;
            record.status = value;
        }
        "account_type" => {
            record.account_type = as_opt_str(Some(new), "change.account_type")?;
            if let Some(t) = &record.account_type {
                check_one_of(t, &ACCOUNT_TYPES, "change.account_type")?;
            }
        }
        "routes" => record.routes = parse_routes(Some(new))?,
        other => return bad_shape(format!("{other:?} is not a diff field")),
    }
    Ok(())
}

// --- freshness -----------------------------------------------------------------------------

/// True when `now` is at or past `confirmed_at + stale_after` (I7: `now` is an argument).
pub fn is_stale(snapshot: &Snapshot, now: &DateTime, stale_after_seconds: i128) -> R<bool> {
    if stale_after_seconds < 0 {
        return bad_shape("stale_after must not be negative");
    }
    let Some(confirmed) = &snapshot.confirmed_at else {
        return bad_shape("a replayed snapshot has no confirmed_at to judge freshness by");
    };
    let (now_s, now_us) = now.instant();
    let (then_s, then_us) = confirmed.instant();
    // Microsecond-exact elapsed, so "just under" is judged exactly.
    let elapsed_us = (now_s - then_s) as i128 * 1_000_000 + (now_us as i128 - then_us as i128);
    Ok(elapsed_us >= stale_after_seconds * 1_000_000)
}

// --- the event ring --------------------------------------------------------------------------

/// A bounded ring of account events for `account_events(since)`: 10,000 events or 24 h,
/// whichever comes first. The oldest are dropped and counted; the ledger on disk is the
/// history (B7, I1). Append is O(1) per event.
#[derive(Debug, Default)]
pub struct EventRing {
    events: VecDeque<Event>,
    dropped: usize,
}

impl EventRing {
    pub fn new() -> EventRing {
        EventRing { events: VecDeque::new(), dropped: 0 }
    }

    pub fn len(&self) -> usize {
        self.events.len()
    }

    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    pub fn dropped(&self) -> usize {
        self.dropped
    }

    pub fn oldest(&self) -> Option<&Event> {
        self.events.front()
    }

    fn elapsed_seconds(at: &DateTime, now: &DateTime) -> i128 {
        let (now_s, now_us) = now.instant();
        let (then_s, then_us) = at.instant();
        (now_s - then_s) as i128 + ((now_us as i128 - then_us as i128) / 1_000_000)
    }

    /// Drop what has aged out (24 h), counting each drop.
    pub fn prune(&mut self, now: &DateTime) {
        while let Some(front) = self.events.front() {
            if Self::elapsed_seconds(&front.at, now) >= RING_MAX_AGE_SECONDS {
                self.events.pop_front();
                self.dropped += 1;
            } else {
                break;
            }
        }
    }

    /// Append events (in order), dropping the oldest past the cap, then the aged out.
    pub fn push(&mut self, events: impl IntoIterator<Item = Event>, now: &DateTime) {
        for event in events {
            if self.events.len() >= RING_CAPACITY {
                self.events.pop_front();
                self.dropped += 1;
            }
            self.events.push_back(event);
        }
        self.prune(now);
    }

    /// The events at or after `since`, oldest first (pruning by age first).
    pub fn since(&mut self, since: &DateTime, now: &DateTime) -> Vec<Event> {
        self.prune(now);
        let (s_s, s_us) = since.instant();
        self.events
            .iter()
            .filter(|e| {
                let (a_s, a_us) = e.at.instant();
                (a_s, a_us) >= (s_s, s_us)
            })
            .cloned()
            .collect()
    }
}

// --- the ledger payload's checks ------------------------------------------------------------

/// The change fields a ledger payload may name, sorted and unique.
pub fn check_changes(changes: &[(String, super::ledger::model::Val, super::ledger::model::Val)]) -> R<()> {
    use super::ledger::model::{Obj, Val};
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    for (name, old, new) in changes {
        if !CHANGE_FIELDS.contains(&name.as_str()) {
            return bad_shape(format!(
                "{name:?} is not a diff field; a changed event may name only {CHANGE_FIELDS:?}"
            ));
        }
        if !seen.insert(name.as_str()) {
            return bad_shape(format!("{name:?} is named twice in one change"));
        }
        for (side, value) in [("old", old), ("new", new)] {
            let ok = match value {
                // Python's StrEnum members are `str`s, so its own check accepts them.
                Val::Str(_)
                | Val::None
                | Val::Side(_)
                | Val::Right(_)
                | Val::OrderType(_)
                | Val::OrderState(_)
                | Val::Tif(_)
                | Val::Kind(_) => true,
                Val::Tuple(items) => items
                    .iter()
                    .all(|v| matches!(v, Val::Obj(boxed) if matches!(boxed.as_ref(), Obj::Route(_)))),
                _ => false,
            };
            if !ok {
                return bad_shape(format!(
                    "{name} {side} must be a string, null or a list of routes, got {}",
                    value.type_name()
                ));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dt(s: &str) -> DateTime {
        let (d, aware) = parse_datetime(s).expect("iso");
        assert!(aware);
        d
    }

    fn route(platform: &str, local: &str) -> Route {
        Route { platform: platform.into(), local_name: local.into() }
    }

    fn rec(key: &str, status: &str, mut routes: Vec<Route>) -> Record {
        let (_, id) = key.split_once(':').expect("key");
        routes.sort_by(|a, b| (&a.platform, &a.local_name).cmp(&(&b.platform, &b.local_name)));
        Record {
            key: key.to_string(),
            display: display(id),
            env: "paper".into(),
            status: status.into(),
            class_: None,
            firm: None,
            account_type: Some("margin".into()),
            routes,
            source: "tos_paper".into(),
            confirmed_at: dt("2026-11-11T18:00:00Z"),
        }
    }

    fn snap(records: Vec<Record>) -> Snapshot {
        Snapshot { records, confirmed_at: Some(dt("2026-11-11T18:00:00Z")) }
    }

    #[test]
    fn key_keeps_case_and_refuses_empty_whitespace_and_local() {
        assert_eq!(key("Tradovate", "APEX12345aBc").unwrap(), "Tradovate:APEX12345aBc");
        assert!(key("broker", "").is_err());
        assert!(key("", "acct").is_err());
        assert!(key("bro wer", "acct").is_err());
        assert!(key("broker", "ac ct").is_err());
        assert!(key("broker", "ac\tct").is_err());
        assert!(key("broker", "a:b").is_err());
        assert!(key("local", "Sim101").is_err());
    }

    #[test]
    fn masking_shows_only_the_last_three_characters() {
        assert_eq!(display("ACCT-TEST-194"), "*194");
        assert_eq!(display("ab"), "*ab");
        assert_eq!(display("a"), "*a");
        assert_eq!(display(""), "*");
    }

    #[test]
    fn diff_add_remove_change_and_nothing_changed() {
        let now = dt("2026-11-11T18:01:00Z");
        let old = snap(vec![
            rec("tos_paper:A194", "active", vec![route("tos", "a")]),
            rec("tos_paper:B195", "active", vec![route("tos", "b")]),
        ]);
        let new = snap(vec![
            rec("tos_paper:A194", "closed", vec![route("tos", "a"), route("tradingview", "D1")]),
            rec("tos_paper:C196", "active", vec![route("tos", "c")]),
        ]);
        let events = diff(Some(&old), &new, &now, 10);
        let kinds: Vec<(&str, &str, u64)> =
            events.iter().map(|e| (e.kind, e.key.as_str(), e.seq)).collect();
        assert_eq!(
            kinds,
            vec![
                ("changed", "tos_paper:A194", 10),
                ("removed", "tos_paper:B195", 11),
                ("added", "tos_paper:C196", 12)
            ]
        );
        let names: Vec<&str> = events[0].changes.iter().map(|(n, _, _)| n.as_str()).collect();
        assert_eq!(names, vec!["routes", "status"]);
        assert!(events[2].record.is_some());
        assert!(events[2].record.as_ref().unwrap().key == "tos_paper:C196");
        assert!(diff(Some(&new), &new, &now, 10).is_empty());
    }

    #[test]
    fn routes_compare_as_a_sorted_set() {
        let now = dt("2026-11-11T18:01:00Z");
        let old = snap(vec![rec("tos_paper:A194", "active", vec![route("tos", "a"), route("nt8", "n")])]);
        let new = snap(vec![rec("tos_paper:A194", "active", vec![route("nt8", "n"), route("tos", "a")])]);
        assert!(diff(Some(&old), &new, &now, 1).is_empty());
    }

    #[test]
    fn staleness_is_exact_at_the_boundary() {
        let s = snap(vec![]);
        let at = dt("2026-11-11T18:02:00Z");
        assert!(is_stale(&s, &at, 120).unwrap());
        let just_under = dt("2026-11-11T18:01:59.999999Z");
        assert!(!is_stale(&s, &just_under, 120).unwrap());
    }

    #[test]
    fn interval_refuses_under_thirty_seconds() {
        assert_eq!(interval_seconds(30).unwrap(), 30);
        assert_eq!(interval_seconds(60).unwrap(), 60);
        assert!(interval_seconds(29).is_err());
    }

    #[test]
    fn snapshot_round_trips_with_z_times_and_no_api_ref() {
        let s = snap(vec![rec("tos_paper:A194", "active", vec![route("tos", "a")])]);
        let text = snapshot_to_json(&s);
        assert!(text.contains("\"confirmed_at\":\"2026-11-11T18:00:00Z\""), "{text}");
        assert!(!text.contains("api_ref"));
        let back = snapshot_from_json(&text).unwrap();
        assert_eq!(back.records.len(), 1);
        assert_eq!(back.records[0].display, "*194");
        let bad = text.replace("\"*194\"", "\"A194\"");
        assert!(snapshot_from_json(&bad).is_err());
    }

    #[test]
    fn a_record_with_api_ref_is_refused() {
        let s = snap(vec![rec("tos_paper:A194", "active", vec![])]);
        let text = snapshot_to_json(&s).replace("\"source\"", "\"api_ref\":\"secret\",\"source\"");
        let err = snapshot_from_json(&text).unwrap_err();
        assert_eq!(err.kind, "bad_shape");
    }

    #[test]
    fn more_than_eight_routes_is_refused() {
        let routes: Vec<Route> = (0..9).map(|i| route("tos", &format!("r{i}"))).collect();
        let text = snapshot_to_json(&snap(vec![rec("tos_paper:A194", "active", routes)]));
        assert_eq!(snapshot_from_json(&text).unwrap_err().kind, "bad_shape");
    }

    #[test]
    fn replay_rebuilds_records_and_applies_changes() {
        let events = concat!(
            "[",
            r#"{"kind":"added","key":"tos_paper:ACCT-TEST-194","display":"*194","seq":1,"at":"2026-11-11T18:00:00Z","record":{"key":"tos_paper:ACCT-TEST-194","display":"*194","env":"paper","status":"active","class":null,"firm":null,"account_type":"margin","routes":[{"platform":"tos","local_name":"ACCT-TEST-194"}],"source":"tos_paper","confirmed_at":"2026-11-11T18:00:00Z"}},"#,
            r#"{"kind":"changed","key":"tos_paper:ACCT-TEST-194","display":"*194","seq":2,"at":"2026-11-11T18:01:00Z","changes":[["status","active","closed"]]}"#,
            "]"
        );
        let out = replay(events).unwrap();
        let back = snapshot_from_json(&out).unwrap();
        assert_eq!(back.records.len(), 1);
        assert_eq!(back.records[0].key, "tos_paper:ACCT-TEST-194");
        assert_eq!(back.records[0].status, "closed");
        assert_eq!(back.records[0].routes[0].platform, "tos");
    }

    fn ev(key: &str, at: &str) -> Event {
        Event {
            kind: "added",
            key: key.to_string(),
            display: "*194".into(),
            at: dt(at),
            seq: 1,
            record: None,
            changes: Vec::new(),
        }
    }

    #[test]
    fn the_event_ring_drops_the_oldest_past_the_cap() {
        let now = dt("2026-11-11T19:00:00Z");
        let mut ring = EventRing::new();
        let events: Vec<Event> =
            (0..=RING_CAPACITY).map(|i| ev(&format!("tos_paper:A{i:05}"), "2026-11-11T18:00:00Z")).collect();
        ring.push(events, &now);
        assert_eq!(ring.len(), RING_CAPACITY);
        assert_eq!(ring.dropped(), 1);
        assert_eq!(ring.oldest().unwrap().key, "tos_paper:A00001");
    }

    #[test]
    fn the_event_ring_drops_events_older_than_a_day() {
        let mut ring = EventRing::new();
        ring.push([ev("tos_paper:A194", "2026-11-10T18:00:00Z")], &dt("2026-11-10T19:00:00Z"));
        ring.push([ev("tos_paper:B194", "2026-11-11T19:00:00Z")], &dt("2026-11-11T19:00:00Z"));
        assert_eq!(ring.len(), 1);
        assert_eq!(ring.dropped(), 1);
        assert_eq!(ring.oldest().unwrap().key, "tos_paper:B194");
    }

    #[test]
    fn a_snapshot_over_the_cap_is_refused() {
        let records: Vec<Record> =
            (0..MAX_RECORDS + 1).map(|i| rec(&format!("tos_paper:A{i:04}"), "active", vec![])).collect();
        let text = snapshot_to_json(&snap(records));
        assert_eq!(snapshot_from_json(&text).unwrap_err().kind, "cap_exceeded");
    }
}
