Parent design: trade-engine/docs/RUST_PORT.md (P6 row, "What retire Python means")

# P6-retire: take the Python engine logic out of service, in place

Read `docs/RUST_PORT.md` lines 1-40 ("What retire Python means", ruled 2026-10-17) and
the P6 row, the P7 amendment (2026-10-07: the owner kept the Python "a few more weeks";
that window is over, and this brief is the go). BP-COMMON working rules apply (worktree,
one commit, no push, trailer, delete the cargo target).

**Retire = keep the code at its current path as reference, take it out of service.**
Never delete or move a file.

## Steps

1. **Inventory.** List every module under `src/trade_engine/` that still holds engine
   logic that is now done in Rust (`trade_engine_rs`): ledger fold, OMS, rules,
   runtime, books, and anything else. For each one, name its live importers inside
   trade-engine, in `tvDownloadOHLC` (`scan_engine` and anything else under
   `C:\Users\vinay\tvDownloadOHLC`), and in tools/launch scripts. Read-only on
   tvDownloadOHLC.
2. **Keep the client API.** The names `scan_engine` (and any other tvDownloadOHLC
   caller) import stay importable with the same signatures. They become thin wrappers
   that call `trade_engine_rs` (or they already are; prove it).
3. **Rewire.** Each live path that still runs Python logic is switched to Rust,
   unless a parity gate for it is missing. If one is missing, stop for that module
   and report it; do not write a new oracle in this slice.
4. **Mark.** Each retired module gets a one-line header: `# RETIRED (P6): reference
   only; live path is trade_engine_rs::<x>. Do not import.`
5. **Gate.** Add a test (`tests/test_p6_retired_imports.py`) that imports the live entry
   points (daemon, CLI, the `scan_engine` client API) in a fresh interpreter. Assert
   that no retired module is in `sys.modules` afterwards. Tests and oracle tools may
   still import them; list those exceptions in the test.
6. Update the P6 row in `docs/RUST_PORT.md`: it becomes **done**, and the row names
   the gate test.

## Tests

- `python tools/ci_local.py` green. The known `tests/test_p4c_flip.py` failure is not
  yours. State the counts before and after.
- `cargo test --manifest-path crates/Cargo.toml -j2` green.
- Mutant: add an import of one retired module to a live entry point → the gate fails.

## Not in this brief

- Restarting any running daemon or quantd child. The owner runs with the p4c owner
  PIDs. Never stop processes by image name.
