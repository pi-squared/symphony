---
name: github-tracking
description: Maintain GitHub Project issues and workpads during Symphony execution, including human-triggered Rework and linked Backlog follow-ups.
---

Call `project_claim` and use its roles and current cycle as the authority for routing.
Use `github_api` for REST (paths begin `/repos/REPOSITORY/`)
and POST `/graphql` for the configured project. Paginate comments, reviews and project items.
Credentials belong to the host tool; never copy tokens into prompts or files.

Update the entire current workpad through `project_workpad` with `body`, retaining
Plan, Acceptance Criteria, Validation, Notes and Confusions. Include a compact
`hostname:absolute-workspace@short-sha` stamp. Reconcile checked items before edits.
Controller notes append to the workpad; read it again before replacing it.

In review means wait. Never move an item to Merging or Rework yourself, including
after a changes-requested review. These board transitions belong to the human.
In explicit Rework, read previous_cycle, close its old PR with PATCH `/pulls/N`
body `{"state":"closed"}`, delete its old workpad with DELETE
`/issues/comments/ID`, then call `project_restart`. The hook has already preserved
the old checkout and reserved a fresh cycle/branch from BASE_BRANCH. Retry the same
cycle after an interruption. Do not close unrelated PRs or discard workspaces.

For discoveries outside scope, POST a new issue with title, description,
acceptance criteria and a link to the current issue; add it to the configured project via
`addProjectV2ItemById`, then set its existing Status option to Backlog with
`updateProjectV2ItemFieldValue`. Record the related URL in both issues. If the
follow-up depends on this issue, POST `/issues/FOLLOWUP/dependencies/blocked_by`
with the current issue's numeric database `issue_id`. Preserve existing field and
option IDs. Never rewrite issue descriptions to store plans.

For a genuine external blocker, exhaust supported access fallbacks, keep the
workpad's validation evidence, then use `project_block` with a concrete `reason`.
