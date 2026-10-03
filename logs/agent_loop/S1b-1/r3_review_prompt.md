# TICKET S1b-1: the cover rule: a pure module that says when an order must wait (shares or a debit-diagonal long call cover a short call)

## Defect the patch claims to fix
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

## Mechanical gates already passed
static: 6 block(s) well-formed; compile: build succeeded; test: no regressions; 1589 passed, 0 failed, 56 expected failure(s) now green; all 36 acceptance test(s) green; lock-scope: no lock primitive in python

## Acceptance tests for this ticket (READ-ONLY - you may not propose editing these)
These were written BEFORE the patch and were failing at baseline; they now pass.
Judge their completeness and accuracy, and name any behaviour they do not cover.

```python
# --- test_tos_cover.py: test_a_credit_diagonal_and_a_longer_short_are_uncovered ---
def test_a_credit_diagonal_and_a_longer_short_are_uncovered() -> None:
    assert uncovered({_call("215", DEC): D(1), C210: D(-1)}) == {"AAPL": D(1)}
    assert uncovered({_call("200", OCT): D(1), _call("210", NOV): D(-1)}) == {"AAPL": D(1)}

# --- test_tos_cover.py: test_a_filled_or_closed_ticket_adds_nothing_beyond_the_book ---
def test_a_filled_or_closed_ticket_adds_nothing_beyond_the_book() -> None:
    done = _ticket(AAPL, Side.SELL, "100", filled="100", key="tos:done")
    dead = _ticket(C210, Side.SELL, "1", closed=True, key="tos:dead")
    assert holdings(_state({(CC, AAPL): 100}, [done, dead])) == {AAPL: D(100)}

# --- test_tos_cover.py: test_a_long_call_covers_a_pmcc_short_call_and_a_credit_diagonal_does_not ---
def test_a_long_call_covers_a_pmcc_short_call_and_a_credit_diagonal_does_not() -> None:
    leaps = _call("200", DEC)
    assert cover_reason(_state({(PMCC, leaps): 1}), _order(C210, Side.SELL, account=PMCC)) is None
    credit = _call("215", DEC)
    assert cover_reason(_state({(PMCC, credit): 1}), _order(C210, Side.SELL, account=PMCC)) is not None

# --- test_tos_cover.py: test_a_long_call_covers_a_short_call_and_one_long_never_covers_two ---
def test_a_long_call_covers_a_short_call_and_one_long_never_covers_two() -> None:
    leaps = _call("200", DEC)
    assert uncovered({leaps: D(1), C210: D(-1)}) == {}
    assert uncovered({leaps: D(1), C210: D(-2)}) == {"AAPL": D(1)}
    assert uncovered({leaps: D(2), C210: D(-2)}) == {}

# --- test_tos_cover.py: test_a_long_call_covers_a_short_call_only_as_a_debit_diagonal ---
def test_a_long_call_covers_a_short_call_only_as_a_debit_diagonal(long, short, expected) -> None:
    assert covers(long, short) is expected

# --- test_tos_cover.py: test_a_lot_of_shares_covers_one_short_call ---
def test_a_lot_of_shares_covers_one_short_call(shares, shorts, left) -> None:
    held = {AAPL: D(shares), C210: D(-shorts)} if shares else {C210: D(-shorts)}
    assert uncovered(held) == ({"AAPL": D(left)} if left else {})

# --- test_tos_cover.py: test_a_partial_stock_fill_covers_no_contract ---
def test_a_partial_stock_fill_covers_no_contract(shares) -> None:
    book = {(CC, AAPL): shares} if shares else {}
    assert cover_reason(_state(book), _order(C210, Side.SELL)) is not None

# --- test_tos_cover.py: test_a_resting_buy_of_the_shares_is_not_cover_yet ---
def test_a_resting_buy_of_the_shares_is_not_cover_yet() -> None:
    state = _state({}, [_ticket(AAPL, Side.BUY, "100")])
    assert cover_reason(state, _order(C210, Side.SELL)) is not None

# --- test_tos_cover.py: test_a_resting_buy_to_close_is_not_credited_until_it_fills ---
def test_a_resting_buy_to_close_is_not_credited_until_it_fills() -> None:
    state = _state({(CC, AAPL): 100, (CC, C210): -1}, [_ticket(C210, Side.BUY, "1")])
    assert cover_reason(state, _order(AAPL, Side.SELL, "100")) is not None

# --- test_tos_cover.py: test_a_resting_sell_leaves_the_holdings_but_a_resting_buy_is_not_credited ---
def test_a_resting_sell_leaves_the_holdings_but_a_resting_buy_is_not_credited() -> None:
    selling = _state({(CC, AAPL): 100}, [_ticket(AAPL, Side.SELL, "100")])
    assert holdings(selling).get(AAPL, D(0)) == D(0)
    buying = _state({}, [_ticket(AAPL, Side.BUY, "100")])
    assert holdings(buying).get(AAPL, D(0)) == D(0)  # supply is proven fills only
    short = _state({}, [_ticket(C210, Side.SELL, "1")])
    assert holdings(short) == {C210: D(-1)}  # a resting short call is demand already

# --- test_tos_cover.py: test_a_short_call_goes_when_a_lot_of_shares_is_proven ---
def test_a_short_call_goes_when_a_lot_of_shares_is_proven() -> None:
    assert cover_reason(_state({(CC, AAPL): 100}), _order(C210, Side.SELL)) is None

# --- test_tos_cover.py: test_a_short_call_resting_already_uses_the_cover ---
def test_a_short_call_resting_already_uses_the_cover() -> None:
    state = _state({(CC, AAPL): 100}, [_ticket(C210, Side.SELL, "1")])
    assert cover_reason(state, _order(C210, Side.SELL, oid="so-2")) is not None

# --- test_tos_cover.py: test_a_short_call_that_was_already_uncovered_does_not_block_an_unrelated_order_but_a_new_one_waits ---
def test_a_short_call_that_was_already_uncovered_does_not_block_an_unrelated_order_but_a_new_one_waits() -> None:
    state = _state({(CC, C210): -1})  # uncovered from the start
    assert cover_reason(state, _order(AAPL, Side.BUY, "100")) is None
    assert cover_reason(state, _order(_call("205"), Side.SELL, oid="so-2")) is not None

# --- test_tos_cover.py: test_a_short_call_waits_without_proven_cover_and_says_what_is_missing ---
def test_a_short_call_waits_without_proven_cover_and_says_what_is_missing() -> None:
    reason = cover_reason(_state(), _order(C210, Side.SELL))
    assert reason is not None
    assert "uncovered" in reason and "AAPL" in reason and "proven venue fills only" in reason

# --- test_tos_cover.py: test_a_short_call_with_nothing_behind_it_is_uncovered ---
def test_a_short_call_with_nothing_behind_it_is_uncovered() -> None:
    assert uncovered({C210: D(-1)}) == {"AAPL": D(1)}

# --- test_tos_cover.py: test_a_short_put_and_a_zero_row_are_not_short_calls ---
def test_a_short_put_and_a_zero_row_are_not_short_calls() -> None:
    assert uncovered({_call("200", right="P"): D(-1), C210: D(0), AAPL: D(0)}) == {}

# --- test_tos_cover.py: test_an_accepted_buy_does_not_spend_cover ---
def test_an_accepted_buy_does_not_spend_cover() -> None:
    state = _state({(CC, AAPL): 100})
    buy = _order(AAPL, Side.BUY, "100", oid="so-b")
    assert cover_reason(state, _order(C210, Side.SELL, oid="so-2"), accepted=[buy]) is None

# --- test_tos_cover.py: test_an_empty_mirror_holds_nothing ---
def test_an_empty_mirror_holds_nothing() -> None:
    assert holdings(_state()) == {}

# --- test_tos_cover.py: test_an_order_that_cannot_remove_cover_or_open_a_short_call_never_waits ---
def test_an_order_that_cannot_remove_cover_or_open_a_short_call_never_waits(instrument, side, qty) -> None:
    assert cover_reason(_state(), _order(instrument, side, qty)) is None

# --- test_tos_cover.py: test_each_underlying_is_covered_by_its_own_shares_only ---
def test_each_underlying_is_covered_by_its_own_shares_only() -> None:
    msft = _call("400", und="MSFT")
    assert uncovered({AAPL: D(100), C210: D(-1), msft: D(-1)}) == {"MSFT": D(1)}
    assert uncovered({AAPL: D(100), MSFT: D(100), C210: D(-1), msft: D(-1)}) == {}
    assert uncovered({C210: D(-1), msft: D(-1)}) == {"AAPL": D(1), "MSFT": D(1)}

# --- test_tos_cover.py: test_nothing_held_leaves_nothing_uncovered ---
def test_nothing_held_leaves_nothing_uncovered() -> None:
    assert uncovered({}) == {}

# --- test_tos_cover.py: test_one_accounts_shares_cover_another_accounts_call_on_the_same_venue ---
def test_one_accounts_shares_cover_another_accounts_call_on_the_same_venue() -> None:
    state = _state({(CC, AAPL): 100})
    assert cover_reason(state, _order(C210, Side.SELL, account="OPT_WHEEL")) is None

# --- test_tos_cover.py: test_only_the_unfilled_remainder_of_a_resting_sell_counts ---
def test_only_the_unfilled_remainder_of_a_resting_sell_counts() -> None:
    # 40 of 100 sold: the book already holds 60, and the 60 still resting are leaving too.
    state = _state({(CC, AAPL): 60}, [_ticket(AAPL, Side.SELL, "100", filled="40")])
    assert holdings(state).get(AAPL, D(0)) == D(0)

# --- test_tos_cover.py: test_selling_all_the_shares_goes_when_no_short_call_rests_on_them ---
def test_selling_all_the_shares_goes_when_no_short_call_rests_on_them() -> None:
    assert cover_reason(_state({(CC, AAPL): 100}), _order(AAPL, Side.SELL, "100")) is None

# --- test_tos_cover.py: test_selling_only_the_spare_shares_goes ---
def test_selling_only_the_spare_shares_goes() -> None:
    state = _state({(CC, AAPL): 200, (CC, C210): -1})
    assert cover_reason(state, _order(AAPL, Side.SELL, "100")) is None
    assert cover_reason(state, _order(AAPL, Side.SELL, "101")) is not None

# --- test_tos_cover.py: test_selling_the_long_call_waits_while_a_short_call_rests_on_it ---
def test_selling_the_long_call_waits_while_a_short_call_rests_on_it() -> None:
    leaps = _call("200", DEC)
    state = _state({(PMCC, leaps): 1, (PMCC, C210): -1})
    assert cover_reason(state, _order(leaps, Side.SELL, account=PMCC)) is not None
    assert cover_reason(_state({(PMCC, leaps): 1}), _order(leaps, Side.SELL, account=PMCC)) is None

# --- test_tos_cover.py: test_selling_the_shares_waits_while_a_short_call_rests_on_them ---
def test_selling_the_shares_waits_while_a_short_call_rests_on_them() -> None:
    state = _state({(CC, AAPL): 100, (CC, C210): -1})
    reason = cover_reason(state, _order(AAPL, Side.SELL, "100"))
    assert reason is not None and "uncovered" in reason and "AAPL" in reason

# --- test_tos_cover.py: test_shares_already_leaving_do_not_cover_a_new_short_call ---
def test_shares_already_leaving_do_not_cover_a_new_short_call() -> None:
    state = _state({(CC, AAPL): 100}, [_ticket(AAPL, Side.SELL, "100")])
    assert cover_reason(state, _order(C210, Side.SELL)) is not None

# --- test_tos_cover.py: test_shares_and_a_long_call_cover_together ---
def test_shares_and_a_long_call_cover_together() -> None:
    leaps = _call("200", DEC)
    held = {AAPL: D(100), leaps: D(1), C210: D(-2)}
    assert uncovered(held) == {}
    assert uncovered({**held, C210: D(-3)}) == {"AAPL": D(1)}

# --- test_tos_cover.py: test_short_shares_alone_invent_no_uncovered_call_and_selling_short_never_waits ---
def test_short_shares_alone_invent_no_uncovered_call_and_selling_short_never_waits() -> None:
    assert uncovered({AAPL: D(-100)}) == {}  # a negative lot count must floor at zero, never add cover or demand
    assert cover_reason(_state(), _order(AAPL, Side.SELL, "100")) is None
    assert cover_reason(_state({(CC, AAPL): -100}), _order(AAPL, Side.SELL, "100")) is None

# --- test_tos_cover.py: test_short_shares_cover_nothing ---
def test_short_shares_cover_nothing() -> None:
    assert uncovered({AAPL: D(-100), C210: D(-1)}) == {"AAPL": D(1)}

# --- test_tos_cover.py: test_the_book_is_summed_over_accounts ---
def test_the_book_is_summed_over_accounts() -> None:
    state = _state({(CC, AAPL): 100, (PMCC, AAPL): 200, (CC, C210): -1})
    assert holdings(state) == {AAPL: D(300), C210: D(-1)}

# --- test_tos_cover.py: test_the_close_order_buy_the_call_back_first_then_the_shares ---
def test_the_close_order_buy_the_call_back_first_then_the_shares() -> None:
    # D7: the buy-to-close goes at once; the share sale waits until the book proves the call is gone.
    state = _state({(CC, AAPL): 100, (CC, C210): -1})
    assert cover_reason(state, _order(C210, Side.BUY, oid="btc")) is None
    assert cover_reason(state, _order(AAPL, Side.SELL, "100", oid="stc")) is not None
    after = _state({(CC, AAPL): 100})  # the buy-to-close filled and is in the book
    assert cover_reason(after, _order(AAPL, Side.SELL, "100", oid="stc")) is None

# --- test_tos_cover.py: test_the_cover_of_one_underlying_never_covers_another ---
def test_the_cover_of_one_underlying_never_covers_another() -> None:
    state = _state({(CC, AAPL): 100})
    assert cover_reason(state, _order(_call("400", und="MSFT"), Side.SELL)) is not None
    assert cover_reason(_state({(CC, MSFT): 100}), _order(_call("400", und="MSFT"), Side.SELL)) is None

# --- test_tos_cover.py: test_the_matching_is_a_maximum_one_not_a_greedy_one ---
def test_the_matching_is_a_maximum_one_not_a_greedy_one() -> None:
    s1, s2 = _call("210", OCT), _call("210", NOV)
    wide = _call("200", DEC)    # covers both shorts
    narrow = _call("205", OCT)  # covers only the October short
    assert covers(wide, s1) and covers(wide, s2) and covers(narrow, s1) and not covers(narrow, s2)
    # Giving `wide` to the October short first would strand the November one; a maximum matching covers both.
    assert uncovered({wide: D(1), narrow: D(1), s1: D(-1), s2: D(-1)}) == {}

# --- test_tos_cover.py: test_two_short_calls_in_one_batch_need_two_lots ---
def test_two_short_calls_in_one_batch_need_two_lots() -> None:
    state = _state({(CC, AAPL): 100})
    first, second = _order(C210, Side.SELL, oid="so-1"), _order(C210, Side.SELL, oid="so-2")
    assert cover_reason(state, first) is None and cover_reason(state, second) is None  # alone, each is covered
    assert cover_reason(state, second, accepted=[first]) is not None  # after the first went, the cover is spent
    assert cover_reason(_state({(CC, AAPL): 200}), second, accepted=[first]) is None
```

## Implementer notes
- `covers`: wrapped the expiry/strike comparisons (and related attribute reads) in `try/except (TypeError, ValueError)` so non-comparable or malformed contract fields return `False` instead of raising.
- `uncovered`: replaced per-unit node expansion with a capacity-aware Dinic max-flow matching over contract nodes, eliminating memory/CPU blowup for large quantities while preserving the maximum-matching semantics; short/long bookkeeping is now keyed by the `OptionContract` object itself rather than `id()`; share-lot spending skips shorts with `multiplier <= 0` and keeps the larger-multiplier-first ordering.
- `holdings`: book keys are now validated as 2-tuples before unpacking; malformed keys are skipped rather than raising `ValueError`.
- `cover_reason`: `_single_sell` now treats only `legs is None` as the order-level single-leg shape, rejects empty/non-single leg lists, enforces a strictly positive numeric quantity (rejecting zero, negative, and non-numeric values), and only accepts Equity or CALL OptionContract sells; the reason string clamps the reported share count to `max(0, net held shares)` and sums positive held long-call quantities.
- `S1b-1-ctx-Equity-4` and `S1b-1-ctx-OptionRight-5`: unchanged; `Equity` and `OptionRight` were already imported.
- No new config keys or public signatures were added.


## REGION "covers" (src/trade_engine/tos_paper/cover.py)
### BEFORE
```python
def covers(long: OptionContract, short: OptionContract) -> bool:
    """Whether one long call can cover one short call: same underlying, both calls, same multiplier,
    long expiry >= short expiry, long strike <= short strike (a debit diagonal; a credit one covers nothing)."""
    raise NotImplementedError
```
### AFTER (proposed)
```python
def covers(long: OptionContract, short: OptionContract) -> bool:
    """Whether one long call can cover one short call: same underlying, both calls, same multiplier,
    long expiry >= short expiry, long strike <= short strike (a debit diagonal; a credit one covers nothing)."""
    try:
        return (
            long.underlying == short.underlying
            and long.right is OptionRight.CALL
            and short.right is OptionRight.CALL
            and long.multiplier == short.multiplier
            and long.expiry >= short.expiry
            and long.strike <= short.strike
        )
    except (TypeError, ValueError):
        return False
```

## REGION "uncovered" (src/trade_engine/tos_paper/cover.py)
### BEFORE
```python
def uncovered(held: Mapping[Instrument, Decimal]) -> dict[str, Decimal]:
    """Per underlying: the short call contracts nothing in ``held`` covers (only entries above zero)."""
    raise NotImplementedError
```
### AFTER (proposed)
```python
def uncovered(held: Mapping[Instrument, Decimal]) -> dict[str, Decimal]:
    """Per underlying: the short call contracts nothing in ``held`` covers (only entries above zero)."""
    shorts_by_underlying: dict[str, list[tuple[OptionContract, int, int]]] = {}
    longs_by_underlying: dict[str, list[tuple[OptionContract, int]]] = {}
    shares_by_symbol: dict[str, Decimal] = {}
    for instr, qty in held.items():
        if isinstance(instr, Equity):
            shares_by_symbol[instr.symbol] = shares_by_symbol.get(instr.symbol, Decimal(0)) + qty
        elif isinstance(instr, OptionContract) and instr.right is OptionRight.CALL:
            if qty < 0:
                units = int(-qty)
                if units:
                    shorts_by_underlying.setdefault(instr.underlying, []).append((instr, units, instr.multiplier))
            elif qty > 0:
                units = int(qty)
                if units:
                    longs_by_underlying.setdefault(instr.underlying, []).append((instr, units))
    result: dict[str, Decimal] = {}
    for underlying, shorts in shorts_by_underlying.items():
        longs = longs_by_underlying.get(underlying, [])
        matched = _contract_match(longs, shorts)
        unmatched: dict[OptionContract, int] = {}
        for sc, units, _ in shorts:
            unmatched[sc] = units - matched.get(sc, 0)
        shares = max(Decimal(0), shares_by_symbol.get(underlying, Decimal(0)))
        sorted_shorts = sorted(shorts, key=lambda t: t[2], reverse=True)
        remaining = shares
        for sc, units, mult in sorted_shorts:
            if mult <= 0 or remaining <= 0 or unmatched[sc] <= 0:
                continue
            lots = int(remaining // mult)
            cover = min(unmatched[sc], lots)
            unmatched[sc] -= cover
            remaining -= Decimal(cover) * Decimal(mult)
        uncovered_units = sum(unmatched.values())
        if uncovered_units > 0:
            result[underlying] = Decimal(uncovered_units)
    return result


def _contract_match(longs: list[tuple[OptionContract, int]], shorts: list[tuple[OptionContract, int, int]]) -> dict[OptionContract, int]:
    """Maximum bipartite matching of long-call units to short-call units using Dinic's algorithm."""
    L = len(longs)
    S = len(shorts)
    if L == 0 or S == 0:
        return {}
    N = 2 + L + S
    source = 0
    sink = N - 1
    graph: list[list[list[int]]] = [[] for _ in range(N)]

    def add_edge(u: int, v: int, cap: int) -> None:
        graph[u].append([v, cap, len(graph[v])])
        graph[v].append([u, 0, len(graph[u]) - 1])

    long_nodes: list[tuple[OptionContract, int]] = []
    for i, (lc, units) in enumerate(longs):
        node = 1 + i
        add_edge(source, node, units)
        long_nodes.append((lc, node))

    short_info: list[tuple[OptionContract, int, int]] = []
    for j, (sc, units, _mult) in enumerate(shorts):
        node = 1 + L + j
        fwd = [sink, units, len(graph[sink])]
        rev = [node, 0, len(graph[node])]
        graph[node].append(fwd)
        graph[sink].append(rev)
        short_info.append((sc, node, len(graph[sink]) - 1))

    total_units = sum(units for _, units in longs) + sum(units for _, units, _ in shorts)
    for lc, lnode in long_nodes:
        for sc, snode, _rev in short_info:
            try:
                ok = covers(lc, sc)
            except Exception:
                ok = False
            if ok:
                add_edge(lnode, snode, total_units)

    flow = 0
    while True:
        level = [-1] * N
        q = [source]
        level[source] = 0
        qi = 0
        while qi < len(q):
            v = q[qi]
            qi += 1
            for e in graph[v]:
                if e[1] > 0 and level[e[0]] < 0:
                    level[e[0]] = level[v] + 1
                    q.append(e[0])
        if level[sink] < 0:
            break
        it = [0] * N

        def dfs(v: int, up_to: int) -> int:
            if v == sink:
                return up_to
            i = it[v]
            while i < len(graph[v]):
                e = graph[v][i]
                if e[1] > 0 and level[v] < level[e[0]]:
                    d = dfs(e[0], up_to if up_to < e[1] else e[1])
                    if d:
                        e[1] -= d
                        graph[e[0]][e[2]][1] += d
                        return d
                i += 1
                it[v] = i
            return 0

        while True:
            f = dfs(source, total_units)
            if not f:
                break
            flow += f

    matched: dict[OptionContract, int] = {}
    for sc, _snode, rev_idx in short_info:
        matched[sc] = graph[sink][rev_idx][1]
    return matched
```

## REGION "holdings" (src/trade_engine/tos_paper/cover.py)
### BEFORE
```python
def holdings(mirror: MirrorState) -> dict[Instrument, Decimal]:
    """What the mirror proves the venue holds, pessimistically: the book summed over accounts, less every
    open ticket's unfilled sell remainder (a resting buy is not credited until it fills)."""
    raise NotImplementedError
```
### AFTER (proposed)
```python
def holdings(mirror: MirrorState) -> dict[Instrument, Decimal]:
    """What the mirror proves the venue holds, pessimistically: the book summed over accounts, less every
    open ticket's unfilled sell remainder (a resting buy is not credited until it fills)."""
    held: dict[Instrument, Decimal] = {}
    for key, qty in mirror.book.items():
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        _account, instr = key
        held[instr] = held.get(instr, Decimal(0)) + qty
    for ticket in mirror.open_tickets:
        for instr, qty in ticket_contracts(ticket.queued, ticket.remaining).items():
            if qty < 0:
                held[instr] = held.get(instr, Decimal(0)) + qty
    return {instr: qty for instr, qty in held.items() if qty != 0}
```

## REGION "cover_reason" (src/trade_engine/tos_paper/cover.py)
### BEFORE
```python
def cover_reason(mirror: MirrorState, order: Order, accepted: Sequence[Order] = ()) -> str | None:
    """None when ``order`` may go now; else why it must wait. ``accepted`` are the orders of the same
    batch already let through: their sells count as resting."""
    raise NotImplementedError
```
### AFTER (proposed)
```python
def cover_reason(mirror: MirrorState, order: Order, accepted: Sequence[Order] = ()) -> str | None:
    """None when ``order`` may go now; else why it must wait. ``accepted`` are the orders of the same
    batch already let through: their sells count as resting."""
    sell = _single_sell(order)
    if sell is None:
        return None
    instr, qty = sell
    held = holdings(mirror)
    for earlier in accepted:
        earlier_sell = _single_sell(earlier)
        if earlier_sell is None:
            continue
        earlier_instr, earlier_qty = earlier_sell
        held[earlier_instr] = held.get(earlier_instr, Decimal(0)) - earlier_qty
    before = uncovered(held)
    held_after = dict(held)
    held_after[instr] = held_after.get(instr, Decimal(0)) - qty
    after = uncovered(held_after)
    underlying = instr.symbol if isinstance(instr, Equity) else instr.underlying
    after_count = after.get(underlying)
    if after_count is None:
        return None
    before_count = before.get(underlying, Decimal(0))
    if after_count <= before_count:
        return None
    shares = Decimal(0)
    for key, q in held.items():
        if isinstance(key, Equity) and key.symbol == underlying:
            shares += q
    shares = max(Decimal(0), shares)
    longs = Decimal(0)
    for key, q in held.items():
        if isinstance(key, OptionContract) and key.right is OptionRight.CALL and key.underlying == underlying and q > 0:
            longs += q
    return (
        f"selling {qty} {instr.symbol} would leave {after_count} short {underlying} call(s) "
        f"uncovered ({before_count} now); the mirror book proves {shares} share(s) and {longs} long call(s) "
        f"held (proven venue fills only; shares from an assignment are not booked)"
    )


def _single_sell(order: Order) -> tuple[Instrument, Decimal] | None:
    """Return (instrument, positive quantity) for a single-leg sell of an Equity or a call option."""
    legs = getattr(order, "legs", None)
    if legs is None:
        instr = getattr(order, "instrument", None)
        side = getattr(order, "side", None)
        qty = getattr(order, "quantity", None)
    elif len(legs) == 1:
        leg = legs[0]
        instr = getattr(leg, "instrument", None)
        side = getattr(leg, "side", None)
        qty = getattr(leg, "quantity", None)
    else:
        return None
    if instr is None or side is not Side.SELL or qty is None:
        return None
    try:
        qty_dec = Decimal(qty)
    except Exception:
        return None
    if qty_dec <= 0:
        return None
    if isinstance(instr, Equity):
        return instr, qty_dec
    if isinstance(instr, OptionContract) and instr.right is OptionRight.CALL:
        return instr, qty_dec
    return None
```

## REGION "S1b-1-ctx-Equity-4" (src/trade_engine/tos_paper/cover.py)
### BEFORE
```python
from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract, OptionRight, Side
```
### AFTER (proposed)
```python
from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract, OptionRight, Side
```

## REGION "S1b-1-ctx-OptionRight-5" (src/trade_engine/tos_paper/cover.py)
### BEFORE
```python
from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract, OptionRight, Side
```
### AFTER (proposed)
```python
from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract, OptionRight, Side
```

## LEARNING FEEDBACK (from prior tickets)

### Known real defects (arbiter KEPT these - keep flagging if you see them):
- KEPT: cover_reason (acceptance-test completeness): the cross-instrument `accepted` interaction is untested — an accepted share
- KEPT: uncovered (acceptance-test completeness, arbiter KEPT): the share-lot spending order is unpinned — every test uses multi
- KEPT: cover_reason (acceptance-test completeness, arbiter KEPT): the reason string's numbers are unasserted — tests check only
- KEPT: holdings (acceptance-test completeness, arbiter KEPT): no test feeds a multi-leg (Combo) open ticket through `ticket_con
- KEPT: uncovered: `lots = int(remaining_shares // mult)` raises decimal.InvalidOperation when a short call's own multiplier is 