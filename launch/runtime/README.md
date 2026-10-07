# Staged runtime-owner launch kit (P4c T14)

The kit's own tools and tests never register, start or touch a real scheduled
task, live ledger or venue: they fake every Task Scheduler command. Applying
it is the client repo's step (plan `P4C_RUNTIME_FLIP.md` section 7, Stage D):
`register_runtime_owners_task.ps1` there registers the owner tasks, and this
folder's selector then routes the client tasks to them.

## What is staged

- `run_runtime_owner.ps1` — hosts one owner. It starts the certified
  `te.exe serve --config <absolute config>` with a PIPE for stdin and holds
  the write end for as long as the launcher lives (a scheduled task's NUL
  stdin would end the serve at once; the owner drains and exits 0 when the
  pipe closes). The owner's output is appended to
  `<config dir>\logs\trade_engine\RuntimeOwner_<role>_<date>.log`: the config
  sits in an ancestor of its ledger, i.e. the client repo root. Once the
  owner reports it is serving, the launcher publishes
  `<LogDir>\runtime-owners\<role>.json` (port, generation, config, pid; no
  secret: the capability stays in the file the config names). A graceful
  stop is `<role>.stop` in that folder (the launcher closes stdin, the
  owner drains the active job); Stop-ScheduledTask kills the tree with no
  drain and the next start marks the unfinished job `uncertain`. It refuses
  relative config paths and a missing `TE_BINARY`.
- `runtime_selector.json` — the per-ledger owner selector, default
  `legacy`. This file is the only switch the rollback procedure touches.
  It ships with the two live ledgers (options, scan) on `runtime` and the
  seven client tasks that write them (`tasks`); the owners are the
  `RuntimeOwnerBatch` / `RuntimeOwnerScan` scheduled tasks the client repo's
  `launch\tasks\register_runtime_owners_task.ps1` registers, each running
  this launcher on its repo-root config (`p4c-owner-batch.json`,
  `p4c-owner-scan.json`; the capability file is never committed).
  A ledger with no row uses the default. `runtime` never falls back
  mid-job: if the staged owner cannot start, the job fails loudly. The
  optional `owners` index maps a ledger to the owner's published endpoint
  file and the env prefix its clients read (`TE_RUNTIME` for the scan,
  `TE_OPT_OWNER` for the options): the client's task wrapper sets that env
  from the endpoint file and the config's capability file, then runs the
  task's own command, which is already an owner client.

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