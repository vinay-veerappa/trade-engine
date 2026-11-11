# <Feature name>

| | |
|---|---|
| Status | Draft / In review / Agreed / Superseded by … |
| Owner | owner (approver) |
| Author | … |
| Last updated | YYYY-MM-DD |
| Repos touched | … |
| Plan | link to `docs/<FEATURE>_PLAN.md` |
| Briefs | links |
| Issues | L-### … |

## 1. Summary

Three to five sentences: what changes, for whom, and why now.

## 2. Context and problem

What exists today (with file:line citations), what is wrong with it, and the evidence:
an incident, a measurement, or an owner request.

## 3. Goals and non-goals

- **Goals:** each one testable.
- **Non-goals:** explicitly out of scope, so nobody builds them.

## 4. Prior art and patterns considered

For each outside project or pattern looked at: what it does, what we take, what we
leave out, and why. Give sources.

## 5. Architecture

- A diagram (Mermaid or ASCII) of the components and data flow.
- Which component owns which truth.
- Trust and failure boundaries.

## 6. Design

- Components and their responsibilities.
- Data model and wire contracts, with the exact shapes.
- Invariants. Name them and say which test enforces each one.
- Failure modes: what happens when each dependency is slow, down, or wrong.
- Latency and time budget, in ms, where an order path is involved.
- Security and privacy: tokens, masking.

## 7. Alternatives rejected

| Alternative | Why not |
|---|---|

## 8. Decisions

| # | Decision | Date | By |
|---|---|---|---|
| D-1 | … | | owner |

## 9. Rollout and verification

The phases (summarised, linking to the plan), the test grades, the owner gates, and how
to roll back.

## 10. Open questions

Numbered. Each names who answers it and what it blocks.

## 11. Review log

| Date | Reviewer | Comment | Resolution |
|---|---|---|---|
