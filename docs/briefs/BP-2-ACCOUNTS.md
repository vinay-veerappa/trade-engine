Parent design: trade-engine/docs/design/BROKER-PLATFORM.md

# BP-2: account port on BrokerAdapter (plan phase 2)

Read BP-COMMON.md first. Phase 3 (the Tradovate adapter) waits for this phase. This phase has no network code.

## What to build

1. **Protocol.** In `src/trade_engine/interfaces/broker.py` add to `BrokerAdapter`:
   - `accounts() -> AccountSnapshot`: the full account list for this broker, one `AccountRecord` per account.
   - `account_events(since: datetime) -> list[AccountEvent]`: `added | removed | changed`.

   Carriers are frozen dataclasses like the existing `Venue*` ones. `AccountRecord` fields (the registry record, ACCT-REGISTRY-1 decision 2): `key`, `display`, `env`, `status`, `class_`, `firm`, `account_type`, `api_ref`, `routes` (a tuple of `Route(platform, local_name)`, at most 8), `source`, `confirmed_at`. `class_` and `firm` are overlays riskguard computes: this side always sends `null` and never guesses. `api_ref` is engine-internal (D-13): `field(repr=False)`, and the contract serializer (`docs/BROKER_CONTRACT.md` section 2) omits it. Update every existing adapter and test double so the protocol contract test passes.
2. **`te_core::accounts` (Rust, one implementation for all adapters), exposed through pyo3:**
   - **Key rule:** `broker:accountId`, the id's case kept; an empty id or any whitespace is refused with a `kind`.
   - **Masking rule:** `display(account_id)` is `*` plus the last 3 characters; an id shorter than 3 shows `*` plus what exists. Nothing else about the id is ever shown.
   - **Diff rule:** `diff(old_snapshot, new_snapshot, now) -> Vec<AccountEvent>`. added = in new only; removed = in old only; changed = same key and any of `status`, `env`, `account_type`, `routes` differs (ignore `confirmed_at`; `routes` compare as a set sorted by `(platform, local_name)`). Deterministic order (sorted by key). Events follow contract section 3: `seq` (per broker, rising strictly), `kind`, `key`, `display`, `at`; `added` carries the record without `api_ref`; `changed` carries the changed field names with old and new values. `api_ref` never appears in an event.
   - **Freshness:** a snapshot has `confirmed_at`; `is_stale(snapshot, now, stale_after)` with default `stale_after` 120 s (per-broker argument). Time is always an argument (I7).
3. **Poll-and-diff helper** (a Python I/O shell around the Rust rule) for brokers that cannot push: poll interval minimum 30 s (a smaller value is refused), default 60 s. On ANY error the last good snapshot is kept and NO event is emitted (B3). Events come only from a successful read. The first successful read after start diffs against the membership the ledger already holds (replay), or yields `added` for every account if the ledger holds none.
4. **Ledger.** Account events are appended to the engine ledger (I1, I2) as new event kinds, and replay on restart rebuilds the membership so a restart does not start empty. Follow how existing event kinds are added (`ledger/model.rs`, codec, fold, the JSON schema, and the schema/enum drift checks). Ledger events are the events above, `seq` included, so the seq survives a restart. They never carry `api_ref`.
5. **`tos_paper`** implements `accounts()` trivially: its fixed list comes from `mirror.json` (read the file where the adapter already finds it; each entry becomes one record with `env: paper`, `source: tos_paper`, `status: active`). `account_events` returns events from the poll helper's diff.

## Tests (the enforcing tests; name them in the RUST_PORT.md row)

- Snapshot to add, remove, change, and nothing-changed: exact events, in order.
- A failed poll emits nothing and keeps the list; a later good poll diffs against the last good one, not an empty list.
- A restart replays membership from the ledger (write events, rebuild, compare).
- Interval below 30 s refused; staleness detected at exactly `stale_after` and just under it.
- Key rule: case kept, whitespace refused, empty refused. Masking rule: every case above.
- No `api_ref` in any event, ledger record, contract serialization, `repr` or captured log line; no account id other than `display` in any captured log line (scans using a fake id and a fake api_ref).
- Contract-shape fixture: a record and each event kind serialize to exactly the shapes in `docs/BROKER_CONTRACT.md` sections 2 and 3 (fake id).
- A contract test that every registered adapter satisfies the protocol, including `accounts` and `account_events`.

## Performance and memory budget (design 6.8a, invariant B7)

Rows copied from `docs/design/BROKER-PLATFORM.md` 6.8a; the general rules are in BP-COMMON. Sizes assume 200 accounts; the hard cap is 2,000.

| Component | Latency / CPU budget | Memory ceiling | How it stays bounded | Enforcing test (add before you report done) |
|---|---|---|---|---|
| Account registry in engine (P2) | Diff of N=200 under 1 ms, O(N) by key | Under 256 KB for 200 records (about 1 KB each) | Hard cap 2,000 records: above it, ingest is refused and alerted (a `kind`), never grown silently | Cap test; diff bench at N=200 and N=2,000 |
| Account event ring (P2) | Append O(1) | 10,000 events or 24 h, whichever comes first | Ring buffer; the ledger on disk is the history | Fill past the cap, check size and oldest-drop |

- The poll-and-diff helper keeps exactly one last-good snapshot, replaced on each good read, never appended.
- The G3 report records RSS and CPU before and after, while flat (engine account plane budget: at most +20 MB).
