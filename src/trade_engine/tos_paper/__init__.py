"""T2 — TosPaperBroker: the thinkorswim paperMoney mirror (architecture §4.7).

The sim is the book of record; paperMoney is a mirror of selected option accounts.
The mirror's own memory is the ledger: ``Mirror*`` events under ``__venue__:<venue>``
fold into ``ledger.mirror`` (tickets, venue Order IDs, the book of proven venue fills)
and never touch a sim account's positions or cash.

- ``netting``: single-leg strategy orders → same-side venue tickets per contract; a
  2-leg vertical is mirrored 1:1 as one combo ticket, never netted; conflicts across
  virtual accounts (and against the mirror book) are screened per leg and refused **at
  the venue only** (§4.4) — the sim book still takes every order.
- ``transport``: the protocols the host wires (order transport, balance reader, and the
  optional cancel and order-fill readers) and the ticket shapes. The engine never
  imports tos-ui-mcp (I13).
- ``normalize``: pure raw-result → domain normalization, golden-vector tested.
- ``reconcile``: read-back confirmation and the drift check that halts the venue.
- ``broker``: the adapter — connect gates, a submit queue off the sim critical path,
  restore from the fold, fill read-back.
- ``session``: one host-callable mirror session (collect → reconcile → queue → drain),
  every step appended to the ledger, idempotent on re-run.
- ``slippage``: venue fills allocated down to strategy orders; the sim-vs-venue report.
"""
