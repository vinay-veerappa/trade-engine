# TICKET S1b-1: the cover rule: a pure module that says when an order must wait (shares or a debit-diagonal long call cover a short call)
## Defect
Nothing stops a short call reaching the venue before the shares or long call that cover it are proven held, or those shares being sold while a short call rests on them (hazards H1, H2). docs/architecture/TOS_STOCK_AND_LEAPS_MIRROR.md 'S1b design'.
## Required change
Acceptance tests (already written, do not edit): tests/test_tos_cover.py. Edit ONLY the four regions named, all
in src/trade_engine/tos_paper/cover.py (a new module whose signatures and imports are already there: Mapping,
Sequence, Decimal, Combo, Equity, Instrument, OptionContract, OptionRight, Side, Order, MirrorState,
ticket_contracts; no other module-level import is available, so put any other import inside the body that needs it
(for example `from math import floor`, or use `//` on Decimals). The regions are rewritten whole; you may add
private helper functions (leading underscore) after a function's body inside the same region.

The rule: an order waits iff filling it in full would leave MORE short calls uncovered on its underlying than there
are now. Cover is what the mirror book proves the venue holds, pessimistically. Raise nothing for ordinary input.

1. `covers(long, short)`: True iff long.underlying == short.underlying, both `.right is OptionRight.CALL`,
`long.multiplier == short.multiplier`, `long.expiry >= short.expiry` and `long.strike <= short.strike`; else False.

2. `uncovered(held)`: held maps Instrument -> signed Decimal quantity. Return {underlying symbol: Decimal count of
short call contracts nothing covers}, ONLY entries above zero. Consider each underlying U that has at least one
short call (an OptionContract with right CALL and quantity < 0; its units = int(-quantity)). Shares of U are the
held quantity of the Equity whose `.symbol == U` (scan held's keys with isinstance(key, Equity); do not construct an
Equity). Long calls of U are OptionContracts with right CALL, underlying U and quantity > 0 (units = int(quantity)).
Cover: first a MAXIMUM bipartite matching (Kuhn's augmenting paths, expanding every short and long to one node per
unit; an edge when `covers(long, short)`) of longs to shorts, so one long never covers two shorts and a greedy
assignment never strands a short that another assignment would cover. Then each short still unmatched is covered by
a lot of shares: shares provide `max(0, shares) // multiplier` lots where multiplier is the short call's own
(spend lots on the shorts with the larger multiplier first; the tests only use 100). NEGATIVE shares provide zero
lots, never negative (a short share position must not add demand or cover). uncovered = unmatched shorts minus lots,
floored at zero. A put, a zero quantity, an Equity and a long call are never a short call.

3. `holdings(mirror)`: the mirror's proven holdings, pessimistic. Sum `mirror.book` (keys are (account, instrument))
over accounts per instrument; then for every ticket in `mirror.open_tickets`, for each (instrument, signed quantity)
in `ticket_contracts(ticket.queued, ticket.remaining).items()`, ADD the quantity only when it is negative (a resting
sell leaves the holdings; a resting buy is never credited, so supply is proven fills only and resting short calls
count as demand). Return only entries with a non-zero quantity.

4. `cover_reason(mirror, order, accepted=())`: None unless the order is a single-leg SELL of an `Equity` or of an
OptionContract whose right is CALL (a BUY, a put, a Combo and anything else returns None at once). Otherwise:
`held = holdings(mirror)`; for each earlier order in `accepted` that is such a SELL (Equity or call, single leg),
subtract its quantity from `held[its instrument]` (accepted buys and everything else are ignored); then
`before = uncovered(held)`, subtract this order's quantity from a copy of `held` for its instrument and take
`after = uncovered(that copy)`. The order waits iff for some underlying `after[u] > before.get(u, 0)`. When it must
wait return ONE string naming the order's underlying (the OptionContract's `.underlying`, or the Equity's `.symbol`)
and containing the words "uncovered" and "proven venue fills only", in this shape: f"selling {quantity}
{instrument.symbol} would leave {after_count} short {underlying} call(s) uncovered ({before_count} now); the mirror
book proves {shares} share(s) and {longs} long call(s) held (proven venue fills only; shares from an assignment are
not booked)" where shares is the held quantity of the underlying's Equity (0 when none) and longs the held long
calls of that underlying (sum of quantities above zero). Otherwise return None.

Do not touch any other file. Run nothing you cannot see the result of; the gate runs the full suite.
## Regions to rewrite
### REGION id="covers"  file=src/trade_engine/tos_paper/cover.py  lines 28-31
```python
def covers(long: OptionContract, short: OptionContract) -> bool:
    """Whether one long call can cover one short call: same underlying, both calls, same multiplier,
    long expiry >= short expiry, long strike <= short strike (a debit diagonal; a credit one covers nothing)."""
    raise NotImplementedError
```
### REGION id="uncovered"  file=src/trade_engine/tos_paper/cover.py  lines 34-36
```python
def uncovered(held: Mapping[Instrument, Decimal]) -> dict[str, Decimal]:
    """Per underlying: the short call contracts nothing in ``held`` covers (only entries above zero)."""
    raise NotImplementedError
```
### REGION id="holdings"  file=src/trade_engine/tos_paper/cover.py  lines 39-42
```python
def holdings(mirror: MirrorState) -> dict[Instrument, Decimal]:
    """What the mirror proves the venue holds, pessimistically: the book summed over accounts, less every
    open ticket's unfilled sell remainder (a resting buy is not credited until it fills)."""
    raise NotImplementedError
```
### REGION id="cover_reason"  file=src/trade_engine/tos_paper/cover.py  lines 45-48
```python
def cover_reason(mirror: MirrorState, order: Order, accepted: Sequence[Order] = ()) -> str | None:
    """None when ``order`` may go now; else why it must wait. ``accepted`` are the orders of the same
    batch already let through: their sells count as resting."""
    raise NotImplementedError
```
### REGION id="S1b-1-ctx-Equity-4"  file=src/trade_engine/tos_paper/cover.py  lines 23-23
Purpose: CF-31 auto-attached read-only context for symbol 'Equity'
```python
from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract, OptionRight, Side
```
### REGION id="S1b-1-ctx-OptionRight-5"  file=src/trade_engine/tos_paper/cover.py  lines 23-23
Purpose: CF-31 auto-attached read-only context for symbol 'OptionRight'
```python
from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract, OptionRight, Side
```
FIDELITY RULE — obey exactly:
- Lines you are NOT changing must come back byte-for-byte identical,
  including non-ASCII characters (emojis, box-drawing, arrows), comment
  syntax (// vs ///), indentation, and trailing whitespace.
- Do not rewrite, reflow, or 'normalise' comments you were not asked to
  change. Do not strip or replace non-ASCII glyphs in existing code.
- If you only need to change 3 lines, the other N lines in the block
  must be reproduced verbatim. A block is replaced whole, so every
  line you alter unnecessarily degrades the file.
Return one block per region id above, in the same order. No other output.