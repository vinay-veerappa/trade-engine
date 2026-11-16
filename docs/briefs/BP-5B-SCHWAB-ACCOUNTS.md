Parent design: trade-engine/docs/design/BROKER-PLATFORM.md

# BP-5b: Schwab Trader API accounts adapter (plan phase 5b)

**Status 2026-11-16: merged.** Half 1 (engine) is on trade-engine main (38c3d7f, merge b1db8e9). Half 2 (hub) is on tvDownloadOHLC main (6786a8866, merge 1bd792cbc), live since the hub restart 2026-11-15 ~20:18 local (per nt8-riskguard `docs/product/11_REMAINING_WORK.md`).

Read BP-COMMON.md first, then plan phase 5, `te_core::accounts` (the port, the key,
mask and diff rules) and BP-3 (the Tradovate adapter is the model to copy).

Two halves, two repos, two commits. **Neither is deployed by this brief.**

## Half 1: engine adapter (trade-engine)

1. A Schwab accounts adapter on the phase 2 port. `accountNumber` is the account id
   (key `schwab:<accountNumber>`). `hashValue` is the broker's `api_ref` and stays in
   the adapter (D-13); it never reaches a `Record`, an event, a log or the outbox.
2. Input is the JSON the hub returns for `GET /trader/v1/accounts/accountNumbers` plus
   `GET /trader/v1/accounts` (fields only). The adapter takes the response text as an
   argument and has no HTTP client of its own (D3: the hub is the only Schwab client).
   Fixtures are hand-written with fake ids (`ACCT-TEST-...`).
3. Poll-and-diff every 60 s (`DEFAULT_INTERVAL_SECONDS`) through the existing
   snapshot/diff, plus an event entry point for ACCT_ACTIVITY messages that only marks
   "re-poll now". The account set comes from the poll, never from activity content.
4. The adapter feeds the 4a outbox with broker `schwab`. It is behind a switch that
   is off by default.

## Half 2: hub passthrough (tvDownloadOHLC, merged to main)

1. `scripts/streaming/schwab_hub.py`: add **only** the two accounts endpoints above to
   the REST allowlist, through the existing budgeted REST lane (the shared 120/min).
   Add no new streamer: Schwab allows one per account, and a second one drops the
   market feed.
2. ACCT_ACTIVITY: subscribe on the hub's existing streamer. Forward each message as an
   "accounts changed" notification. If the existing streamer cannot take the
   subscription without a reconnect, stop and report; don't work around it.
3. Tests with the hub's existing fakes: allowlist accepts exactly the two new paths and
   still refuses everything else; the 120/min budget is shared, not doubled.
4. Merged to main (status 2026-11-16). The owner deploys hub changes outside
   market hours and checks the feed afterwards (owner gate).

## Tests

- Fixture round trip: accounts JSON → records (masked display, no `hashValue` anywhere
  in the output; assert with a string search).
- Diff: an added, a closed, or a changed account produces the right events. The
  activity hint triggers one re-poll and adds no account.
- Unknown or missing `accountNumber` → refused with a `kind` (I5).
- Cap test at `MAX_RECORDS`.

## Performance and memory budget (B7)

| Component | Budget | Bound | Enforcing test |
|---|---|---|---|
| Schwab poll | ≤ 2 REST calls/min of the shared 120/min | 60 s floor, hint debounced to ≤ 1 re-poll / 10 s | debounce test |
| Adapter state | < 2 MB for 200 accounts | `MAX_RECORDS` cap | cap test |
