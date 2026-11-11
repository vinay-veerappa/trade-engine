# BP-COMMON: rules for every BROKER_PLATFORM brief

Read `docs/BROKER_PLATFORM_PLAN.md` (your phase and the invariants B1-B6) and the
"Working rules" section of `docs/RUST_PORT.md` first.

- Work in your worktree. NEVER push, never merge, never touch another branch. Commit once on your branch when done.
- D2: Rust lives in `crates/` (one pyo3 module, `trade_engine_rs`). D3: no second reader of anything; Python carries data and does I/O only. A rule lives once, in `te_core`. D6: money is `rust_decimal`/`Money` with `checked_*`, and crosses the boundary as strings.
- I7: no clock read under `crates/*/src` (time arrives as an argument). No float for money or ticks.
- Refuse, never approximate (I5): an unknown root, month, spelling or account key is an error with a `kind`.
- Mask every account id outside the registry (`display`, last 3 characters, e.g. `*194`). No full id in any log line, event, test name or commit message. Use fake ids such as `ACCT-TEST-194` in fixtures.
- Build and test: `cargo test --manifest-path crates/Cargo.toml -p te_core` for Rust; `python -m pip install --no-deps --force-reinstall ./crates/te_py` for the pyo3 module; then `python tools/ci_local.py`. Use `-j2` for cargo. The known pre-existing failure `tests/test_p4c_flip.py` is not yours to fix.
- Add a hand mutant check for each rule you add (change one line, see a test fail, restore it) and say which in the commit message.
- Update the `docs/RUST_PORT.md` status table only with a row that names your enforcing test.
- Do not read from or write to the live riskguard install, NT8, or any broker. Tests use fixtures only.
- When done, delete your cargo target directory if it sits inside the worktree (they are 3-12 GB).
- Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Final reply: at most 8 lines: branch, sha, test counts, anything you could not do.
