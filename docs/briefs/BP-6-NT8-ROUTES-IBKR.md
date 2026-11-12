Parent design: trade-engine/docs/design/BROKER-PLATFORM.md

# BP-6: NT8 Desktop routes and the IBKR accounts adapter (plan phase 6)

Read BP-COMMON.md first, then plan phase 6, `te_core::accounts` and BP-3 (alias rule:
numeric, `D<id>`, name). Two sub-slices, two commits. Read-only: **no order placement
through any adapter** (B6; each broker needs its own owner-approved brief).

## 6a: NT8 Desktop as a route (trade-engine)

1. NT8 is a platform, not a broker. There is no NT8 `BrokerAdapter`.
2. A pure function maps an NT8 account name to a broker key with the phase 3 alias
   rule: an NT8 Tradovate-connected account (prop firms such as APEX, LUCID, TPT) →
   `tradovate:<id>`, giving the record a route `{platform: "nt8", local_name}`. An
   NT8-local account (Sim101, Playback, SimFanNN and any account with no broker
   behind it) → `local:nt8:<name>`. That key is riskguard-only and never enters the
   engine's broker set; check the existing `local:` refusal in `key()`.
3. Input: the NT8 account list as the bridge returns it, given as text (fixture
   captured from the existing bridge account-list JSON shape, with ids faked). Add no
   live read.
4. A name that matches no alias and is not a known local pattern → refused
   (`unknown_nt8_account`, I5), never guessed.

## 6b: IBKR accounts adapter (trade-engine)

1. Adapter on the phase 2 port. Source shapes: TWS or IB Gateway
   `managedAccounts` + `reqAccountUpdates` (account value messages), or Client
   Portal `GET /portfolio/accounts`. Never Flex. Both shapes are parsed from text and
   checked with fixtures. There is no socket or HTTP client in te_core (I7, D3); the
   I/O shell is a stub in Python that takes the text.
2. Key `ibkr:<accountId>`. Paper accounts (`DU...`) → `env: paper`.
3. Off by default, behind a switch. Nothing connects to TWS in this slice.

## Tests

- 6a: a table test of NT8 names → key or local key or refusal (at least 10 rows,
  covering each firm prefix seen in the riskguard firm map, Sim101, Playback and a
  SimFan name).
- 6b: fixture round trip for both source shapes; paper/live env; cap test; unknown
  shape → refused with a `kind`.
- Mutant for each rule added.

## Performance and memory budget (B7)

| Component | Budget | Bound | Enforcing test |
|---|---|---|---|
| NT8 name map | < 50 µs per name | pure, no state | bench in the table test |
| IBKR adapter state | < 2 MB / 200 accounts | `MAX_RECORDS` | cap test |
