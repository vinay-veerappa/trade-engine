Parent design: trade-engine/docs/design/BROKER-PLATFORM.md

# BP-7: Vela reads instruments and accounts from the engine (plan phase 7)

Read BP-COMMON.md working rules (worktree, one commit, no push, masking). Vela lives in
`C:\Users\vinay\tvDownloadOHLC` (web app; read its `docs/architecture/` notes on Vela
and on the te_wasm pkg, `P6_REPLAY_SIM_PARITY.md`). Work in a tvDownloadOHLC worktree.

## Steps

1. **Inventory.** Find every tick-size, tick-value, point-value, symbol-map, or
   root/month table in Vela code, and every account list Vela keeps or builds itself.
   Give file:line for each.
2. **Instruments.** Read them from the engine. Prefer te_wasm if it already exports
   the futures specs. Otherwise use the generated
   `trade-engine/crates/te_core/data/futures_instruments.json`
   (schema `te-futures-instruments/1`), vendored byte-identical. A sha check against the
   pinned te rev uses the same pattern as the te-wasm-pkg `stamp.json`. Make no hand
   copy.
3. **Accounts.** Read them from riskguard `GET /v1/accounts?platform=...` (the phase 4
   API; registry-backed). If the read fails, show the last good list (design 6.5).
   Don't build an account list in Vela.
4. **Gate.** Add a test that fails if any Vela source file defines a tick-value or
   symbol-map table of its own: search for numeric tick-value literals keyed by root,
   or a root→symbol map. The allow-list holds only the vendored file and the gate
   itself.
5. Parity: for every root Vela used before, old and new tick values are equal. Any
   difference stops the slice and is reported.

## Tests

- The repo's web test suite green; state the counts. Gate test plus a mutant (re-add a
  one-line table → gate fails). Parity test.
- Browser check optional and read-only. Don't restart the live :3000 web; that's the
  owner's job.

## Not in this brief

- Deploy or restart of the live web.
