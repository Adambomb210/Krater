# Krater Compute

Krater rents Vast.ai machines for Ganymede members itself, replacing SkyPilot. This branch (`claude/krater-compute`, on
top of PR #1's `claude/compute-off`) is the Compute PR. Design: "Krater Compute v1: design" (a Claude Doc Adam shared).
Nothing here has rented a real machine yet: see [What still needs a paid check](#what-still-needs-a-paid-check).

## The pieces

**Vast client** (`krater/vast/`). A typed `httpx` client for search, create, show, list, change bid, stop/start,
destroy, logs, charges and account SSH keys, built on what the 2026-10-11 spike learned the hard way (trailing slashes,
the 512-offer cap, the v1 list, never logging a create response). `FakeVast` serves the same API in tests and copies the
behaviours that cost money to get wrong. Only the worker builds a client. See [vast-api.md](vast-api.md).

**Settings** (`/admin/compute`). Every policy number is a global default with per-project overrides, changed with a
reason and an audit event: reliability floor, bid margin, pause grace, move after, box idle, destroy after, reconcile
alert, runner start deadline, heartbeat timeout, storage quota, box sync deadline, box resume timeout.

**Price history** (`/pricing/history`). Every 10 minutes the worker samples 18 GPU models, bid and on demand: min, p10
and median full hourly cost of 1-GPU offers with 32 GB of disk. Members pick targets from the chart.

**Scheduler** (`krater/compute/scheduler.py`, every minute under a lock). Waits for an offer at or under a job's target,
bids min_bid plus the margin, holds budget under the project's row lock, survives a create whose response is lost (row
and label committed first), destroys Krater-labelled machines it doesn't know, handles pauses (grace, raise the bid,
move), runtime limits and finished projects, and confirms every destroy. See [scheduler.md](scheduler.md).

**Spend**. A live estimate per instance (disk from create, GPU only while running) into `SpendSnapshot`, so the project
page, the 80%/100% alerts and Quilt events work as before; a hard stop one minute ahead of the ceiling; an hourly
reconciliation with Vast's charges, which never lowers a live instance's estimate and never sends Quilt a decrease
(quilt#17).

**Runner** (`runner/`, Go, standard library). Fetched by a bootstrap that checks its sha256; reports start, heartbeats
with nvidia-smi GPU use, restores and uploads checkpoints and outputs through presigned URLs (signed sizes, multipart
above 64 MB, a per-project quota), reports the exit code. One token per machine. See [runner.md](runner.md).

**Boxes**. SSH workspaces in Vast's SSH mode: owner's keys from `/account/ssh-keys`, idle stop after the runner saves
`/workspace`, resume on the same machine or move elsewhere (the member's choice), destroy after the destroy time.
Refused while the Vast account has SSH keys.

**Member UI**. `/projects/{id}/compute`: jobs and boxes, the submit form beside current prices, a job page with SSH
details, stop/resume/move/cancel and an event log.

**SkyPilot removed.** Its code, compose services, policy endpoint, reconcile task and project columns are gone. The
public GPU catalog behind `/pricing` and the budget estimator stays (`krater/pricing/catalog.py`), and renamed settings
still read their old `KRATER_SKYPILOT_*` names for one release.

**Staging helpers**. `python -m krater.compute.submit` (queue, list, cancel from a shell) and
`scripts/dev/compute_watchdog.py` (the independent backstop for a paid test).

## Still to do on this branch

- Not built, and needing a decision: project secrets (such as `HF_TOKEN`) encrypted at rest, and where that key lives;
  a member CLI, which needs an auth design; Slack messages for box idle and destroy warnings (they are events only).

## What still needs a paid check

One round on staging, with Adam's approval, each a few cents with the spike's safety rules:

- an image with an ENTRYPOINT and one without (is `onstart` really the entrypoint override in `args` mode?);
- the full run: runner download from a Vast host, start, heartbeat, exit, destroy confirmed;
- whether Vast's charges are current while a machine runs;
- a natural pause (a long bid at `min_bid`), and what a paused instance's status says;
- a box: sshd and the runner in SSH mode, what lands in authorized_keys, the 22/tcp mapping, `JUPYTER_TOKEN`.

## Deploying

Production stays `KRATER_COMPUTE_MODE=off` until those checks pass. Turning it on needs, in infra: the Vast key mounted
into the worker only (`KRATER_VAST_API_KEY_FILE`), `KRATER_VAST_MODE=live`, a production `KRATER_COMPUTE_LABEL_PREFIX`,
`KRATER_COMPUTE_MODE=vast`, and the `krater-compute` bucket (created by the deploy's `ensure_bucket`).
