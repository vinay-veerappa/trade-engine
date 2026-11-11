//! BP-2 performance and memory gate (design 6.8a, invariant B7).
//!
//! - The registry diff is O(N) by key: N=200 is under 1 ms; N=2,000 (the hard cap) is
//!   benched too and stays far under it.
//! - The carrier/wire form of 200 records is under the 256 KB ceiling (about 1 KB each).
//! - The event ring holds 10,000 events or 24 h, dropping the oldest and counting drops.
//!
//! Timing lives here, outside `crates/*/src`, so the I7 clock-read rule stays intact.

use std::time::Instant;

use te_core::accounts::{
    diff, snapshot_to_json, Event, EventRing, Record, Route, Snapshot, MAX_RECORDS, RING_CAPACITY,
};
use te_core::ledger::model::{parse_datetime, DateTime};

fn at(text: &str) -> DateTime {
    let (dt, aware) = parse_datetime(text).expect("iso");
    assert!(aware);
    dt
}

fn record(i: usize, status: &str) -> Record {
    let key = format!("tos_paper:ACCT-TEST-{i:04}");
    let id = key.split_once(':').expect("key").1;
    Record {
        key: key.clone(),
        display: format!("*{}", &id[id.len().saturating_sub(3)..]),
        env: "paper".into(),
        status: status.into(),
        class_: None,
        firm: None,
        account_type: Some("margin".into()),
        routes: vec![
            Route { platform: "tos".into(), local_name: format!("local-{i:04}") },
            Route { platform: "tradingview".into(), local_name: format!("D{i:04}") },
        ],
        source: "tos_paper".into(),
        confirmed_at: at("2026-11-11T18:00:00Z"),
    }
}

fn snapshot(n: usize, status: &str) -> Snapshot {
    Snapshot {
        records: (0..n).map(|i| record(i, status)).collect(),
        confirmed_at: Some(at("2026-11-11T18:00:00Z")),
    }
}

#[test]
fn the_diff_at_two_hundred_records_is_under_one_millisecond() {
    let old = snapshot(200, "active");
    let mut new = snapshot(200, "active");
    // One change and one addition: the diff still walks every key, as the budget assumes.
    new.records[7].status = "closed".into();
    new.records[9].routes = vec![Route { platform: "tos".into(), local_name: "local-0009".into() }];
    new.records.push(record(500, "active"));
    let now = at("2026-11-11T18:01:00Z");

    // Warm the code once, then time a batch and take the per-diff average.
    let events = diff(Some(&old), &new, &now, 1);
    assert_eq!(events.len(), 3);
    let runs = 100;
    let start = Instant::now();
    for _ in 0..runs {
        std::hint::black_box(diff(Some(&old), &new, &now, 1));
    }
    let per_diff = start.elapsed() / runs as u32;
    assert!(
        per_diff.as_micros() < 1_000,
        "diff of N=200 took {per_diff:?}, over the 1 ms budget"
    );
}

#[test]
fn the_diff_at_the_two_thousand_record_cap_stays_linear_and_fast() {
    assert_eq!(MAX_RECORDS, 2_000);
    let old = snapshot(MAX_RECORDS, "active");
    let new = snapshot(MAX_RECORDS, "closed");
    let now = at("2026-11-11T18:01:00Z");
    let runs = 10;
    let start = Instant::now();
    for _ in 0..runs {
        std::hint::black_box(diff(Some(&old), &new, &now, 1));
    }
    let per_diff = start.elapsed() / runs as u32;
    assert!(
        per_diff.as_millis() < 50,
        "diff of N={MAX_RECORDS} took {per_diff:?}, far over an O(N) budget"
    );
}

#[test]
fn two_hundred_records_fit_under_the_256_kb_ceiling() {
    let text = snapshot_to_json(&snapshot(200, "active"));
    assert!(
        text.len() < 256 * 1024,
        "200 records serialized to {} bytes, over the 256 KB ceiling",
        text.len()
    );
}

#[test]
fn the_ring_fills_past_the_cap_and_drops_the_oldest() {
    let mut ring = EventRing::new();
    let now = at("2026-11-11T19:00:00Z");
    let events: Vec<Event> = (0..RING_CAPACITY + 5)
        .map(|i| Event {
            kind: "added",
            key: format!("tos_paper:ACCT-TEST-{i:05}"),
            display: format!("*{:03}", i % 1000),
            at: at("2026-11-11T18:00:00Z"),
            seq: i as u64 + 1,
            record: None,
            changes: Vec::new(),
        })
        .collect();
    ring.push(events, &now);
    assert_eq!(ring.len(), RING_CAPACITY);
    assert_eq!(ring.dropped(), 5);
    assert_eq!(ring.oldest().expect("oldest").key, "tos_paper:ACCT-TEST-00005");
}
