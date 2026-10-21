# GitHub Projects integration

This integration brings the OpenContracts Symphony controller into this fork as
a configurable tracker. The Elixir scheduler owns dispatch, concurrency,
continuation, retries and stalls. The Python host owns Project admission,
workspace preparation, work-cycle recovery and validation. Codex owns commits,
pushes and PR creation through the supplied worker skills.

```text
queued → working → validated draft PR → review
human rework → archived previous checkout → fresh branch → working
human merging → feedback/check/conflict loop → expected-head squash merge → done
```

Review is idle. Comments, failing checks and changes-requested reviews do not
restart an issue. Only human board transitions authorize rework and merging.
SQLite records preserve cycle history and interrupted completion. Cleanup moves
dirty or unpublished work outside the deletion root; failed required local
cleanup prevents deletion. Each installation owns one repository and board.

## Build and install

Use Linux x86-64, Python 3.11, the fork's Elixir/Erlang toolchain, Git, bubblewrap,
the complete standalone Codex package and a saved GitHub CLI login with repository
and Projects access. Browser dependencies are optional. Installation does not
start a service or enable admission.

1. Build the selected fork revision with `make -C elixir all`.
2. Copy `settings.example.json` to a private location outside the checkout.
   Set the repository, project owner/number, expected CI check names, and
   independent check argument arrays. `{python}` selects the installed Python.
   Checks run in bubblewrap without credentials or network access and with Git
   metadata mounted read-only; configure required runtime roots explicitly.
3. Run:

   ```bash
   python3.11 integrations/github_projects/install.py \
     --settings /private/project-settings.json \
     --destination /private/symphony-project
   /private/symphony-project/operate add-states
   /private/symphony-project/preflight
   /private/symphony-project/operate install-service
   /private/symphony-project/controller enable --only-issue 123
   /private/symphony-project/controller status
   ```

The saved GitHub login is reused; tokens stay out of workflow prompts and tracked
files. If `gh` is omitted, installation fetches and verifies the pinned official
GitHub CLI. A supplied `gh` path uses the operator's existing installation;
native attachment uploads require 2.102.0. Codex and Python are copied into the
installation so a temporary source checkout can be removed after installation.
Erlang/escript remains a configured host dependency.

After validating a pilot, use `controller enable` to admit the remaining queue.
Pause with `controller disable` and stop the configured user systemd service
before upgrading. Reinstallation backs up configuration and a consistent SQLite
snapshot, preserves workspaces and cycle archives, and selects a versioned
release. A different repository or board requires a separate destination.
For rollback, keep admission paused, stop the service, restore the backed-up
configuration/workflow/release selection, and rerun preflight. Preserve newer
workspaces and ledger records rather than restoring an old database over them.

## Configuration

Both user-owned and organization-owned Projects v2 boards are supported.
`base_branch` defaults to `main`. `fields` maps status, priority and execution
order names; defaults are `Status`, `Priority` and `Execution order`. Priority
values P0/P1/P2 retain the existing numeric ordering. Execution order wins over
priority, then issue number breaks ties.

The optional `roles` object maps all seven roles: `backlog`, `queued`, `working`,
`review`, `rework`, `merging`, `done`. Default states are Backlog, Ready,
In progress, In review, Rework, Merging and Done. Existing state option IDs are
preserved when `operate add-states` adds Rework/Merging; other roles must already
exist. Runtime policy and role names are rendered into the installed workflow.
Worker skills are templates installed into issue workspaces, separate from this
repository's contributor skills. Existing target-repository skills are preserved.

Defaults are one worker, 30-second polling, 20 turns, five-minute maximum retry
backoff, `gpt-6.1-sol` with medium reasoning, and the named `symphony` permission
profile. `max_concurrent_agents`, `model` and `reasoning` are configurable.
Explicit human Rework starts a fresh cycle using `rework_model: gpt-6.1-sol`
and `rework_reasoning: xhigh`. Settings are persisted with that cycle, so retries
and resumption after the board returns to In progress keep xhigh. Ordinary work
keeps the installation's existing model/reasoning. The bound worker launcher
uses actual CLI configuration, not prompt instructions.
Preflight checks both ordinary and Rework settings, profile, dynamic-tool protocol and actual
Git writes without starting a model turn. Enabling admission requires a receipt
matching the installed release and workflow.

`required_labels` defaults to `ready-for-agent`; `assignee` optionally filters
candidates. `dependency_gating` defaults to true. `protected_paths` is a list of
workspace-relative glob patterns checked across the entire prospective PR,
including committed changes. `required_checks` and `checks` must be supplied;
missing, pending, cancelled, skipped, failing or stale CI cannot satisfy review
or landing. External tests that need live models remain an explicit project task.

The installed workflow selects `tracker.kind: github_projects` and supplies
`tracker.provider.repo`, `controller`, `roles` and `browser_enabled`. The adapter
provides `github_api`, `project_claim`, `project_workpad`, `project_restart`,
`project_validate`, `project_review`, `project_done`, `project_block` and
`project_evidence`. `project_browser` is advertised only when an application
worker is configured. Tool calls are bound to the current issue and workspace.

## External application worker

No application journeys, fixtures or issue-number scenario registry ship in this
integration. Applications supply their worker outside issue checkouts. Add a
`browser` object to private installation settings:

```json
{
  "python": "/private/browser-venv/bin/python",
  "worker": "/private/application-worker.py",
  "scenarios": ["application-flow"],
  "required_paths": ["ui/**"],
  "browser_cache": "/private/playwright-cache",
  "timeout_seconds": 600
}
```

These are configuration identifiers, not a bundled scenario implementation.
`project_browser` accepts only `start`, `status`, `record`, `stop` and a configured
scenario name. Executables, URLs and fixture payloads are not tool arguments.
Without browser configuration, core orchestration and image evidence remain
usable. Matching `required_paths` changes require a completed current-source
capture before review; an empty list makes browser evidence optional.

The harness runs the trusted entrypoint as:

```text
PYTHON WORKER start|record|stop ABSOLUTE_JOB_JSON
```

Workers read and update that job file, preserving its `id`, `issue` and
`workspace`. The directory is private, outside the checkout. Worker stdout/stderr
go to private logs. Runtime parameters and the worker's SHA-256 are bound to the
session. The worker supplies application-specific behavior:

- **start:** launch/build the application, set `build.sha256` to the SHA-256 of
  the production build, and store cleanup identifiers in `provider_state`.
  Return only after the application is ready. The harness sets session status.
- **record:** read the selected `scenario` from the capture job and the sibling
  session at `JOB.parent.parent/session.json`. Run the full application journey,
  record `capture.webm` beside the job, and write a nonempty `results` list with
  `name`, `result: "passed"`, and `seconds` for each assertion. Set
  `persisted_state_verified: true` and `browser_errors: []` only after verifying
  the application's persisted outcome and browser errors. The harness sets the
  final receipt, hashes and completion status.
- **stop:** clean up application resources using the session's `provider_state`.
  Cleanup must tolerate partial startup and repeated calls. The harness cancels
  owned jobs first and writes a stop marker before invoking cleanup.

Use Playwright 1.62.0 and its matching Chromium/encoder cache for attachment
playback. Record a native 1440×900 WebM, at most 50 MiB. The harness independently
decodes it using host `ffprobe`/`ffmpeg`, checks source freshness before and after
the job, and rejects incomplete, failed, altered or foreign captures. Committing
tested bytes preserves source identity; application edits invalidate captures.
The recording utilities provide cursor/click indicators and browser preflight.

`project_evidence` publishes native video attachments in the bound issue
discussion, verifies downloaded bytes and persisted player playback, and caches
receipts for retries. Supply `browser_cache` for this verification. PRs link to
the discussion; video files and direct PR video embeds are rejected. PNG/JPEG/GIF
evidence is supported separately without a browser worker.

## Validation and provenance

```bash
PYTHONDONTWRITEBYTECODE=1 python3.11 -m unittest discover \
  -s integrations/github_projects -p 'test_*.py'
make -C elixir all
```

Tests use disposable repositories, synthetic workers and local media. They do
not operate a live board or run application verification journeys.

Imported from `pi-squared/OpenContracts-Development` revision
`e5eeac62ea2afbfd43c57c2d8522d8cefaf04d52`, whose engine overlay targeted
`be10a1b79df723d6d7612b5651c8522704dafb2e`. Engine changes are integrated directly
in this fork. Application-specific launchers, fixtures, assertions, journeys,
deployment code and pilot issue 33 additions are excluded. Recording helper
provenance is retained in `recording/provenance.json`.

## Local Docker issue workers

Set `docker.image` to an immutable local `sha256:...` image ID or registry
`name@sha256:...`, with the complete Codex package at `/opt/codex/bin/codex`,
Git, GitHub CLI and project build/test tools. The host's Docker daemon is never
mounted into the worker. `docker.auth_file` selects a dedicated Codex auth cache;
only that file seeds a private per-issue home. Refreshed credentials and sessions
persist across retries. `docker.git_auth_file`, when configured, is a private
repository-scoped GitHub CLI `hosts.yml`; board credentials remain on the host.
An optional `ca_bundle` preserves the configured proxy's certificate trust.

The issue checkout is mounted at the same absolute path, with the invoking
UID/GID. The image root is read-only, capabilities are dropped, and default
ceilings are 2 CPUs, 4 GiB and 512 processes. Docker Cloud is not used.
Repository skill discovery is disabled through supported per-skill CLI overrides;
project Codex configuration is untrusted. Tracked skill/config files remain intact
for Git. Packaged system skills are extracted from the pinned image and mounted
read-only, rather than trusted by a writable directory name. Preflight inspects the effective skill catalog.
The existing Git, review, evidence and landing procedures become ordinary inline
workflow instructions rather than installed skills. AGENTS.md remains readable.

Independent checks use separate networkless containers without auth caches or
Git credentials and with Git metadata read-only. Application browser workers
continue to use the explicitly configured external worker contract; their
application-specific environment remains the operator's responsibility.

Container names and labels bind installation, issue workspace and cycle.
Cancellation removes only matching owned containers. An exclusive per-workspace
launcher lock prevents duplicate workers. Startup reconciles and removes orphaned
containers from previous cycles before reusing the persistent checkout. Controller
cleanup also removes owned containers. Failed Docker inspection blocks retries
rather than assuming an orphan is gone. Enabling admission requires a preflight
receipt bound to the actual executable/arguments and complete runtime settings.

## Optional concurrent Jules worker

Configure `jules` with `enabled: true`, the absolute official CLI `binary` path,
and `label: "runner:jules"`. Sign in and authorize the exact target repository
through Jules before enabling the installation. Labelled eligible Ready issues
select Jules; other issues select Codex. Required labels, native blockers,
assignee filtering and the configured repository/board still apply. The local
supervisor runs one Jules companion alongside Symphony's configured Codex pool.
Only different issues run concurrently; claim locks and persisted backend choices
prevent duplicate ownership. Label edits affect future claims, not active cycles.

The CLI's lack of idempotent submission requires a write-ahead `submitting`
record. A lost or malformed response becomes `submission_unknown` and reserves
ownership; the worker never resubmits it automatically. Inspect the exact private
prompt and Jules session list to reconcile it. Stopping the local supervisor
leaves remote sessions recorded; their existence is not inferred from local PIDs.
A known remote session resumes polling after restart. Human Rework waits for a
remote session to become terminal, then selects a fresh Sol/xhigh Codex cycle,
even when the original issue retains the Jules label. Merging also uses Codex.

Jules returns a patch and a structured temporary handoff. The host checks it in
its isolated issue checkout, removes the handoff file, runs independent checks,
commits, pushes without force and verifies the remote SHA. It discovers or creates
one exact-branch draft PR; interrupted application, commit, push and publication
have recorded recovery points. Raw CLI output, prompts, patches and reports stay
under private state, outside the issue checkout and source repository.

Current-head CI and the existing application-evidence gates still govern entry
to In review. Missing browser evidence or unresolved PR feedback retains the work
and records an operational error; it does not waive validation or trigger an
unrequested duplicate attempt. The CLI's plan/input requests remain visible in
its remote status. The companion does not merge or deploy; only existing human
Rework and Merging transitions authorize those lifecycle routes.
