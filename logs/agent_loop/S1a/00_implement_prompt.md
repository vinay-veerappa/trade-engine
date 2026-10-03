# TICKET S1a: a share of stock is a legal mirror instrument: ticket, venue rows, queued event, netting screen
## Defect
The mirror refuses every Equity order (ticket_for raises UnsupportedCapability, _screen refuses 'equities are not mirrored', MirrorQueued accepts options and verticals only) and cannot read a share row (_contract requires an OCC symbol), so a covered call's 100 shares, or the shares an assignment leaves, would make the reconcile lie. docs/architecture/TOS_STOCK_AND_LEAPS_MIRROR.md D1, D2.
## Required change
Acceptance tests (already written, do not edit): tests/test_tos_stock.py, plus the rewritten
tests/test_mirror_ledger.py::test_a_queued_ticket_takes_a_single_option_a_vertical_or_shares. No module-level imports: put every import you need inside the body that needs it (the regions are rewritten whole; the file's import block is not yours). Edit ONLY the regions named.

Goal: one share of stock becomes a legal mirror instrument. A stock row from the venue carries an explicit
"kind": "stock"; an option row is unmarked (or "kind": "option") and must name an OCC symbol. Nothing about
options changes.

1. transport.py, region `stock_ticket` (class MirrorStockTicket, fields already there): add `__post_init__`
validation mirroring MirrorTicket's, in this order, each raising ValueError with these phrases in the message:
(a) symbol: `from trade_engine.domain.instruments import Equity`; the symbol must be a str for which
`Equity(self.symbol).symbol == self.symbol` (Equity raises ValueError on '', spaces, an OCC string; catch
ValueError and TypeError) else "stock ticket symbol must be an upper-case equity symbol, got <repr>";
(b) "ticket side must be BUY or SELL, got <repr>"; (c) quantity must be an int, not a bool, and > 0, else
"ticket quantity must be a positive int, got <repr>"; (d) "ticket order_type must be MKT or LMT, got <repr>";
(e) an LMT ticket with limit_price None or <= 0: "an LMT ticket needs a positive limit_price (I5)"; (f) a MKT
ticket with a limit_price: "a MKT ticket cannot carry a limit_price"; (g) "ticket tif must be DAY or GTC, got
<repr>". Same order as MirrorTicket.__post_init__ (copy its logic).

2. transport.py, region `ticket_for` (def ticket_for): add an Equity branch BEFORE the OptionContract guard.
`from trade_engine.domain.instruments import Equity` inside the body. For an Equity instrument: the same
order-type check (UnsupportedCapability "order type <value>: MARKET/LIMIT only"), the same TIF check
("TIF <value>: DAY/GTC only"), then a non-integral quantity raises UnsupportedCapability(f"quantity
{order.quantity} is not a whole number of shares (I5)"), then return MirrorStockTicket(symbol=
order.instrument.symbol, side=order.side.value, quantity=int(order.quantity), order_type=_TICKET_TYPES[
order.order_type], limit_price=order.limit_price if order.order_type is OrderType.LIMIT else None, tif=
_TICKET_TIFS[order.tif]). Change the existing not-an-option message to "only single option contracts, shares
and verticals are mirrored (§4.7)". Update the return annotation to `MirrorTicket | MirrorComboTicket |
MirrorStockTicket`. Everything for options and combos stays byte-for-byte.

3. normalize.py, region `contract` (def _contract): leave `_contract` as it is and add, BELOW it, a new
function `_instrument(raw: Mapping[str, object])` returning an Instrument. If "kind" is a key of raw: its value
must be the str "stock" or "option", else NormalizeError(f"row kind {kind!r} is neither 'stock' nor
'option'") (a present None counts as a bad kind). "option", or no kind key at all: return `_contract(raw)`.
"stock": `symbol = raw.get("symbol")`; `from trade_engine.domain.instruments import Equity`; try
`return Equity(symbol)` except (ValueError, TypeError) raise NormalizeError(f"not a mirrored stock symbol:
{symbol!r}") from the exception (Equity rejects '', spaces and OCC strings, and a non-str raises TypeError or
ValueError).

4. normalize.py, region `working` (def normalize_working_order): build the instrument with `_instrument(raw)`
instead of `_contract(raw)`. When it is an Equity, quantity and filled must each be whole numbers: else
NormalizeError(f"stock quantity {quantity} is not a whole number of shares") and the same for "stock filled".
The existing side/type/quantity/filled checks and their messages are unchanged for options; do the whole-number
checks AFTER the existing `quantity <= 0 or filled < 0 or filled > quantity` check. Leave the `WorkingOrder`
dataclass alone (its `instrument` annotation is not enforced at runtime).

5. normalize.py, region `position` (def normalize_position): use `_instrument(raw)`; read quantity and avg_price
with `_decimal` exactly as now (quantity first). For an Equity: quantity must be a whole number, else
NormalizeError(f"stock quantity {quantity} is not a whole number of shares") (a NEGATIVE whole number and ZERO are
legal: a short share row, a closed row), and avg_price must not be negative, else NormalizeError(f"stock avg_price
must not be negative, got {avg_price}") (ZERO is legal: shares an assignment delivered carry no cost the venue
reports). Options unchanged (no new check on their avg_price).

6. ledger/events.py, region `queued` (class MirrorQueued): accept an Equity. In `__post_init__` change the
non-Combo instrument check to accept `(OptionContract, Equity)` (`from trade_engine.domain.instruments import
Equity` inside the method) and the message to f"MirrorQueued.instrument must be an option contract, a vertical
or an equity, got {self.instrument!r} (I6)". Update the class docstring's first sentence to say the instrument
is one option contract, a 2-leg vertical Combo or shares of an Equity (quantity then counts shares). A Combo
still must be a vertical of two OptionContracts.

7. tos_paper/netting.py, region `screen` (def _screen): let a share through. Delete the early
`if isinstance(order.instrument, Equity): return ...equities are not mirrored` refusal; change the final
instrument guard to `elif not isinstance(order.instrument, (OptionContract, Equity)):` (message unchanged apart
from "is not a mirrored option or share"). For an Equity the whole-number message is f"quantity {order.quantity}
is not a whole number of shares; refusing to round (I5)", an option's stays "contracts". Order type, TIF and the
account rule stay exactly as they are (MARKET/LIMIT, DAY/GTC, the account must be mirrored).

Do not touch exits.py, session.py, broker.py, reconcile.py or ledger/mirror.py: they are already generic over
Instrument (a stock close is a later slice). Run nothing you cannot see the result of; the gate runs the full
suite.
## Regions to rewrite
### REGION id="stock_ticket"  file=src/trade_engine/tos_paper/transport.py  lines 87-99
```python
class MirrorStockTicket:
    """One order for shares of stock (no OCC symbol), as the venue driver renders and echoes it.

    Whole shares, MKT or LMT, DAY or GTC: the same limits as :class:`MirrorTicket`. S1a: the fields
    only, validation arrives with the acceptance tests in tests/test_tos_stock.py.
    """

    symbol: str                        # an equity symbol, e.g. 'AAPL' (never an OCC string)
    side: Literal["BUY", "SELL"]
    quantity: int                      # shares
    order_type: Literal["MKT", "LMT"]
    limit_price: Decimal | None
    tif: Literal["DAY", "GTC"]
```
### REGION id="ticket_for"  file=src/trade_engine/tos_paper/transport.py  lines 157-185
```python
def ticket_for(order: VenueOrder) -> MirrorTicket | MirrorComboTicket:
    """The ticket for one venue order, or UnsupportedCapability — never an approximation."""
    if isinstance(order.instrument, Combo):
        return _combo_ticket(order)
    if not isinstance(order.instrument, OptionContract):
        raise UnsupportedCapability(
            f"{order.instrument!r}: only single option contracts and verticals are mirrored (§4.7)"
        )
    if order.order_type not in _TICKET_TYPES:
        raise UnsupportedCapability(f"order type {order.order_type.value}: MARKET/LIMIT only")
    if order.tif not in _TICKET_TIFS:
        raise UnsupportedCapability(f"TIF {order.tif.value}: DAY/GTC only")
    if order.quantity != order.quantity.to_integral_value():
        raise UnsupportedCapability(
            f"quantity {order.quantity} is not a whole number of contracts (I5)"
        )
    contract = order.instrument
    return MirrorTicket(
        symbol=contract.to_occ(),
        side=order.side.value,
        quantity=int(order.quantity),
        order_type=_TICKET_TYPES[order.order_type],
        limit_price=order.limit_price if order.order_type is OrderType.LIMIT else None,
        tif=_TICKET_TIFS[order.tif],
        underlying=contract.underlying,
        expiry=contract.expiry,
        strike=contract.strike,
        right=contract.right.value,
    )
```
### REGION id="contract"  file=src/trade_engine/tos_paper/normalize.py  lines 162-167
```python
def _contract(raw: Mapping[str, object]) -> OptionContract:
    symbol = raw.get("symbol")
    try:
        return OptionContract.from_occ(str(symbol))
    except ValueError as exc:
        raise NormalizeError(f"not a mirrored option symbol: {symbol!r}") from exc
```
### REGION id="working"  file=src/trade_engine/tos_paper/normalize.py  lines 170-193
```python
def normalize_working_order(raw: Mapping[str, object]) -> WorkingOrder:
    """One Order Book row → WorkingOrder. An unknown status is PENDING_UNKNOWN."""
    side_text = str(raw.get("side", "")).strip().upper()
    if side_text not in ("BUY", "SELL"):
        raise NormalizeError(f"working order side {raw.get('side')!r}")
    type_text = str(raw.get("order_type", "")).strip().upper()
    if type_text not in _ROW_TYPES:
        raise NormalizeError(f"working order type {raw.get('order_type')!r}")
    quantity = _decimal(raw.get("quantity"), "quantity")
    filled = _decimal(raw.get("filled", "0"), "filled")
    if quantity <= 0 or filled < 0 or filled > quantity:
        raise NormalizeError(f"working order quantity {quantity} / filled {filled}")
    limit_raw = raw.get("limit_price")
    limit = None if limit_raw in (None, "") else _decimal(limit_raw, "limit_price")
    status = str(raw.get("status", "")).strip().upper()
    return WorkingOrder(
        instrument=_contract(raw),
        side=Side(side_text),
        quantity=quantity,
        filled=filled,
        order_type=_ROW_TYPES[type_text],
        limit_price=limit,
        state=_ROW_STATES.get(status, OrderState.PENDING_UNKNOWN),
    )
```
### REGION id="position"  file=src/trade_engine/tos_paper/normalize.py  lines 233-240
```python
def normalize_position(raw: Mapping[str, object], as_of: datetime) -> VenuePosition:
    """One Position row → VenuePosition (signed quantity)."""
    return VenuePosition(
        instrument=_contract(raw),
        quantity=_decimal(raw.get("quantity"), "quantity"),
        avg_price=_decimal(raw.get("avg_price"), "avg_price"),
        as_of=as_of,
    )
```
### REGION id="queued"  file=src/trade_engine/ledger/events.py  lines 407-465
```python
class MirrorQueued:
    """A venue ticket queued for sending: written ahead of the send (I2, I3).

    ``instrument`` is one option contract, or a 2-leg vertical ``Combo`` whose legs trade
    as written; for a combo ``side`` is the price effect (SELL collects a net credit,
    BUY pays a net debit) and ``quantity`` counts spread units.
    """

    venue: str
    ticket_key: str
    instrument: Instrument
    side: Side
    quantity: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    tif: TimeInForce
    allocations: tuple[MirrorAllocation, ...]
    at: datetime

    def __post_init__(self) -> None:
        if not self.venue:
            raise EventPayloadError("MirrorQueued.venue must be non-empty")
        if not self.ticket_key:
            raise EventPayloadError("MirrorQueued.ticket_key must be non-empty (I3)")
        if isinstance(self.instrument, Combo):
            _require_vertical(self.instrument, "MirrorQueued.instrument")
        elif not isinstance(self.instrument, OptionContract):
            raise EventPayloadError(
                f"MirrorQueued.instrument must be an option contract or a vertical, got {self.instrument!r} (I6)"
            )
        if not isinstance(self.side, Side):
            raise EventPayloadError(f"MirrorQueued.side must be a Side, got {self.side!r}")
        object.__setattr__(
            self, "quantity", _require_whole(self.quantity, "MirrorQueued.quantity", positive=True)
        )
        if self.order_type not in MIRROR_ORDER_TYPES:
            raise EventPayloadError(f"MirrorQueued.order_type must be MARKET or LIMIT, got {self.order_type!r}")
        if self.tif not in MIRROR_TIFS:
            raise EventPayloadError(f"MirrorQueued.tif must be DAY or GTC, got {self.tif!r}")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise EventPayloadError("MirrorQueued: a LIMIT ticket needs a limit_price (I5)")
            object.__setattr__(self, "limit_price", _as_decimal(self.limit_price, "MirrorQueued.limit_price"))
            if self.limit_price <= 0:
                raise EventPayloadError("MirrorQueued.limit_price must be positive (I5)")
        elif self.limit_price is not None:
            raise EventPayloadError("MirrorQueued: a MARKET ticket cannot carry a limit_price")
        object.__setattr__(self, "allocations", tuple(self.allocations))
        if not self.allocations or not all(isinstance(a, MirrorAllocation) for a in self.allocations):
            raise EventPayloadError("MirrorQueued.allocations must be MirrorAllocations, at least one (§4.4)")
        ids = [a.strategy_order_id for a in self.allocations]
        if len(set(ids)) != len(ids):
            raise EventPayloadError("MirrorQueued allocates one strategy order twice (I3)")
        total = sum((a.quantity for a in self.allocations), Decimal("0"))
        if total != self.quantity:
            raise EventPayloadError(
                f"MirrorQueued allocations total {total} but the ticket is {self.quantity} (I11)"
            )
        _require_utc(self.at, "MirrorQueued.at")
```
### REGION id="screen"  file=src/trade_engine/tos_paper/netting.py  lines 120-145
```python
def _screen(order: Order, mirrored: frozenset[str]) -> str | None:
    """Reason this single order cannot go to the venue at all, or None."""
    if order.account_id not in mirrored:
        return (
            f"account {order.account_id} is not mirrored on this venue "
            f"(mirrors {sorted(mirrored)}); refused at the venue only (§4.7)"
        )
    if isinstance(order.instrument, Equity):
        return f"{order.instrument.symbol}: equities are not mirrored (§4.7)"
    if isinstance(order.instrument, Combo):
        reason = vertical_reason(order.instrument)
        if reason is not None:
            return f"UnsupportedCapability: multi-leg combo: {reason}"
        if order.order_type is not OrderType.LIMIT:
            return "UnsupportedCapability: a vertical is mirrored with one net LIMIT price only"
    elif not isinstance(order.instrument, OptionContract):
        return f"UnsupportedCapability: instrument {order.instrument!r} is not a mirrored option"
    if order.order_type not in MIRRORED_ORDER_TYPES:
        return f"UnsupportedCapability: order type {order.order_type.value} (MARKET/LIMIT only)"
    if order.tif not in MIRRORED_TIFS:
        return f"UnsupportedCapability: TIF {order.tif.value} (DAY/GTC only)"
    if order.quantity != order.quantity.to_integral_value():
        return f"quantity {order.quantity} is not a whole number of contracts; refusing to round (I5)"
    # A LIMIT's price is positive by Order's own validation; tickets pass it through
    # unchanged (never averaged), so no ticket can carry a price <= 0.
    return None
```
### REGION id="S1a-ctx-ValueError-7"  file=src/trade_engine/tos_paper/transport.py  lines 73-73
Purpose: CF-31 auto-attached read-only context for symbol 'ValueError'
```python
            raise ValueError(f"ticket side must be BUY or SELL, got {self.side!r}")
```
### REGION id="S1a-ctx-OrderType-8"  file=src/trade_engine/tos_paper/normalize.py  lines 33-33
Purpose: CF-31 auto-attached read-only context for symbol 'OrderType'
```python
from trade_engine.domain.orders import OrderState, OrderType
```
### REGION id="S1a-ctx-ValueError-9"  file=src/trade_engine/ledger/events.py  lines 608-608
Purpose: CF-31 auto-attached read-only context for symbol 'ValueError'
```python
            except ValueError as err:
```
### REGION id="S1a-ctx-ValueError-10"  file=src/trade_engine/tos_paper/netting.py  lines 234-234
Purpose: CF-31 auto-attached read-only context for symbol 'ValueError'
```python
            except (ValueError, ArithmeticError) as exc:
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