"""O4: options entry rules (rules doc §6.1–§6.2) — each fires, and each does not."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side
from trade_engine.domain.option_orders import OptionIntent
from trade_engine.domain.orders import Order, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.risk import RiskControlChange
from trade_engine.eod.options import OptionContext
from trade_engine.interfaces.market_data import OptionQuote, StaleDataError
from trade_engine.ledger import CashFlow, Event, EventKind, Ledger, Mark, fold
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.options import open_structures
from trade_engine.risk_options import OptionRiskConfigurationError, OptionRiskEngine, OptionRiskRules

D = Decimal
ACCOUNT = "OPT_TEST"
SESSION = date(2026, 9, 25)
NOW = datetime(2026, 9, 25, 21, 45, tzinfo=UTC)
SNAP = datetime(2026, 9, 25, 19, 45, tzinfo=UTC)
EXPIRY = date(2026, 10, 30)
LEAPS = date(2027, 6, 17)
XYZ = Equity("XYZ")


def put(strike: str, expiry: date = EXPIRY) -> OptionContract:
    return OptionContract("XYZ", expiry, D(strike), OptionRight.PUT)


def call(strike: str, expiry: date = EXPIRY) -> OptionContract:
    return OptionContract("XYZ", expiry, D(strike), OptionRight.CALL)


P45, P40, P35 = put("45"), put("40"), put("35")
C55, C40_LEAPS = call("55"), call("40", LEAPS)
BULL_PUT = Combo((ComboLeg(P45, 1, Side.SELL), ComboLeg(P40, 1, Side.BUY)))


def rules(**changes) -> OptionRiskRules:
    values = dict(
        max_margin_frac=D("0.50"),
        allowed_regimes=frozenset({"BULL_EXPLOSIVE", "BULL_CHOPIER", "BEAR_PROTECTIVE"}),
        no_earnings_before_expiry=False,
    )
    values.update(changes)
    return OptionRiskRules(**values)


class Earnings:
    def __init__(self, found=None, unknown: bool = False) -> None:
        self.found, self.unknown = found, unknown

    def next_earnings(self, symbol, session):
        if self.unknown:
            raise StaleDataError(f"no earnings date for {symbol}")
        return self.found


class Book:
    """An account folded from a deposit, fills and marks."""

    def __init__(self, cash: str = "50000") -> None:
        self.events = [Event(account=ACCOUNT, kind=EventKind.CASH_FLOW, ts_utc=NOW, command_id="deposit",
                             payload=CashFlow(amount=D(cash), kind="deposit", as_of=NOW))]
        self.n = 0
        self.mark(XYZ, "50")

    def trade(self, instrument, side: Side, quantity: str, price: str, mark: str | None = None) -> "Book":
        self.n += 1
        order = Order(order_id=f"t{self.n}:entry", account_id=ACCOUNT, instrument=instrument, order_type=OrderType.MARKET,
                      side=side, quantity=D(quantity), command_id=f"t{self.n}:entry", created_at=NOW)
        fill = Fill(fill_id=f"f{self.n}", order_id=order.order_id, account_id=ACCOUNT, instrument=instrument,
                    quantity=D(quantity), price=D(price), venue_env="sim", filled_at=NOW, side=side)
        self.events += [
            Event(account=ACCOUNT, kind=EventKind.ORDER_SUBMITTED, payload=order, ts_utc=NOW, command_id=f"t{self.n}:submit"),
            Event(account=ACCOUNT, kind=EventKind.FILL, payload=fill, ts_utc=NOW, command_id=f"fill:f{self.n}"),
        ]
        self.mark(instrument, mark or price)
        return self

    def mark(self, instrument, price: str) -> "Book":
        self.events.append(Event(account=ACCOUNT, kind=EventKind.MARK, ts_utc=NOW, command_id=f"mark:{instrument.symbol}:{len(self.events)}",
                                 payload=Mark(instrument=instrument, price=D(price), as_of=NOW)))
        return self

    def context(self, snapshot: ChainSnapshot | None = None, snapshots: dict | None = None) -> OptionContext:
        state = fold(self.events)[ACCOUNT]
        return OptionContext(
            session=SESSION, account_id=ACCOUNT, phase="snapshot" if snapshot else "close",
            now=SNAP if snapshot else NOW, state=state, structures=open_structures(state), snapshot=snapshot,
            snapshots=snapshots or ({} if snapshot is None else {snapshot.underlying: snapshot}),
        )


def chain(price: str = "50", **quotes: tuple[str, str]) -> ChainSnapshot:
    names = {"P45": P45, "P40": P40, "P35": P35, "C55": C55, "C40_LEAPS": C40_LEAPS}
    book = {"P45": ("1.90", "2.10"), "P40": ("0.70", "0.90"), "P35": ("0.20", "0.30"), "C55": ("1.00", "1.20"), "C40_LEAPS": ("13.00", "14.00")}
    book.update(quotes)
    rows = tuple(OptionQuote(names[k], D(b), D(a), D(10), D(10), SNAP) for k, (b, a) in book.items())
    return ChainSnapshot("XYZ", SNAP, D(price), rows, None, None, "test")


def intent(instrument=P45, side=Side.SELL, quantity: str = "1", limit: str | None = "2.00", name: str = "e1") -> OptionIntent:
    return OptionIntent(
        intent_id=name, account_id=ACCOUNT, instrument=instrument, side=side, quantity=D(quantity), reason="test",
        command_id=name, order_type=OrderType.MARKET if limit is None else OrderType.LIMIT,
        limit_price=None if limit is None else D(limit),
    )


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.db").open()
    yield led
    led.close()


def evaluate(ledger, the_rules, the_intent, context, *, regime="BULL_EXPLOSIVE", earnings=None):
    engine = OptionRiskEngine(the_rules, ReplayClock(NOW), ledger, venue_id="sim", regime_of=lambda s: regime, earnings=earnings)
    return engine.evaluate(the_intent, context)


def rule(verdict, name: str):
    return next(r for r in verdict.evaluations if r.rule_name == name)


# -- margin: the whole book, with the entry, under half of equity (§6.1) ---------------


def test_puts_whose_margin_stays_under_half_of_equity_pass(ledger) -> None:
    # A naked 45 put at S=50 for 2.00: max(20% x 50 - 5 OTM, 10% x 45) + 2.00 = 7.00 -> $700.
    verdict = evaluate(ledger, rules(), intent(quantity="35"), Book().context())
    assert rule(verdict, "margin").passed and rule(verdict, "margin").measured_value == D("24500")
    assert verdict.accepted


def test_puts_that_take_margin_past_half_of_equity_refuse(ledger) -> None:
    verdict = evaluate(ledger, rules(), intent(quantity="36"), Book().context())
    assert not rule(verdict, "margin").passed and not verdict.accepted


def test_margin_counts_what_the_account_already_holds(ledger) -> None:
    book = Book().trade(P35, Side.SELL, "20", "0.25")  # 20 x (max(10 - 15, 3.5) + 0.25) x 100 = 7,500
    verdict = evaluate(ledger, rules(), intent(quantity="25"), book.context())  # + 17,500
    assert rule(verdict, "margin").measured_value == D("25000") and rule(verdict, "margin").passed
    verdict = evaluate(ledger, rules(), intent(quantity="26"), book.context())
    assert not rule(verdict, "margin").passed


def test_at_a_snapshot_the_book_is_margined_at_its_quotes(ledger) -> None:
    # Last night's mark says the short 35s are cheap; at 15:45 they have tripled.
    book = Book().trade(P35, Side.SELL, "20", "0.25")
    calm = evaluate(ledger, rules(), intent(quantity="25"), book.context())
    assert calm.accepted
    selloff = evaluate(ledger, rules(), intent(quantity="25", limit=None), book.context(chain(P35=("4.00", "4.40"), P45=("6.00", "6.40"))))
    assert not rule(selloff, "margin").passed


def test_at_a_snapshot_held_options_are_revalued_at_its_mids(ledger) -> None:
    # The same limit entry: 25,000 of margin at last night's marks, far more at 15:45.
    book = Book().trade(P35, Side.SELL, "20", "0.25")
    assert evaluate(ledger, rules(), intent(quantity="25"), book.context()).accepted
    live = evaluate(ledger, rules(), intent(quantity="25"), book.context(chain(P35=("4.00", "4.40"))))
    assert not rule(live, "margin").passed


def test_at_a_snapshot_the_underlying_is_valued_at_its_price(ledger) -> None:
    # The 45 puts are in the money with XYZ at 40: 20% x 40 + 2.00 = 10.00 a share.
    assert evaluate(ledger, rules(), intent(quantity="30"), Book().context()).accepted  # 21,000 at S=50
    live = evaluate(ledger, rules(), intent(quantity="30"), Book().context(chain(price="40")))
    assert not rule(live, "margin").passed and rule(live, "margin").measured_value == D("30000")


def test_a_market_entry_whose_legs_net_the_wrong_way_has_no_price(ledger) -> None:
    # Sold for a credit, but the legs as written pay a debit: nothing to measure it at.
    backwards = Combo((ComboLeg(P45, 1, Side.BUY), ComboLeg(P40, 1, Side.SELL)))
    verdict = evaluate(ledger, rules(), intent(backwards, limit=None), Book().context(chain()))
    assert not rule(verdict, "margin").passed and "no price" in str(rule(verdict, "margin").measured_value)


def test_an_entry_with_no_price_is_not_margined_without_it(ledger) -> None:
    # A market entry after the close, with no snapshot to price it: unknown, so refused.
    verdict = evaluate(ledger, rules(), intent(limit=None), Book().context())
    assert not rule(verdict, "margin").passed and "UNKNOWN" in str(rule(verdict, "margin").measured_value)


def test_a_spread_after_the_close_is_priced_leg_by_leg_from_the_sessions_snapshot(ledger) -> None:
    verdict = evaluate(ledger, rules(), intent(BULL_PUT, limit="1.20"), Book().context(snapshots={"XYZ": chain()}))
    assert rule(verdict, "margin").passed and rule(verdict, "margin").measured_value == D("500")  # the width


def test_a_spread_with_no_quotes_to_price_its_legs_refuses(ledger) -> None:
    verdict = evaluate(ledger, rules(), intent(BULL_PUT, limit="1.20"), Book().context())
    assert not rule(verdict, "margin").passed


# -- margin per name (the owner's reading of §6.2's 10%) ------------------------------------


def test_margin_on_one_name_within_ten_percent_passes(ledger) -> None:
    # Seven 45 puts at 700 each: 4,900 of 5,000.
    verdict = evaluate(ledger, rules(max_name_margin_frac=D("0.10")), intent(quantity="7"), Book().context())
    assert rule(verdict, "name_margin").passed and rule(verdict, "name_margin").measured_value == D("4900")


def test_margin_on_one_name_past_ten_percent_refuses(ledger) -> None:
    verdict = evaluate(ledger, rules(max_name_margin_frac=D("0.10")), intent(quantity="8"), Book().context())
    assert not rule(verdict, "name_margin").passed


def test_name_margin_counts_that_names_shares_and_no_other_name(ledger) -> None:
    other = OptionContract("ABC", EXPIRY, D("45"), OptionRight.PUT)
    book = Book().trade(XYZ, Side.BUY, "100", "50").trade(other, Side.SELL, "5", "2.00").mark(Equity("ABC"), "50")
    verdict = evaluate(ledger, rules(max_name_margin_frac=D("0.10")), intent(quantity="1"), book.context())
    # 100 XYZ shares at 25% maintenance (1,250) plus one 45 put (700); the ABC puts don't count.
    assert rule(verdict, "name_margin").measured_value == D("1950")


def test_a_call_written_on_shares_over_the_name_cap_passes_because_it_lowers_the_margin(ledger) -> None:
    # 300 XYZ shares: 3,750 of maintenance, over a 5% (2,500) name cap on their own.
    book = Book().trade(XYZ, Side.BUY, "300", "50")
    r = rules(max_name_margin_frac=D("0.05"))
    call = evaluate(ledger, r, intent(C55, quantity="3", limit="1.10"), book.context())
    put = evaluate(ledger, r, intent(P45, quantity="1"), book.context())
    assert rule(call, "name_margin").passed
    assert not rule(put, "name_margin").passed  # a put adds to the name


def test_an_entry_that_lowers_margin_passes_on_a_book_over_the_cap(ledger) -> None:
    # 30 short 35 puts marked at 7.00 (31,500) and 100 shares (1,250) on 29,750 of equity.
    book = Book().trade(P35, Side.SELL, "30", "0.25", mark="7.00").trade(XYZ, Side.BUY, "100", "50")
    covered = evaluate(ledger, rules(), intent(C55, limit="1.10"), book.context())
    more = evaluate(ledger, rules(), intent(P45, quantity="30"), book.context())
    assert rule(covered, "margin").passed and "no higher than before" in rule(covered, "margin").reason
    assert not rule(more, "margin").passed


# -- cash per name (§6.2 CSP: 10%) ------------------------------------------------------


def test_cash_securing_one_name_within_ten_percent_passes(ledger) -> None:
    verdict = evaluate(ledger, rules(max_name_collateral_frac=D("0.10")), intent(), Book().context())
    assert rule(verdict, "name_collateral").passed and rule(verdict, "name_collateral").measured_value == D("4500")


def test_cash_securing_one_name_past_ten_percent_refuses(ledger) -> None:
    book = Book().trade(P35, Side.SELL, "1", "0.25")  # 3,500 already secured on XYZ
    verdict = evaluate(ledger, rules(max_name_collateral_frac=D("0.10")), intent(), book.context())
    assert not rule(verdict, "name_collateral").passed and rule(verdict, "name_collateral").measured_value == D("8000")


def test_name_collateral_not_configured_says_so(ledger) -> None:
    verdict = evaluate(ledger, rules(), intent(), Book().context())
    assert rule(verdict, "name_collateral").reason == "name_collateral is not configured for this account"


# -- naked put notional by regime (§6.2) -------------------------------------------------

BY_REGIME = {"BULL_EXPLOSIVE": D("1.00"), "BULL_CHOPIER": D("0.50"), "BEAR_PROTECTIVE": D("0")}


def test_put_notional_within_the_regimes_fraction_passes(ledger) -> None:
    verdict = evaluate(ledger, rules(put_notional_frac_by_regime=BY_REGIME), intent(quantity="5"), Book().context(), regime="BULL_CHOPIER")
    assert rule(verdict, "put_notional").passed  # 22,500 <= 25,000


def test_put_notional_past_the_regimes_fraction_refuses(ledger) -> None:
    verdict = evaluate(ledger, rules(put_notional_frac_by_regime=BY_REGIME), intent(quantity="6"), Book().context(), regime="BULL_CHOPIER")
    assert not rule(verdict, "put_notional").passed  # 27,000 > 25,000


def test_bear_protective_allows_spreads_but_no_naked_put(ledger) -> None:
    r = rules(put_notional_frac_by_regime=BY_REGIME)
    naked = evaluate(ledger, r, intent(), Book().context(), regime="BEAR_PROTECTIVE")
    spread = evaluate(ledger, r, intent(BULL_PUT, limit="1.20"), Book().context(snapshots={"XYZ": chain()}), regime="BEAR_PROTECTIVE")
    assert not rule(naked, "put_notional").passed
    assert rule(spread, "put_notional").passed


def test_a_regime_with_no_fraction_configured_refuses(ledger) -> None:
    r = rules(put_notional_frac_by_regime={"BULL_EXPLOSIVE": D("1")})
    verdict = evaluate(ledger, r, intent(), Book().context(), regime="BULL_CHOPIER")
    assert not rule(verdict, "put_notional").passed


# -- the structure's worst case (§6.2 spread: 2%) ----------------------------------------


def test_a_spread_whose_max_loss_is_within_two_percent_passes(ledger) -> None:
    verdict = evaluate(ledger, rules(max_loss_per_structure_frac=D("0.02")), intent(BULL_PUT, quantity="2", limit="1.20"), Book().context(snapshots={"XYZ": chain()}))
    assert rule(verdict, "max_loss").passed and rule(verdict, "max_loss").measured_value == D("760")  # (5 - 1.20) x 200


def test_a_spread_whose_max_loss_passes_two_percent_refuses(ledger) -> None:
    verdict = evaluate(ledger, rules(max_loss_per_structure_frac=D("0.02")), intent(BULL_PUT, quantity="3", limit="1.20"), Book().context(snapshots={"XYZ": chain()}))
    assert not rule(verdict, "max_loss").passed  # 1,140 > 1,000


def test_a_naked_call_has_no_bounded_loss_and_refuses(ledger) -> None:
    book = Book().trade(XYZ, Side.BUY, "100", "50")
    verdict = evaluate(ledger, rules(max_loss_per_structure_frac=D("0.02")), intent(C55, limit="1.10"), book.context())
    assert not rule(verdict, "max_loss").passed


# -- debit structures (§6.2 PMCC: 5% each, 30% together) -----------------------------------


def test_a_leaps_within_five_percent_passes_and_one_over_refuses(ledger) -> None:
    r = rules(max_debit_per_structure_frac=D("0.05"))
    ok = evaluate(ledger, r, intent(C40_LEAPS, Side.BUY, "1", "13.50"), Book().context())
    over = evaluate(ledger, r, intent(C40_LEAPS, Side.BUY, "2", "13.50"), Book().context())
    assert rule(ok, "debit").passed and rule(ok, "debit").measured_value == D("1350")
    assert not rule(over, "debit").passed  # 2,700 > 2,500


def test_debit_structures_together_past_thirty_percent_refuse(ledger) -> None:
    far = call("30", LEAPS)
    book = Book().trade(far, Side.BUY, "7", "20.00")  # 14,000 of debit already open
    r = rules(max_total_debit_frac=D("0.30"))
    within = evaluate(ledger, r, intent(C40_LEAPS, Side.BUY, "1", "10.00"), book.context())
    over = evaluate(ledger, r, intent(C40_LEAPS, Side.BUY, "1", "13.50"), book.context())
    assert rule(within, "total_debit").passed and rule(within, "total_debit").measured_value == D("15000")
    assert not rule(over, "total_debit").passed


# -- shares bought (§6.2 buy-write: 20%) ----------------------------------------------------


def test_shares_within_twenty_percent_pass_and_over_refuse(ledger) -> None:
    r = rules(max_share_notional_frac=D("0.20"))
    ok = evaluate(ledger, r, intent(XYZ, Side.BUY, "200", None), Book().context())
    over = evaluate(ledger, r, intent(XYZ, Side.BUY, "201", None), Book().context())
    assert rule(ok, "share_notional").passed and rule(ok, "share_notional").measured_value == D("10000")
    assert not rule(over, "share_notional").passed


# -- regime ---------------------------------------------------------------------------------


@pytest.mark.parametrize("regime", [None, "UNKNOWN"])
def test_an_unknown_regime_refuses(ledger, regime) -> None:
    assert not rule(evaluate(ledger, rules(), intent(), Book().context(), regime=regime), "regime").passed


def test_a_regime_the_account_does_not_enter_in_refuses(ledger) -> None:
    r = rules(allowed_regimes=frozenset({"BULL_EXPLOSIVE"}))
    assert not rule(evaluate(ledger, r, intent(), Book().context(), regime="BULL_CHOPIER"), "regime").passed
    assert rule(evaluate(ledger, r, intent(), Book().context(), regime="BULL_EXPLOSIVE"), "regime").passed


# -- earnings before expiry --------------------------------------------------------------------


def test_earnings_before_expiry_refuse_and_after_pass(ledger) -> None:
    r = rules(no_earnings_before_expiry=True)
    before = evaluate(ledger, r, intent(), Book().context(), earnings=Earnings(date(2026, 10, 20)))
    after = evaluate(ledger, r, intent(), Book().context(), earnings=Earnings(date(2026, 11, 5)))
    on = evaluate(ledger, r, intent(), Book().context(), earnings=Earnings(EXPIRY))
    assert not rule(before, "earnings").passed and rule(after, "earnings").passed and not rule(on, "earnings").passed


def test_earnings_are_measured_against_the_short_legs_only(ledger) -> None:
    r = rules(no_earnings_before_expiry=True)
    report = Earnings(date(2026, 12, 1))  # after the October short, before the June LEAPS
    leaps = evaluate(ledger, r, intent(C40_LEAPS, Side.BUY, "1", "13.50"), Book().context(), earnings=report)
    diagonal = Combo((ComboLeg(C40_LEAPS, 1, Side.BUY), ComboLeg(C55, 1, Side.SELL)))
    pmcc = evaluate(ledger, r, intent(diagonal, Side.BUY, "1", "12.40"), Book().context(), earnings=report)
    assert rule(leaps, "earnings").passed and rule(leaps, "earnings").reason == "The entry sells no option"
    assert rule(pmcc, "earnings").passed and rule(pmcc, "earnings").threshold == "after 2026-10-30"


def test_an_unknown_earnings_date_refuses_and_none_scheduled_passes(ledger) -> None:
    r = rules(no_earnings_before_expiry=True)
    assert not rule(evaluate(ledger, r, intent(), Book().context(), earnings=Earnings(unknown=True)), "earnings").passed
    assert rule(evaluate(ledger, r, intent(), Book().context(), earnings=Earnings(None)), "earnings").passed


def test_the_earnings_rule_needs_a_source(ledger) -> None:
    with pytest.raises(OptionRiskConfigurationError, match="earnings source"):
        OptionRiskEngine(rules(no_earnings_before_expiry=True), ReplayClock(NOW), ledger, venue_id="sim", regime_of=lambda s: None)


# -- the OMS guards, recorded (C3, C4) -----------------------------------------------------------


def test_an_entry_on_a_contract_already_held_is_refused_and_recorded(ledger) -> None:
    book = Book().trade(P45, Side.SELL, "1", "2.00")
    verdict = evaluate(ledger, rules(), intent(), book.context())
    assert not rule(verdict, "duplicate_entry").passed
    assert rule(evaluate(ledger, rules(), intent(P40), book.context()), "duplicate_entry").passed


def test_a_call_on_shares_not_held_is_refused_and_one_on_held_shares_passes(ledger) -> None:
    bare = evaluate(ledger, rules(), intent(C55, limit="1.10"), Book().context())
    covered = evaluate(ledger, rules(), intent(C55, limit="1.10"), Book().trade(XYZ, Side.BUY, "100", "50").context())
    assert not rule(bare, "covered_calls").passed
    assert rule(covered, "covered_calls").passed


def test_a_command_id_already_used_refuses(ledger) -> None:
    ledger.append(Event(account=ACCOUNT, kind=EventKind.CASH_FLOW, ts_utc=NOW, command_id="e1",
                        payload=CashFlow(amount=D("1"), kind="deposit", as_of=NOW)))
    assert not rule(evaluate(ledger, rules(), intent(name="e1"), Book().context()), "duplicate_protection").passed


def test_the_kill_switch_refuses(ledger) -> None:
    assert rule(evaluate(ledger, rules(), intent(), Book().context()), "persistent_kill_switch").passed
    ledger.append(Event(account="__venue__:sim", kind=EventKind.RISK_CONTROL, ts_utc=NOW, command_id="kill",
                        payload=RiskControlChange("kill_switch", True, "test", NOW)))
    assert not rule(evaluate(ledger, rules(), intent(), Book().context()), "persistent_kill_switch").passed


def test_a_verdict_can_be_stored_in_the_ledger(ledger) -> None:
    # The runner appends every verdict (I11); a measurement the codec can't store fails the run.
    from trade_engine.ledger.codec import decode_payload, encode_payload

    book = Book().trade(P45, Side.SELL, "1", "2.00")
    for verdict in (
        evaluate(ledger, rules(), intent(), book.context(), regime=None),
        evaluate(ledger, rules(no_earnings_before_expiry=True, put_notional_frac_by_regime=BY_REGIME,
                               max_name_margin_frac=D("0.1"), max_name_collateral_frac=D("0.1"),
                               max_loss_per_structure_frac=D("0.02"), max_debit_per_structure_frac=D("0.05"),
                               max_total_debit_frac=D("0.3"), max_share_notional_frac=D("0.2")),
                 intent(C55, limit="1.10"), book.context(), earnings=Earnings(unknown=True)),
    ):
        assert decode_payload(encode_payload(verdict)) == verdict


def test_every_rule_is_recorded_even_when_one_refuses(ledger) -> None:
    verdict = evaluate(ledger, rules(), intent(quantity="36"), Book().context(), regime=None)
    names = [r.rule_name for r in verdict.evaluations]
    assert {"regime", "margin", "name_collateral", "put_notional", "max_loss", "debit", "total_debit",
            "share_notional", "earnings", "duplicate_entry", "covered_calls", "duplicate_protection",
            "persistent_kill_switch"} <= set(names)
    assert len(verdict.refusal_reasons) == 2  # regime and margin


# -- configuration ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (dict(allowed_regimes=frozenset()), "non-empty subset"),
        (dict(allowed_regimes=frozenset({"UNKNOWN"})), "non-empty subset"),
        (dict(max_margin_frac=D("-0.1")), "fraction of equity"),
        (dict(put_notional_frac_by_regime={"SIDEWAYS": D("1")}), "Unknown regime"),
    ],
)
def test_rules_that_make_no_sense_refuse(changes, message) -> None:
    with pytest.raises(OptionRiskConfigurationError, match=message):
        rules(**changes)


# -- entry_quote: the quotes an entry is made on still pass the scan's gates -------------------

from trade_engine.interfaces.market_data import Greeks  # noqa: E402
from trade_engine.risk_options import EntryQuoteRules  # noqa: E402

GATES = EntryQuoteRules(
    short_put_abs_delta=(D("0.10"), D("0.30")),
    min_short_bid=D("0.05"),
    short_bid_return=(D("0.02"), D("0.05")),
    min_short_implied_vol=D("0.70"),
    min_open_interest=100,
    max_leg_spread_frac=D("0.50"),
    min_underlying_price=D("8"),
    min_credit_width_frac=D("0.15"),
    min_credit_return=D("0.15"),
    max_friction_frac=D("0.45"),
)
GATE_NAMES = {"underlying_price", "delta", "bid", "bid_return", "implied_vol", "open_interest", "leg_spread"}
VERTICAL_NAMES = {"credit_width", "credit_return", "friction"}


def quoted(contract, bid: str, ask: str, *, delta: float | None, iv: str | None = "0.75", oi: int | None = 500) -> OptionQuote:
    greeks = None if delta is None else Greeks(delta, 0.01, -0.02, 0.05, -0.01, "vendor")
    return OptionQuote(contract, D(bid), D(ask), D(10), D(10), SNAP, implied_vol=None if iv is None else D(iv),
                       greeks=greeks, open_interest=oi)


def morning(price: str = "50", **changes) -> ChainSnapshot:
    # 45 put: bid/strike 0.0422, spread 10% of mid, |delta| 0.25. The 45/40 vertical: credit
    # 1.90 - 0.90 = 1.00, 20% of the width, 1.00 / 4.00 = 25% on risk, friction 0.40 / 1.00.
    rows = {
        "P45": dict(contract=P45, bid="1.90", ask="2.10", delta=-0.25),
        "P40": dict(contract=P40, bid="0.70", ask="0.90", delta=-0.12),
        "C55": dict(contract=C55, bid="1.00", ask="1.20", delta=0.30),
    }
    for name, change in changes.items():
        if change is None:
            rows.pop(name)
        else:
            rows[name] = {**rows[name], **change}
    return ChainSnapshot("XYZ", SNAP, D(price), tuple(quoted(**row) for row in rows.values()), None, None, "test")


def gated(ledger, the_intent, snapshot, gates=GATES):
    return evaluate(ledger, rules(entry_quote=gates), the_intent, Book().context(snapshot))


def gate_names(verdict) -> set[str]:
    return {r.rule_name.removeprefix("entry_quote.") for r in verdict.evaluations if r.rule_name.startswith("entry_quote")}


def test_a_put_whose_morning_quotes_still_pass_the_scan_is_entered(ledger) -> None:
    verdict = gated(ledger, intent(limit=None), morning())
    assert gate_names(verdict) == GATE_NAMES  # the credit gates do not measure a single put
    assert verdict.accepted, verdict.refusal_reasons


def test_a_vertical_whose_morning_quotes_still_pass_the_scan_is_entered(ledger) -> None:
    verdict = gated(ledger, intent(BULL_PUT, limit=None), morning())
    assert gate_names(verdict) == GATE_NAMES | VERTICAL_NAMES
    assert verdict.accepted, verdict.refusal_reasons
    assert rule(verdict, "entry_quote.credit_width").measured_value == D("0.2000")
    assert rule(verdict, "entry_quote.friction").measured_value == D("0.4000")


@pytest.mark.parametrize(
    ("price", "changes", "gate"),
    [
        ("7.50", {}, "underlying_price"),  # the stock gapped under the scan's price floor
        ("50", dict(P45=dict(delta=-0.42)), "delta"),  # gapped down: the put is now too close
        ("50", dict(P45=dict(delta=-0.08)), "delta"),  # rallied away: too far out to pay
        ("50", dict(P45=dict(bid="0.05", ask="0.06")), "bid"),  # (and 0.05 / 45 is under 2%)
        ("50", dict(P45=dict(bid="0.80", ask="0.90")), "bid_return"),  # 0.80 / 45 < 2%
        ("50", dict(P45=dict(bid="2.40", ask="2.50")), "bid_return"),  # 2.40 / 45 > 5%: priced for trouble
        ("50", dict(P45=dict(iv="0.615")), "implied_vol"),  # the vol the scan sold is gone
        ("50", dict(P45=dict(oi=99)), "open_interest"),
        ("50", dict(P45=dict(bid="1.40", ask="2.60")), "leg_spread"),  # 1.20 on a 2.00 mid
    ],
)
def test_a_put_whose_morning_quotes_fail_a_gate_is_refused(ledger, price, changes, gate) -> None:
    verdict = gated(ledger, intent(limit=None), morning(price, **changes))
    failed = {r.rule_name for r in verdict.evaluations if not r.passed}
    assert f"entry_quote.{gate}" in failed
    also = {"entry_quote.bid_return"} if gate == "bid" else set()
    assert {name for name in failed if name.startswith("entry_quote")} == {f"entry_quote.{gate}"} | also, failed


@pytest.mark.parametrize(
    ("changes", "gate"),
    [
        (dict(P40=dict(bid="1.00", ask="1.30")), "credit_width"),  # credit 0.60: 12% of the width
        (dict(P40=dict(bid="1.00", ask="1.30")), "credit_return"),  # 0.60 / 4.40 = 13.6%
        (dict(P45=dict(bid="1.90", ask="2.20"), P40=dict(bid="0.60", ask="0.90")), "friction"),  # 0.60 / 1.00
        (dict(P40=dict(bid="0.50", ask="1.00")), "leg_spread"),
    ],
)
def test_a_vertical_whose_morning_quotes_fail_a_gate_is_refused(ledger, changes, gate) -> None:
    verdict = gated(ledger, intent(BULL_PUT, limit=None), morning(**changes))
    assert not rule(verdict, f"entry_quote.{gate}").passed and not verdict.accepted


def test_a_vertical_at_the_gates_edges_is_entered(ledger) -> None:
    # credit 1.90 - 1.15 = 0.75: 15% of the width; 0.75 / 4.25 = 17.6%; friction 0.30 / 0.75 = 40%.
    snapshot = morning(P40=dict(bid="1.05", ask="1.15"))
    verdict = gated(ledger, intent(BULL_PUT, limit=None), snapshot)
    assert verdict.accepted, verdict.refusal_reasons


@pytest.mark.parametrize(
    ("changes", "gate"),
    [
        (dict(P45=dict(delta=None)), "delta"),
        (dict(P45=dict(iv=None)), "implied_vol"),
        (dict(P45=dict(oi=None)), "open_interest"),
    ],
)
def test_a_quote_without_what_a_gate_measures_refuses(ledger, changes, gate) -> None:
    verdict = gated(ledger, intent(limit=None), morning(**changes))
    result = rule(verdict, f"entry_quote.{gate}")
    assert not result.passed and "UNKNOWN" in str(result.measured_value)


def test_a_leg_the_snapshot_does_not_quote_refuses(ledger) -> None:
    verdict = gated(ledger, intent(BULL_PUT, limit="1.00"), morning(P40=None))
    result = rule(verdict, "entry_quote")
    assert not result.passed and "XYZ" in str(result.measured_value)


def test_an_entry_with_no_snapshot_to_check_refuses(ledger) -> None:
    verdict = evaluate(ledger, rules(entry_quote=GATES), intent(), Book().context())
    assert not rule(verdict, "entry_quote").passed and rule(verdict, "entry_quote").measured_value == "UNKNOWN"


def test_a_gate_left_unset_is_not_measured(ledger) -> None:
    # The IV floor off: the vol collapse no longer refuses; the gates set still measure.
    verdict = gated(ledger, intent(limit=None), morning(P45=dict(iv="0.40")),
                    EntryQuoteRules(short_put_abs_delta=(D("0.10"), D("0.30")), min_open_interest=100))
    assert gate_names(verdict) == {"delta", "open_interest"} and verdict.accepted


def test_an_entry_that_opens_no_short_put_is_not_measured(ledger) -> None:
    book = Book().trade(XYZ, Side.BUY, "100", "50")
    verdict = evaluate(ledger, rules(entry_quote=GATES), intent(C55, limit="1.10"), book.context(morning(C55=dict(oi=1))))
    assert rule(verdict, "entry_quote").passed and rule(verdict, "entry_quote").measured_value == "n/a"


def test_accounts_without_entry_quote_rules_say_so(ledger) -> None:
    verdict = evaluate(ledger, rules(), intent(limit=None), Book().context(morning(P45=dict(delta=-0.9))))
    assert rule(verdict, "entry_quote").measured_value == "n/a" and verdict.accepted


def test_the_credit_gates_refuse_a_combo_that_is_not_a_bull_put_vertical(ledger) -> None:
    wide_short = Combo((ComboLeg(P40, 1, Side.SELL), ComboLeg(P45, 1, Side.BUY)))  # long over short
    verdict = gated(ledger, intent(wide_short, limit="1.10"), morning())
    assert not rule(verdict, "entry_quote.vertical").passed


def test_an_entry_quote_verdict_can_be_stored_in_the_ledger(ledger) -> None:
    from trade_engine.ledger.codec import decode_payload, encode_payload

    for snapshot in (morning(), morning("7", P45=dict(delta=None, iv=None, oi=None)), morning(P40=None)):
        verdict = gated(ledger, intent(BULL_PUT, limit="1.00"), snapshot)
        assert decode_payload(encode_payload(verdict)) == verdict


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (dict(short_put_abs_delta=(D("0.3"), D("0.1"))), "short_put_abs_delta"),
        (dict(short_bid_return=(D("0.02"),)), "short_bid_return"),
        (dict(short_put_abs_delta=(0.1, 0.3)), "short_put_abs_delta"),
        (dict(min_short_bid=D("-1")), "min_short_bid"),
        (dict(max_friction_frac=0.45), "max_friction_frac"),
        (dict(min_open_interest=True), "min_open_interest"),
        (dict(min_open_interest=-1), "min_open_interest"),
    ],
)
def test_entry_quote_rules_that_make_no_sense_refuse(changes, message) -> None:
    with pytest.raises(OptionRiskConfigurationError, match=message):
        EntryQuoteRules(**changes)


def test_entry_quote_must_be_entry_quote_rules() -> None:
    with pytest.raises(OptionRiskConfigurationError, match="entry_quote"):
        rules(entry_quote={"min_open_interest": 100})


def test_a_verticals_wing_is_not_held_to_the_open_interest_floor(ledger) -> None:
    # The scan measures the put it sells; a thin wing shows in the friction instead.
    verdict = gated(ledger, intent(BULL_PUT, limit=None), morning(P40=dict(oi=40), P45=dict(oi=99)))
    measured = str(rule(verdict, "entry_quote.open_interest").measured_value)
    assert "99" in measured and "40" not in measured
    assert not rule(verdict, "entry_quote.open_interest").passed
    assert gated(ledger, intent(BULL_PUT, limit=None), morning(P40=dict(oi=40))).accepted
