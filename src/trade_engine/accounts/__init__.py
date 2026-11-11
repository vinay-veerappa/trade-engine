"""The broker account port (BROKER_PLATFORM phase 2).

The carriers are frozen dataclasses in :mod:`trade_engine.interfaces.broker`; every rule
(key, masking, diff, freshness, poll interval, the event ring) lives in
``te_core::accounts`` and is reached through :mod:`trade_engine.accounts._rs`. Python
carries data and does I/O only (D2, D3).
"""
