"""ToS domain behaviour (P0a-T5): position book, follow flatness, netting reasons, fills, the door.

Every assertion is a literal value written from the rules, through the public functions and the
door's documented refusal table (``trade_engine.tos_paper._rs``). No frozen oracle is consulted.
"""

from __future__ import annotations

import decimal
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trade_engine.domain.instruments import Combo, ComboLeg, OptionContract, OptionRight, Side  # noqa: E402
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce  # noqa: E402
from trade_engine.domain.portfolio import Position  # noqa: E402
from trade_engine.interfaces.broker import VenueFill, VenueOrder, VenueOrderAllocation, VenuePosition  # noqa: E402
from trade_engine.ledger.errors import LedgerFoldError  # noqa: E402
from trade_engine.ledger.state import AccountState  # noqa: E402
from trade_engine.tos_paper import _rs as door  # noqa: E402
from trade_engine.tos_paper import follow as PF  # noqa: E402
from trade_engine.tos_paper import netting as PNET  # noqa: E402
from trade_engine.tos_paper import reconcile as PR  # noqa: E402
from trade_engine.tos_paper import slippage as PS  # noqa: E402
from trade_engine.tos_paper.normalize import NormalizeError  # noqa: E402

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)


def opt(strike: str = "200", right: str = "P") -> OptionContract:
    return OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal(strike),
                          right=OptionRight(right), multiplier=100)


P200, P195 = opt(), opt("195")


# -- reconcile.position_book (public) ------------------------------------------------------------


def test_position_book_sums_signed_quantities_per_instrument() -> None:
    positions = [
        VenuePosition(P200, Decimal("2"), Decimal("1.00"), T),
        VenuePosition(P200, Decimal("-1"), Decimal("1.20"), T),
        VenuePosition(P195, Decimal("3"), Decimal("1.00"), T),
    ]
    assert PR.position_book(positions) == {P200: Decimal("1"), P195: Decimal("3")}


def test_position_book_of_no_positions_is_empty() -> None:
    assert PR.position_book([]) == {}


# -- follow._flat: no src caller (see the report); a direct call pins its rule --------------------


def _order(instrument, oid: str = "so-1") -> Order:
    return Order(order_id=oid, account_id="OPT_CSP", instrument=instrument, order_type=OrderType.LIMIT,
                 side=Side.SELL, quantity=Decimal("1"), command_id=oid, created_at=T,
                 limit_price=Decimal("1.00"), tif=TimeInForce.GTC, state=OrderState.ACCEPTED)


def _account(positions, orders) -> AccountState:
    return AccountState(
        account_id="OPT_CSP",
        positions=MappingProxyType({i: Position(account_id="OPT_CSP", instrument=i, quantity=Decimal(q),
                                                avg_cost=Decimal("1")) for i, q in positions}),
        orders=MappingProxyType({o.order_id: o for o in orders}),
    )


def test_an_order_is_flat_only_when_the_sim_holds_none_of_its_instrument() -> None:
    vertical = Combo([ComboLeg(P200, 1, Side.SELL), ComboLeg(P195, 1, Side.BUY)])
    single = _order(P200)
    assert PF._flat(_account([(P200, "1")], [single]), single) is False
    assert PF._flat(_account([(P195, "1")], [single]), single) is True
    assert PF._flat(_account([], [single]), single) is True
    assert PF._flat(_account([(P200, "-1")], [_order(vertical)]), _order(vertical)) is False
    assert PF._flat(_account([], [_order(vertical)]), _order(vertical)) is True


# -- netting.vertical_reason ----------------------------------------------------------------------


def test_a_one_to_one_vertical_has_no_reason_to_refuse() -> None:
    assert PNET.vertical_reason(Combo([ComboLeg(P200, 1, Side.SELL), ComboLeg(P195, 1, Side.BUY)])) is None


def test_a_one_to_two_ratio_spread_gives_its_reason() -> None:
    ratio = Combo([ComboLeg(P200, 1, Side.SELL), ComboLeg(P195, 2, Side.BUY)])
    assert PNET.vertical_reason(ratio) == "a 1:2 ratio spread is not a 1:1 vertical"


# -- slippage.allocate_venue_fill: pro-rata split, fee to the cent --------------------------------


def _fill(quantity: str, fee: str) -> VenueFill:
    return VenueFill("vf-1", "tos:t1", P200, Decimal(quantity), Decimal("1.05"), T, Side.SELL, Decimal(fee))


def _ticket(*allocations: tuple[str, str]) -> VenueOrder:
    total = sum(Decimal(q) for _, q in allocations)
    return VenueOrder(
        venue_order_id="tos:t1", instrument=P200, order_type=OrderType.LIMIT, side=Side.SELL, quantity=total,
        submitted_at=T, tif=TimeInForce.DAY, limit_price=Decimal("1.05"),
        allocations=tuple(VenueOrderAllocation(o, "OPT_CSP", Decimal(q)) for o, q in allocations),
    )


def test_a_fill_splits_pro_rata_across_the_orders_with_the_fee_to_the_cent() -> None:
    fills = PS.allocate_venue_fill(_fill("4", "0.04"), _ticket(("so-1", "1"), ("so-2", "3")))
    assert [(f.order_id, f.quantity, f.fee, f.price, f.fill_id) for f in fills] == [
        ("so-1", Decimal("1"), Decimal("0.01"), Decimal("1.05"), "vf-1:so-1"),
        ("so-2", Decimal("3"), Decimal("0.03"), Decimal("1.05"), "vf-1:so-2"),
    ]


def test_a_later_partial_fill_goes_only_to_the_order_still_lacking_quantity() -> None:
    fills = PS.allocate_venue_fill(_fill("1", "0"), _ticket(("so-1", "1"), ("so-2", "3")),
                                   already_filled={"so-1": Decimal("1")})
    assert [(f.order_id, f.quantity, f.fee) for f in fills] == [("so-2", Decimal("1"), Decimal("0"))]


# -- the door's documented refusal table (tos_paper/_rs.py) ---------------------------------------


def test_a_zero_over_zero_crosses_as_decimal_division_undefined() -> None:
    refused = door.refusal("tos_division_undefined", "")
    assert isinstance(refused, decimal.InvalidOperation)
    assert refused.args == ([decimal.DivisionUndefined],)


def test_an_overflow_crosses_as_python_overflow_error() -> None:
    refused = door.refusal("tos_overflow_error", "too big")
    assert type(refused) is OverflowError and str(refused) == "too big"


def test_a_tos_normalize_refusal_keeps_its_message() -> None:
    refused = door.refusal("tos_normalize", "bad row")
    assert type(refused) is NormalizeError and str(refused) == "bad row"


def test_a_kind_the_door_does_not_name_is_the_ledgers_fold_error() -> None:
    refused = door.refusal("bad_shape", "boom")
    assert type(refused) is LedgerFoldError and str(refused) == "bad_shape: boom"


def test_a_ledger_fold_kind_keeps_its_message_unprefixed() -> None:
    refused = door.refusal("fold", "boom")
    assert type(refused) is LedgerFoldError and str(refused) == "boom"


# -- the door's text probe: explicit Unicode facts, one row per character -------------------------


def test_the_text_probe_classifies_explicit_code_points() -> None:
    chars = "7²A\t ͸"  # digit seven, superscript two, A, tab, no-break space, unassigned U+0378
    assert door.decide("text_probe", {"chars": chars}) == [
        [True, True, False],    # '7': a digit, printable, not space
        [True, True, False],    # '²': isdigit (superscript), printable
        [False, True, False],   # 'A'
        [False, False, True],   # tab: not printable, space
        [False, False, True],   # no-break space: not printable, space
        [False, False, False],  # U+0378: unassigned, not printable
    ]
