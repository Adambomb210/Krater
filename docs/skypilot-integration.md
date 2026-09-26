# SkyPilot integration & budget enforcement

SkyPilot runs Ganymede's compute, mainly renting GPUs from Vast.ai. It runs as its own API server. Krater doesn't
schedule or run jobs, but it **does** enforce each project's dollar ceiling, because SkyPilot has no budget feature to
hand that job to.

Checked against the SkyPilot docs (docs.skypilot.ai) and `skypilot-org/skypilot` master on 2026-09-26.

## What SkyPilot does and doesn't provide

| Need | SkyPilot | Notes |
| --- | --- | --- |
| Dollar budget per project/team | ❌ None | No budget, quota or spend-cap feature in the CLI, SDK, workspaces or config |
| Price cap | ⚠️ `resources.max_hourly_cost` | **Per instance, per hour.** Filters which instances can be picked; doesn't cap a total |
| Spend figures | ⚠️ `sky cost-report` / `cost_report()` | Per-cluster **estimate**: catalog price × uptime. The docstring says it "may not be accurate for the cluster with autostop/use_spot set or terminated/stopped on the cloud console." Rows include `workspace` and `total_cost` |
| Isolating projects | ✅ Workspaces | `private: true` + `allowed_users`. The API server has `/workspaces/create`, `/update`, `/delete`, `/batch_add_users`, `/batch_remove_users` (these return async request IDs) |
| Gating launches | ✅ Admin policies | Server-side `validate_and_mutate(UserRequest)`. Can reject or rewrite any launch. `RestfulAdminPolicy` POSTs the request to a URL and treats **HTTP 400 as a rejection** |
| Machine access | ✅ Service-account tokens | Bearer tokens (`/users/service-account-tokens`). Krater's admin token uses this |
| Member sign-in (SSO) + RBAC | ✅ via oauth2-proxy | The docs only show the Helm chart, but in the source it's two env vars on the API server (`SKYPILOT_AUTH_OAUTH2_PROXY_ENABLED=true`, `SKYPILOT_AUTH_OAUTH2_PROXY_BASE_URL`) pointing at an oauth2-proxy. That works in Docker Compose. Users are identified by email. RBAC (roles, private workspaces) needs SSO to be on |
| arm64 images | ✅ | `linux/amd64` and `linux/arm64` are published |

### Vast.ai caveats

- Vast support is **community-maintained**.
- Not supported on Vast: multi-node clusters, mounting object stores, custom disk or network tiers, and HA controllers.
- **Pricing drift:** SkyPilot prices Vast from a cached catalog (`vast/vms.csv`), but the actual rental is chosen from
  live offers (`search_offers`), and Vast can charge extra for disk. SkyPilot's spend estimate will not match the Vast
  bill exactly.
- The `datacenter_only` option limits Vast to datacenter hosts (not consumer machines). Consider turning it on for
  reliability.

## Design

```
            ┌──────────────── Krater ────────────────┐
 approve ──►│ provisioner ── /workspaces/create ─────┼──► SkyPilot API server ──► Vast.ai
            │                                        │         │
            │ policy endpoint ◄── RestfulAdminPolicy ┼─────────┘  (every launch)
            │                                        │
            │ reconciler ── cost_report, down/cancel ┼──► SkyPilot API server
            └────────────────────────────────────────┘

 member ── sky CLI / dashboard ──► oauth2-proxy ──(Weave OIDC)──► SkyPilot API server
```

### 0. Member access: one workspace per project, sign-in with Weave

- **Identity:** members sign in to SkyPilot with their Weave account. oauth2-proxy runs as its own container, with Weave
  as the OIDC issuer. The SkyPilot API server delegates authentication to it through the two env vars above. The CLI
  works too: `sky api login -e https://<skypilot-host>` opens a browser to the Weave sign-in.
- **Who may sign in:** oauth2-proxy only lets in Ganymede members. It requests the `groups` scope from Weave and
  requires `ganymede:member`.
- **Isolation:** each approved project gets its own **private** workspace. `allowed_users` is set to the project team's
  emails (the same `email` claim oauth2-proxy passes on). A member on two projects can use both workspaces and picks one
  per launch. Everyone else can't see the workspace at all.
- **Roles:** SkyPilot's default role for new users is `user`. Only Krater's service account (and SkyPilot operators)
  get `admin`, since SkyPilot admins can see and edit every workspace.
- **Offboarding:** when a project is completed or withdrawn, Krater removes the team from `allowed_users` before tearing
  the workspace down. Someone suspended in Weave can't sign in again. Existing SkyPilot sessions last until they expire,
  so keep the oauth2-proxy cookie lifetime short (e.g. 8h).

oauth2-proxy settings (sketch):

```ini
provider = "oidc"
oidc_issuer_url = "https://weave.patchworklabs.org"
client_id = "<weave oauth app uid>"            # a separate confidential Weave app, not Krater's
client_secret = "<secret>"
scope = "openid email profile groups"
oidc_groups_claim = "groups"
allowed_groups = ["ganymede:member"]
email_domains = ["*"]
redirect_url = "https://<skypilot-host>/oauth2/callback"
cookie_expire = "8h"
code_challenge_method = "S256"                 # Weave requires PKCE
```

Depends on Weave's `groups` claim (on Weave branch `Krater-Integration`, not merged yet). Until it lands, drop
`allowed_groups` and rely on private workspaces alone. Anyone with a Weave account could then sign in, but they'd see
nothing.

### 1. Provisioning (on approval)

When a project's proposal is approved, a worker job:

1. Creates a private workspace named `ganymede-<project_id>` with `allowed_users` set to the project team. Other
   clouds are disabled in that workspace, so it can only use Vast.
2. Saves the workspace name on `Project.skypilot_workspace`.
3. Keeps `allowed_users` in step with the team: the submitter plus credited builders, by their Weave email. Members
   sign in with Weave (see section 0); no per-project tokens are handed out.

When the project is completed or withdrawn: tear down its clusters and managed jobs, cut off its access, and keep the
workspace until the cost history has been recorded, then delete it.

Krater calls the API server with an **admin** service-account token, stored as a secret in the portal and worker.

### 2. Admin policy endpoint (every launch)

The SkyPilot server config points at Krater:

```yaml
# SkyPilot API server config
admin_policy: http://portal:8000/internal/skypilot/policy
```

For each request, the endpoint decodes the body with `sky.admin_policy.UserRequest.decode(...)`, reads
`skypilot_config.active_workspace`, and maps it to a project.

**Reject** (HTTP 400, with a message the user will see) if:
- the workspace doesn't belong to a Krater project, or the project isn't `approved` (for example, it's completed,
  withdrawn or still in review);
- the project's latest estimated spend is at or above its ceiling;
- the request asks for something Vast doesn't support, such as multi-node. This is optional but gives friendlier errors.

**Change** the request (return `MutatedUserRequest.encode()`) so that:
- the cluster autodowns after idling (e.g. `idle_minutes: 30`, `down: true`) unless the user asked for something
  stricter;
- `max_hourly_cost` is capped at a per-project limit. The simple version is a global default; later it could be
  something like 5% of the remaining budget;
- the cluster is labelled with the project ID, if resource labels work on Vast. Otherwise the workspace alone is enough
  to attribute spend.

Every rejection writes an `AuditEvent(launch_blocked)` and posts a short note in the project's Slack channel.

**Security:** `RestfulAdminPolicy` sends **no auth header**. The endpoint must only be reachable on the internal Docker
network, never on the public router. Also check that requests come from the `skypilot` container's address.

**Availability:** if the endpoint is down, every launch fails (SkyPilot raises `RestfulPolicyError`). That's the right
failure mode for a budget gate, but the endpoint must be cheap. It reads the latest `SpendSnapshot` from Postgres and
never calls SkyPilot back.

### 3. Spend reconciler (periodic)

Every 5 minutes, a worker job:

1. Calls `cost_report(days=…)` with the admin token and groups `total_cost` by `workspace` → project.
2. Writes a `SpendSnapshot(estimated_spend_cents)` for each active project.
3. At **≥ 80%** of the ceiling, posts a one-time warning in the project channel.
4. At **≥ 100%**, tears down the project workspace's clusters and cancels its managed jobs, then writes
   `AuditEvent(teardown)` and posts in the channel. The policy endpoint already blocks new launches from this point.

Estimates only grow while a cluster is up, so the reconciler errs toward stopping early.

### 4. Final spend

When a project is completed or withdrawn, the reconciler takes a final snapshot after teardown. That figure becomes the
gallery's "compute spent" (shown as an estimate), and the unspent remainder is written to the ledger as a
`BudgetEntry(reclaim)`.

## Things to verify in a spike (before building on them)

1. `cost_report` run with an admin service account returns **every** user's clusters, with `workspace` filled in, and
   includes managed-job clusters. The jobs controller's own cost isn't attributed to any project.
2. The workspace create, update and add-users endpoints work with a service-account token on a plain Docker (non-Helm)
   deployment, and don't need a server restart.
3. **Member access:** end to end on Docker Compose: oauth2-proxy + Weave sign-in, `sky api login` from the CLI, a
   private workspace blocking a non-member, and a member launching on Vast in it. Also check that SkyPilot's user
   identity is the email (so `allowed_users` entries match), and whether allowing users into a workspace needs them to
   have signed in once first.
4. Whether autodown and `max_hourly_cost` changes made by the policy are respected for Vast launches and managed jobs.
5. How far `cost_report` drifts from actual Vast billing on a few real runs. That sets the safety margin (if any) to take
   off the ceiling.
