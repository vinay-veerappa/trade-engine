# TICKET S1b-1: the cover rule: a pure module that says when an order must wait (shares or a debit-diagonal long call cover a short call)

## Defect this patch must close
Nothing stops a short call reaching the venue before the shares or long call that cover it are proven held, or those shares being sold while a short call rests on them (hazards H1, H2). docs/architecture/TOS_STOCK_AND_LEAPS_MIRROR.md 'S1b design'.

## Mechanical gates (facts - you may not contradict these)
static: 6 block(s) well-formed; compile: build succeeded; test: no regressions; 1589 passed, 0 failed, 56 expected failure(s) now green; all 36 acceptance test(s) green; lock-scope: no lock primitive in python

## Convergence history
round 1: 5 blocking finding(s)
round 2: 15 blocking finding(s)
findings shared between the last two rounds: 0

## Findings to rule on

#1 [BLOCKER] (from deepseek-v4.1-flash:cloud)
uncovered: the Dinic flow is built with `add_edge(lnode, snode, total_units)` where `total_units = sum(units for _, units in longs) + sum(units for _, units, _ in shorts)`. This is a per-edge capacity, not a global bound, so it is fine as a cap — but the real defect is that `matched[sc] = graph[sink][rev_idx][1]` reads the *reverse* edge's residual capacity as the matched amount. The reverse edge from sink back to the short node starts at 0 and gains `d` on every augmenting push, so `graph[sink][rev_idx][1]` does equal the flow into that short node. That part is correct. The actual blocker: `short_info.append((sc, node, len(graph[sink]) - 1))` records the index of the reverse edge *after* appending `rev` to `graph[sink]`, i.e. `len(graph[sink]) - 1` is the index of `rev` in `graph[sink]`. Correct. However `fwd = [sink, units, len(graph[sink])]` is computed *before* `graph[sink].append(rev)`, so `fwd[2]` points at the index `rev` will occupy — correct. So the flow bookkeeping is sound. The genuine blocker is elsewhere: `_contract_match` returns `matched` keyed by `OptionContract` object, and `uncovered` does `unmatched[sc] = units - matched.get(sc, 0)`. If two distinct short rows in `held` are the *same* `OptionContract` object (e.g. the same contract appearing under two accounts, summed by `holdings` into one entry — fine) this is fine, but if `held` is passed directly by a caller with the same contract object appearing twice as separate keys it cannot happen since dict keys are unique. Not a blocker. Re-examining: the real blocker is `int(-qty)` / `int(qty)` on a `Decimal` with a fractional part silently truncates. `uncovered({C210: D("-1.5")})` yields `units = int(Decimal("1.5")) = 1`, under-counting the short by half a contract and reporting `{"AAPL": D(1)}` when the true uncovered demand is 1.5. The spec says "its units = int(-quantity)" so truncation is arguably intended, but combined with `unmatched[sc] = units - matched.get(sc, 0)` and `uncovered_units = sum(unmatched.values())` this can produce a *negative* `unmatched[sc]` if `matched` ever exceeds `units` — it cannot, since the short node's capacity is `units`. Not a blocker. The actual blocker: `_contract_match` is called with `longs` and `shorts` lists, and `matched` is keyed by `OptionContract`. If the *same* `OptionContract` object appears as both a long and a short (impossible: qty>0 vs qty<0 branches are exclusive per key). I cannot find a correctness blocker in the flow itself. Downgrading.

#2 [BLOCKER] (from deepseek-v4.1-flash:cloud)
cover_reason: `held[earlier_instr] = held.get(earlier_instr, Decimal(0)) - earlier_qty` subtracts the accepted sell's quantity from `held`, but `holdings` already subtracted every *open ticket's* unfilled sell remainder. An order in `accepted` that is also still an open ticket (the normal case: the batch was accepted but the ticket has not yet been removed from `mirror.open_tickets`) is therefore double-counted, removing its quantity twice. Concrete failure: `mirror.book = {(CC, AAPL): 100}`, `mirror.open_tickets = [ticket(AAPL, SELL, 100)]`, `accepted = [order(AAPL, SELL, 100)]`. `holdings` gives `{AAPL: 0}`; then the accepted subtraction gives `{AAPL: -100}`; `uncovered` floors negative shares to zero lots, so the result happens to be the same here — but for a *call* accepted sell the double subtraction drives the short count more negative, which *reduces* `before`, making `after > before` more likely and producing a spurious wait. The spec says accepted sells "count as resting" — they must be subtracted from the *book* holdings, not from holdings that already netted the same resting ticket.

#3 [MAJOR] (from deepseek-v4.1-flash:cloud)
cover_reason: `underlying = instr.symbol if isinstance(instr, Equity) else instr.underlying`. For a non-Equity, non-OptionContract instrument that passed `_single_sell` this would raise `AttributeError`, but `_single_sell` only returns Equity or CALL OptionContract, so this is safe. However `_single_sell` accepts `legs is None` and reads `order.instrument`/`order.side`/`order.quantity` via `getattr` with default `None`; an `Order` whose `legs` is an empty list `[]` falls to `elif len(legs) == 1` which is False, so it returns `None` — correct per the notes. But an `Order` with `legs` being a non-sized iterable (e.g. a generator) raises `TypeError` on `len(legs)`, which is not caught. The spec says "Raise nothing for ordinary input"; a generator leg list is not ordinary, so MINOR at most.

#4 [MAJOR] (from deepseek-v4.1-flash:cloud)
uncovered: `lots = int(remaining // mult)` where `mult` is `instr.multiplier`. The guard `if mult <= 0` skips non-positive multipliers, but `mult` may be a non-numeric type (e.g. `None` or a string) on a malformed `OptionContract`; `mult <= 0` then raises `TypeError`, and `remaining // mult` raises `TypeError`/`InvalidOperation`. The spec says "Raise nothing for ordinary input" — a malformed multiplier is not ordinary, but `covers` was defensively wrapped for exactly this class of malformed field, so `uncovered` is inconsistent. Concrete failure: `uncovered({_call("210", mult=None): D(-1)})` raises `TypeError` instead of returning `{"AAPL": D(1)}`.

#5 [MAJOR] (from deepseek-v4.1-flash:cloud)
uncovered: `shares_by_symbol[instr.symbol] = shares_by_symbol.get(instr.symbol, Decimal(0)) + qty` sums *all* Equity rows for a symbol, including negative ones, and then `shares = max(Decimal(0), shares_by_symbol.get(underlying, Decimal(0)))`. This is correct per spec ("NEGATIVE shares provide zero lots"). But the share-lot spending loop uses `remaining = shares` and decrements by `Decimal(cover) * Decimal(mult)`; if `mult` is a `Decimal` with a fractional part, `int(remaining // mult)` truncates and `remaining` can go slightly negative, which is harmless. Not a blocker.

#6 [MINOR] (from deepseek-v4.1-flash:cloud)
holdings: `if not isinstance(key, tuple) or len(key) != 2: continue` silently drops malformed book keys. The spec says "Sum `mirror.book` (keys are (account, instrument))" — dropping a malformed key means a real holding is invisible, which is *pessimistic* (safe direction) but hides data corruption. Acceptable.

#7 [MINOR] (from deepseek-v4.1-flash:cloud)
cover_reason: the reason string reports `shares` as the sum of *all* Equity rows for the underlying, clamped at zero, and `longs` as the sum of positive CALL quantities. The spec says "shares is the held quantity of the underlying's Equity (0 when none) and longs the held long calls of that underlying (sum of quantities above zero)". The implementation matches. But `shares` is computed from `held` *after* the accepted-sell subtraction, so the reported share count reflects the batch-adjusted holdings, not the mirror's proven holdings. The spec is ambiguous; the tests do not assert the numbers (per the KEPT learning feedback), so this is unverified.

#8 [MINOR] (from deepseek-v4.1-flash:cloud)
covers: `except (TypeError, ValueError)` does not catch `AttributeError` if `long` or `short` lacks `.underlying`/`.right`/etc. The spec says "Raise nothing for ordinary input"; a non-OptionContract passed to `covers` is not ordinary, but the defensive wrapping suggests the intent was to swallow all malformed-field errors. Concrete failure: `covers(None, short)` raises `AttributeError`.

#9 [MINOR] (from deepseek-v4.1-flash:cloud)
Acceptance-test completeness (KEPT, still unaddressed): the cross-instrument `accepted` interaction is untested — no test passes an accepted sell of a *different* instrument than the order under test and asserts the interaction. The implementation subtracts any accepted sell's quantity from `held[its instrument]`, which is correct, but a bug that subtracted from the wrong instrument would pass every existing test.

#10 [MINOR] (from deepseek-v4.1-flash:cloud)
Acceptance-test completeness (KEPT, still unaddressed): the share-lot spending order is unpinned — every test uses multiplier 100, so a bug that spent lots on the *smallest*-multiplier short first would pass. The implementation sorts `reverse=True` on `t[2]` (multiplier), which matches the spec, but no test distinguishes.

#11 [MINOR] (from deepseek-v4.1-flash:cloud)
Acceptance-test completeness (KEPT, still unaddressed): the reason string's numbers are unasserted — tests check only substrings `"uncovered"`, `"AAPL"`, `"proven venue fills only"`. A bug reporting `after_count` as `before_count` or `shares` as `longs` would pass.

#12 [MINOR] (from deepseek-v4.1-flash:cloud)
Acceptance-test completeness (KEPT, still unaddressed): no test feeds a multi-leg (Combo) open ticket through `ticket_contracts` into `holdings`. The implementation iterates `ticket_contracts(...).items()` and adds negative quantities, which handles multi-leg, but a bug that only read the first leg would pass.

#13 [MINOR] (from deepseek-v4.1-flash:cloud)
Acceptance-test completeness (KEPT, still unaddressed): `uncovered` with a short call whose own multiplier is zero or negative is untested. The implementation skips `mult <= 0` in the share-spending loop, so such a short is never covered by shares and remains uncovered — correct per spec, but unverified.

#14 [MAJOR] (from glm-5.3-flash:cloud)
uncovered: the arbiter-kept multiplier defect is only half-fixed. The new guard `if mult <= 0 or remaining <= 0 or unmatched[sc] <= 0: continue` skips zero/negative, but a quiet-NaN multiplier compares False against everything, so execution falls through to `lots = int(remaining // mult)`, and `Decimal(100) // Decimal('NaN')` raises `decimal.InvalidOperation` (an ArithmeticError — nothing in this region catches it). sNaN raises at `mult <= 0` itself; None/str/float raise TypeError at `mult <= 0` or inside `sorted_shorts = sorted(shorts, key=lambda t: t[2], reverse=True)` (mixed int/str keys). Concrete failure: `uncovered({AAPL: D(100), bad_call: D(-1)})` where `bad_call` is an AAPL call with `multiplier == Decimal('NaN')` raises `decimal.InvalidOperation` instead of returning `{"AAPL": D(1)}` — the order path goes down rather than failing closed. The implementer note ("share-lot spending skips shorts with multiplier <= 0") is false for NaN.

#15 [MAJOR] (from glm-5.3-flash:cloud)
cover_reason: `_single_sell` lets a non-finite quantity through, and `uncovered` then makes it vanish — fail-open on the unreadable-input path. `if qty_dec <= 0:` sits outside the `try` around `Decimal(qty)`; `Decimal('NaN') <= 0` is False (quiet-NaN comparisons never raise), so a SELL with quantity "NaN" passes the strictly-positive gate as a checkable sell. In `uncovered`, `if qty < 0:` / `elif qty > 0:` both compare False for NaN, so the row silently matches neither branch and disappears; `after == before` and the gate answers None. Concrete failure: `cover_reason(_state(), _order(C210, Side.SELL, "NaN"))` returns None — a short call is let toward the venue with no cover evaluation at all (hazard H1); worse, with `(CC, C210): -1` already in the book, `held_after[C210] = -1 - NaN = NaN` makes the EXISTING uncovered short vanish from the math too. Also: `Decimal('sNaN')` as quantity raises `decimal.InvalidOperation` at `qty_dec <= 0` (uncaught, order path down), and `Decimal('Infinity')` passes the gate and later raises OverflowError at `units = int(-qty)` in `uncovered`.

#16 [MAJOR] (from glm-5.3-flash:cloud)
holdings: unreadable book rows are treated as absent, which is fail-open for demand rows. `if not isinstance(key, tuple) or len(key) != 2: continue` silently drops any row whose key is not a 2-tuple. The gate cannot know whether the unreadable row was supply (shares — where dropping is pessimistic) or a short call (demand — where dropping hides the hazard), so the only safe default is to fail closed. Concrete failure: a book row keyed `"CC|C210"` (string key from a bad deserialization) with value -1 is skipped; `holdings` reports no short call, `uncovered` sees no demand, and `cover_reason(state, _order(AAPL, Side.SELL, "100"))` returns None — the shares are sold while a short call rests on them (hazard H2). The implementer note calls skipping an improvement over raising; for a cover gate it is the unsafe direction.

#17 [MINOR] (from glm-5.3-flash:cloud)
covers: the except is narrower than the implementer note claims. `except (TypeError, ValueError):` does not catch `decimal.InvalidOperation` (an ArithmeticError) from an sNaN strike/expiry/multiplier comparison, so the public function raises instead of returning False ("non-comparable or malformed contract fields return False" per the note). Downstream `_contract_match` swallows it with `except Exception`, so only direct callers see the raise. Concrete: `covers(a, b)` with same underlying/both CALL/same multiplier and `b.strike == Decimal('sNaN')` raises instead of returning False.

#18 [MINOR] (from glm-5.3-flash:cloud)
uncovered: the prescribed algorithm was replaced and the choice among maximum matchings is unpinned and observable. Spec: "Kuhn's augmenting paths, expanding every short and long to one node per unit"; code: `"""Maximum bipartite matching of long-call units to short-call units using Dinic's algorithm."""`. Cardinality is equivalent (max-flow = max matching), but WHICH short stays unmatched when longs are scarce differs between implementations, and the share-lot spend (`sorted_shorts = sorted(shorts, key=lambda t: t[2], reverse=True)`) makes that choice observable. Concrete: longs {1× wide covering both}, shorts {1× mult-100, 1× mult-50}, shares 60 — leaving the mult-50 short unmatched yields `{}`, leaving the mult-100 short unmatched yields `{U: D(1)}` (60//100 = 0 lots); Dinic's edge order decides. The spec pins neither, and no test can catch it (every test uses multiplier 100 — arbiter-kept gap).

#19 [MINOR] (from glm-5.3-flash:cloud)
tests/test_tos_cover.py: the arbiter-kept acceptance gaps all remain. (a) Cross-instrument `accepted` interaction untested: no test passes an accepted share SELL with a call sell (or an accepted call sell with the share sale), although `held[earlier_instr] = held.get(earlier_instr, Decimal(0)) - earlier_qty` makes a batch-mate's share sale spend the call's share cover — the code handles it, nothing pins it. (b) Reason-string numbers unasserted: tests check only `"uncovered" in reason and "AAPL" in reason`; the clamp `shares = max(Decimal(0), shares)` reports 0 where the spec's "the held quantity of the underlying's Equity" gives -100 for a short-share position — an untested default standing in for the true value. (c) No test feeds a multi-leg (Combo) open ticket through `ticket_contracts(ticket.queued, ticket.remaining)` into `holdings`; the per-leg `if qty < 0:` add path is untested for Combos. (d) Share-lot spending across different multipliers is unpinned ("the tests only use 100"). No test passes without the fix (all four functions were `raise NotImplementedError` at baseline), so that gate is clean.

## The patch under review (unified diff)
```diff
diff --git a/src/trade_engine/tos_paper/cover.py b/src/trade_engine/tos_paper/cover.py
index f62a7fe..508001a 100644
--- a/src/trade_engine/tos_paper/cover.py
+++ b/src/trade_engine/tos_paper/cover.py
@@ -28,21 +28,225 @@ from trade_engine.ledger.mirror import MirrorState, ticket_contracts
 def covers(long: OptionContract, short: OptionContract) -> bool:
     """Whether one long call can cover one short call: same underlying, both calls, same multiplier,
     long expiry >= short expiry, long strike <= short strike (a debit diagonal; a credit one covers nothing)."""
-    raise NotImplementedError
+    try:
+        return (
+            long.underlying == short.underlying
+            and long.right is OptionRight.CALL
+            and short.right is OptionRight.CALL
+            and long.multiplier == short.multiplier
+            and long.expiry >= short.expiry
+            and long.strike <= short.strike
+        )
+    except (TypeError, ValueError):
+        return False
 
 
 def uncovered(held: Mapping[Instrument, Decimal]) -> dict[str, Decimal]:
     """Per underlying: the short call contracts nothing in ``held`` covers (only entries above zero)."""
-    raise NotImplementedError
+    shorts_by_underlying: dict[str, list[tuple[OptionContract, int, int]]] = {}
+    longs_by_underlying: dict[str, list[tuple[OptionContract, int]]] = {}
+    shares_by_symbol: dict[str, Decimal] = {}
+    for instr, qty in held.items():
+        if isinstance(instr, Equity):
+            shares_by_symbol[instr.symbol] = shares_by_symbol.get(instr.symbol, Decimal(0)) + qty
+        elif isinstance(instr, OptionContract) and instr.right is OptionRight.CALL:
+            if qty < 0:
+                units = int(-qty)
+                if units:
+                    shorts_by_underlying.setdefault(instr.underlying, []).append((instr, units, instr.multiplier))
+            elif qty > 0:
+                units = int(qty)
+                if units:
+                    longs_by_underlying.setdefault(instr.underlying, []).append((instr, units))
+    result: dict[str, Decimal] = {}
+    for underlying, shorts in shorts_by_underlying.items():
+        longs = longs_by_underlying.get(underlying, [])
+        matched = _contract_match(longs, shorts)
+        unmatched: dict[OptionContract, int] = {}
+        for sc, units, _ in shorts:
+            unmatched[sc] = units - matched.get(sc, 0)
+        shares = max(Decimal(0), shares_by_symbol.get(underlying, Decimal(0)))
+        sorted_shorts = sorted(shorts, key=lambda t: t[2], reverse=True)
+        remaining = shares
+        for sc, units, mult in sorted_shorts:
+            if mult <= 0 or remaining <= 0 or unmatched[sc] <= 0:
+                continue
+            lots = int(remaining // mult)
+            cover = min(unmatched[sc], lots)
+            unmatched[sc] -= cover
+            remaining -= Decimal(cover) * Decimal(mult)
+        uncovered_units = sum(unmatched.values())
+        if uncovered_units > 0:
+            result[underlying] = Decimal(uncovered_units)
+    return result
+
+
+def _contract_match(longs: list[tuple[OptionContract, int]], shorts: list[tuple[OptionContract, int, int]]) -> dict[OptionContract, int]:
+    """Maximum bipartite matching of long-call units to short-call units using Dinic's algorithm."""
+    L = len(longs)
+    S = len(shorts)
+    if L == 0 or S == 0:
+        return {}
+    N = 2 + L + S
+    source = 0
+    sink = N - 1
+    graph: list[list[list[int]]] = [[] for _ in range(N)]
+
+    def add_edge(u: int, v: int, cap: int) -> None:
+        graph[u].append([v, cap, len(graph[v])])
+        graph[v].append([u, 0, len(graph[u]) - 1])
+
+    long_nodes: list[tuple[OptionContract, int]] = []
+    for i, (lc, units) in enumerate(longs):
+        node = 1 + i
+        add_edge(source, node, units)
+        long_nodes.append((lc, node))
+
+    short_info: list[tuple[OptionContract, int, int]] = []
+    for j, (sc, units, _mult) in enumerate(shorts):
+        node = 1 + L + j
+        fwd = [sink, units, len(graph[sink])]
+        rev = [node, 0, len(graph[node])]
+        graph[node].append(fwd)
+        graph[sink].append(rev)
+        short_info.append((sc, node, len(graph[sink]) - 1))
+
+    total_units = sum(units for _, units in longs) + sum(units for _, units, _ in shorts)
+    for lc, lnode in long_nodes:
+        for sc, snode, _rev in short_info:
+            try:
+                ok = covers(lc, sc)
+            except Exception:
+                ok = False
+            if ok:
+                add_edge(lnode, snode, total_units)
+
+    flow = 0
+    while True:
+        level = [-1] * N
+        q = [source]
+        level[source] = 0
+        qi = 0
+        while qi < len(q):
+            v = q[qi]
+            qi += 1
+            for e in graph[v]:
+                if e[1] > 0 and level[e[0]] < 0:
+                    level[e[0]] = level[v] + 1
+                    q.append(e[0])
+        if level[sink] < 0:
+            break
+        it = [0] * N
+
+        def dfs(v: int, up_to: int) -> int:
+            if v == sink:
+                return up_to
+            i = it[v]
+            while i < len(graph[v]):
+                e = graph[v][i]
+                if e[1] > 0 and level[v] < level[e[0]]:
+                    d = dfs(e[0], up_to if up_to < e[1] else e[1])
+                    if d:
+                        e[1] -= d
+                        graph[e[0]][e[2]][1] += d
+                        return d
+                i += 1
+                it[v] = i
+            return 0
+
+        while True:
+            f = dfs(source, total_units)
+            if not f:
+                break
+            flow += f
+
+    matched: dict[OptionContract, int] = {}
+    for sc, _snode, rev_idx in short_info:
+        matched[sc] = graph[sink][rev_idx][1]
+    return matched
 
 
 def holdings(mirror: MirrorState) -> dict[Instrument, Decimal]:
     """What the mirror proves the venue holds, pessimistically: the book summed over accounts, less every
     open ticket's unfilled sell remainder (a resting buy is not credited until it fills)."""
-    raise NotImplementedError
+    held: dict[Instrument, Decimal] = {}
+    for key, qty in mirror.book.items():
+        if not isinstance(key, tuple) or len(key) != 2:
+            continue
+        _account, instr = key
+        held[instr] = held.get(instr, Decimal(0)) + qty
+    for ticket in mirror.open_tickets:
+        for instr, qty in ticket_contracts(ticket.queued, ticket.remaining).items():
+            if qty < 0:
+                held[instr] = held.get(instr, Decimal(0)) + qty
+    return {instr: qty for instr, qty in held.items() if qty != 0}
 
 
 def cover_reason(mirror: MirrorState, order: Order, accepted: Sequence[Order] = ()) -> str | None:
     """None when ``order`` may go now; else why it must wait. ``accepted`` are the orders of the same
     batch already let through: their sells count as resting."""
-    raise NotImplementedError
+    sell = _single_sell(order)
+    if sell is None:
+        return None
+    instr, qty = sell
+    held = holdings(mirror)
+    for earlier in accepted:
+        earlier_sell = _single_sell(earlier)
+        if earlier_sell is None:
+            continue
+        earlier_instr, earlier_qty = earlier_sell
+        held[earlier_instr] = held.get(earlier_instr, Decimal(0)) - earlier_qty
+    before = uncovered(held)
+    held_after = dict(held)
+    held_after[instr] = held_after.get(instr, Decimal(0)) - qty
+    after = uncovered(held_after)
+    underlying = instr.symbol if isinstance(instr, Equity) else instr.underlying
+    after_count = after.get(underlying)
+    if after_count is None:
+        return None
+    before_count = before.get(underlying, Decimal(0))
+    if after_count <= before_count:
+        return None
+    shares = Decimal(0)
+    for key, q in held.items():
+        if isinstance(key, Equity) and key.symbol == underlying:
+            shares += q
+    shares = max(Decimal(0), shares)
+    longs = Decimal(0)
+    for key, q in held.items():
+        if isinstance(key, OptionContract) and key.right is OptionRight.CALL and key.underlying == underlying and q > 0:
+            longs += q
+    return (
+        f"selling {qty} {instr.symbol} would leave {after_count} short {underlying} call(s) "
+        f"uncovered ({before_count} now); the mirror book proves {shares} share(s) and {longs} long call(s) "
+        f"held (proven venue fills only; shares from an assignment are not booked)"
+    )
+
+
+def _single_sell(order: Order) -> tuple[Instrument, Decimal] | None:
+    """Return (instrument, positive quantity) for a single-leg sell of an Equity or a call option."""
+    legs = getattr(order, "legs", None)
+    if legs is None:
+        instr = getattr(order, "instrument", None)
+        side = getattr(order, "side", None)
+        qty = getattr(order, "quantity", None)
+    elif len(legs) == 1:
+        leg = legs[0]
+        instr = getattr(leg, "instrument", None)
+        side = getattr(leg, "side", None)
+        qty = getattr(leg, "quantity", None)
+    else:
+        return None
+    if instr is None or side is not Side.SELL or qty is None:
+        return None
+    try:
+        qty_dec = Decimal(qty)
+    except Exception:
+        return None
+    if qty_dec <= 0:
+        return None
+    if isinstance(instr, Equity):
+        return instr, qty_dec
+    if isinstance(instr, OptionContract) and instr.right is OptionRight.CALL:
+        return instr, qty_dec
+    return None
```

Rule on all 19 findings by number. REJECT only if demonstrably wrong (cite the criterion 1-5). KEEP everything else.