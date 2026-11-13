//! BP-6b performance and memory gate (design 6.8a, invariant B7).
//!
//! - The IBKR adapter's state for 200 accounts is the one snapshot it keeps, and its
//!   contract form stays under the 2 MB ceiling.
//! - A read over the account port's hard cap (`accounts::MAX_RECORDS`, 2,000) is refused
//!   rather than grown silently; both source shapes are checked.
//!
//! Timing is not asserted here: this slice's budget is memory, and the parser is a
//! single pass over the text. No clock is read.

use te_core::accounts::{snapshot_to_json, MAX_RECORDS};
use te_core::ibkr::snapshot_from_text;
use te_core::ledger::model::{parse_datetime, DateTime};

fn at(text: &str) -> DateTime {
    let (dt, aware) = parse_datetime(text).expect("iso");
    assert!(aware);
    dt
}

fn portal(ids: &[String]) -> String {
    let rows: Vec<String> = ids.iter().map(|id| format!(r#"{{"accountId":"{id}","currency":"USD"}}"#)).collect();
    format!("[{}]", rows.join(","))
}

fn ids(n: usize) -> Vec<String> {
    (0..n)
        .map(|i| if i % 2 == 0 { format!("ACCT-TEST-{i:04}") } else { format!("DUACCT-TEST-{i:04}") })
        .collect()
}

#[test]
fn ibkr_snapshot_meets_the_b7_budget() {
    let now = at("2026-11-11T18:00:00Z");
    assert_eq!(MAX_RECORDS, 2_000);

    // The budget's assumed fleet: 200 accounts, half of them paper.
    let text = portal(&ids(200));
    let snap = snapshot_from_text(&text, &now).expect("200 accounts parse");
    assert_eq!(snap.records.len(), 200);
    assert_eq!(snap.records[0].env, "live");
    assert_eq!(snap.records[1].env, "paper");
    let wire = snapshot_to_json(&snap);
    assert!(
        wire.len() < 2 * 1024 * 1024,
        "200 IBKR accounts serialized to {} bytes, over the 2 MB ceiling",
        wire.len()
    );

    // One past the hard cap is refused in both source shapes, never truncated.
    let over = ids(MAX_RECORDS + 1);
    assert_eq!(snapshot_from_text(&portal(&over), &now).unwrap_err().kind, "cap_exceeded");
    let tws = format!("managedAccounts\t{}\n", over.join(","));
    assert_eq!(snapshot_from_text(&tws, &now).unwrap_err().kind, "cap_exceeded");
}
