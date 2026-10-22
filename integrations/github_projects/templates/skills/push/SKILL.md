---
name: push
description: Push validated Symphony issue commits and create or maintain a draft PR through native Git and GitHub tools.
---

Run the pull skill and all required scope validation before every push. Invoke
`project_validate` to run independent credential, frozen-input and repository
checks across committed and uncommitted changes. Fix failures and rerun. Read
`project_claim` for current toolchain and board state; stop if no longer active.

Codex owns publication: commit scoped paths, `git push -u origin CURRENT_BRANCH`
without force, and verify the remote ref equals local HEAD. Recover an interrupted
push by reading the remote before retrying. Use the saved checkout credential
helper; do not export tracker tokens.

Discover existing PRs with GET `/repos/REPOSITORY/pulls`
params `state=open`, `head=OWNER:CURRENT_BRANCH`, `base=BASE_BRANCH`. Reuse the
matching current-cycle PR. If none exists, POST `/pulls` with title, head, base
BASE_BRANCH, draft true and a body following the repository PR template. Include the
issue URL and follow the repository's body checks, including executable test
checkboxes when required. Keep internal HTML markers out of the PR body; exact
repository/base/branch ownership already identifies this cycle. After an ambiguous API response,
rediscover before retrying creation. Maintain the body for the final behavior,
validation and risks. Add the symphony label if available (create it if allowed).
Videos belong only in the linked issue discussion.

Sweep all feedback: GET `/issues/PR/comments`, `/pulls/PR/comments` and
`/pulls/PR/reviews`; paginate each. Read any Manual QA Plan. Address every
existing actionable item or post a reasoned reply on its thread. Record each
resolution in the workpad; rerun checks after edits. Optional bot reviews are
never a prerequisite to wait for.

Require all checks returned by project_claim success for the exact PR head.
Pending, missing, cancelled, skipped or stale checks fail the gate. Refresh the
workpad, confirm its criteria and ticket validation are complete, publish required
current-source evidence, then call `project_review` with `pr_number` and `sha`.
On failure continue the check-address-verify loop while the board remains active.
