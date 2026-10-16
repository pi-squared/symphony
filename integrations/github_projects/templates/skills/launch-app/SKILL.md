---
name: launch-app
description: Start or record full-application validation using the configured browser worker.
---

Read project_claim and the repository validation instructions. When project_browser
is available, call action start and poll status until ready. Choose a scenario
from the returned scenarios list, call action record, then poll for a completed
capture. Fix failures and record again after source changes.

Use completed capture IDs with project_evidence. The operator supplies application
startup, fixtures, journeys and persisted-state assertions through the external
worker. If a required scenario is unavailable, record a concrete configuration
blocker through project_block. Stop owned resources using action stop.
