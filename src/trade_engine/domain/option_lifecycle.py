"""What an option becomes at expiry, on exercise and on assignment (O2, I9). Pure.

At expiry the OCC exercises by exception: a contract in the money by at least
``EXERCISE_THRESHOLD`` is exercised (a long position) or assigned (a short one, since its
holder will exercise), and anything else expires worthless. Pin risk, a holder who
declines to exercise, is not modelled.

Physical delivery moves shares at the strike. The option's own premium goes into the
share trade's price, the way the premium goes into a tax lot: an assigned put buys shares
at strike - credit, an assigned call sells them at strike + credit, an exercised call
buys at strike + debit, an exercised put sells at strike - debit. The option leg closes
with no P&L of its own, and the shares carry it. So a cash-secured put assigned in the
money holds shares whose cost basis is strike - credit, and a put spread assigned and
exercised through both strikes realises exactly its maximum loss on the shares.

Cash settlement (SPX, SPXW) closes the option at its intrinsic value and moves that
much cash, paid by the short and received by the long.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.option_roots import Exercise, Settlement, option_style

# The OCC's exercise-by-exception threshold for expiring equity and index options.
EXERCISE_THRESHOLD = Decimal("0.01")

ZERO = Decimal("0")


class Outcome(StrEnum):
    EXPIRE = "expire"  # worthless
    EXERCISE = "exercise"  # a long position, exercised
    ASSIGN = "assign"  # a short position, assigned


def intrinsic(contract: OptionContract, underlying_price: Decimal) -> Decimal:
    """Per-share intrinsic value at ``underlying_price``; never negative."""
    if underlying_price <= ZERO:
        raise ValueError(f"Underlying price must be positive, got {underlying_price} (I5)")
    if contract.right is OptionRight.CALL:
        return max(underlying_price - contract.strike, ZERO)
    return max(contract.strike - underlying_price, ZERO)


def expiry_outcome(contract: OptionContract, held: Side, settlement_price: Decimal) -> Outcome:
    """Exercise by exception: in the money by at least the threshold is exercised or
    assigned, according to which way the position is held; anything else expires."""
    if intrinsic(contract, settlement_price) < EXERCISE_THRESHOLD:
        return Outcome.EXPIRE
    return Outcome.EXERCISE if held is Side.BUY else Outcome.ASSIGN


def is_cash_settled(contract: OptionContract) -> bool:
    return option_style(contract.underlying).settlement is Settlement.CASH


def can_exercise_early(contract: OptionContract) -> bool:
    return option_style(contract.underlying).exercise is Exercise.AMERICAN


def deliverable(contract: OptionContract) -> Equity:
    """The shares a physically settled contract delivers; a cash-settled one refuses."""
    if is_cash_settled(contract):
        raise ValueError(f"{contract.occ} settles in cash; it delivers no shares")
    return Equity(option_style(contract.underlying).underlying)


def delivery(contract: OptionContract, held: Side, premium: Decimal) -> tuple[Side, Decimal]:
    """The share trade one exercised or assigned lot makes: its side and per-share price.

    The shares are bought when a call is exercised or a put is assigned, and sold
    otherwise. The premium adds to a call's strike and comes off a put's.
    """
    if premium < ZERO:
        raise ValueError(f"Premium must not be negative, got {premium}")
    buys = (contract.right is OptionRight.CALL) is (held is Side.BUY)
    side = Side.BUY if buys else Side.SELL
    if contract.right is OptionRight.CALL:
        return side, contract.strike + premium
    price = contract.strike - premium
    if price <= ZERO:
        raise ValueError(f"{contract.occ}: premium {premium} is not below the strike (I5)")
    return side, price


def exercised_for_dividend(
    contract: OptionContract, underlying_close: Decimal, bid: Decimal, dividend: Decimal
) -> bool:
    """Whether the holder of an American call exercises it on the last session before
    the ex-dividend date, which assigns the short side.

    Holding the shares over the ex-date earns the dividend; holding the call does not.
    The holder's alternative to exercising is selling the call at the bid, which gives
    up the extrinsic value ``bid - intrinsic``. Exercise pays when that extrinsic is
    less than the dividend. The bid, not the mid, is what the holder can sell at.
    """
    if contract.right is not OptionRight.CALL:
        raise ValueError(f"{contract.occ} is a put; the dividend rule is for calls")
    if not can_exercise_early(contract):
        return False
    if dividend <= ZERO:
        raise ValueError(f"Dividend must be positive, got {dividend} (I5)")
    if bid < ZERO:
        raise ValueError(f"Bid must not be negative, got {bid}")
    value = intrinsic(contract, underlying_close)
    if value < EXERCISE_THRESHOLD:
        return False
    return bid - value < dividend
