---
name: commit
description: Commit scoped repository changes during a Symphony issue work cycle.
---

Inspect status, branch, diff and staged paths. Use the selected cycle branch
from `project_claim`. Preserve unrelated work and keep frozen inputs unchanged.
Stage explicit paths, inspect the staged diff and run the relevant validation.
Commit with a concise behavior-focused message using the checkout's identity;
if absent use the saved GitHub login's noreply identity for this command only.
Do not amend or rewrite published history without explicit authority. Keep
credentials, recordings, receipts, build outputs and caches out of the commit.
