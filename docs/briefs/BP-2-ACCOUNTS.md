# BP-2: account port on BrokerAdapter (plan phase 2)

Read BP-COMMON.md first. Phase 3 (the Tradovate adapter) waits for this phase. This phase has no network code.

## What to build

1. **Protocol.** In `src/trade_engine/interfaces/broker.py` add to `BrokerAdapter`:
   - `accounts() -> AccountSnapshot`: the full account list for this broker, one `AccountRecord` per account.
   - `account_events(since: datetime) -> list[AccountEvent]`: `added | removed | changed`.

   Carriers are frozen dataclasses like the existing `Venue*` ones. `AccountRecord` fields (the registry record, ACCT-REGISTRY-1 decision 2): `key`, `display`, `env`, `status`, `class_`, `firm`, `account_type`, `api_ref`, `routes` (platform names), `source`, `confirmed_at`. `class_` and `firm` are overlays riskguard computes: this side always sends `null` and never guesses. Update every existing adapter and test double so the protocol contract test passes.
2. **`te_core::accounts` (Rust, one implementation for all adapters), exposed through pyo3:**
   - **Key rule:** `broker:accountId`, the id's case kept; an empty id or any whitespace is refused with a `kind`.
   - **Masking rule:** `display(account_id)` is `*` plus the last 3 characters; an id shorter than 3 shows `*` plus what exists. Nothing else about the id is ever shown.
   - **Diff rule:** `diff(old_snapshot, new_snapshot, now) -> Vec<AccountEvent>`. added = in new only; removed = in old only; changed = same key and any of `status`, `env`, `account_type`, `routes` differs (ignore `confirmed_at`). Deterministic order (sorted by key). Event payloads carry `key` and `display` only, never the raw id; for `changed`, the changed field names plus old and new values of those non-id fields.
   - **Freshness:** a snapshot has `confirmed_at`; `is_stale(snapshot, now, stale_after)` with default `stale_after` 120 s (per-broker argument). Time is always an argument (I7).
3. **Poll-and-diff helper** (a Python I/O shell around the Rust rule) for brokers that cannot push: poll interval minimum 30 s (a smaller value is refused), default 60 s. On ANY error the last good snapshot is kept and NO event is emitted (B3). Events come only from a successful read. The first successful read after start diffs against the membership the ledger already holds (replay), or yields `added` for every account if the ledger holds none.
4. **Ledger.** Account events are appended to the engine ledger (I1, I2) as new event kinds, and replay on restart rebuilds the membership so a restart does not start empty. Follow how existing event kinds are added (`ledger/model.rs`, codec, fold, the JSON schema, and the schema/enum drift checks). The ledger carries `key` and `display` only.
5. **`tos_paper`** implements `accounts()` trivially: its fixed list comes from `mirror.json` (read the file where the adapter already finds it; each entry becomes one record with `env: paper`, `source: tos_paper`, `status: active`). `account_events` returns events from the poll helper's diff.

## Tests (the enforcing tests; name them in the RUST_PORT.md row)

- Snapshot to add, remove, change, and nothing-changed: exact events, in order.
- A failed poll emits nothing and keeps the list; a later good poll diffs against the last good one, not an empty list.
- A restart replays membership from the ledger (write events, rebuild, compare).
- Interval below 30 s refused; staleness detected at exactly `stale_after` and just under it.
- Key rule: case kept, whitespace refused, empty refused. Masking rule: every case above.
- No full id in any event, ledger record or captured log line (a scan over captured logs and serialized events using a fake id).
- A contract test that every registered adapter satisfies the protocol, including `accounts` and `account_events`.
