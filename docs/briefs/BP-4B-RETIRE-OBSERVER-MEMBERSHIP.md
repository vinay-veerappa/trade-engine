Parent design: trade-engine/docs/design/BROKER-PLATFORM.md

# BP-4b: retire the ntx observer's account-membership role (plan phase 4b)

Read BP-COMMON.md first, then plan phase 4 and BP-4-PUBLISH-RISKGUARD.md. 4a (engine
outbox sink, `te_core::registry_outbox`) and 4c are merged. Owner ruling 2026-11-12:
**no soak or comparison window**. When the tests below are green, this cuts over;
problems get fixed as they show up.

Repos: nt8-riskguard (main work), nt_tradovate_mcp/ntx (only if step 3 needs it).
One worktree per repo, one commit each.

## What stays and what goes

- **Stays:** the observer's observation push (`AccountSnapshot`, positions, orders,
  balances, P&L) through `/v1/register` + `/v1/observations`, its seq and heartbeat,
  and `/v1/aliases`. Rules read those; nothing here changes them.
- **Goes:** every place riskguard decides **which accounts exist** (membership) from
  the observer path instead of from the registry (`registry.rs`, fed only by
  `POST /v1/registry/ingest`). Examples to look for: the account lists that
  `/v1/accounts`, the TV account list, `account_unconfirmed`, the fleet "all" view, or the
  UI account picker build from connector `register` bodies, observation account keys
  or the alias map.

## Steps

1. **Inventory (report it in the commit message).** List every reader in
   riskguard-service (and surfaces/UI if they read membership from a non-registry
   source) that derives the account set from the observer path. For each one, give the
   file:line, what it serves, and whether the registry already serves the same thing.
2. **Switch each reader to the registry snapshot.** An account that is in observations
   but not in the registry gets `account_unconfirmed`, as phase 4 defines, and is never
   added silently. A registry account with no observation yet is shown as known with no
   state, not dropped.
3. If the observer's `register` body `accounts` list is used for **anything but session
   bookkeeping**, stop using it for membership in riskguard. Change ntx only if
   riskguard cannot ignore the field. Prefer a riskguard-only change.
4. Rollback: one config key (default = new behaviour) that restores the old membership
   source, so a bad cutover is fixed with a config flip and an rg_service restart. This
   key is a rollback, not a shadow mode: there is no compare or dual-run path.

## Tests

- Each reader from step 1 has a test: registry has A,B; observations carry A,B,C →
  the list is A,B; C → `account_unconfirmed`.
- Registry stale (ntx down) → known accounts are not refused (phase 4 rule unchanged).
- Observation for a registry-closed account → not re-listed.
- The rollback key restores the old answer (one test).
- Full nt8-riskguard Rust workspace + `surfaces` `run_tests.mjs`; state the pass counts.
- Mutant: drop the registry filter in one reader → a test fails.

## Performance and memory budget (B7)

| Component | Budget | Bound | Enforcing test |
|---|---|---|---|
| Membership lookup on the order path | < 1 ms, no lock shared with ingest | reads the pointer-swapped registry snapshot | existing gate-latency test extended to the moved readers |

## Not in this brief

- Deploy. Turning the 4a sink on, restarting rg_service: owner, flat, then `isArmed` and
  the three switches.
- Retiring the observation (fill/P&L) push: that is PNL-SOURCE-1, a later slice.
