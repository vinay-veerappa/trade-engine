"""Deterministic options paper venue driven by chain snapshots (O4, rules doc §3).

There are no historical intraday option quotes, so option orders are matched against one
chain snapshot per session (the 15:45 ET pull). The EOD runner replays that snapshot at
its own ``as_of`` inside the session's timeline, the same way one-minute bars replay
equity fills: re-runnable, and never using a price from after the clock (I7).

Fill model (owner's choice, 2026-09-24): a leg trades halfway between mid and natural,
so a sale gets ``mid − fill_fraction × spread`` and a purchase ``mid + fill_fraction ×
spread``, with ``fill_fraction`` 0.25 by default. That is always inside the quote.

- A MARKET order fills at those prices. If a leg has no usable price in the snapshot, the
  order is rejected rather than filled at a guess (I5).
- A LIMIT order fills at its limit once the model price is at or through it: the model
  credit at or above a sell limit, the model debit at or below a buy limit. It is taken
  as having rested all session, as a real resting limit fills at its price when the
  market crosses it. An order the snapshot cannot price stays working.
- A combo trades its legs as written. SELL means it collects a net credit and BUY that it
  pays a net debit; the limit is that net amount per unit, in option points. At a limit
  fill the legs on the favourable side are scaled so the legs add up to the limit
  exactly.
- A quote older than ``max_quote_age_seconds`` at the snapshot's instant prices nothing.
- A share order on the snapshot's underlying fills at ``underlying_price``, moved against
  the order by ``equity_slippage_bps``.
- Orders fill all or nothing, so a combo never leaves a leg behind.
- Fees are ``fee_per_contract`` per option contract. Shares trade free (rules doc §5.1).
- A DAY order lapses at its session's close, but only once a snapshot of its underlying
  from that session has been processed. Only a snapshot proves it did not fill.

Every rule above lives in Rust: ``te_core::sim::snapshot`` (docs/RUST_PORT.md P3a). The
conversion shell — reading the clock where the rules ask for it, looking a quote up by
OCC symbol as ``ChainSnapshot.get`` does, and building the carriers from what comes
back — lives in ``SnapshotVenueBase`` (``crates/te_py/src/snapshot_venue.rs``), reached
through ``trade_engine_rs``. The sessions are the engine's XNYS ``ExchangeCalendar`` (D7).
"""

from __future__ import annotations

from typing import Any

import trade_engine_rs as rs

from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract
from trade_engine.domain.orders import OrderType, TimeInForce
from trade_engine.interfaces.broker import Capabilities
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.sim import _rs
from trade_engine.sim.broker import _Carriers


class SnapshotVenueError(RuntimeError):
    """An invalid operation on the snapshot venue."""


_rs.register("snapshot", SnapshotVenueError)

_UNDERLYINGS: dict[Any, str] = {}
_UNDERLYINGS_HELD = 4096
_TEXTS = _Carriers()


def underlying_of(instrument: Instrument) -> str:
    """The underlying whose chain snapshot prices ``instrument``."""
    if not isinstance(instrument, (Equity, OptionContract, Combo)):
        raise ValueError(f"Cannot price {type(instrument).__name__} from a chain snapshot")
    try:
        held = _UNDERLYINGS.get(instrument)
    except TypeError:
        held = None
    if held is not None:
        return held
    found = _rs.call(_rs.rs.sim_underlying_of, _TEXTS.text(instrument))
    try:
        if len(_UNDERLYINGS) >= _UNDERLYINGS_HELD:
            del _UNDERLYINGS[next(iter(_UNDERLYINGS))]
        _UNDERLYINGS[instrument] = found
    except TypeError:
        pass
    return found


class _Quotes:
    """``ChainSnapshot.get`` by OCC symbol: the first quote listed under it."""

    def __init__(self, snapshot: ChainSnapshot) -> None:
        self._snapshot = snapshot
        self._by_occ: dict[str, Any] | None = None

    def __call__(self, occ: str) -> tuple[str, str, str] | None:
        if self._by_occ is None:
            self._by_occ = {}
            for quote in self._snapshot.quotes:
                self._by_occ.setdefault(quote.contract.occ, quote)
        quote = self._by_occ.get(occ)
        if quote is None:
            return None
        return (str(quote.mid), str(quote.spread), quote.as_of.isoformat())


class SnapshotVenue(rs.SnapshotVenueBase):
    """Single-account simulator for options and shares, filled only from chain snapshots."""

    name = "SnapshotVenue"
    env = "sim"
    stale_after = 120
    capabilities = Capabilities(
        supported_order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
        supported_tifs=frozenset({TimeInForce.DAY, TimeInForce.GTC}),
        supports_multi_leg=True,
        supports_native_stops=False,
        supports_streaming=False,
    )
