---
name: publish-evidence
description: Publish validated full-app browser captures and image evidence in a Symphony issue discussion.
---

Use completed `project_browser` capture IDs from the current source. Call
`project_evidence` with summary, blocked false, blocker empty, checks containing
actual command/result/details evidence, browser_capture_ids and optional artifacts
(path, sha256, caption). The service validates receipts, source inventory,
production build, runtime assertions, download hashes and inline-player playback.

For image evidence without browser support, supply the same report and checks
with image artifacts, and omit browser_capture_ids. Browser evidence is required
only for the installation's configured changed-path rules or explicit issue criteria.

Native inline videos are published only in issue comments; link that discussion
from the PR. Never commit videos, put player embeds in PR bodies, manufacture
capture receipts or substitute component recordings. If evidence validation fails,
fix the source/service failure and record again; preserve honest failed evidence.
