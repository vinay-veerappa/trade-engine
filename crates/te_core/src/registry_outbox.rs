//! The engine's account-registry outbox (BROKER_PLATFORM phase 4a, I12,
//! `docs/BROKER_CONTRACT.md` section 4). One implementation (D3): Python only does the I/O.
//!
//! The outbox turns each good poll (snapshot + events) into `broker-account/1` ingest
//! envelopes for riskguard and tracks what riskguard has confirmed:
//!
//! - **Confirmed delivery only on an acknowledged `applied_seq`.** A 200 whose body does not
//!   carry an integer `applied_seq` is `ack_missing`: nothing is confirmed, nothing is pruned.
//! - **Strictly in order.** `next_envelope` hands out events from `acked_seq + 1`; the caller
//!   sends one envelope at a time and stops at the first failure (`on_response` says `retry`).
//! - **Seq gaps are refused.** `observe` accepts events only contiguous from the head; a gap
//!   changes nothing and raises `seq_gap`. riskguard's own 409 `need: snapshot` re-seeds the
//!   outbox with a full snapshot at the head.
//! - **Compaction.** The pending tail is capped by bytes and by count (B7). Past either cap the
//!   tail is dropped and counted, and the next envelope is the latest snapshot (membership is
//!   idempotent: a snapshot replaces the broker's set), so a long outage resumes as
//!   "latest snapshot, then the events since". The persisted state (`to_json`) is members plus
//!   tail and never exceeds `cap_bytes`.
//! - **Freshness.** Every envelope carries the last good poll's `confirmed_at`, never the
//!   delivery time, so a dead source goes stale on riskguard's side while heartbeats continue
//!   (an envelope with neither snapshot nor events refreshes `confirmed_at` only, and is due
//!   every `stale_after_s / 3`).
//! - **Halts.** 400, 401, 413 and any other non-retryable answer halt the outbox with the
//!   refusal's kind (never the body): the caller alerts and does not retry.
//!
//! - **Broker realized P&L (PS-4a).** Optional and off by default (`set_pnl_enabled`). The latest
//!   figure per account rides on envelopes as a top-level `"pnl"` list. It never takes a seq.
//!   See `set_pnl` for the staleness rule.
//!
//! Time arrives as an argument (I7); nothing here reads a clock. The wire never carries
//! `api_ref` (D-13): records are re-serialized through `accounts::record_json`, which has no
//! such field, and a `class`/`firm` that is not null is `overlay_from_engine`.

use std::collections::{BTreeMap, VecDeque};
use std::str::FromStr;

use rust_decimal::Decimal;

use super::accounts::{self, parse_at, parse_record, record_json, Record};
use super::ledger::json::{self, Json};
use super::ledger::model::{err, LErr, R};

pub const CONTRACT: &str = "broker-account/1";
/// The persisted-state format version.
pub const STATE_VERSION: i128 = 1;
/// The on-disk cap for the outbox state (I12 / BP-4: 50 MB).
pub const DEFAULT_CAP_BYTES: usize = 50 * 1024 * 1024;
/// The pending-tail cap by count (B7).
pub const DEFAULT_MAX_TAIL_EVENTS: usize = 100_000;
/// riskguard's envelope caps: 1000 events, 1 MB body. We stay under with margin.
pub const MAX_EVENTS_PER_ENVELOPE: usize = 1000;
pub const MAX_BODY_BYTES: usize = 900_000;
/// Slack for the state file's own keys, counted against the cap.
const STATE_OVERHEAD_BYTES: usize = 4096;

/// What riskguard may name as the `kind` of a refusal. Anything else is recorded as `unknown`
/// so a hostile or garbled body can never put text into our errors or logs.
const KNOWN_REFUSALS: [&str; 7] = [
    "bad_key",
    "overlay_from_engine",
    "bad_shape",
    "unknown_contract",
    "too_large",
    "cap_exceeded",
    "unauthorized",
];

#[derive(Debug, Clone, Default)]
pub struct Counters {
    pub compactions: u64,
    pub dropped_events: u64,
    pub duplicates_ignored: u64,
    pub covered_by_snapshot: u64,
    pub gaps_refused: u64,
    pub gap_409: u64,
    pub unacked_200: u64,
    pub ack_short: u64,
    pub regressions: u64,
    pub halts: u64,
    pub retries: u64,
    pub envelopes: u64,
    pub acked: u64,
    /// `set_pnl` calls refused (new key past the account cap, bad value, wrong broker, switch off).
    pub pnl_refused: u64,
    /// pnl entries dropped because their account left the snapshot set.
    pub pnl_dropped: u64,
}

#[derive(Debug, Clone)]
struct Inflight {
    top_seq: u64,
    snapshot: bool,
    /// `Outbox::pnl_version` when the envelope was built (what it carried).
    pnl_version: u64,
    /// `Outbox::pnl_fresh_version` when the envelope was built (BP-4f).
    pnl_fresh_version: u64,
}

#[derive(Debug)]
pub struct Outbox {
    broker: String,
    stale_after_s: i64,
    cap_bytes: usize,
    max_tail_events: usize,
    seeded: bool,
    head_seq: u64,
    acked_seq: u64,
    confirmed_at: Option<String>,
    needs_snapshot: bool,
    /// key -> record JSON text (the latest good snapshot's membership, at `head_seq`).
    members: BTreeMap<String, String>,
    members_bytes: usize,
    /// Wire events with `seq > acked_seq`, contiguous, ascending.
    tail: VecDeque<(u64, String)>,
    tail_bytes: usize,
    inflight: Option<Inflight>,
    last_sent_s: Option<i64>,
    halted: Option<String>,
    counters: Counters,
    pnl_enabled: bool,
    /// key -> (realized, `as_of`). Bounded by `accounts::MAX_RECORDS` (B7): one Decimal plus one
    /// timestamp string per account. Memory only; never persisted (the next poll refills it).
    pnl: BTreeMap<String, (Decimal, String)>,
    /// Bumped on every change to `pnl` (value, new key, removal). A change is "sent" once an
    /// envelope that carried it was acknowledged: `pnl_version > pnl_sent_version` is due.
    pnl_version: u64,
    pnl_sent_version: u64,
    /// BP-4f: bumped when only an `as_of` moves forward (same value). Sent no sooner than
    /// `PNL_FRESH_MIN_INTERVAL_S` after the last acknowledged envelope, so riskguard's 10 s
    /// staleness check sees a live figure without a post per re-read.
    pnl_fresh_version: u64,
    pnl_fresh_sent_version: u64,
}

/// BP-4f: the shortest gap between two envelopes sent only to carry a newer `as_of`.
/// B7: at most 30 such posts a minute; the feed re-reads every 2 s while a position is open
/// and every 30 s when flat, so the flat cost is one post per 30 s.
pub const PNL_FRESH_MIN_INTERVAL_S: i64 = 2;

fn str_json(s: &str) -> String {
    json::dumps(&Json::Str(s.to_string()))
}

fn as_u64(j: Option<&Json>, what: &str) -> R<u64> {
    match j {
        Some(Json::Int(n)) if *n >= 0 && *n <= u64::MAX as i128 => Ok(*n as u64),
        _ => err("bad_shape", format!("{what} must be a non-negative integer")),
    }
}

fn as_text(j: Option<&Json>, what: &str) -> R<String> {
    match j {
        Some(Json::Str(s)) => Ok(s.clone()),
        _ => err("bad_shape", format!("{what} must be a string")),
    }
}

impl Outbox {
    pub fn new(broker: &str, stale_after_s: i64, cap_bytes: usize, max_tail_events: usize) -> R<Outbox> {
        accounts::key(broker, "x")?; // the one broker-name rule
        if stale_after_s < 3 {
            return err("bad_shape", "stale_after_s must be at least 3 (heartbeat is stale_after_s / 3)");
        }
        if cap_bytes < 2 * STATE_OVERHEAD_BYTES {
            return err("bad_shape", format!("cap_bytes must be at least {}", 2 * STATE_OVERHEAD_BYTES));
        }
        if max_tail_events == 0 {
            return err("bad_shape", "max_tail_events must be positive");
        }
        Ok(Outbox {
            broker: broker.to_string(),
            stale_after_s,
            cap_bytes,
            max_tail_events,
            seeded: false,
            head_seq: 0,
            acked_seq: 0,
            confirmed_at: None,
            needs_snapshot: true,
            members: BTreeMap::new(),
            members_bytes: 0,
            tail: VecDeque::new(),
            tail_bytes: 0,
            inflight: None,
            last_sent_s: None,
            halted: None,
            counters: Counters::default(),
            pnl_enabled: false,
            pnl: BTreeMap::new(),
            pnl_version: 0,
            pnl_sent_version: 0,
            pnl_fresh_version: 0,
            pnl_fresh_sent_version: 0,
        })
    }

    /// Turn the `pnl` list on or off (default off). Turning it off forgets the held values, so
    /// the envelopes are exactly the pre-PS-4a bytes.
    pub fn set_pnl_enabled(&mut self, on: bool) {
        self.pnl_enabled = on;
        if !on {
            self.pnl.clear();
            self.pnl_version = 0;
            self.pnl_sent_version = 0;
            self.pnl_fresh_version = 0;
            self.pnl_fresh_sent_version = 0;
        }
    }

    pub fn pnl_len(&self) -> usize {
        self.pnl.len()
    }

    /// Keep the latest realized P&L for one account (overwrites). `realized` must be a plain
    /// decimal string (D6: no exponent, never a float); `as_of` the daemon's `as_of_utc`.
    ///
    /// A new key past `accounts::MAX_RECORDS` is refused (`cap_exceeded`) and counted. A pnl
    /// change takes no seq. Staleness: riskguard treats the figure as stale after 10 s, but
    /// heartbeats are due only every `stale_after_s / 3` (40 s at the default 120), so a changed
    /// value makes the next `next_envelope` due at once (a heartbeat-shaped envelope with no
    /// events) instead of waiting for the cadence. An unchanged value with a newer `as_of`
    /// (BP-4f) is due once `PNL_FRESH_MIN_INTERVAL_S` has passed since the last send; an older
    /// or equal `as_of` is kept out (the stored one stays) and is never due.
    pub fn set_pnl(&mut self, key: &str, realized: &str, as_of: &str) -> R<()> {
        let value = match self.check_pnl(key, realized, as_of) {
            Ok(v) => v,
            Err(e) => {
                self.counters.pnl_refused += 1;
                return Err(e);
            }
        };
        if !self.pnl.contains_key(key) && self.pnl.len() >= accounts::MAX_RECORDS {
            self.counters.pnl_refused += 1;
            return err("cap_exceeded", format!("more than {} accounts carry a pnl figure", accounts::MAX_RECORDS));
        }
        match self.pnl.get(key) {
            Some((old, old_as_of)) if *old == value => {
                // Same figure: only a strictly newer as_of refreshes it (BP-4f).
                let newer = parse_at(as_of, "pnl.as_of")?.instant() > parse_at(old_as_of, "pnl.as_of")?.instant();
                if newer {
                    self.pnl.insert(key.to_string(), (value, as_of.to_string()));
                    self.pnl_fresh_version += 1;
                }
            }
            _ => {
                self.pnl.insert(key.to_string(), (value, as_of.to_string()));
                self.pnl_version += 1;
            }
        }
        Ok(())
    }

    /// Forget one account's figure (it has none now: another day, unknown). Absent is a no-op.
    pub fn clear_pnl(&mut self, key: &str) {
        if self.pnl.remove(key).is_some() {
            self.pnl_version += 1;
        }
    }

    fn check_pnl(&self, key: &str, realized: &str, as_of: &str) -> R<Decimal> {
        if !self.pnl_enabled {
            return err("pnl_disabled", "the pnl list is switched off");
        }
        self.check_broker(key)?;
        parse_at(as_of, "pnl.as_of")?;
        let plain = !realized.is_empty()
            && realized.bytes().enumerate().all(|(i, b)| b.is_ascii_digit() || b == b'.' || (i == 0 && b == b'-'));
        match Decimal::from_str(realized) {
            Ok(v) if plain => Ok(v),
            _ => err("bad_money", "realized_usd must be a plain decimal string"),
        }
    }

    /// The `,"pnl":[...]` fragment, or empty when the switch is off or nothing is held.
    fn pnl_fragment(&self) -> String {
        if !self.pnl_enabled || self.pnl.is_empty() {
            return String::new();
        }
        let rows: Vec<String> = self
            .pnl
            .iter()
            .map(|(key, (value, as_of))| {
                format!(
                    "{{\"key\":{},\"realized_usd\":{},\"as_of\":{}}}",
                    str_json(key),
                    str_json(&value.to_string()),
                    str_json(as_of)
                )
            })
            .collect();
        format!(",\"pnl\":[{}]", rows.join(","))
    }

    pub fn counters(&self) -> &Counters {
        &self.counters
    }

    pub fn head_seq(&self) -> u64 {
        self.head_seq
    }

    pub fn acked_seq(&self) -> u64 {
        self.acked_seq
    }

    pub fn needs_snapshot(&self) -> bool {
        self.needs_snapshot
    }

    pub fn halted(&self) -> Option<&str> {
        self.halted.as_deref()
    }

    pub fn pending(&self) -> usize {
        self.tail.len()
    }

    /// The bytes the persisted state occupies (an upper estimate of `to_json().len()`).
    pub fn size_bytes(&self) -> usize {
        self.members_bytes + self.tail_bytes + STATE_OVERHEAD_BYTES
    }

    /// Forget everything and start again from the next poll's snapshot (the caller does this
    /// after a corrupt state file or a seq regression it cannot reconcile).
    pub fn reset(&mut self) {
        self.seeded = false;
        self.head_seq = 0;
        self.acked_seq = 0;
        self.needs_snapshot = true;
        self.members.clear();
        self.members_bytes = 0;
        self.tail.clear();
        self.tail_bytes = 0;
        self.inflight = None;
        self.last_sent_s = None;
    }

    /// Clear a halt (the operator fixed the cause); the next envelope is a full snapshot.
    pub fn resume(&mut self) {
        self.halted = None;
        self.needs_snapshot = true;
        self.tail.clear();
        self.tail_bytes = 0;
        self.inflight = None;
    }

    // --- observing a good poll ---------------------------------------------------------------

    /// Record one good poll: its snapshot (`{"records": [...], ...}`), the internal events the
    /// diff produced, the poller's head seq after the poll, and the poll's `confirmed_at`.
    /// Nothing changes when this refuses.
    pub fn observe(
        &mut self,
        snapshot_json: &str,
        events_json: &str,
        head_seq: u64,
        confirmed_at: &str,
    ) -> R<()> {
        parse_at(confirmed_at, "confirmed_at")?;
        let snapshot = accounts::snapshot_from_json(snapshot_json)?;
        let mut members: BTreeMap<String, String> = BTreeMap::new();
        let mut members_bytes = 0usize;
        for record in &snapshot.records {
            self.check_broker(&record.key)?;
            let text = json::dumps(&record_json(record));
            members_bytes += text.len();
            members.insert(record.key.clone(), text);
        }
        if members_bytes + STATE_OVERHEAD_BYTES > self.cap_bytes {
            return err(
                "cap_exceeded",
                format!("the account set alone is {members_bytes} bytes, over the {}-byte cap", self.cap_bytes),
            );
        }
        let events = self.wire_events(events_json)?;

        // Contiguity: refuse a gap before touching any state.
        let mut fresh: Vec<(u64, String)> = Vec::new();
        let mut duplicates = 0u64;
        let mut next = self.head_seq + 1;
        for (seq, text) in events {
            if self.seeded && seq <= self.head_seq {
                duplicates += 1;
                continue;
            }
            if self.seeded && seq != next {
                self.counters.gaps_refused += 1;
                return err(
                    "seq_gap",
                    format!("event seq {seq} does not follow the outbox head (expected {next})"),
                );
            }
            if let Some((last, _)) = fresh.last() {
                if seq != last + 1 {
                    self.counters.gaps_refused += 1;
                    return err("seq_gap", format!("event seq {seq} does not follow {last}"));
                }
            }
            next = seq + 1;
            fresh.push((seq, text));
        }
        let new_head = fresh.last().map_or(self.head_seq, |(s, _)| *s);
        if self.seeded {
            if head_seq != new_head {
                self.counters.gaps_refused += 1;
                return err(
                    "seq_gap",
                    format!("the poll's head seq {head_seq} does not match the events (head {new_head})"),
                );
            }
        } else if head_seq < new_head {
            self.counters.gaps_refused += 1;
            return err("seq_gap", format!("the poll's head seq {head_seq} is behind its events"));
        }

        // Commit.
        self.counters.duplicates_ignored += duplicates;
        // A figure for an account the snapshot no longer lists is dropped with it.
        let before = self.pnl.len();
        self.pnl.retain(|k, _| members.contains_key(k));
        if self.pnl.len() != before {
            self.counters.pnl_dropped += (before - self.pnl.len()) as u64;
            self.pnl_version += 1;
        }
        self.members = members;
        self.members_bytes = members_bytes;
        self.confirmed_at = Some(confirmed_at.to_string());
        if !self.seeded {
            self.seeded = true;
            self.needs_snapshot = true;
            self.counters.covered_by_snapshot += fresh.len() as u64;
            self.head_seq = head_seq;
            return Ok(());
        }
        self.head_seq = new_head;
        if self.needs_snapshot {
            // The snapshot at the new head covers these events.
            self.counters.covered_by_snapshot += fresh.len() as u64;
            return Ok(());
        }
        for (seq, text) in fresh {
            self.tail_bytes += text.len();
            self.tail.push_back((seq, text));
        }
        if self.tail.len() > self.max_tail_events || self.size_bytes() > self.cap_bytes {
            self.compact();
        }
        Ok(())
    }

    fn compact(&mut self) {
        self.counters.compactions += 1;
        self.counters.dropped_events += self.tail.len() as u64;
        self.tail.clear();
        self.tail_bytes = 0;
        self.needs_snapshot = true;
    }

    fn check_broker(&self, key: &str) -> R<()> {
        match key.split_once(':') {
            Some((broker, _)) if broker == self.broker => Ok(()),
            _ => err("bad_key", format!("a key must name the broker {:?}", self.broker)),
        }
    }

    /// Internal event JSON -> wire events (contract section 3): `added` carries the record,
    /// `changed` the new values only, `removed` nothing more. Sorted by seq.
    fn wire_events(&self, events_json: &str) -> R<Vec<(u64, String)>> {
        let node = json::parse(events_json).map_err(|e| LErr { kind: "bad_shape", msg: e.message })?;
        let Json::Arr(items) = &node else {
            return err("bad_shape", "events must be a list");
        };
        let mut out = Vec::with_capacity(items.len());
        for item in items {
            let kind = as_text(item.get("kind"), "event.kind")?;
            let key = as_text(item.get("key"), "event.key")?;
            self.check_broker(&key)?;
            let at = as_text(item.get("at"), "event.at")?;
            let at_dt = parse_at(&at, "event.at")?;
            let seq = as_u64(item.get("seq"), "event.seq")?;
            let mut pairs: Vec<(String, Json)> = vec![
                ("kind".into(), Json::Str(kind.clone())),
                ("key".into(), Json::Str(key.clone())),
                ("seq".into(), Json::Int(seq as i128)),
                ("at".into(), Json::Str(at)),
            ];
            match kind.as_str() {
                "added" => {
                    let Some(node) = item.get("record") else {
                        return err("bad_shape", "an added event needs its record");
                    };
                    let record = parse_record(node)?;
                    pairs.push(("record".into(), record_json(&record)));
                }
                "changed" => {
                    let Some(Json::Arr(rows)) = item.get("changes") else {
                        return err("bad_shape", "a changed event needs its changes");
                    };
                    // Validate each new value with the one apply rule, on a scratch record.
                    let mut scratch = Record {
                        key: key.clone(),
                        display: String::new(),
                        env: "paper".into(),
                        status: "active".into(),
                        class_: None,
                        firm: None,
                        account_type: None,
                        routes: Vec::new(),
                        source: "scratch".into(),
                        confirmed_at: at_dt,
                    };
                    let mut fields: Vec<(String, Json)> = Vec::new();
                    for row in rows {
                        let Json::Arr(triple) = row else {
                            return err("bad_shape", "a change must be [name, old, new]");
                        };
                        if triple.len() != 3 {
                            return err("bad_shape", "a change must be [name, old, new]");
                        }
                        let Json::Str(name) = &triple[0] else {
                            return err("bad_shape", "a change name must be a string");
                        };
                        accounts::apply_change(&mut scratch, name, &triple[2])?;
                        fields.push((name.clone(), triple[2].clone()));
                    }
                    pairs.push(("fields".into(), Json::Obj(fields)));
                }
                "removed" => {}
                other => return err("bad_shape", format!("not an account event kind: {other:?}")),
            }
            out.push((seq, json::dumps(&Json::Obj(pairs))));
        }
        out.sort_by_key(|(seq, _)| *seq);
        Ok(out)
    }

    // --- the next envelope -------------------------------------------------------------------

    /// The next envelope to POST, or `None` when there is nothing due. Stops being offered
    /// while halted, and before the first good poll (there is no `confirmed_at` to carry).
    pub fn next_envelope(&mut self, now: &str) -> R<Option<String>> {
        if self.halted.is_some() || !self.seeded {
            return Ok(None);
        }
        let now_s = parse_at(now, "now")?.instant().0;
        let Some(confirmed_at) = self.confirmed_at.clone() else {
            return Ok(None);
        };
        let head = format!(
            "{{\"contract\":{},\"broker\":{},\"confirmed_at\":{},\"stale_after_s\":{}{}",
            str_json(CONTRACT),
            str_json(&self.broker),
            str_json(&confirmed_at),
            self.stale_after_s,
            self.pnl_fragment()
        );
        let pnl_version = self.pnl_version;
        let pnl_fresh_version = self.pnl_fresh_version;
        if self.needs_snapshot {
            let accounts: Vec<&str> = self.members.values().map(String::as_str).collect();
            let body = format!(
                "{head},\"snapshot\":{{\"seq\":{},\"accounts\":[{}]}},\"events\":[]}}",
                self.head_seq,
                accounts.join(",")
            );
            if body.len() > MAX_BODY_BYTES {
                return self.halt("too_large");
            }
            self.inflight = Some(Inflight { top_seq: self.head_seq, snapshot: true, pnl_version, pnl_fresh_version });
            self.counters.envelopes += 1;
            return Ok(Some(body));
        }
        if let Some((first, _)) = self.tail.front() {
            if *first != self.acked_seq + 1 {
                // The tail no longer follows the acked point: fall back to a snapshot.
                self.counters.gaps_refused += 1;
                self.needs_snapshot = true;
                self.tail.clear();
                self.tail_bytes = 0;
                return self.next_envelope(now);
            }
            let mut chunk: Vec<&str> = Vec::new();
            let mut size = head.len() + 64;
            let mut top = self.acked_seq;
            for (seq, text) in self.tail.iter() {
                if chunk.len() >= MAX_EVENTS_PER_ENVELOPE || size + text.len() + 1 > MAX_BODY_BYTES {
                    break;
                }
                size += text.len() + 1;
                chunk.push(text);
                top = *seq;
            }
            let body = format!("{head},\"events\":[{}]}}", chunk.join(","));
            self.inflight = Some(Inflight { top_seq: top, snapshot: false, pnl_version, pnl_fresh_version });
            self.counters.envelopes += 1;
            return Ok(Some(body));
        }
        let pnl_due = self.pnl_enabled && self.pnl_version > self.pnl_sent_version;
        let fresh_due = self.pnl_enabled
            && self.pnl_fresh_version > self.pnl_fresh_sent_version
            && self.last_sent_s.map_or(true, |last| now_s - last >= PNL_FRESH_MIN_INTERVAL_S);
        let due = pnl_due
            || fresh_due
            || match self.last_sent_s {
                None => true,
                Some(last) => now_s - last >= self.stale_after_s / 3,
            };
        if due {
            self.inflight = Some(Inflight { top_seq: self.acked_seq, snapshot: false, pnl_version, pnl_fresh_version });
            self.counters.envelopes += 1;
            return Ok(Some(format!("{head},\"events\":[]}}")));
        }
        Ok(None)
    }

    fn halt(&mut self, kind: &str) -> R<Option<String>> {
        self.counters.halts += 1;
        self.halted = Some(kind.to_string());
        err("rejected", format!("the outbox is halted: riskguard refused with kind {kind}"))
    }

    // --- the response ------------------------------------------------------------------------

    /// Fold riskguard's answer to the last envelope. `status` 0 is "no answer" (network error).
    /// Returns `acked`, `retry` or `need_snapshot`; a refusal that must not be retried halts
    /// the outbox and raises `rejected`.
    pub fn on_response(&mut self, status: u16, body: &str, now: &str) -> R<&'static str> {
        let now_s = parse_at(now, "now")?.instant().0;
        let inflight = self.inflight.clone();
        match status {
            0 | 408 | 429 | 500..=599 => {
                self.counters.retries += 1;
                Ok("retry")
            }
            200 => {
                let applied = match json::parse(body).ok().as_ref().and_then(|j| j.get("applied_seq").cloned()) {
                    Some(Json::Int(n)) if n >= 0 && n <= u64::MAX as i128 => n as u64,
                    _ => {
                        self.counters.unacked_200 += 1;
                        return err("ack_missing", "a 200 without an integer applied_seq is not a confirmation");
                    }
                };
                if applied > self.head_seq {
                    self.counters.halts += 1;
                    self.halted = Some("ack_ahead".into());
                    return err("ack_ahead", "riskguard acknowledged a seq this outbox never produced");
                }
                if applied < self.acked_seq {
                    self.counters.regressions += 1;
                    self.needs_snapshot = true;
                    self.tail.clear();
                    self.tail_bytes = 0;
                    self.inflight = None;
                    return Ok("need_snapshot");
                }
                if let Some(f) = &inflight {
                    if applied < f.top_seq {
                        self.counters.ack_short += 1;
                        return err("ack_short", "riskguard acknowledged less than the envelope carried");
                    }
                }
                self.acked_seq = applied;
                while matches!(self.tail.front(), Some((s, _)) if *s <= applied) {
                    if let Some((_, text)) = self.tail.pop_front() {
                        self.tail_bytes -= text.len();
                    }
                }
                if let Some(f) = inflight.as_ref().filter(|f| f.snapshot) {
                    // The snapshot covered the head it was built at. Polls observed while it
                    // was in flight were not tailed (a snapshot was pending), so if the head
                    // moved on, riskguard is behind and needs a newer snapshot.
                    self.needs_snapshot = self.head_seq > f.top_seq;
                }
                if let Some(f) = &inflight {
                    self.pnl_sent_version = self.pnl_sent_version.max(f.pnl_version);
                    self.pnl_fresh_sent_version = self.pnl_fresh_sent_version.max(f.pnl_fresh_version);
                }
                self.inflight = None;
                self.last_sent_s = Some(now_s);
                self.counters.acked += 1;
                Ok("acked")
            }
            409 => {
                let need = json::parse(body).ok().and_then(|j| j.get("need").cloned());
                if matches!(need, Some(Json::Str(ref s)) if s == "snapshot") {
                    self.counters.gap_409 += 1;
                    self.needs_snapshot = true;
                    self.tail.clear();
                    self.tail_bytes = 0;
                    self.inflight = None;
                    Ok("need_snapshot")
                } else {
                    err("bad_response", "a 409 that does not ask for a snapshot")
                }
            }
            other => {
                let named = json::parse(body).ok().and_then(|j| match j.get("kind") {
                    Some(Json::Str(s)) => Some(s.clone()),
                    _ => None,
                });
                let kind = if other == 401 {
                    "unauthorized"
                } else {
                    named.as_deref().filter(|k| KNOWN_REFUSALS.contains(k)).unwrap_or("unknown")
                };
                self.counters.halts += 1;
                self.halted = Some(kind.to_string());
                self.inflight = None;
                err("rejected", format!("riskguard refused the envelope: status {other}, kind {kind}"))
            }
        }
    }

    // --- persistence -------------------------------------------------------------------------

    /// The state as JSON text: members plus the pending tail. At most `size_bytes()` long.
    pub fn to_json(&self) -> String {
        let members: Vec<&str> = self.members.values().map(String::as_str).collect();
        let tail: Vec<&str> = self.tail.iter().map(|(_, t)| t.as_str()).collect();
        let confirmed = self.confirmed_at.as_deref().map_or("null".to_string(), str_json);
        format!(
            "{{\"version\":{STATE_VERSION},\"broker\":{},\"stale_after_s\":{},\"seeded\":{},\"head_seq\":{},\"acked_seq\":{},\"confirmed_at\":{},\"needs_snapshot\":{},\"members\":[{}],\"tail\":[{}]}}",
            str_json(&self.broker),
            self.stale_after_s,
            self.seeded,
            self.head_seq,
            self.acked_seq,
            confirmed,
            self.needs_snapshot,
            members.join(","),
            tail.join(",")
        )
    }

    /// Rebuild from `to_json` text. A state that is over the caps, names another broker, or
    /// whose tail is not contiguous after `acked_seq` is refused (the caller resets).
    pub fn from_json(
        text: &str,
        broker: &str,
        stale_after_s: i64,
        cap_bytes: usize,
        max_tail_events: usize,
    ) -> R<Outbox> {
        if text.len() > cap_bytes {
            return err("cap_exceeded", "the outbox file is over the cap");
        }
        let node = json::parse(text).map_err(|e| LErr { kind: "bad_shape", msg: e.message })?;
        match node.get("version") {
            Some(Json::Int(v)) if *v == STATE_VERSION => {}
            _ => return err("bad_shape", "unknown outbox state version"),
        }
        if as_text(node.get("broker"), "broker")? != broker {
            return err("bad_key", "the outbox file belongs to another broker");
        }
        let mut ob = Outbox::new(broker, stale_after_s, cap_bytes, max_tail_events)?;
        ob.head_seq = as_u64(node.get("head_seq"), "head_seq")?;
        ob.acked_seq = as_u64(node.get("acked_seq"), "acked_seq")?;
        ob.seeded = matches!(node.get("seeded"), Some(Json::Bool(true)));
        ob.needs_snapshot = !matches!(node.get("needs_snapshot"), Some(Json::Bool(false)));
        ob.confirmed_at = match node.get("confirmed_at") {
            Some(Json::Str(s)) => {
                parse_at(s, "confirmed_at")?;
                Some(s.clone())
            }
            _ => None,
        };
        if ob.acked_seq > ob.head_seq {
            return err("seq_gap", "acked_seq is ahead of head_seq");
        }
        let Some(Json::Arr(members)) = node.get("members") else {
            return err("bad_shape", "members must be a list");
        };
        if members.len() > accounts::MAX_RECORDS {
            return err("cap_exceeded", "too many members");
        }
        for item in members {
            let record = parse_record(item)?;
            ob.check_broker(&record.key)?;
            let text = json::dumps(&record_json(&record));
            ob.members_bytes += text.len();
            ob.members.insert(record.key.clone(), text);
        }
        let Some(Json::Arr(tail)) = node.get("tail") else {
            return err("bad_shape", "tail must be a list");
        };
        if tail.len() > max_tail_events {
            return err("cap_exceeded", "the tail is over the event cap");
        }
        let mut expect = ob.acked_seq + 1;
        for item in tail {
            let seq = as_u64(item.get("seq"), "event.seq")?;
            if seq != expect {
                return err("seq_gap", "the persisted tail is not contiguous after acked_seq");
            }
            expect += 1;
            let text = json::dumps(item);
            ob.tail_bytes += text.len();
            ob.tail.push_back((seq, text));
        }
        if !ob.tail.is_empty() && expect - 1 != ob.head_seq {
            return err("seq_gap", "the persisted tail does not end at head_seq");
        }
        if ob.size_bytes() > cap_bytes {
            return err("cap_exceeded", "the outbox state is over the cap");
        }
        // The tail after `acked_seq` is exactly what riskguard has not confirmed. After a
        // restart it is resent as is: seq <= applied is a no-op there, and if riskguard lost
        // its state its 409 asks for a snapshot.
        Ok(ob)
    }

    pub fn stats_json(&self) -> String {
        let c = &self.counters;
        let halted = self.halted.as_deref().map_or(Json::Null, |h| Json::Str(h.to_string()));
        let n = |v: u64| Json::Int(v as i128);
        json::dumps(&Json::Obj(vec![
            ("head_seq".into(), n(self.head_seq)),
            ("acked_seq".into(), n(self.acked_seq)),
            ("pending".into(), n(self.tail.len() as u64)),
            ("members".into(), n(self.members.len() as u64)),
            ("size_bytes".into(), n(self.size_bytes() as u64)),
            ("cap_bytes".into(), n(self.cap_bytes as u64)),
            ("needs_snapshot".into(), Json::Bool(self.needs_snapshot)),
            ("halted".into(), halted),
            ("compactions".into(), n(c.compactions)),
            ("dropped_events".into(), n(c.dropped_events)),
            ("duplicates_ignored".into(), n(c.duplicates_ignored)),
            ("covered_by_snapshot".into(), n(c.covered_by_snapshot)),
            ("gaps_refused".into(), n(c.gaps_refused)),
            ("gap_409".into(), n(c.gap_409)),
            ("unacked_200".into(), n(c.unacked_200)),
            ("ack_short".into(), n(c.ack_short)),
            ("regressions".into(), n(c.regressions)),
            ("halts".into(), n(c.halts)),
            ("retries".into(), n(c.retries)),
            ("envelopes".into(), n(c.envelopes)),
            ("acked".into(), n(c.acked)),
            ("pnl_held".into(), n(self.pnl.len() as u64)),
            ("pnl_refused".into(), n(c.pnl_refused)),
            ("pnl_dropped".into(), n(c.pnl_dropped)),
        ]))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const BROKER: &str = "tradovate";
    const T0: &str = "2026-11-11T14:00:00Z";

    fn rec_json(id: &str, status: &str) -> String {
        format!(
            "{{\"key\":\"{BROKER}:{id}\",\"display\":\"{}\",\"env\":\"paper\",\"status\":\"{status}\",\"class\":null,\"firm\":null,\"account_type\":null,\"routes\":[{{\"platform\":\"tradingview\",\"local_name\":\"{id}\"}}],\"source\":\"ntx\",\"confirmed_at\":\"{T0}\"}}",
            accounts::display(id)
        )
    }

    fn snap(ids: &[(&str, &str)]) -> String {
        let rows: Vec<String> = ids.iter().map(|(i, s)| rec_json(i, s)).collect();
        format!("{{\"records\":[{}],\"confirmed_at\":\"{T0}\"}}", rows.join(","))
    }

    fn added(id: &str, seq: u64) -> String {
        format!(
            "{{\"kind\":\"added\",\"key\":\"{BROKER}:{id}\",\"display\":\"{}\",\"at\":\"{T0}\",\"seq\":{seq},\"record\":{},\"changes\":[]}}",
            accounts::display(id),
            rec_json(id, "active")
        )
    }

    fn changed(id: &str, seq: u64, status: &str) -> String {
        format!(
            "{{\"kind\":\"changed\",\"key\":\"{BROKER}:{id}\",\"display\":\"{}\",\"at\":\"{T0}\",\"seq\":{seq},\"record\":null,\"changes\":[[\"status\",\"x\",\"{status}\"]]}}",
            accounts::display(id)
        )
    }

    fn evs(items: &[String]) -> String {
        format!("[{}]", items.join(","))
    }

    fn outbox() -> Outbox {
        Outbox::new(BROKER, 120, DEFAULT_CAP_BYTES, DEFAULT_MAX_TAIL_EVENTS).unwrap()
    }

    fn seeded() -> Outbox {
        let mut ob = outbox();
        ob.observe(&snap(&[("A194", "active")]), &evs(&[added("A194", 1)]), 1, T0).unwrap();
        let env = ob.next_envelope(T0).unwrap().unwrap();
        assert!(env.contains("\"snapshot\":{\"seq\":1"));
        assert_eq!(ob.on_response(200, "{\"applied_seq\":1}", T0).unwrap(), "acked");
        ob
    }

    fn secs(n: i64) -> String {
        let h = 14 + n / 3600;
        let m = (n % 3600) / 60;
        let s = n % 60;
        let day = 11 + h / 24;
        format!("2026-11-{day:02}T{:02}:{m:02}:{s:02}Z", h % 24)
    }

    #[test]
    fn first_poll_sends_one_snapshot_then_acks_it() {
        let ob = seeded();
        assert_eq!(ob.acked_seq(), 1);
        assert!(!ob.needs_snapshot());
        assert_eq!(ob.pending(), 0);
    }

    #[test]
    fn a_bare_200_is_not_a_confirmation() {
        let mut ob = outbox();
        ob.observe(&snap(&[("A194", "active")]), &evs(&[added("A194", 1)]), 1, T0).unwrap();
        ob.next_envelope(T0).unwrap().unwrap();
        for body in ["", "{}", "ok", "{\"applied_seq\":\"1\"}", "{\"applied_seq\":null}"] {
            let e = ob.on_response(200, body, T0).unwrap_err();
            assert_eq!(e.kind, "ack_missing");
        }
        assert_eq!(ob.acked_seq(), 0);
        assert!(ob.needs_snapshot());
        assert_eq!(ob.counters().unacked_200, 5);
    }

    #[test]
    fn events_drain_in_order_and_prune_on_ack() {
        let mut ob = seeded();
        let items: Vec<String> = (2..=6).map(|s| changed("A194", s, if s % 2 == 0 { "closed" } else { "active" })).collect();
        ob.observe(&snap(&[("A194", "active")]), &evs(&items), 6, T0).unwrap();
        assert_eq!(ob.pending(), 5);
        let env = ob.next_envelope(T0).unwrap().unwrap();
        let positions: Vec<usize> = (2..=6).map(|s| env.find(&format!("\"seq\":{s}}}")).unwrap()).collect();
        assert!(positions.windows(2).all(|w| w[0] < w[1]), "in order");
        assert!(!env.contains("\"snapshot\""));
        assert!(!env.contains("\"display\""), "events carry no display field on the wire");
        // a short ack confirms only what was applied
        let e = ob.on_response(200, "{\"applied_seq\":3}", T0).unwrap_err();
        assert_eq!(e.kind, "ack_short");
        assert_eq!(ob.acked_seq(), 1);
        assert_eq!(ob.on_response(200, "{\"applied_seq\":6}", T0).unwrap(), "acked");
        assert_eq!(ob.pending(), 0);
    }

    #[test]
    fn a_seq_gap_is_refused_and_changes_nothing() {
        let mut ob = seeded();
        let before = ob.to_json();
        let e = ob
            .observe(&snap(&[("A194", "active")]), &evs(&[changed("A194", 3, "closed")]), 3, T0)
            .unwrap_err();
        assert_eq!(e.kind, "seq_gap");
        assert_eq!(ob.to_json(), before);
        // head mismatch with no events is a gap too
        let e = ob.observe(&snap(&[("A194", "active")]), "[]", 5, T0).unwrap_err();
        assert_eq!(e.kind, "seq_gap");
        assert_eq!(ob.counters().gaps_refused, 2);
        // a replayed old event is a counted no-op
        ob.observe(&snap(&[("A194", "active")]), &evs(&[added("A194", 1)]), 1, T0).unwrap();
        assert_eq!(ob.counters().duplicates_ignored, 1);
    }

    #[test]
    fn a_poll_observed_while_a_snapshot_is_in_flight_is_not_lost() {
        let mut ob = outbox();
        ob.observe(&snap(&[("A194", "active")]), &evs(&[added("A194", 1)]), 1, T0).unwrap();
        ob.next_envelope(T0).unwrap().unwrap(); // snapshot at 1 goes out
        // the poller moves on while the post is in flight (the sink does not hold a lock)
        ob.observe(&snap(&[("A194", "closed")]), &evs(&[changed("A194", 2, "closed")]), 2, T0).unwrap();
        assert_eq!(ob.on_response(200, "{\"applied_seq\":1}", T0).unwrap(), "acked");
        assert!(ob.needs_snapshot(), "riskguard is at 1, the head is 2: it needs the newer snapshot");
        let env = ob.next_envelope(T0).unwrap().unwrap();
        assert!(env.contains("\"snapshot\":{\"seq\":2") && env.contains("\"status\":\"closed\""));
        assert_eq!(ob.on_response(200, "{\"applied_seq\":2}", T0).unwrap(), "acked");
        assert!(!ob.needs_snapshot());
    }

    #[test]
    fn riskguard_409_resends_a_snapshot_at_the_head() {
        let mut ob = seeded();
        ob.observe(&snap(&[("A194", "closed")]), &evs(&[changed("A194", 2, "closed")]), 2, T0).unwrap();
        ob.next_envelope(T0).unwrap().unwrap();
        assert_eq!(ob.on_response(409, "{\"need\":\"snapshot\",\"applied_seq\":0}", T0).unwrap(), "need_snapshot");
        let env = ob.next_envelope(T0).unwrap().unwrap();
        assert!(env.contains("\"snapshot\":{\"seq\":2"));
        assert!(env.contains("\"status\":\"closed\""));
        assert_eq!(ob.on_response(200, "{\"applied_seq\":2}", T0).unwrap(), "acked");
        assert_eq!(ob.counters().gap_409, 1);
        assert_eq!(ob.on_response(409, "{\"x\":1}", T0).unwrap_err().kind, "bad_response");
    }

    #[test]
    fn an_ack_ahead_of_the_head_halts_and_a_lower_ack_reseeds() {
        let mut ob = seeded();
        ob.observe(&snap(&[("A194", "active")]), "[]", 1, T0).unwrap();
        ob.next_envelope(&secs(100).as_str()).unwrap().unwrap(); // heartbeat
        assert_eq!(ob.on_response(200, "{\"applied_seq\":0}", T0).unwrap(), "need_snapshot");
        assert_eq!(ob.counters().regressions, 1);
        ob.next_envelope(T0).unwrap().unwrap();
        let e = ob.on_response(200, "{\"applied_seq\":9}", T0).unwrap_err();
        assert_eq!(e.kind, "ack_ahead");
        assert_eq!(ob.halted(), Some("ack_ahead"));
        assert!(ob.next_envelope(T0).unwrap().is_none());
    }

    #[test]
    fn non_retryable_answers_halt_with_a_known_kind_only() {
        for (status, body, kind) in [
            (400, "{\"kind\":\"overlay_from_engine\"}", "overlay_from_engine"),
            (401, "{\"error\":\"nope\"}", "unauthorized"),
            (413, "{\"kind\":\"too_large\"}", "too_large"),
            (400, "{\"kind\":\"SECRET-token-ish\"}", "unknown"),
        ] {
            let mut ob = outbox();
            ob.observe(&snap(&[("A194", "active")]), &evs(&[added("A194", 1)]), 1, T0).unwrap();
            ob.next_envelope(T0).unwrap().unwrap();
            let e = ob.on_response(status, body, T0).unwrap_err();
            assert_eq!(e.kind, "rejected");
            assert!(e.msg.contains(kind), "{}", e.msg);
            assert!(!e.msg.contains("SECRET"), "a body is never echoed");
            assert_eq!(ob.halted(), Some(kind));
            assert!(ob.next_envelope(T0).unwrap().is_none());
        }
        let mut ob = seeded();
        assert_eq!(ob.on_response(503, "", T0).unwrap(), "retry");
        assert_eq!(ob.on_response(0, "", T0).unwrap(), "retry");
        assert_eq!(ob.on_response(429, "", T0).unwrap(), "retry");
    }

    #[test]
    fn heartbeats_carry_the_polls_confirmed_at_and_are_due_every_third_of_stale() {
        let mut ob = seeded();
        assert!(ob.next_envelope(&secs(39)).unwrap().is_none(), "not yet due");
        let env = ob.next_envelope(&secs(40)).unwrap().unwrap();
        assert!(env.contains(&format!("\"confirmed_at\":\"{T0}\"")), "the poll's time, not delivery time");
        assert!(env.contains("\"events\":[]") && !env.contains("\"snapshot\""));
        assert_eq!(ob.on_response(200, "{\"applied_seq\":1}", secs(40).as_str()).unwrap(), "acked");
        assert!(ob.next_envelope(&secs(79)).unwrap().is_none());
        assert!(ob.next_envelope(&secs(80)).unwrap().is_some());
    }

    #[test]
    fn nothing_is_offered_before_the_first_good_poll() {
        let mut ob = outbox();
        assert!(ob.next_envelope(T0).unwrap().is_none());
    }

    #[test]
    fn the_wire_never_carries_api_ref_and_refuses_overlays() {
        let mut ob = outbox();
        let bad = snap(&[("A194", "active")]).replace("\"class\":null", "\"class\":\"prop\"");
        assert_eq!(ob.observe(&bad, "[]", 0, T0).unwrap_err().kind, "overlay_from_engine");
        let api = snap(&[("A194", "active")]).replace("\"source\":\"ntx\"", "\"source\":\"ntx\",\"api_ref\":\"secret-ref\"");
        assert_eq!(ob.observe(&api, "[]", 0, T0).unwrap_err().kind, "bad_shape");
        let mut ob = outbox();
        let ok = snap(&[("A194", "active")]);
        ob.observe(&ok, "[]", 0, T0).unwrap();
        let env = ob.next_envelope(T0).unwrap().unwrap();
        assert!(!env.contains("api_ref"));
        let other = snap(&[("A194", "active")]).replace("tradovate:", "nt8:");
        assert_eq!(outbox().observe(&other, "[]", 0, T0).unwrap_err().kind, "bad_key");
    }

    #[test]
    fn filling_the_byte_cap_compacts_to_a_snapshot_and_the_state_stays_under_the_cap() {
        let cap = 3 * STATE_OVERHEAD_BYTES; // 12 KiB: a handful of events fit
        let mut ob = Outbox::new(BROKER, 120, cap, DEFAULT_MAX_TAIL_EVENTS).unwrap();
        ob.observe(&snap(&[("A194", "active")]), &evs(&[added("A194", 1)]), 1, T0).unwrap();
        ob.next_envelope(T0).unwrap().unwrap();
        ob.on_response(200, "{\"applied_seq\":1}", T0).unwrap();
        let mut seq = 1;
        for _ in 0..2000 {
            seq += 1;
            let status = if seq % 2 == 0 { "closed" } else { "active" };
            ob.observe(&snap(&[("A194", status)]), &evs(&[changed("A194", seq, status)]), seq, T0).unwrap();
            assert!(ob.size_bytes() <= cap, "size {} over cap {cap}", ob.size_bytes());
            assert!(ob.to_json().len() <= cap);
        }
        assert!(ob.counters().compactions > 0, "the cap was crossed and counted");
        assert!(ob.counters().dropped_events > 0);
        assert!(ob.needs_snapshot());
        // resuming delivery sends the latest snapshot, then the rest in order
        let env = ob.next_envelope(T0).unwrap().unwrap();
        assert!(env.contains(&format!("\"snapshot\":{{\"seq\":{seq}")));
        assert_eq!(ob.on_response(200, &format!("{{\"applied_seq\":{seq}}}"), T0).unwrap(), "acked");
        assert_eq!(ob.pending(), 0);
    }

    #[test]
    fn filling_the_event_count_cap_compacts_and_counts() {
        let mut ob = Outbox::new(BROKER, 120, DEFAULT_CAP_BYTES, 50).unwrap();
        ob.observe(&snap(&[("A194", "active")]), &evs(&[added("A194", 1)]), 1, T0).unwrap();
        ob.next_envelope(T0).unwrap().unwrap();
        ob.on_response(200, "{\"applied_seq\":1}", T0).unwrap();
        for seq in 2..=200u64 {
            ob.observe(&snap(&[("A194", "active")]), &evs(&[changed("A194", seq, "active")]), seq, T0).unwrap();
            assert!(ob.pending() <= 50);
        }
        assert_eq!(ob.counters().compactions, 1);
        assert_eq!(ob.counters().dropped_events, 51, "the whole tail is dropped at the crossing");
        assert!(ob.needs_snapshot());
        assert!(ob.counters().covered_by_snapshot >= 148);
        // delivery resumes with the snapshot, then the tail fills and compacts again
        ob.next_envelope(T0).unwrap().unwrap();
        ob.on_response(200, "{\"applied_seq\":200}", T0).unwrap();
        for seq in 201..=260u64 {
            ob.observe(&snap(&[("A194", "active")]), &evs(&[changed("A194", seq, "active")]), seq, T0).unwrap();
            assert!(ob.pending() <= 50);
        }
        assert_eq!(ob.counters().compactions, 2);
    }

    #[test]
    fn a_day_of_events_stays_under_the_cap_and_replays_in_order() {
        // 24 h of one-minute polls, two status flips per poll on a 40-account set.
        let ids: Vec<String> = (0..40).map(|i| format!("ACCT-TEST-{i:03}")).collect();
        let members: Vec<(&str, &str)> = ids.iter().map(|s| (s.as_str(), "active")).collect();
        let mut ob = outbox();
        ob.observe(&snap(&members), "[]", 0, T0).unwrap();
        let mut seq = 0u64;
        for minute in 0..1440i64 {
            let items: Vec<String> = (0..2)
                .map(|k| {
                    seq += 1;
                    changed(&ids[((minute as usize) + k) % 40], seq, if minute % 2 == 0 { "closed" } else { "active" })
                })
                .collect();
            ob.observe(&snap(&members), &evs(&items), seq, &secs(minute * 60)).unwrap();
        }
        assert_eq!(ob.counters().compactions, 0, "24 h fits without compaction");
        assert!(ob.to_json().len() <= DEFAULT_CAP_BYTES);
        // the snapshot goes first (riskguard was never reached), then the tail is empty: seed it
        let mut last = 0u64;
        let snapshot_env = ob.next_envelope(&secs(86_400)).unwrap().unwrap();
        assert!(snapshot_env.contains(&format!("\"snapshot\":{{\"seq\":{seq}")));
        ob.on_response(200, &format!("{{\"applied_seq\":{seq}}}"), &secs(86_400)).unwrap();
        // now a second day-chunk replays strictly in order across several envelopes
        let first = seq + 1;
        let items: Vec<String> = (0..2500u64)
            .map(|k| {
                seq += 1;
                changed(&ids[(k as usize) % 40], seq, if k % 2 == 0 { "closed" } else { "active" })
            })
            .collect();
        ob.observe(&snap(&members), &evs(&items), seq, &secs(86_460)).unwrap();
        let mut expect = first;
        for _ in 0..3 {
            let env = ob.next_envelope(&secs(86_460)).unwrap().unwrap();
            let node = json::parse(&env).unwrap();
            let Some(Json::Arr(events)) = node.get("events") else { panic!("events") };
            assert!(events.len() <= MAX_EVENTS_PER_ENVELOPE);
            assert!(env.len() <= MAX_BODY_BYTES);
            for e in events {
                let Some(Json::Int(s)) = e.get("seq") else { panic!("seq") };
                assert_eq!(*s as u64, expect, "strictly in order");
                expect += 1;
                last = *s as u64;
            }
            ob.on_response(200, &format!("{{\"applied_seq\":{last}}}"), &secs(86_460)).unwrap();
        }
        assert_eq!(last, seq);
        assert_eq!(ob.pending(), 0);
    }

    #[test]
    fn state_round_trips_and_a_corrupt_file_is_refused() {
        let mut ob = seeded();
        ob.observe(&snap(&[("A194", "closed")]), &evs(&[changed("A194", 2, "closed")]), 2, T0).unwrap();
        let text = ob.to_json();
        assert!(text.len() <= ob.size_bytes());
        let back = Outbox::from_json(&text, BROKER, 120, DEFAULT_CAP_BYTES, DEFAULT_MAX_TAIL_EVENTS).unwrap();
        assert_eq!(back.head_seq(), 2);
        assert!(!back.needs_snapshot());
        assert_eq!(back.pending(), 1, "the unconfirmed tail survives a restart");
        assert_eq!(back.acked_seq(), 1);
        assert_eq!(Outbox::from_json("{", BROKER, 120, DEFAULT_CAP_BYTES, 10).unwrap_err().kind, "bad_shape");
        assert_eq!(Outbox::from_json(&text, "nt8", 120, DEFAULT_CAP_BYTES, 10).unwrap_err().kind, "bad_key");
        assert_eq!(Outbox::from_json(&text, BROKER, 120, 100, 10).unwrap_err().kind, "cap_exceeded");
        let gap = text.replace("\"seq\":2}", "\"seq\":7}");
        assert_eq!(
            Outbox::from_json(&gap, BROKER, 120, DEFAULT_CAP_BYTES, 10).unwrap_err().kind,
            "seq_gap"
        );
    }

    #[test]
    fn a_snapshot_over_the_account_cap_is_refused() {
        let mut ob = outbox();
        let rows: Vec<String> = (0..accounts::MAX_RECORDS + 1).map(|i| rec_json(&format!("ACCT-TEST-{i:05}"), "active")).collect();
        let text = format!("{{\"records\":[{}]}}", rows.join(","));
        assert_eq!(ob.observe(&text, "[]", 0, T0).unwrap_err().kind, "cap_exceeded");
    }

    // --- PS-4a: broker realized P&L ------------------------------------------------------------

    const AS_OF: &str = "2026-11-15T14:03:11Z";

    fn pnl_seeded() -> Outbox {
        let mut ob = outbox();
        ob.set_pnl_enabled(true);
        ob.observe(
            &snap(&[("A194", "active"), ("B207", "active")]),
            &evs(&[added("A194", 1), added("B207", 2)]),
            2,
            T0,
        )
        .unwrap();
        ob.next_envelope(T0).unwrap().unwrap();
        assert_eq!(ob.on_response(200, "{\"applied_seq\":2}", T0).unwrap(), "acked");
        ob
    }

    fn key(id: &str) -> String {
        format!("{BROKER}:{id}")
    }

    #[test]
    fn a_newer_as_of_alone_is_sent_after_the_min_interval_bp4f() {
        let mut ob = pnl_seeded();
        ob.set_pnl(&key("A194"), "-62.50", "2026-11-11T14:00:00Z").unwrap();
        ob.next_envelope(T0).unwrap().unwrap();
        ob.on_response(200, "{\"applied_seq\":2}", T0).unwrap();
        // Same value, newer as_of: not due inside the min interval, due at it, carrying the new time.
        ob.set_pnl(&key("A194"), "-62.50", &secs(1)).unwrap();
        assert!(ob.next_envelope(&secs(1)).unwrap().is_none());
        let env = ob.next_envelope(&secs(PNL_FRESH_MIN_INTERVAL_S)).unwrap().unwrap();
        assert!(env.contains(&format!("\"as_of\":\"{}\"", secs(1))), "{env}");
        ob.on_response(200, "{\"applied_seq\":2}", &secs(2)).unwrap();
        // Nothing new: not due until the heartbeat cadence.
        assert!(ob.next_envelope(&secs(10)).unwrap().is_none());
        // An equal or older as_of keeps the stored one and is never due.
        ob.set_pnl(&key("A194"), "-62.50", &secs(1)).unwrap();
        ob.set_pnl(&key("A194"), "-62.50", T0).unwrap();
        assert!(ob.next_envelope(&secs(10)).unwrap().is_none());
        // A changed value is still due at once, regardless of the interval.
        ob.set_pnl(&key("A194"), "-70.00", &secs(3)).unwrap();
        ob.next_envelope(&secs(10)).unwrap();
        ob.on_response(200, "{\"applied_seq\":2}", &secs(10)).unwrap();
        ob.set_pnl(&key("A194"), "-75.00", &secs(11)).unwrap();
        assert!(ob.next_envelope(&secs(10)).unwrap().is_some());
    }

    #[test]
    fn pnl_off_envelopes_are_the_pre_ps4a_bytes() {
        // Same inputs, switch never touched vs. switch on but nothing set vs. set while off.
        let run = |mutate: fn(&mut Outbox)| {
            let mut ob = outbox();
            mutate(&mut ob);
            ob.observe(&snap(&[("A194", "active")]), &evs(&[added("A194", 1)]), 1, T0).unwrap();
            let snapshot = ob.next_envelope(T0).unwrap().unwrap();
            ob.on_response(200, "{\"applied_seq\":1}", T0).unwrap();
            let beat = ob.next_envelope(&secs(40)).unwrap().unwrap();
            (snapshot, beat)
        };
        let (snapshot, beat) = run(|_| {});
        assert_eq!(
            snapshot,
            format!(
                "{{\"contract\":\"broker-account/1\",\"broker\":\"tradovate\",\"confirmed_at\":\"{T0}\",\"stale_after_s\":120,\"snapshot\":{{\"seq\":1,\"accounts\":[{}]}},\"events\":[]}}",
                wire_record_for_test("A194")
            )
        );
        assert_eq!(
            beat,
            format!("{{\"contract\":\"broker-account/1\",\"broker\":\"tradovate\",\"confirmed_at\":\"{T0}\",\"stale_after_s\":120,\"events\":[]}}")
        );
        assert_eq!(run(|ob| ob.set_pnl_enabled(false)), (snapshot.clone(), beat.clone()));
        // Enabled but empty map: still the same bytes.
        assert_eq!(run(|ob| ob.set_pnl_enabled(true)), (snapshot, beat));
        // A set while off is refused, counted, and leaves no trace.
        let mut ob = outbox();
        assert_eq!(ob.set_pnl(&key("A194"), "-62.50", AS_OF).unwrap_err().kind, "pnl_disabled");
        assert_eq!(ob.pnl_len(), 0);
        assert_eq!(ob.counters().pnl_refused, 1);
    }

    fn wire_record_for_test(id: &str) -> String {
        let mut ob = outbox();
        ob.observe(&snap(&[(id, "active")]), "[]", 0, T0).unwrap();
        let env = ob.next_envelope(T0).unwrap().unwrap();
        let start = env.find("\"accounts\":[").unwrap() + "\"accounts\":[".len();
        let end = env.rfind("]},\"events\"").unwrap();
        env[start..end].to_string()
    }

    #[test]
    fn pnl_on_is_a_sorted_list_of_decimal_strings_on_any_envelope() {
        let mut ob = pnl_seeded();
        // Set out of order: the wire is sorted by key.
        ob.set_pnl(&key("B207"), "125", AS_OF).unwrap();
        ob.set_pnl(&key("A194"), "-62.50", AS_OF).unwrap();
        let env = ob.next_envelope(&secs(1)).unwrap().unwrap();
        let want = format!(
            "\"pnl\":[{{\"key\":\"{}\",\"realized_usd\":\"-62.50\",\"as_of\":\"{AS_OF}\"}},{{\"key\":\"{}\",\"realized_usd\":\"125\",\"as_of\":\"{AS_OF}\"}}]",
            key("A194"),
            key("B207")
        );
        assert!(env.contains(&want), "{env}");
        assert!(env.contains("\"events\":[]") && !env.contains("\"snapshot\""), "a heartbeat shape");
        // It rides on a snapshot and on an events envelope too.
        ob.observe(&snap(&[("A194", "closed"), ("B207", "active")]), &evs(&[changed("A194", 3, "closed")]), 3, T0).unwrap();
        ob.on_response(200, "{\"applied_seq\":2}", &secs(1)).unwrap();
        let env = ob.next_envelope(&secs(2)).unwrap().unwrap();
        assert!(env.contains(&want) && env.contains("\"seq\":3"), "{env}");
    }

    #[test]
    fn a_value_change_is_sent_at_once_without_a_seq_and_an_unchanged_value_waits() {
        let mut ob = pnl_seeded();
        ob.set_pnl(&key("A194"), "-62.50", AS_OF).unwrap();
        // The change is due now, long before the 40 s heartbeat.
        let env = ob.next_envelope(&secs(1)).unwrap().unwrap();
        assert!(env.contains("\"-62.50\""));
        let head_before = ob.head_seq();
        assert_eq!(ob.on_response(200, "{\"applied_seq\":2}", &secs(1)).unwrap(), "acked");
        assert_eq!(ob.head_seq(), head_before, "a pnl change takes no seq");
        // Re-setting the same value (even with a new as_of) is not a change: nothing due.
        ob.set_pnl(&key("A194"), "-62.50", "2026-11-15T14:03:41Z").unwrap();
        assert!(ob.next_envelope(&secs(2)).unwrap().is_none());
        // A different value is due on the very next call.
        ob.set_pnl(&key("A194"), "-80.25", "2026-11-15T14:04:11Z").unwrap();
        let env = ob.next_envelope(&secs(3)).unwrap().unwrap();
        assert!(env.contains("\"-80.25\"") && env.contains("2026-11-15T14:04:11Z"));
        // An unacknowledged send stays due (retry-safe).
        assert_eq!(ob.on_response(503, "", &secs(3)).unwrap(), "retry");
        assert!(ob.next_envelope(&secs(4)).unwrap().is_some());
        ob.on_response(200, "{\"applied_seq\":2}", &secs(4)).unwrap();
        assert!(ob.next_envelope(&secs(5)).unwrap().is_none());
        // The unchanged figure keeps riding on the 40 s cadence.
        let env = ob.next_envelope(&secs(44)).unwrap().unwrap();
        assert!(env.contains("\"-80.25\""));
    }

    #[test]
    fn clear_pnl_removes_one_entry_and_a_cleared_list_is_omitted() {
        let mut ob = pnl_seeded();
        ob.set_pnl(&key("A194"), "1", AS_OF).unwrap();
        ob.clear_pnl(&key("A194"));
        ob.clear_pnl(&key("nobody")); // absent: no-op
        let env = ob.next_envelope(&secs(1)).unwrap().unwrap();
        assert!(!env.contains("\"pnl\""), "{env}");
    }

    #[test]
    fn bad_pnl_values_are_refused_with_a_kind() {
        let mut ob = pnl_seeded();
        for bad in ["", "1e3", "NaN", "0x10", "+5", "1,5", " 5", "--1", "1.2.3", "-"] {
            assert_eq!(ob.set_pnl(&key("A194"), bad, AS_OF).unwrap_err().kind, "bad_money", "{bad:?}");
        }
        assert_eq!(ob.set_pnl("nt8:A194", "1", AS_OF).unwrap_err().kind, "bad_key");
        assert_eq!(ob.set_pnl(&key("A194"), "1", "yesterday").unwrap_err().kind, "value");
        assert_eq!(ob.pnl_len(), 0);
        assert_eq!(ob.counters().pnl_refused, 12);
    }

    #[test]
    fn the_pnl_map_holds_at_the_account_cap_and_counts_refusals() {
        let mut ob = outbox();
        ob.set_pnl_enabled(true);
        for i in 0..accounts::MAX_RECORDS + 25 {
            let r = ob.set_pnl(&key(&format!("ACCT-TEST-{i:05}")), "1.5", AS_OF);
            if i < accounts::MAX_RECORDS {
                r.unwrap();
            } else {
                assert_eq!(r.unwrap_err().kind, "cap_exceeded");
            }
        }
        assert_eq!(ob.pnl_len(), accounts::MAX_RECORDS, "size holds at the cap");
        assert_eq!(ob.counters().pnl_refused, 25);
        // An existing key still overwrites at the cap, and does not count as a refusal.
        ob.set_pnl(&key("ACCT-TEST-00000"), "2.5", AS_OF).unwrap();
        assert_eq!(ob.pnl_len(), accounts::MAX_RECORDS);
        assert_eq!(ob.counters().pnl_refused, 25);
        // Removing one frees a slot.
        ob.clear_pnl(&key("ACCT-TEST-00001"));
        ob.set_pnl(&key("ACCT-TEST-99999"), "3", AS_OF).unwrap();
        assert_eq!(ob.pnl_len(), accounts::MAX_RECORDS);
    }

    #[test]
    fn an_account_gone_from_the_snapshot_drops_its_pnl_entry() {
        let mut ob = pnl_seeded();
        ob.set_pnl(&key("A194"), "10", AS_OF).unwrap();
        ob.set_pnl(&key("B207"), "20", AS_OF).unwrap();
        ob.next_envelope(&secs(1)).unwrap().unwrap();
        ob.on_response(200, "{\"applied_seq\":2}", &secs(1)).unwrap();
        let removed = "{\"kind\":\"removed\",\"key\":\"tradovate:B207\",\"display\":\"*207\",\"at\":\"2026-11-11T14:00:00Z\",\"seq\":3,\"record\":null,\"changes\":[]}";
        ob.observe(&snap(&[("A194", "active")]), &evs(&[removed.to_string()]), 3, T0).unwrap();
        assert_eq!(ob.pnl_len(), 1);
        assert_eq!(ob.counters().pnl_dropped, 1);
        // The drop is itself a change that goes out; the dropped entry is absent.
        let env = ob.next_envelope(&secs(2)).unwrap().unwrap();
        assert!(env.contains(&key("A194")) && !env.contains("\"realized_usd\":\"20\""), "{env}");
    }

    #[test]
    fn pnl_is_never_persisted_so_outbox_files_keep_their_shape() {
        let mut ob = pnl_seeded();
        let before = ob.to_json();
        ob.set_pnl(&key("A194"), "10", AS_OF).unwrap();
        assert_eq!(ob.to_json(), before);
        assert!(ob.stats_json().contains("\"pnl_held\":1"));
    }
}
