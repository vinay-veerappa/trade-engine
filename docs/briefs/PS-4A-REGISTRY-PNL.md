Parent design: nt8-riskguard/docs/design/PNL-SOURCE-1.md (§6 wire shapes, §9 slice PS-4, §10 Q1/Q3). Standing rules: `BP-COMMON.md`. Builds on BP-4a (`src/trade_engine/sinks/riskguard_registry.py`, `crates/te_core/src/registry_outbox.rs`).

# PS-4a: the registry sink carries each account's broker realized P&L

## Goal
riskguard gets a third realized-P&L source: the broker's own figure. ntx already gives the engine one running realized figure per account (`TradovateAdapter.realized_pnl`, `src/trade_engine/tradovate/adapter.py` ~394). This slice makes the registry sink send it on the existing `POST /v1/registry/ingest`. It only measures, and it adds no new route and no new connection.

## Decisions (made; do not reopen)
- **Q1.** Every account the ntx daemon reports is covered. There is no hard-coded account list. An account whose figure is None (another day, no trading-day mapping, or unknown) is simply left out.
- **Q3** (whether Lucid's ntx realized includes fees) does not block this slice. riskguard's PS-6 checks it before anything selects the broker figure.
- **Off by default, behind its own switch.** riskguard's `IngestRequest` is `deny_unknown_fields` today, so a `pnl` field would be refused until the riskguard side (PS-4b) ships. Add `TE_RISKGUARD_REGISTRY_PNL` (only `on` enables it). With it off, the envelopes are byte-identical to today's. Prove that with a test.

## Wire (top-level, optional, on any envelope including heartbeats)
```json
"pnl": [{"key": "tradovate:ACCT-TEST-194", "realized_usd": "-62.50", "as_of": "2026-11-15T14:03:11Z"}]
```
- `key` is the registry key already used in the snapshot.
- `realized_usd` is a decimal string (D6: never a float).
- `as_of` is the daemon's `as_of_utc`.
- The list holds the latest value per account, sorted by key, at most one entry per account.

## Do
1. **Rule in Rust (D3).** In `te_core::registry_outbox`, keep the latest pnl per account.
   - `set_pnl(key, realized, as_of)` overwrites. Use a map capped at the existing account cap: refuse a new key past the cap with a `kind`, and count it.
   - A key that is no longer in the current snapshot set is dropped on the next snapshot.
   - `next_envelope` attaches the `pnl` list only when the pnl switch is on and the map is non-empty.
   - A pnl change alone does not create a new seq. It rides on the next envelope or heartbeat. Find the heartbeat cadence and say in a comment how stale the value can get. riskguard treats it as stale after 10 s (design §6), so if the heartbeat is slower, a changed value has to trigger a send without a seq. Choose the smallest change that does this and justify it in the commit message.
   - Expose the new calls through `te_py`.
2. **Python I/O.** Wherever the account poller calls `sink.observe(...)`, also pass the ntx realized values for the accounts in that snapshot. Use the adapter's `realized_pnl(key, day)` with the same trading-day mapping it already uses. The sink forwards them to Rust. Python decides nothing.
3. **Config.** Add `RegistrySinkConfig.pnl_enabled` from `TE_RISKGUARD_REGISTRY_PNL` and include it in `__repr__`. The switch is ignored when the sink itself is off.
4. **Do not touch:** riskguard (another repo), NT8, any live daemon, the token handling or the ack rules.

## Tests
- **Off:** the envelopes are byte-identical to before for the same inputs (a golden test).
- **On:** two accounts give a sorted list with decimal strings. An account with None is absent. A value change shows up on the next heartbeat.
- **Cap:** fill past the account cap. The map size holds and the refusal counter counts.
- **Snapshot drop:** an account removed from the snapshot drops its pnl entry.
- **Masking:** no full id appears in any log line.
- Existing `tests/test_bp4a_registry_sink.py` and the te_core outbox tests stay green. Never weaken an assertion.
- Run the hand mutant check required by BP-COMMON.

## Performance and memory budget (B7)
- The map is bounded by the account cap: one Decimal plus one timestamp per account. Say so in a comment.
- `observe` stays memory-only under the existing lock, so nothing new waits on the network.

## Docs
- Add a `docs/RUST_PORT.md` row naming the enforcing test.
- Add a "pnl (PS-4a)" note to `docs/BROKER_CONTRACT.md` §2/§ingest describing the optional field.

One commit: `feat(registry): PS-4a send ntx realized P&L on registry ingest (default off)`.
