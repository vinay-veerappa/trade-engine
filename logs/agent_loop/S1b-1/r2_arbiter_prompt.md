# TICKET S1b-1: the cover rule: a pure module that says when an order must wait (shares or a debit-diagonal long call cover a short call)

## Defect this patch must close
Nothing stops a short call reaching the venue before the shares or long call that cover it are proven held, or those shares being sold while a short call rests on them (hazards H1, H2). docs/architecture/TOS_STOCK_AND_LEAPS_MIRROR.md 'S1b design'.

## Mechanical gates (facts - you may not contradict these)
static: 6 block(s) well-formed; compile: build succeeded; test: no regressions; 1589 passed, 0 failed, 56 expected failure(s) now green; all 36 acceptance test(s) green; lock-scope: no lock primitive in python

## Findings to rule on

#1 [BLOCKER] (from deepseek-v4.1-flash:cloud)
cover_reason: `_single_sell` accepts a negative quantity and returns it as-is; `qty = getattr(leg, "quantity", None)` then `return instr, Decimal(qty)`. The docstring claims "positive quantity" but nothing enforces it. A SELL order with `quantity=-100` (or a leg with negative quantity) is treated as a sell of 100 units: `held_after[instr] = held_after.get(instr, Decimal(0)) - qty` becomes `held + 100`, which *adds* cover and can suppress a legitimate wait, or for an Equity it inflates shares. Concrete failure: `cover_reason(_state(), _order(AAPL, Side.SELL, "-100"))` returns None (no wait) even though a negative-quantity sell is malformed input that must not be silently reinterpreted as a buy-sized credit. The spec says "Raise nothing for ordinary input" — a negative quantity is not ordinary and must not be coerced into a positive sell.

#2 [BLOCKER] (from deepseek-v4.1-flash:cloud)
cover_reason: `_single_sell` does not validate that the order's instrument is the same kind the leg claims, and more importantly it does not reject a SELL whose `quantity` is zero. `if instr is None or side is not Side.SELL or qty is None: return None` — a zero quantity passes. Then `held_after[instr] = held_after.get(instr, Decimal(0)) - Decimal(0)` leaves `after == before`, so it returns None (correct outcome by luck), but the same zero-quantity path with a negative `before` interaction is untested. More seriously, the guard `qty is None` uses `is` on a possibly-Decimal/possibly-int field; a `quantity=0` Equity sell is accepted as a "sell" and flows through the whole matching machinery. This is a correctness gap in the unreadable-input path: the function must reject non-positive quantities explicitly rather than relying on arithmetic coincidence.

#3 [MAJOR] (from deepseek-v4.1-flash:cloud)
cover_reason: the reason string's `shares` and `longs` are computed from `held` (post-accepted-subtraction) but the spec says "shares is the held quantity of the underlying's Equity (0 when none) and longs the held long calls of that underlying (sum of quantities above zero)". The code sums `shares += q` for every Equity key matching the symbol without clamping per-key, then clamps the total with `max(Decimal(0), shares)`. If one account holds +100 and another holds -150 of the same Equity, the sum is -50 and clamps to 0 — but the spec's "held quantity" is ambiguous and the per-key negative is silently absorbed. The acceptance tests never assert the numeric values in the string (known KEPT defect), so this is unverified. Concrete failure: `_state({(A, AAPL): 100, (B, AAPL): -150})` reports "0 share(s)" while the pessimistic reading should arguably report the net or refuse; either way the number is unasserted and wrong-by-construction is possible.

#4 [MAJOR] (from deepseek-v4.1-flash:cloud)
uncovered: `_kuhn_match` is called with `long_units` and `short_units` expanded one node per unit via `short_units.extend([sc] * units)` and `long_units.extend([lc] * units)`. For a large short position (e.g. `C210: D(-1000000)`) this materializes a million-element list and a million-column `match_to_long`, and the iterative Kuhn is O(V*E) — a memory/CPU blowup on ordinary-but-large input. The spec says "expanding every short and long to one node per unit", so this matches the letter, but there is no bound and no test exercises a large quantity. Concrete failure: `uncovered({C210: D(-10**7)})` allocates ~10^7 OptionContract references and runs augmenting paths over them.

#5 [MAJOR] (from deepseek-v4.1-flash:cloud)
uncovered: the share-lot spending loop mutates `remaining_shares` by `Decimal(cover) * Decimal(mult)`, but `covered_by_shares` is keyed by `id(sc)`. If two distinct short entries share the same `OptionContract` object identity (e.g. the same contract object appears twice in `held` under different keys, or `held` is built with a repeated key), `id(sc)` collides and `unmatched[id(sc)]` / `covered_by_shares[id(sc)]` overwrite each other. Concrete failure: `held = {C210: D(-1)}` is fine, but a caller passing a dict-like with the same OptionContract instance under two keys (or `uncovered` being called with a mapping whose iteration yields the same object twice) double-counts or drops units. The `id()`-keyed bookkeeping is fragile; the spec's per-underlying aggregation should key by the contract's identity semantics, not raw `id()`.

#6 [MINOR] (from deepseek-v4.1-flash:cloud)
cover_reason: `_single_sell` uses `getattr(order, "legs", None)` and treats a falsy `legs` (empty tuple, empty list, or `None`) as "fall back to order-level fields". The implementer note claims this fixes an empty-`legs` no-op, but it also means an order that genuinely has `legs=()` (a malformed multi-leg order with zero legs) is reinterpreted as a single-leg order using `order.instrument`/`order.side`/`order.quantity`. If those order-level fields are stale or default, the cover rule acts on the wrong instrument. Concrete failure: an order with `legs=()` and `instrument=None` returns None (safe), but an order with `legs=()` and a populated `instrument`/`side=SELL`/`quantity` is treated as a real sell even though its leg list says it has no legs.

#7 [MINOR] (from deepseek-v4.1-flash:cloud)
holdings: `for (_account, instr), qty in mirror.book.items()` assumes every book key is a 2-tuple `(account, instrument)`. If `mirror.book` ever contains a malformed key (wrong arity), this raises `ValueError` during unpacking — the spec says "Raise nothing for ordinary input", and a malformed book is not ordinary, but the unreadable-input path is unguarded. Concrete failure: `mirror.book = {AAPL: D(100)}` raises `ValueError: too many values to unpack` instead of returning a pessimistic empty/partial result.

#8 [MINOR] (from deepseek-v4.1-flash:cloud)
cover_reason: `before_count = before.get(underlying, Decimal(0))` and `after_count = after.get(underlying)` — the wait condition is `after_count > before_count`. But `uncovered` only returns entries above zero, so `before` never contains a zero entry and `after` never contains a zero entry. When `after_count` is None (no uncovered after) the function returns None early, which is correct. However, when `before` has an entry and `after` has a *smaller* entry, the function returns None — correct. The logic is sound but relies on `uncovered`'s "only above zero" contract; if `uncovered` ever returned zero entries this would misbehave. This is a coupling risk, not a live bug.

#9 [MINOR] (from deepseek-v4.1-flash:cloud)
covers: `long.expiry >= short.expiry` and `long.strike <= short.strike` compare `Decimal`/date fields directly. If `expiry` is a string or `strike` is a float, the comparison may raise `TypeError` or compare lexicographically. The spec says "Raise nothing for ordinary input"; a string expiry is not ordinary, but the function has no type guard. Concrete failure: `covers(_call("200", "20241220"), _call("210", "20241115"))` compares strings lexicographically and may return True/False incorrectly.

#10 [MAJOR] (from glm-5.3-flash:cloud)
cover_reason: `_single_sell` never checks the sign of the sell quantity, contradicting its own docstring "Return (instrument, positive quantity) for a single-leg sell of an Equity or a call option." The only guard is `if instr is None or side is not Side.SELL or qty is None:` and both branches end in `return instr, Decimal(qty)` for any non-None qty. A SELL order carrying a negative quantity is therefore read as a sell of negative size, and in the accepted loop `held[earlier_instr] = held.get(earlier_instr, Decimal(0)) - earlier_qty` SUBTRACTS a negative — minting phantom holdings nothing proved. Concrete failure (H1, the exact hazard this module exists to stop): empty mirror; batch = [order with side=SELL, quantity=Decimal("-1") on C210 (accepted), then `_order(C210, Side.SELL, "1")`]. The accepted loop sets held[C210] = 0 - (-1) = +1 (a phantom long call), so `before = uncovered(held)` is {} and `after` is {} → `cover_reason` returns None → the naked short call reaches the venue with nothing proven held. The same minting works for shares (quantity=Decimal("-100") fabricates 100 cover shares for a later short-call sell in the batch).

#11 [MINOR] (from glm-5.3-flash:cloud)
uncovered: `lots = int(remaining_shares // mult)` raises decimal.InvalidOperation when a short call's own multiplier is 0 and shares > 0 (e.g. `uncovered({AAPL: D(100), <call with multiplier 0>: D(-1)})` crashes instead of returning a count). A negative multiplier also burns shares spuriously: `cover = min(unmatched[id(sc)], lots)` goes negative and `remaining_shares -= Decimal(cover) * Decimal(mult)` deducts a positive amount for no cover. The share-spend loop must skip shorts with `mult <= 0`.

#12 [MINOR] (from glm-5.3-flash:cloud)
holdings (acceptance-test completeness, arbiter KEPT): no test feeds a multi-leg (Combo) open ticket through `ticket_contracts(ticket.queued, ticket.remaining)` — the per-leg loop `for instr, qty in ticket_contracts(ticket.queued, ticket.remaining).items():` / `if qty < 0:` is only ever driven with single-leg tickets, so a regression that keeps one leg or mis-signs a leg stays green. Tests are read-only; this behavior must simply stay correct.

#13 [MINOR] (from glm-5.3-flash:cloud)
cover_reason (acceptance-test completeness, arbiter KEPT): the reason string's numbers are unasserted — tests check only substrings ("uncovered", "AAPL", "proven venue fills only"); after_count, before_count, shares and longs are unpinned, so the clamp `shares = max(Decimal(0), shares)` versus the spec's literal "the held quantity of the underlying's Equity" is unobservable, and a string with wrong counts would pass.

#14 [MINOR] (from glm-5.3-flash:cloud)
uncovered (acceptance-test completeness, arbiter KEPT): the share-lot spending order is unpinned — every test uses multiplier 100, so `sorted_shorts = sorted(shorts, key=lambda t: t[2], reverse=True)` is never observably exercised; a case like {AAPL: D(150), short(mult 500): D(-1), short(mult 100): D(-1)} (expected {"AAPL": D(1)}, the 100-mult short covered first) is what would pin "spend lots on the shorts with the larger multiplier first".

#15 [MINOR] (from glm-5.3-flash:cloud)
cover_reason (acceptance-test completeness): the cross-instrument `accepted` interaction is untested — an accepted share sale spending the lots a later short-call sell in the same batch needs (book {(CC, AAPL): 100}; accepted=[SELL 100 AAPL]; then SELL 1 C210 must wait) pins H1 and H2 interacting inside one batch. Also, unless the `_order` helper builds leg-carrying orders, the single-`legs` branch (`leg = legs[0]` with the leg's own instrument/side/quantity) and the Combo rejection `if len(legs) != 1: return None` are unpinned by the shown tests.

## The patch under review (unified diff)
```diff
diff --git a/src/trade_engine/tos_paper/cover.py b/src/trade_engine/tos_paper/cover.py
index f62a7fe..1819aa3 100644
--- a/src/trade_engine/tos_paper/cover.py
+++ b/src/trade_engine/tos_paper/cover.py
@@ -28,21 +28,185 @@ from trade_engine.ledger.mirror import MirrorState, ticket_contracts
 def covers(long: OptionContract, short: OptionContract) -> bool:
     """Whether one long call can cover one short call: same underlying, both calls, same multiplier,
     long expiry >= short expiry, long strike <= short strike (a debit diagonal; a credit one covers nothing)."""
-    raise NotImplementedError
+    return (
+        long.underlying == short.underlying
+        and long.right is OptionRight.CALL
+        and short.right is OptionRight.CALL
+        and long.multiplier == short.multiplier
+        and long.expiry >= short.expiry
+        and long.strike <= short.strike
+    )
 
 
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
+                shorts_by_underlying.setdefault(instr.underlying, []).append((instr, units, instr.multiplier))
+            elif qty > 0:
+                units = int(qty)
+                longs_by_underlying.setdefault(instr.underlying, []).append((instr, units))
+    result: dict[str, Decimal] = {}
+    for underlying in shorts_by_underlying:
+        shorts = shorts_by_underlying[underlying]
+        longs = longs_by_underlying.get(underlying, [])
+        short_units: list[OptionContract] = []
+        for sc, units, _ in shorts:
+            short_units.extend([sc] * units)
+        long_units: list[OptionContract] = []
+        for lc, units in longs:
+            long_units.extend([lc] * units)
+        match_to_long = _kuhn_match(long_units, short_units)
+        unmatched: dict[int, int] = {}
+        for sc, units, _ in shorts:
+            unmatched[id(sc)] = units
+        for si, long_idx in enumerate(match_to_long):
+            if long_idx >= 0:
+                unmatched[id(short_units[si])] -= 1
+        shares = max(0, shares_by_symbol.get(underlying, Decimal(0)))
+        sorted_shorts = sorted(shorts, key=lambda t: t[2], reverse=True)
+        covered_by_shares: dict[int, int] = {}
+        remaining_shares = shares
+        for sc, units, mult in sorted_shorts:
+            if remaining_shares <= 0 or unmatched[id(sc)] <= 0:
+                covered_by_shares[id(sc)] = 0
+                continue
+            lots = int(remaining_shares // mult)
+            cover = min(unmatched[id(sc)], lots)
+            covered_by_shares[id(sc)] = cover
+            remaining_shares -= Decimal(cover) * Decimal(mult)
+        uncovered_units = 0
+        for sc, units, _ in shorts:
+            uncovered_units += max(0, unmatched[id(sc)] - covered_by_shares.get(id(sc), 0))
+        if uncovered_units > 0:
+            result[underlying] = Decimal(uncovered_units)
+    return result
+
+
+def _kuhn_match(long_units: list[OptionContract], short_units: list[OptionContract]) -> list[int]:
+    """Maximum bipartite matching from long call units to short call units using Kuhn's algorithm (iterative)."""
+    match_to_long = [-1] * len(short_units)
+    for start in range(len(long_units)):
+        seen_short = [-1] * len(short_units)
+        parent_short_of_long = [-1] * len(long_units)
+        stack: list[tuple[int, int]] = [(start, 0)]
+        visited_long: set[int] = {start}
+        found = -1
+        while stack:
+            cur_long, next_idx = stack[-1]
+            if next_idx == 0:
+                visited_long.add(cur_long)
+            if next_idx < len(short_units):
+                stack[-1] = (cur_long, next_idx + 1)
+                si = next_idx
+                if not covers(long_units[cur_long], short_units[si]):
+                    continue
+                if seen_short[si] != -1:
+                    continue
+                seen_short[si] = cur_long
+                matched_long = match_to_long[si]
+                if matched_long == -1:
+                    found = si
+                    break
+                if matched_long not in visited_long:
+                    parent_short_of_long[matched_long] = si
+                    stack.append((matched_long, 0))
+            else:
+                stack.pop()
+        if found != -1:
+            si = found
+            while True:
+                long_pred = seen_short[si]
+                prev_si = parent_short_of_long[long_pred] if long_pred != start else -1
+                match_to_long[si] = long_pred
+                if prev_si == -1:
+                    break
+                si = prev_si
+    return match_to_long
 
 
 def holdings(mirror: MirrorState) -> dict[Instrument, Decimal]:
     """What the mirror proves the venue holds, pessimistically: the book summed over accounts, less every
     open ticket's unfilled sell remainder (a resting buy is not credited until it fills)."""
-    raise NotImplementedError
+    held: dict[Instrument, Decimal] = {}
+    for (_account, instr), qty in mirror.book.items():
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
+    if isinstance(instr, Equity):
+        underlying = instr.symbol
+    else:
+        underlying = instr.underlying
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
+    if legs:
+        if len(legs) != 1:
+            return None
+        leg = legs[0]
+        instr = getattr(leg, "instrument", None)
+        side = getattr(leg, "side", None)
+        qty = getattr(leg, "quantity", None)
+    else:
+        instr = getattr(order, "instrument", None)
+        side = getattr(order, "side", None)
+        qty = getattr(order, "quantity", None)
+    if instr is None or side is not Side.SELL or qty is None:
+        return None
+    if isinstance(instr, Equity):
+        return instr, Decimal(qty)
+    if isinstance(instr, OptionContract) and instr.right is OptionRight.CALL:
+        return instr, Decimal(qty)
+    return None
```

Rule on all 15 findings by number. REJECT only if demonstrably wrong (cite the criterion 1-5). KEEP everything else.