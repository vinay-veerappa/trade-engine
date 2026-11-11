# Broker contract (phase 0, frozen wire shapes)

| | |
|---|---|
| Status | Frozen for phases 1, 2 and 4. A change needs a new decision in the design doc. |
| Parent design | [docs/design/BROKER-PLATFORM.md](design/BROKER-PLATFORM.md) (sections 6.1, 6.2, 6.5, 6.8a) |
| Plan | [docs/BROKER_PLATFORM_PLAN.md](BROKER_PLATFORM_PLAN.md), phase 0 |
| Consumers | riskguard ACCT-REGISTRY-1 sub-slice B (`POST /v1/registry/ingest`), Vela |
| Version tag | `broker-account/1` |

Where this file and the design doc disagree, the design doc wins and this file is fixed.

## 1. Instrument JSON (phase 1)

```json
{
  "root": "NQ",
  "month_code": "Z",
  "year": 2026,
  "text": "NQZ6"
}
```

- `text` is the canonical spelling: root, month code, last digit of the year.
- Root facts travel separately, only when asked for:
  `{"root":"NQ","exchange":"CME","tick_size":"0.25","tick_value":"5.00","multiplier":"20","currency":"USD","session":"globex"}`.
  Money and sizes are decimal **strings**, never JSON floats.
- Platform spellings (`NQ 12-26`, `NQZ2026`, `/NQZ26`, ...) never cross this contract.
  They are converted at the edge (design 6.1).
- An unknown root, month or spelling is refused with a `kind`; it is never guessed.

## 2. Account record (phase 2)

This is ACCT-REGISTRY-1 decision 2, unchanged:

```json
{
  "key": "tradovate:APEX12345194",
  "display": "*194",
  "env": "sim|paper|live",
  "status": "active|closed",
  "class": null,
  "firm": null,
  "account_type": "margin|ira|cash|null",
  "routes": [{"platform": "tradingview", "local_name": "D<id>"},
             {"platform": "nt8", "local_name": "<name>"}],
  "source": "tradovate:ntx",
  "confirmed_at": "2026-11-11T18:44:00Z"
}
```

| Field | Rule |
|---|---|
| `key` | `broker:accountId`. Case is kept. An empty id, or one with whitespace, is refused (`bad_key`). The `local:` prefix is riskguard-only and is refused from the engine. |
| `display` | Masked to the last 3 characters. It is the only form that is logged or shown. |
| `class`, `firm` | The engine always sends `null`. riskguard computes these overlays and never pushes them back. A non-null value is refused (`overlay_from_engine`). |
| `api_ref` | **Not part of this contract (D-13).** The broker handle (Tradovate numeric id, Schwab hashValue) stays inside trade-engine. A record carrying `api_ref`, or any other unknown field, is refused (`bad_shape`). |
| `routes[]` | Objects `{platform, local_name}`, at most 8 per record. Platforms: `nt8`, `tradingview`, `tradovate`, `tos`, `ibkr`. For diffing they compare as a set, sorted by `(platform, local_name)`. |
| `confirmed_at` | RFC 3339 UTC, with a `Z`. |

## 3. Account events (phase 2)

```json
{"kind": "added",   "key": "...", "record": { ... }, "seq": 42, "at": "2026-11-11T18:44:00Z"}
{"kind": "changed", "key": "...", "fields": {"status": "closed"}, "seq": 43, "at": "..."}
{"kind": "removed", "key": "...", "seq": 44, "at": "..."}
```

- `seq` is a u64 that rises strictly **per broker**, never globally. It survives restarts, because it is kept in the engine ledger.
- `changed.fields` is a partial record. It may not contain `key`, `class` or `firm`.
- `removed` means that a successful snapshot no longer lists the account. A failed read never emits `removed` (design 6.3).
- `changed.fields` carries the new values only. The engine ledger keeps old and new; the wire sends new.
- The engine ledger stores these same events, `seq` included, with `display` added. `added.record` is stored whole (no `api_ref`, as above), so a restart can rebuild the set from the ledger.

## 4. Ingest envelope (phase 4, `POST /v1/registry/ingest`)

The connector token goes in the `Authorization: Bearer` header. The content type is `application/json`.

```json
{
  "contract": "broker-account/1",
  "broker": "tradovate",
  "confirmed_at": "2026-11-11T18:44:00Z",
  "stale_after_s": 120,
  "snapshot": {"seq": 40, "accounts": [ /* records */ ]},
  "events": [ /* events with seq > snapshot.seq */ ]
}
```

- `snapshot` is optional. When present, it **replaces** that broker's whole set as of `snapshot.seq`. The engine sends one on its first contact, after every reconnect, and whenever riskguard asks for one.
- `events` may be empty. An envelope with no snapshot and no events is a **heartbeat**: it refreshes `confirmed_at` only. The engine sends one at least every `stale_after_s / 3`.
- `confirmed_at` and `stale_after_s` set that broker's freshness. riskguard computes no freshness of its own.

### Apply rules

1. riskguard sorts the events in the envelope by `seq` and applies them in that order.
2. An event with `seq` ≤ the broker's applied seq is a no-op. This makes redelivery safe.
3. If the first new `seq` is more than applied+1, there is a **gap**. riskguard applies nothing from the envelope and answers `409 {"need": "snapshot", "applied_seq": N}`. The engine then resends with a snapshot.
4. Apply is all-or-nothing per envelope. A refused record refuses the whole envelope.

### Responses

| Code | Body | Engine action |
|---|---|---|
| 200 | `{"applied_seq": N}` | advance the outbox to N |
| 409 | `{"need": "snapshot", "applied_seq": N}` | resend with a snapshot |
| 400 | `{"kind": "bad_key" \| "overlay_from_engine" \| "bad_shape" \| "unknown_contract"}` | do not retry; alert |
| 401 | none | do not retry; alert |
| 413 | `{"kind": "too_large" \| "cap_exceeded"}` | do not retry; alert |

## 5. Caps (design 6.8a)

| Item | Cap | Over the cap |
|---|---|---|
| Request body | 1 MB | 413 `too_large` |
| Accounts per broker snapshot | 2,000 | 413 `cap_exceeded`; the last good set is kept |
| Events per envelope | 1,000 | 413 `too_large`; the engine splits the batch |
| `routes` per record | 8 | 400 `bad_shape` |
| riskguard ingest handling | < 5 ms p99 at N=200 | bench in G2 |
| Gate read | < 1 ms; it reads a pointer-swapped immutable snapshot | test in riskguard B |

## 6. Privacy

- Log lines and `/v1/accounts` responses carry only `display`. Events and the ledger carry `key`, which they need to identify the account, and never `api_ref`.
- `api_ref` never leaves trade-engine (D-13). Inside the engine it is excluded from `repr`, logs and serialization.
- The token is never logged by either side.
