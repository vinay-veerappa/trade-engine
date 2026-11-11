# Design docs

One architecture and design doc for each feature. The doc records the bigger picture
and the agreed design, so the work can be picked up at any time and reviewed by
someone who wasn't in the conversation.

## How a doc relates to the other files

| File | Answers | Changes when |
|---|---|---|
| `docs/design/<FEATURE>.md` (this folder) | Why, what, and how it fits: context, goals, architecture, contracts, alternatives, decisions | the design changes (needs review again) |
| `docs/<FEATURE>_PLAN.md` | In what order: phases, sizing, gates | the schedule changes |
| `docs/briefs/<ID>.md` | Exactly what one delegate builds in one slice | per slice |

The design doc is the parent. A plan or brief that contradicts it is wrong until the
design doc is updated and agreed again.

## Lifecycle

`Draft` → `In review` → `Agreed` → (`Superseded by <doc>`)

- **Draft:** written by Opus from the research and owner discussion.
- **In review:** the owner, or a reviewer the owner picks, reads it. Comments go in the
  doc's "Review log" section or as PR review comments.
- **Agreed:** the owner has signed off. Agreed decisions are numbered (D-1, D-2, …) and
  are not reopened by implementers. A change needs a new decision entry with a date
  and a reason.
- **Superseded:** kept for history, with a link to its replacement. It is never deleted.

## Rules

- One feature per doc. A doc covers a feature end to end, even when it spans repos.
  It lives in the repo that implements the core of it, and the other repos link to it.
- Start from `TEMPLATE.md`. Keep every section; write "None" rather than deleting one.
- Cite file paths with line numbers for current code, and URLs for outside prior art.
- Record alternatives that were rejected, and why. That is what stops the same debate
  from happening again.
- No secrets or full account numbers (mask to `*194`).

## Index

| Doc | Status | Scope |
|---|---|---|
| [BROKER-PLATFORM.md](BROKER-PLATFORM.md) | In review | trade-engine as the single broker port: the canonical futures instrument and account registry, used by riskguard and Vela. Tradovate first (via ntx), then Schwab and IBKR. |
