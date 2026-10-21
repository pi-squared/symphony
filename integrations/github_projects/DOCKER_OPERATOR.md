# Local Docker worker operations

Symphony can run each issue's Codex worker in local Docker. Docker Cloud remains
deferred. See [installation and configuration](README.md) for setup; the behavior
below is implemented in `docker_worker.py`, `worker.py` and `controller.py`.

## Runtime and resources

Set `docker.image` to an immutable image ID (`sha256:…`) or registry digest
(`name@sha256:…`). Supply the complete Codex package and project build/test tools
in that image, and select a dedicated Codex auth cache with `docker.auth_file`.
Run preflight before enabling admission; execution configuration changes require
a fresh matching preflight receipt.

Default per-container ceilings are **2 CPUs, 4 GiB memory and 512 processes**,
configurable through `docker.cpus`, `docker.memory` and `docker.pids_limit`.
These are limits, not a 4 GiB reservation for every idle issue. Account for actual
concurrent workers and checks when sizing the host.

The image root is read-only, capabilities are dropped, and the host Docker socket
is not mounted. The worker uses the invoking UID/GID and a writable bind mount of
the persistent per-issue checkout at the same absolute location. Replacing a
container does not discard checkout edits or commits. A private per-issue Codex
home preserves refreshed authentication and sessions across retries; host
profiles, memories and skill catalogs are not copied into it.

## Git authentication and skills

For this host-authenticated operating mode, leave `docker.git_auth_file` unset:
no host GitHub token is placed in the container. The implementation also supports
an optional dedicated repository-scoped credential mount; that is a separate
configuration, not needed for this mode. Board credentials remain on the host.

Use the issue-bound host tool `project_git` with `action: "fetch"` before locally
merging `origin/BASE_BRANCH`. Make local commits in the selected issue branch,
run required checks and `project_validate`, then use `project_git` with
`action: "push"`. The host checks the bound repository and branch, validates
before pushing, and verifies the remote SHA. These calls replace authenticated
shell fetch/push inside the container. Create the draft PR through `github_api`.

Do not download or install skills. Packaged OpenAI skills from the pinned image
are allowed and mounted read-only. Repository skill discovery is disabled and
project Codex configuration is untrusted; tracked files remain intact and
`AGENTS.md` remains readable. Git/review/landing procedures are inline workflow
instructions. Custom installed skills in the worker home block restart.
Independent checks run in separate networkless containers without auth caches
or Git credentials, with Git metadata mounted read-only.

## Ordinary work and human Rework

Ordinary cycles use installation `model` and `reasoning` settings (defaults:
`gpt-6.1-sol` and `medium`). Only a human board transition to Rework authorizes a
fresh Rework cycle, using `rework_model` and `rework_reasoning` (defaults:
`gpt-6.1-sol` and `xhigh`). The prior checkout is archived and a fresh branch is
prepared. Cycle settings persist through retries and restart recovery, including
after Rework returns to In progress. Review comments or failing checks alone do
not authorize Rework. In review is idle; landing requires the human Merging
transition.

## Cancellation, orphan cleanup and recovery

An exclusive per-workspace launcher lock prevents duplicate local launchers.
Container names encode installation/workspace identity and cycle; cleanup checks
installation and workspace labels before removal. SIGTERM, SIGINT and SIGHUP
handlers force-remove the owned worker container and terminate its Docker client;
normal launcher exit also attempts removal. This is container cancellation, not
a guarantee that in-flight commands finish gracefully.

If a launcher dies before cleanup, the next launch removes matching owned
containers, including previous-cycle orphans, before starting a replacement on
the persistent checkout. Controller cleanup also removes owned containers.
Foreign containers are refused. Failed Docker listing/inspection blocks recovery
rather than treating an unverified container as absent; resolve Docker access
before retrying.

Preserve the checkout, private worker home and SQLite cycle ledger across
restarts. Recovery retains the cycle's branch, work and execution settings;
it does not require a new Rework cycle. Terminal cleanup preserves dirty or
unpublished checkouts outside the deletion root, and failed required cleanup
prevents workspace deletion. Do not manually erase preserved work to clear a
retry.
