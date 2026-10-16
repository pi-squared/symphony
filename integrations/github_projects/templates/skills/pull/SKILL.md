---
name: pull
description: Synchronize a Symphony issue branch with the current base branch and recover synchronization conflicts.
---

Confirm origin is REPOSITORY and branch equals the
current cycle branch. Preserve unfinished edits with a named stash including
untracked source when necessary; record the stash reference and restore it.
Fetch origin/BASE_BRANCH and the current branch using the saved gh credential helper.
Merge origin/BASE_BRANCH into the issue branch. For an existing published branch, fetch
and inspect divergence first; integrate legitimate remote updates without force
pushing. Resolve conflicts using issue intent, rerun affected checks, and record
merge sources, clean/conflicts-resolved result and resulting short HEAD in the
workpad as pull skill evidence. Never discard unknown remote or local changes.
If the selected PR is closed or merged, stop using the branch and return through
Ready/claim to reserve a fresh cycle. Rework is authorized only by its board state.
