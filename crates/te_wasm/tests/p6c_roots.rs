//! P6C: the wasm book follows the same per-root calendar as te_core. `SimBook.newFutures(account, slippage)` is
//! unchanged: the root is derived from each order's / bar's instrument, so one book serves every root.

use te_wasm::api::SimBookApi;

fn bar(instr: &str, ts: &str) -> String {
    serde_json::json!({
        "instr": instr, "ts": ts, "open": "80.00", "high": "81.00", "low": "79.00", "close": "80.00",
        "volume": "100", "as_of": ts,
    })
    .to_string()
}

/// 2024-07-04: energy halts 13:30 ET (17:30 UTC), equity 13:00 ET (17:00 UTC).
#[test]
fn bars_follow_the_instruments_root() {
    let mut cl = SimBookApi::new_futures("ACC", 0).unwrap();
    cl.connect("2024-07-04T14:00:00+00:00").unwrap();
    cl.process_bar(Some(&bar("CL", "2024-07-04T17:15:00+00:00"))).unwrap();
    let e = cl.process_bar(Some(&bar("CL", "2024-07-04T17:30:00+00:00"))).unwrap_err();
    assert_eq!(e.kind, "missing_bar");

    let mut nq = SimBookApi::new_futures("ACC", 0).unwrap();
    nq.connect("2024-07-04T14:00:00+00:00").unwrap();
    let e = nq.process_bar(Some(&bar("NQ", "2024-07-04T17:15:00+00:00"))).unwrap_err();
    assert_eq!(e.kind, "missing_bar");

}

#[test]
fn unknown_root_still_refuses() {
    let mut b = SimBookApi::new_futures("ACC", 0).unwrap();
    b.connect("2024-07-04T14:00:00+00:00").unwrap();
    assert!(b.process_bar(Some(&bar("ZN", "2024-07-04T15:00:00+00:00"))).is_err());
}
