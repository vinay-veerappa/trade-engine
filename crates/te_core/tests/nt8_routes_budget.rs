//! BP-6a performance gate (design 6.8a, invariant B7).
//!
//! The NT8 name map is pure and stateless; one lookup must stay under 50 µs per name.
//! Timing lives outside `crates/*/src`, so the I7 clock-read rule stays intact.

use std::time::Instant;

use te_core::nt8_routes::map_account;

#[test]
fn the_nt8_name_map_meets_the_b7_budget() {
    for name in ["APEX-ACCT-TEST-194", "TAKEPROFITPRO-ACCT-TEST-196", "Sim101", "Playback101"] {
        let runs = 10_000u32;
        // Warm once, then time the batch and take the per-name average.
        std::hint::black_box(map_account(name).expect("mapped"));
        let start = Instant::now();
        for _ in 0..runs {
            std::hint::black_box(map_account(name).expect("mapped"));
        }
        let per = start.elapsed() / runs;
        assert!(per.as_micros() < 50, "{name}: {per:?} per name, over the 50 µs budget");
    }
}
