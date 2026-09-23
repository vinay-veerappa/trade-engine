# trade-engine

Generic trading engine: event-sourced ledger, OMS, risk layer, simulator, and venue adapters.

## Architecture & Invariants

This engine is designed around strict invariants (I1–I13):

1. **I1: One writer, one truth.** The engine's event ledger is the only source of state. Sinks are fed from it.
2. **I2: State = fold(events).** Nothing is updated in place; every state transition is an appended event; restarts replay the log.
3. **I3: Idempotent by persisted key.** Every command carries a `command_id` stored in the ledger; replaying a command is a no-op.
4. **I4: Single instance.** Exactly one process per ledger (OS file lock); a second process refuses to start.
5. **I5: Refuse, never guess.** Missing or stale price, bar, earnings date, regime, or unknown symbol refuses the action and records why. No defaults.
6. **I6: Instruments are resolved objects, not strings.** Books are keyed by resolved instrument (OCC symbol for options) and carry multipliers.
7. **I7: Time is injected.** All engine components read from an injected `Clock`; replay uses bar timestamps, live uses wall time. All timestamps are UTC.
8. **I8: Every account owns everything it holds.** Shares written against must be in that account's ledger with cost paid from its cash.
9. **I9: Expiry/assignment on the settle.** Processed after the 16:00 ET close on the official close price, booking intrinsic value per leg.
10. **I10: Unknown broker state = pending, never filled.** Environment (`sim`, `paper`, `live`) is declared by the adapter and proven at connect; live requires explicit acknowledgment in configuration.
11. **I11: Every decision is recorded with its reason.** Accepted, refused (and by which specific rule), or skipped. Every rule is evaluated without short-circuiting.
12. **I12: A sink confirms delivery, not a 200.** Outbox drains in order and stops at the first failure until confirmed.
13. **I13: Strategies know nothing about brokers; adapters know nothing about strategies.** Clean separation of concerns.

## Development

Requires Python >= 3.13.

### Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[test]
```

### Running Tests & Local CI

```powershell
pytest -q
python tools/ci_local.py
```

### Package Version Check

```powershell
python -m trade_engine --version
```
