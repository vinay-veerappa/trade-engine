# Staged runtime-owner launch kit (P4c T14)

Everything in `launch\runtime\` is **staged only**. Nothing here registers,
starts or touches a real scheduled task, live ledger or venue; applying the
kit is a separately owner-approved canary (plan `P4C_RUNTIME_FLIP.md`
section 7, Stage D). The kit's own tests fake every Task Scheduler command.

## What is staged

- `run_runtime_owner.ps1` — the launcher template. It runs the certified
  `te.exe serve --config <absolute config>` with stdin attached (the
  owner serves until stdin closes, then drains and exits 0), appends all
  output to `logs\trade_engine\RuntimeOwner_<role>_<date>.log` and exits
  with the owner's code. It refuses relative config paths and a missing
  `TE_BINARY`.
- `runtime_selector.json` — the per-ledger owner selector, default
  `legacy`. This file is the only switch the rollback procedure touches.
  A ledger with no row uses the default. `runtime` never falls back
  mid-job: if the staged owner cannot start, the job fails loudly.

## How the kit is used (all dry-run by default)

`tools\p4c_rollout.py` renders one absolute runtime-owner config per
role from a role table, verifies the selector's mutual exclusion, and
implements the one-action `rollback-role` ordered handoff:

1. stop admission (the owner refuses new jobs),
2. request a graceful stop (stdin close; the owner drains the active job),
3. wait for the ledger's `.lock` sidecar release (or report a blocked
   owner explicitly — it never deletes a lock to manufacture liveness),
4. switch the per-ledger selector to `legacy`,
5. start the retained legacy release.

Steps 3-4 are ordered: the selector is never switched while the runtime
still owns the ledger. `--dry-run` runs the whole procedure against a
fake Task Scheduler and fake process handle and prints the exact command
sequence; without it the tool refuses (no real scheduler operation is
implemented at all in this kit).

Proposed client task-script patches (the `run_logged.ps1` selector seam)
live only in the client worktree `p4c/t14-client`; this engine repo ships
templates only.