"""T2 — TosPaperBroker: the thinkorswim paperMoney mirror (architecture §4.7).

The sim is the book of record; paperMoney is a mirror of selected option accounts.

- ``netting``: strategy orders → same-side venue tickets per contract; conflicts across
  virtual accounts (and against the mirror book's holdings) refused **at the venue
  only** (§4.4) — the sim book still takes every order.
- ``transport``: the protocols the host wires (order transport, balance reader) and the
  ticket shape. The engine never imports tos-ui-mcp (I13).
- ``normalize``: pure raw-result → domain normalization, golden-vector tested.
- ``reconcile``: read-back confirmation and the drift check that halts the venue.
- ``broker``: the adapter — connect gates, a submit queue off the sim critical path.
- ``slippage``: venue fills allocated down to strategy orders; the sim-vs-venue report.
"""
