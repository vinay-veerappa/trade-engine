"""The IBKR accounts adapter (BROKER_PLATFORM phase 6b).

The rules live in ``te_core::ibkr``: both source shapes are parsed from text (TWS/IB
Gateway ``managedAccounts`` plus ``reqAccountUpdates`` account values, or Client Portal
``GET /portfolio/accounts``), the key is ``ibkr:<accountId>``, ``DU...`` is paper, and
an unknown shape is refused. This package carries data and the switch only.
"""

from trade_engine.ibkr.adapter import IbkrAccountsAdapter

__all__ = ["IbkrAccountsAdapter"]
