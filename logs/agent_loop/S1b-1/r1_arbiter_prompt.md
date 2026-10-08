# TICKET S1b-1: the cover rule: a pure module that says when an order must wait (shares or a debit-diagonal long call cover a short call)

## Defect this patch must close
Nothing stops a short call reaching the venue before the shares or long call that cover it are proven held, or those shares being sold while a short call rests on them (hazards H1, H2). docs/architecture/TOS_STOCK_AND_LEAPS_MIRROR.md 'S1b design'.

## Mechanical gates (facts - you may not contradict these)
static: 6 block(s) well-formed; compile: build succeeded; test: no regressions; 1589 passed, 0 failed, 56 expected failure(s) now green; all 36 acceptance test(s) green; lock-scope: no lock primitive in python

## Findings to rule on

#1 [MAJOR] (from glm-5.3-flash:cloud)
cover_reason: `_single_sell` fails open on an empty `legs` sequence — it dispatches on `legs = getattr(order, "legs", None)` / `if legs is not None:` / `if len(legs) != 1:` / `return None`. An Order whose single-leg form is an EMPTY `legs` sequence (e.g. a dataclass default `legs: tuple[...] = ()` rather than `None`) alongside `instrument`/`side`/`quantity` is rejected as "not a single-leg sell" without ever consulting the order's own fields — the order is read as the wrong KIND of shape (legs-shaped vs fields-shaped). Concrete failure: book `{(CC, AAPL): 100, (CC, C210): -1}`; a SELL of the 100 shares in that shape returns None ("may go") and strips the shares out from under the resting short call (H2), and a SELL of C210 in that shape is let through with nothing proven held (H1) — while all 36 acceptance tests stay green because `_order` constructs the `legs=None`/one-leg shape this dispatch happens to read. The whole rule silently no-ops for that Order shape.

#2 [MINOR] (from glm-5.3-flash:cloud)
uncovered: `_kuhn_match` recurses once per rematch — `if match_to_long[si] < 0 or _dfs(match_to_long[si], seen):` — so augmenting chains deeper than the interpreter recursion limit (~1000) raise RecursionError out of `uncovered` and `cover_reason` instead of returning a count; "raise nothing for ordinary input" does not hold at scale (a held book whose long/short unit graph chains >~1000 rematches on one underlying).

#3 [MINOR] (from glm-5.3-flash:cloud)
uncovered (acceptance-test completeness): the share-lot spending order is unpinned — every test uses multiplier 100 (the spec itself notes "the tests only use 100"), so a smaller-multiplier-first implementation passes 36/36 while violating the spec: for held `{AAPL: D(100), ×100-short: D(-1), ×50-short: D(-2)}` the spec's larger-first answer is `{"AAPL": D(2)}` (pessimistic) but smaller-first returns `{"AAPL": D(1)}` and no test fails.

#4 [MINOR] (from glm-5.3-flash:cloud)
cover_reason (acceptance-test completeness): the reason string's numbers are unasserted — tests check only substrings ("uncovered", "AAPL", "proven venue fills only"), so wrong `{after_count}`/`{before_count}`/`{shares}`/`{longs}` values pass the suite; and the "everything else is ignored" half of the accepted rule (an accepted put sell, an accepted multi-leg order) is never exercised — only an accepted BUY is (`test_an_accepted_buy_does_not_spend_cover`).

#5 [MINOR] (from glm-5.3-flash:cloud)
holdings (acceptance-test completeness): no test feeds a multi-leg (Combo) open ticket through `ticket_contracts(ticket.queued, ticket.remaining)`, so the behaviour when one resting ticket moves several instruments (each negative leg added, positive legs skipped) is unpinned by the suite.

## The patch under review (unified diff)
```diff
diff --git a/src/trade_engine/tos_paper/cover.py b/src/trade_engine/tos_paper/cover.py
index f62a7fe..1057d6f 100644
--- a/src/trade_engine/tos_paper/cover.py
+++ b/src/trade_engine/tos_paper/cover.py
@@ -28,21 +28,162 @@ from trade_engine.ledger.mirror import MirrorState, ticket_contracts
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
+    """Maximum bipartite matching from long call units to short call units using Kuhn's algorithm."""
+    match_to_long = [-1] * len(short_units)
+    def _dfs(long_idx: int, seen: list[bool]) -> bool:
+        long_contract = long_units[long_idx]
+        for si, short_contract in enumerate(short_units):
+            if not covers(long_contract, short_contract):
+                continue
+            if seen[si]:
+                continue
+            seen[si] = True
+            if match_to_long[si] < 0 or _dfs(match_to_long[si], seen):
+                match_to_long[si] = long_idx
+                return True
+        return False
+    for li in range(len(long_units)):
+        _dfs(li, [False] * len(short_units))
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
+    if legs is not None:
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

Rule on all 5 findings by number. REJECT only if demonstrably wrong (cite the criterion 1-5). KEEP everything else.