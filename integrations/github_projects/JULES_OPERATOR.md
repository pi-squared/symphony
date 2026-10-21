# Jules Operator Guide

This guide describes operational behavior, lifecycle gating, and session handling when running the companion Jules worker alongside Symphony's Codex worker pool.

## Runner Selection and Concurrency

- **Issue Label Selection**: Adding the `runner:jules` label to an eligible issue selects the Jules worker when claimed for a new cycle. Other issues without this label select the default Docker Codex worker.
- **Concurrent Execution**: The local supervisor runs one Jules companion alongside the configured Codex worker pool. Jules and Codex work on separate issues concurrently.
- **Pinned Ownership**: Active-cycle ownership remains pinned to the assigned backend (`jules` or `codex`). Edits to issue labels during an active cycle do not alter backend assignment for that in-flight cycle.

## Session Lifecycle and Submission Reconciliation

- **Session Resumption**: Known remote sessions (`remote` phase) persist state in SQLite and resume polling after a service or process restart.
- **Ambiguous Submissions**: The Jules CLI lacks an idempotency key. If a submission fails or drops response connection without confirming a session ID, state becomes `submission_unknown`. The worker reserves ownership and never automatically replays or resubmits the prompt. Operators must inspect private prompts and the remote Jules session list to reconcile state manually.
- **CLI Execution State**: The CLI may report session status such as `Awaiting...` (e.g. awaiting plan approval or user input). The worker exposes this state in status listings rather than falsely claiming completion.

## Pipeline and Handoff Route

When a remote Jules session completes, work flows through the following controller pipeline:

1. **Retained Patch**: The host pulls the raw Git diff from the Jules session and verifies its hash.
2. **Independent Host/Controller Validation**: The host applies the patch in an isolated clean issue checkout, extracts and validates the temporary `.symphony-jules-report.json` handoff file, removes the handoff file from the index, and executes configured independent verification checks.
3. **Verified Branch and Draft PR**: The controller commits the validated changes, pushes the branch to GitHub, and creates or verifies an exact-branch draft Pull Request.
4. **Current-Head Checks and Review Entry**: The controller verifies current-head CI status and required application evidence. Once all checks pass and PR feedback is resolved, the issue transitions to human **In review**.

## Lifecycle Overrides and Authorization

- **Human Rework**: Transitioning an issue to Rework requires human action on the board. Rework terminates or waits out any active Jules remote session and initiates a fresh cycle using `gpt-6.1-sol` at `xhigh` reasoning via Codex, even if the issue retains the `runner:jules` label.
- **Merging**: Landing code onto the base branch requires human authorization and transitions through the board's Merging state. Merging is performed by Codex; Jules does not merge or deploy code.
