# BP-5e: an ACCOUNTS_CHANGED frame on the hub's /ws triggers an early Schwab re-read

Read `BP-COMMON.md` first. Repo `trade-engine`. Work in your own worktree on a branch off `main`.

## Why
The Schwab half of `registry-feed` reads the hub's accounts endpoints every 60 s (BP-5c). A Schwab
account change can therefore take up to 60 s to reach the riskguard registry. The hub already
sends a content-free hint, and the adapter already has a rate-limited hook for it. The only piece
missing is the wire between the two.

## What exists (don't rebuild)
- Hub side (tvDownloadOHLC, done, BP-5b half 2): `scripts/streaming/schwab_hub.py:883`. An
  ACCT_ACTIVITY event becomes `{"service": "ACCOUNTS_CHANGED", "content": [{"changed": true}]}`
  on the hub's `/ws`, at most one per second. Tests: `tests/streaming/test_hub_accounts_passthrough.py`.
  Read those tests to see how a `/ws` client connects and whether it must subscribe to receive the frame.
- Adapter side (done): `src/trade_engine/schwab/adapter.py:339` `on_activity(now, message)`. It
  returns True at most once per `ACTIVITY_REPOLL_SECONDS` (10 s) and counts suppressed hints. It never
  reads the message content.
- Host: `src/trade_engine/tradovate/registry_feed.py`. `SchwabFeed.from_env` (about line 234) has
  the `TODO(BP-5e)` at line 259. `SchwabPoller.tick/attempt/run` (lines 113-216) is the 60 s loop with
  failure backoff. `PollPump.ask()` (line 312) is the existing Tradovate pattern for "read now".
- Hub base URL and the loopback check: `src/trade_engine/schwab/hub_client.py` (`hub_base_url`,
  `check_loopback`).

## Do
1. **A hub /ws listener for the Schwab half.** It is one daemon thread, started next to the Schwab
   thread in `run()` (about line 526), and only when the Schwab half is enabled.
   - Connect to the hub's `/ws` on loopback only (reuse `check_loopback`).
   - On each text frame, parse the JSON. When `service == "ACCOUNTS_CHANGED"`, call
     `adapter.on_activity(now, None)`. If that returns True, ask the poller for an early read.
   - Ignore every other frame without parsing past `service`. Market data flows on the same socket,
     so the listener must be cheap per frame. Don't buffer frames, and don't keep a list.
2. **An early read in `SchwabPoller`.** Add `ask()`, which sets a flag the same way `PollPump.ask` does
   and never blocks. The next tick then reads even though the 60 s floor has not elapsed. The
   read-failure backoff still wins: no read happens before `_next_attempt_at`. The tick wait must wake
   on an ask (use a Condition or an Event; no busy loop). Keep 60 s as the floor when no hints arrive.
3. **The listener's failure modes.** Hub down, connection refused, socket closed or a bad frame:
   - Count each one by kind. Reconnect with a jittered backoff of 5 s, doubling, capped at 120 s.
   - Never raise into the feed. Never stop the 60 s poll; the poll is the contract and the hint is
     only an optimisation.
   - A frame over 1 MiB is dropped and counted (B7).
4. **The websocket client.** Neither the trade-engine nor its venv has a websocket library. Pick one:
   (a) add `websockets` pinned to an exact version in `pyproject.toml` and use its sync client
   (`websockets.sync.client.connect`); or (b) write a minimal stdlib RFC 6455 client for loopback text
   frames only. Prefer (a) unless the dependency policy in `BP-COMMON.md` forbids it. State which one
   you used and why.
5. **A switch.** `TE_SCHWAB_ACCOUNTS_HINT`, default on whenever `TE_SCHWAB_ACCOUNTS` is on. Setting it
   to `0` turns the listener off. The heartbeat `counts()` gains keys for hints received, hints acted
   on, hints suppressed (from the adapter's stats), ws connects and ws failures by kind. Counts only:
   no account numbers, hashes or frame bodies.
6. **Remove the TODO** at line 259. Update `docs/BROKER_CONTRACT.md:187`, and in
   `docs/design/BROKER-PLATFORM.md` section 12 mark BP-5e built.

## Tests (fake clock, fake socket; no real hub)
- An ACCOUNTS_CHANGED frame leads to exactly one early read. A second frame within 10 s causes no read
  and counts as suppressed. A frame after 10 s causes another read.
- A hint during a failure backoff does not read before `_next_attempt_at`.
- Other services' frames call nothing. A malformed JSON frame is counted and ignored. An oversized frame
  is dropped.
- With no hints, the 60 s floor behaves exactly as before. Existing BP-5c tests stay green.
- A listener connect failure reconnects with backoff, and the poll keeps running.
- With the switch off, no listener thread starts.
- With `TE_SCHWAB_ACCOUNTS` off, nothing starts.
- The stop event ends the listener thread within one second.

Run the Schwab and registry-feed test files plus the full Python suite. Report the counts.

## Don't
- Touch the hub (tvDownloadOHLC), the Schwab tokens or any secret file.
- Restart `registry-feed` (PIDs 41796/32948) or quantd, or deploy. Don't push.
- Read message content beyond `service`. The account set always comes from the poll.

Commit on your branch: `feat(BP-5e): ACCOUNTS_CHANGED on the hub /ws triggers an early Schwab re-read`.
Report the SHA, file:line of each change, the websocket choice, and the test counts.
