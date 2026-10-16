---
name: land
description: Land the selected Symphony PR only after a human moves the issue to Merging.
---

Call `project_claim` and confirm its current board state is the configured
Merging role. Never enter Merging yourself. Use the recorded PR number, cycle
branch and repository. First inspect the selected PR. If it is already merged,
call `project_done` immediately and stop; an interrupted merge response must not
start a new cycle or invoke pull. If it is closed without merging, record a
blocker and wait for a human Ready/Rework decision. Mark the open draft ready using POST `/graphql` with
`markPullRequestReadyForReview(input:{pullRequestId:...})`, discovering its node
ID through GET `/pulls/N` first.

Run the push skill feedback sweep and the pull skill. Resolve conflicts and
check failures, rerun required validation after changes, commit and push updates.
Keep the selected PR body and maintained workpad current. Check that all checks returned by project_claim and all required repository checks succeed for the
current PR head. Require GitHub's mergeability to be confirmed and the PR to
be ready; blocked/dirty/unknown mergeability must be resolved without bypass.
Poll unfinished checks with bounded individual waits and keep
working on actionable failures; do not treat absent checks as success.

Immediately before merge refresh the board with project_claim and GET `/pulls/N`.
Reconfirm Merging, branch, the configured base branch, exact validated remote SHA, completed feedback
and all current-head checks. If HEAD changes, repeat synchronization, validation
and feedback checks. PUT `/repos/REPOSITORY/pulls/N/merge`
through `github_api` with `{"merge_method":"squash","sha":"VALIDATED_SHA"}`.
The host checks the state, selected PR and current-head CI again. Respect branch
protection and merge rules; never request administrator bypass or enable auto
merge. A 409/405 means refresh and resolve the reported condition. After an
interrupted response, GET the PR and confirm merged before retrying. Once GitHub
confirms merged call `project_done`; it closes the issue and moves it to Done.
Stop browser resources; terminal cleanup preserves unfinished work and archives.
