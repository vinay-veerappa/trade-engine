//! BP-1 / invariant B7: the budget for the futures map (`docs/design/BROKER-PLATFORM.md`
//! 6.8a, `docs/briefs/BP-1-FUTURES.md`).
//!
//! | Budget | Enforced here |
//! |---|---|
//! | Lookup under 1 microsecond | 1 M lookups stay under 1 s in any build (a debug build is about 10x slower than optimized, so the same loop is held to 50 ms when compiled optimized) |
//! | 1 M lookups under 50 ms | the same loop, `cfg!(debug_assertions) == false` |
//! | No allocation on a hit | the global allocator counter reads 0 across 1 M hits |
//! | Returns borrowed data | pointer identity with the static row |
//! | Static table under 64 KB | row structs plus their string literals |
//!
//! The map is static data: built at compile time, never grown, no cache and no memo map,
//! so there is nothing here to cap or drop.

use std::alloc::{GlobalAlloc, Layout, System};
use std::hint::black_box;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use te_core::sim::tick::{spec_row, StaticFutureSpec, FUTURE_SPECS};

/// Counts allocations so a hit can be proven allocation-free.
struct Counting;

static ALLOCATIONS: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
        System.alloc(layout)
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
        System.alloc_zeroed(layout)
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
        System.realloc(ptr, layout, new_size)
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        System.dealloc(ptr, layout)
    }
}

#[global_allocator]
static ALLOCATOR: Counting = Counting;

const LOOKUPS: usize = 1_000_000;

fn table_bytes() -> usize {
    let rows = std::mem::size_of::<StaticFutureSpec>() * FUTURE_SPECS.len();
    let literals: usize = FUTURE_SPECS
        .iter()
        .map(|s| {
            s.root.len()
                + s.exchange.len()
                + s.currency.len()
                + s.tick_size_str.len()
                + s.point_value_str.len()
                + s.tick_value_str.len()
        })
        .sum();
    rows + literals
}

#[test]
fn futures_map_meets_the_b7_budget() {
    // Every root, uppercase, as the symbol map passes it (the hit path).
    let roots: Vec<&str> = FUTURE_SPECS.iter().map(|s| s.root).collect();
    assert_eq!(roots.len(), 12);

    // Warm the code path before measuring.
    for root in &roots {
        assert!(spec_row(black_box(root)).is_some());
    }

    let before = ALLOCATIONS.load(Ordering::Relaxed);
    let start = Instant::now();
    let mut sink = 0usize;
    for i in 0..LOOKUPS {
        let row = black_box(spec_row(black_box(roots[i % roots.len()])));
        sink += row.map(|r| r.root.len()).unwrap_or(0);
    }
    let elapsed = start.elapsed();
    let after = ALLOCATIONS.load(Ordering::Relaxed);

    assert!(sink > 0, "the loop must consume every hit");
    println!(
        "{LOOKUPS} lookups in {elapsed:?} ({:.0} ns each), {} allocations, table {} bytes",
        elapsed.as_nanos() as f64 / LOOKUPS as f64,
        after - before,
        table_bytes()
    );

    // 6.8a: "Lookup < 1 microsecond". One microsecond each is one second for the loop.
    assert!(
        elapsed < Duration::from_secs(1),
        "lookup budget (1 us each) broken: {LOOKUPS} lookups took {elapsed:?}"
    );
    // 6.8a: "Bench: 1 M lookups < 50 ms", which is the same loop at optimized speed.
    if !cfg!(debug_assertions) {
        assert!(
            elapsed < Duration::from_millis(50),
            "optimized bench budget broken: {LOOKUPS} lookups took {elapsed:?}"
        );
    }
    // "no allocation on a hit (returns borrowed data)".
    assert_eq!(after, before, "a hit must not allocate");
    // The hit is the static row itself, not a copy.
    assert!(std::ptr::eq(spec_row("NQ").unwrap(), &FUTURE_SPECS[0]));
    assert!(std::ptr::eq(spec_row("mnq").unwrap(), &FUTURE_SPECS[1]));
    // "Static table < 64 KB" and it never grows: FUTURE_SPECS is a `const` slice.
    assert!(
        table_bytes() < 64 * 1024,
        "static futures table is {} bytes, budget 65536",
        table_bytes()
    );
}
