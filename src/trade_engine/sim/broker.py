"""Deterministic equities paper venue driven by one-minute bars.

Every rule (order validation, the restore checks, expiry, the bar-sequence rule, every
fill price and the position average) lives in Rust: ``te_core::sim::broker``
(docs/RUST_PORT.md P3a). The conversion shell is ``SimBrokerBase`` in ``trade_engine_rs``;
this module keeps only the public class (a thin subclass), its class attributes, the
exceptions other modules import, and the shared ``_Carriers`` helper.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trade_engine.domain.instruments import Instrument
from trade_engine.domain.orders import OrderType, TimeInForce
from trade_engine.interfaces.broker import Capabilities
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import codec
from trade_engine.sim import _rs


class SimBrokerError(RuntimeError):
    """Base class for invalid simulated-venue operations."""


class MissingBarError(SimBrokerError):
    """Raised when a session open or required one-minute bar is missing."""


class UnknownVenueOrderError(SimBrokerError):
    """Raised when an operation references an order not held by this simulator."""


_rs.register("sim", SimBrokerError)
_rs.register("missing_bar", MissingBarError)
_rs.register("unknown_order", UnknownVenueOrderError)

_TIMES_HELD = 4096


class _Carriers:
    """The objects the host handed in, by the plain values that crossed for them."""

    def __init__(self) -> None:
        self._times: dict[str, datetime] = {}
        self._texts: dict[Instrument, str] = {}
        self._by_key: dict[str, Instrument] = {}

    def iso(self, value: datetime) -> str:
        text = value.isoformat()
        if text not in self._times:
            if len(self._times) >= _TIMES_HELD:
                del self._times[next(iter(self._times))]
            self._times[text] = value
        return text

    def dt(self, text: str) -> datetime:
        held = self._times.get(text)
        return held if held is not None else datetime.fromisoformat(text)

    def text(self, instrument: Instrument) -> str:
        try:
            held = self._texts.get(instrument)
        except TypeError:
            held = None
        if held is not None:
            return held
        text = codec.text(codec._encode(instrument))
        key = _rs.call(_rs.rs.sim_instrument_key, text)
        self._by_key.setdefault(key, instrument)
        try:
            self._texts[instrument] = text
        except TypeError:
            pass
        return text

    def instrument(self, key: str) -> Instrument:
        return self._by_key[key]


class SimBroker(_rs.rs.SimBrokerBase):
    """Single-account simulator; orders fill only from explicit one-minute bars."""

    name = "SimBroker"
    env = "sim"
    stale_after = 120
    capabilities = Capabilities(
        supported_order_types=frozenset(
            {OrderType.MARKET, OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT}
        ),
        supported_tifs=frozenset({TimeInForce.DAY, TimeInForce.GTC, TimeInForce.OPG}),
        supports_multi_leg=False,
        supports_native_stops=True,
        supports_streaming=False,
    )

    def __init__(
        self,
        account_id: str,
        clock: Clock,
        slippage_bps: Decimal,
    ) -> None:
        # The Rust base's __new__ built the book and carriers; nothing else to set up.
        super().__init__()
