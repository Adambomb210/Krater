# Krater — Project Ganymede Approval & Compute Allocation Portal

Revised Sep 26, 2026. Supersedes the Sep 24 draft (drafted with @Adam).
The previous draft was checked against the Weave codebase and the SkyPilot docs and source. This revision fixes the
places where it assumed capabilities that don't exist. The integration details are in
[weave-integration.md](weave-integration.md) and [skypilot-integration.md](skypilot-integration.md).

## What changed from the Sep 24 draft

| Area | Sep 24 draft | This revision | Why |
| --- | --- | --- | --- |
| Budget enforcement | Handed to "SkyPilot's internal budgeting" | **Krater enforces it**, using a SkyPilot admin policy and a spend reconciler | SkyPilot has no dollar budgets, only per-instance `max_hourly_cost` and estimated `cost-report` |
| Reviewer role | "A claim Krater reads from Weave" | Same, but it needs **new Weave work**: a `groups` claim, a `slack_id` claim, and a user directory API | Weave's claims today are only name, username, email, phone and `admin` |
| Slack membership | "Weave users are already Slack members" | **Only full Slack members can submit** (checked by Krater for now) | Weave signup is open, new users are single-channel guests, and some never join Slack ([weave#118](https://github.com/patchworklabsorg/weave/issues/118)) |
| Reviews | Attached to the project | Attached to a **project revision** | Otherwise approvals of an old version count toward a resubmitted one |
| Amendments | Approved → back through review | Approved project **stays approved** while an amendment revision is reviewed | Otherwise compute is cut off during review |
| Budget | `budget_approved` column + `BudgetReclaim` table | **Append-only budget ledger** | One place for the ceiling and for the audit trail |
| Comments | Mirrored from Slack into Krater's DB | **Not mirrored in v1**; Krater links to the channel | Mirroring means handling Slack edits, deletes and threads, for little gain |
| Deployment | "Two containers" | Portal, worker, Postgres, SkyPilot API server (+ optional sign-in proxy) | That's what it actually takes |

## Overview & scope

**Krater** is the web portal for Project Ganymede (Patchwork Labs). It does three things:

1. Reviews and approves member-submitted project proposals.
2. Allocates a dollar-denominated compute budget to approved projects, and **enforces** it against SkyPilot.
3. Hosts a public gallery of completed Ganymede projects.

**Out of scope:** running compute. SkyPilot, renting GPUs mainly from Vast.ai, runs in its own deployment. Krater doesn't
schedule or run jobs, but it *does* own:

- provisioning a SkyPilot workspace for each approved project;
- gating launches against the project's remaining budget;
- tearing down a project's clusters once it's over budget.

SkyPilot has no budget feature of its own to hand that off to (see [skypilot-integration.md](skypilot-integration.md)).

**Core capabilities**

- Proposal submission, with drafts and resubmission after rejection
- A configurable approval policy (reviewer sign-off), with review happening in a private Slack channel per project
- Dollar-based compute budgets, enforced through SkyPilot
- A two-stage lifecycle: proposal review, then a separate completion review before a project goes public
- A public gallery of completed, open-source projects
- Admin override at every stage, fully audited

## Roles & authentication

Three roles:

- **Submitter:** any Ganymede member who is also a **full Slack member** (see below). Can draft, submit, edit and
  resubmit after rejection, amend an approved project, and submit their project for completion review.
- **Reviewer:** can approve or reject proposals and completion requests. Can't review a project they submitted or are a
  credited builder on.
- **Admin:** a *Ganymede* admin. Can override the normal flow at any point: approve or reject at either stage, and adjust
  or reclaim budget. This is **not** Weave's existing `admin` flag, which means Weave operations admin.

A "logged in but can't submit" tier and a public/anonymous tier are still deferred. The gallery itself is public.

**Authentication** is OIDC against Weave (Authorization Code + PKCE; Weave enforces PKCE). All Weave access goes through
one adapter module (`WeaveClient`) so Weave changes don't spread through the app. The contract, including the Weave work
it depends on, is in [weave-integration.md](weave-integration.md).

**Role data lives in Weave.** Krater reads membership and roles from a `groups` claim at login, and from Weave's directory
API when it needs fresh data (Slack button clicks, inviting reviewers). It never stores a role as its own source of truth.
Proposed groups: `ganymede:member`, `ganymede:reviewer`, `ganymede:admin`, and later `ganymede:reviewer:<tier>`.

**Slack membership gate (interim).** Signing in through Weave doesn't guarantee Slack membership. Weave signup is open,
new users join Slack as single-channel guests until they accept the code of conduct, and Slack won't add guests to
another channel. So until [weave#118](https://github.com/patchworklabsorg/weave/issues/118) lands:

- On draft → submit, Krater looks the user up in Slack (`users.lookupByEmail` with the email claim), or by `slack_id`
  once Weave exposes it.
- The user must exist, and not be deleted, `is_restricted`, or `is_ultra_restricted`.
- If the check fails, the user can still save drafts but sees "Join the Patchwork Labs Slack and accept the code of
  conduct to submit", with a link to Weave.
- The check runs again right before channel creation, so a submission never fails halfway through.

Known limitation: Weave users can have several email addresses, and the email claim is the primary one. If someone's
Slack account uses a different address, the lookup misses them until the `slack_id` claim exists.

## Data model

Postgres. All money is stored as **integer cents** (`*_cents`). Names are suggestions; refine during implementation.

**User** (a cache of the Weave identity; no role data)
- `id`, `weave_sub` (Weave's `p_id`, e.g. `PWL5A1B2C3D4`; unique), `display_name`, `email`, `slack_user_id`
  (nullable), `groups_cached` (last-seen claim; for display only, never for authorization), `last_login_at`

**Project**
- `id`, `title`, `submitter_id`, `status`, `current_revision_id`, `approved_revision_id`, `repo_url`,
  `slack_channel_id`, `skypilot_workspace`, `created_at`, `updated_at`
- `status` values:
  - `draft`
  - `pending_review`
  - `changes_requested`
  - `approved`
  - `pending_completion_review`
  - `completion_changes_requested`
  - `completed`
  - `withdrawn`

**ProjectRevision** (an immutable snapshot of what was reviewed)
- `id`, `project_id`, `number`, `kind` (`proposal` | `amendment` | `completion`), `write_up`,
  `budget_requested_cents`, completion fields (`demo_url`, `screenshot_keys[]`, `credited_builder_ids[]`, `tags[]`),
  `submitted_at`, `outcome` (`pending` | `approved` | `rejected` | `superseded`)
- Drafts are edited in place. Submitting freezes them into a revision, and resubmitting creates the next one.

**Review**
- `id`, `revision_id`, `reviewer_id`, `decision` (`approve` | `reject`), `reason` (required on reject), `source`
  (`slack` | `web`), `created_at`

**BudgetEntry** (append-only ledger; the project's ceiling is the sum of its entries)
- `id`, `project_id`, `kind` (`initial_approval` | `amendment` | `admin_adjustment` | `reclaim`), `amount_cents`
  (signed), `actor_id`, `reason`, `revision_id` (nullable), `created_at`

**SpendSnapshot** (written by the reconciler; see [skypilot-integration.md](skypilot-integration.md))
- `id`, `project_id`, `estimated_spend_cents`, `source` (`skypilot_cost_report`), `taken_at`

**ApprovalPolicy** (configuration, not code)
- `id`, `stage` (`proposal` | `completion`), `min_budget_cents` (nullable; for tiering by budget size),
  `min_approvals` (currently 1), `required_group` (nullable; e.g. `ganymede:reviewer:senior`)

**AuditEvent**
- `id`, `actor_id`, `action` (e.g. `admin_approve`, `admin_reject`, `budget_adjust`, `policy_change`,
  `launch_blocked`, `teardown`), `project_id`, `payload` (jsonb), `reason`, `created_at`

**GalleryEntry:** a view over `completed` projects plus their approved completion revision. It isn't a separate table.

## Proposal & review workflow

1. **Draft.** The submitter fills in a title, requested budget and write-up (repo link optional). They can save and come
   back later.
2. **Submit.** Krater runs the Slack membership check, freezes the draft into revision N, sets `pending_review`, creates
   the project's private Slack channel if it doesn't exist yet, invites the submitter and all current reviewers (from
   Weave's directory API), posts the review message with Approve/Reject buttons, and posts a line in the master feed
   channel.
3. **Review.** Reviewers discuss freely in the channel. Decisions come in through the buttons, or through the web UI as a
   fallback. For every decision, Krater:
   - checks with Weave's directory API that the clicker is a reviewer right now (not from a cached claim);
   - rejects self-review (submitter or credited builder);
   - records the Review against the current revision;
   - asks `ApprovalPolicyService` whether the policy is now satisfied.
4. **Approve.** When the policy is satisfied, the revision is approved, a `BudgetEntry(initial_approval)` is written, the
   project's SkyPilot workspace is provisioned, and the project moves to `approved`.
5. **Reject.** A reason is required. The project moves to `changes_requested`, and the submitter edits and resubmits
   (step 2, same project, new revision).
6. **Amend.** An `approved` project can have its scope or budget amended. This creates an `amendment` revision reviewed
   under the same policy. **The project stays `approved` and keeps its current ceiling while the amendment is
   reviewed.** If the amendment is approved, the budget difference is written to the ledger. If it's rejected, nothing
   changes.
7. **Withdraw.** The submitter or an admin can withdraw a project at any point. The channel is archived, unspent budget
   is reclaimed, and the workspace is torn down.

Only approvals on the **current** revision count. A new revision marks earlier pending ones as `superseded`.

`ApprovalPolicyService` is its own module from day one. Given a revision, it finds the matching `ApprovalPolicy` rows
(by stage and budget size) and decides whether the approvals so far meet them. Multi-approval and reviewer tiers are then
just new rows.

## Slack integration

Slack is where review happens, not just where notifications go. Krater gets **its own Slack app**. Weave's app already
uses the one interactivity URL a Slack app can have, for code-of-conduct acceptance.

**Per-project private channel:** created on first submission and reused for amendments and the completion review.
Members are the submitter, credited builders (once added), and all reviewers. When a reviewer is added in Weave, they
get invited to open project channels by a periodic job, or by a Weave webhook once those actually fire. The channel is
archived when the project ends up `completed` or `withdrawn`.

**Master feed channel:** one top-level post per new submission, for visibility across Ganymede. It carries no decisions.

**Source of truth:** Krater's database holds decisions, budgets and state. Slack holds discussion. Comments are **not**
mirrored into Krater in v1; the project page links to the channel instead.

**App requirements**
- Bot scopes: `groups:write`, `groups:write.invites`, `chat:write`, `users:read`, `users:read.email` (for the interim
  membership check).
- An HTTPS interactivity endpoint on the portal, reachable by Slack.
- Slack signature verification (`X-Slack-Signature` with the signing secret, and a check on how old the timestamp is).
  Weave's `SlackSignatureVerification` concern is a working reference.
- Slack must get an acknowledgement within 3s. Do the real work in the worker, then update the message.
- Invites and posts must be safe to retry: `already_in_channel` counts as success, and failures are retried from the
  worker.

**Not needed:** a separate completion announcement. The gallery is enough (revisit later).

## Completion flow & public gallery

The submitter fills in the completion fields (final write-up, screenshots, demo link, repo link, credited builders,
tags) and submits them as a `completion` revision. The project moves to `pending_completion_review`. Review happens in
the same channel, under the `completion` stage policy.

- **Approved:** the project becomes `completed`. It's published to the gallery, spend is frozen at its final value,
  unspent budget is reclaimed to the ledger, and the SkyPilot workspace is torn down.
- **Rejected:** the project moves to `completion_changes_requested`. The submitter revises and resubmits.

**Public gallery** (no auth). Each entry shows the write-up, screenshots, demo link, repo link, credited builders,
compute spent (latest reconciled estimate, labelled as an estimate), and tags for browsing.

**Screenshot storage:** S3-compatible object storage, as a placeholder until a provider is chosen (see Open questions).
Uploads use presigned URLs, and only objects from approved completion revisions are served publicly.

## Budget handling

- The **ceiling** is the sum of the project's `BudgetEntry` rows. Approvals, amendments, admin adjustments and reclaims
  all add entries, so the ledger doubles as the audit trail.
- **Spend** is SkyPilot's estimate (catalog price × uptime), pulled regularly by the reconciler. It's an estimate:
  Vast's live prices and disk charges mean the real bill differs. Show it as "≈ $X estimated", and reconcile against Vast
  billing out of band.
- **Enforcement** (details in [skypilot-integration.md](skypilot-integration.md)):
  - Every launch goes through Krater's admin policy endpoint. It's rejected if the project isn't `approved` or its
    estimated spend has reached the ceiling. Allowed launches are forced to autodown and capped with `max_hourly_cost`.
  - The reconciler warns the project channel at 80% of the ceiling, and tears down the project's clusters and managed
    jobs at 100%.
- **No automatic expiry.** Stalled projects are reclaimed manually by an admin (`BudgetEntry(reclaim)`).

## Admin overrides

At any point an admin can:
- approve or reject at either review stage, bypassing `ApprovalPolicy`;
- adjust an approved project's budget up or down, or reclaim unspent funds;
- withdraw a project.

Every override writes an `AuditEvent` with who, what, when and a required reason. Budget changes also write a
`BudgetEntry`. Overrides are posted in the project's channel so reviewers can see them.

## Deployment architecture

Everything runs in Docker and is defined in one Compose file, so it can run on any host. The existing host (alastor) is
arm64; SkyPilot publishes arm64 images, so that works.

| Service | What it is |
| --- | --- |
| `portal` | FastAPI web app: UI, OIDC, Slack endpoints, public gallery, SkyPilot admin-policy endpoint |
| `worker` | Same image, runs background jobs: Slack work, the spend reconciler, reviewer-channel sync |
| `db` | Postgres |
| `skypilot` | SkyPilot API server with the Vast credentials; out of scope except for its configuration |
| `auth-proxy` | oauth2-proxy with Weave as the OIDC issuer; members sign in to the SkyPilot CLI and dashboard through it |

Networking:
- Slack must reach `portal` over public HTTPS.
- The admin-policy endpoint is **internal only**. SkyPilot's policy calls carry no authentication, so the endpoint must be
  reachable only from the `skypilot` container.

**Stack:** Python + FastAPI + Postgres. Python matters because Krater imports SkyPilot's own request decoder
(`sky.admin_policy.UserRequest`) and client SDK, rather than reimplementing SkyPilot's wire format.

## Open questions

1. **Screenshot storage provider.** S3-compatible for now; pick a provider (self-hosted MinIO next to the portal, or
   R2/B2/S3). A reminder is set to circle back.
2. **How members use SkyPilot:** decided. One private SkyPilot workspace per project, and members sign in to SkyPilot
   with Weave (oauth2-proxy, limited to `ganymede:member`). See
   [skypilot-integration.md §0](skypilot-integration.md#0-member-access-one-workspace-per-project-sign-in-with-weave).
   Still to confirm in the spike: the full flow on Docker Compose.
3. **What happens at the ceiling.** Proposed: warn at 80%, block new launches and tear down at 100%, with no grace
   period. Consider a small admin-configurable grace so a running training job isn't killed at 100.1%.
4. **Weave changes.** The `groups` claim, `slack_id` claim and directory endpoints are implemented on Weave branch
   `claude/exciting-sagan-7oh2zh` (not merged yet). The Slack membership gate is tracked in
   [weave#118](https://github.com/patchworklabsorg/weave/issues/118).

## Parked / future work

Designed for, not built now:

- **In-progress project visibility:** letting members see active projects, to avoid duplicate work.
- **Multi-approval policy:** new `ApprovalPolicy` rows with `min_approvals > 1`.
- **Reviewer tiers by budget size:** `ApprovalPolicy.min_budget_cents` plus `required_group`.
- **Comment mirroring from Slack,** if the channel link turns out not to be enough.
- **Reconciling against real Vast billing** instead of SkyPilot's estimates.
