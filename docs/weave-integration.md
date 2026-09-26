# Weave integration

Weave ([patchworklabsorg/weave](https://github.com/patchworklabsorg/weave)) is Patchwork Labs' identity provider: a
Rails app with Doorkeeper and `doorkeeper-openid_connect`, running at `https://weave.patchworklabs.org`. Krater uses it
for sign-in and for roles. All Weave access goes through one adapter, `WeaveClient`.

## What Weave provides today

Checked against Weave `main` on 2026-09-26.

| Capability | Status | Where |
| --- | --- | --- |
| OIDC Authorization Code + PKCE | ✅ PKCE is **required** (`force_pkce`) | `config/initializers/doorkeeper.rb` |
| Discovery, JWKS, RS256 id_tokens | ✅ `/.well-known/openid-configuration` | `config/routes.rb` (`use_doorkeeper_openid_connect`) |
| UserInfo | ✅ `/oauth/userinfo`; accepts `openid` or `profile` tokens | `app/controllers/oauth/userinfo_controller.rb` |
| `sub` | ✅ The user's `p_id`, e.g. `PWL5A1B2C3D4`. Stable and uppercase | `doorkeeper_openid_connect.rb` |
| Claims | `name`, `given_name`, `family_name`, `preferred_username`, `updated_at` (profile); `email`, `email_verified` (email); `phone_number` (phone); `admin` (admin) | `doorkeeper_openid_connect.rb` |
| Access token lifetime | 2 hours, plus refresh tokens | `doorkeeper.rb` |
| Service API with API-key auth | ⚠️ The auth plumbing exists (`X-Api-Key`, usage logging), but the only endpoint is `GET /api/v1/health` | `app/controllers/api/v1/base_controller.rb` |
| Outbound webhooks (`user.updated`, …) | ❌ Event types are defined, but **nothing sends them** | `app/models/service/webhook.rb` |
| Reviewer / group / Ganymede roles | ❌ Weave roles are `user/admin/superadmin/owner`, plus `is_staff` and `is_board` flags | `app/models/user.rb` |
| Slack user ID | ⚠️ Stored as `users.slack_id`, but not exposed to other apps | `app/models/user.rb` |

Heads-up: `docs/OAUTH.md` in Weave documents `GET /api/v1/users/me` and `POST /api/v1/auth/authenticate`. **Neither
exists.** Build against `config/routes.rb`, not that doc.

## Required Weave changes

Krater v1 depends on these. Each should be its own Weave issue or PR.

### 1. `groups` claim (new `groups` scope)

A list of strings in the id_token and UserInfo, e.g.:

```json
{ "groups": ["ganymede:member", "ganymede:reviewer"] }
```

- `ganymede:member`: allowed to use Krater at all.
- `ganymede:reviewer`: allowed to review.
- `ganymede:admin`: Krater admin. This is separate from Weave's `admin` claim, which means Weave operations admin and
  must not be reused.
- Later: `ganymede:reviewer:<tier>` for reviewer tiers by budget size.

Weave needs somewhere to store and manage these, e.g. a `user_groups` table plus a UI for admins to edit it. It's better
to make this generic than to add another boolean like `is_board`, since tiers are already planned.

### 2. `slack_id` claim

Weave already stores `slack_id`. Exposing it (under `profile`, or a new `slack` scope) lets Krater match a Slack button
click to a user without email lookups. Email lookups are unreliable now that Weave users can have several addresses.

### 3. Directory endpoints (service-key auth)

Checking claims at sign-in isn't enough. Krater has to:

- invite **every** reviewer to each new project channel, including ones who have never signed in to Krater;
- check a Slack button click, which arrives with only a Slack user ID;
- notice when someone loses reviewer status, without waiting for their next sign-in.

Proposed, under the existing `Api::V1::BaseController` auth:

```
GET /api/v1/users?group=ganymede:reviewer        → [{ sub, name, email, slack_id, groups, status }]
GET /api/v1/users/by_slack_id/:slack_id          → { sub, name, email, slack_id, groups, status }
GET /api/v1/users/:sub                            → { sub, name, email, slack_id, groups, status }
```

Responses should use a serializer (per Weave's conventions) and never include the database `id`. `status` lets Krater
refuse suspended or deactivated users.

### 4. Slack membership in claims

Tracked in [weave#118](https://github.com/patchworklabsorg/weave/issues/118). Until then Krater checks Slack itself (see
[SPEC.md → Roles & authentication](SPEC.md#roles--authentication)).

### Related Weave bug

Weave's OAuth sign-in check (`resource_owner_authenticator`) only verifies that the email is confirmed. It doesn't check
whether the account is locked or suspended, or whether the session is still live. `User#lock!` also doesn't revoke
Doorkeeper tokens. As a result, a locked or suspended user can still sign in to Krater, and their refresh tokens keep
working. Until that's fixed, Krater checks `status` through the directory API before any review decision or budget
action.

## Krater-side contract

### Registration

Register Krater in Weave at `/admin/oauth_applications`:

- **Confidential client.**
- Redirect URI: `https://<krater-host>/auth/callback`, plus `http://localhost:<port>/auth/callback` for development.
- Scopes: `openid profile email groups`. Add `slack` if `slack_id` gets its own scope.
- Issue a separate **service key** for the directory API.

### `WeaveClient` interface

```python
class WeaveClient(Protocol):
    # OIDC
    def authorization_url(self, state: str, code_verifier: str) -> str: ...
    def exchange_code(self, code: str, code_verifier: str) -> WeaveIdentity: ...   # verifies id_token via JWKS

    # Directory (service key)
    def get_user(self, sub: str) -> WeaveUser | None: ...
    def get_user_by_slack_id(self, slack_id: str) -> WeaveUser | None: ...
    def list_users_in_group(self, group: str) -> list[WeaveUser]: ...
```

```python
@dataclass(frozen=True)
class WeaveUser:
    sub: str               # PWL…; Krater's external ID
    name: str
    email: str
    slack_id: str | None
    groups: frozenset[str]
    active: bool           # status == "active"
```

Rules:

- **Authorization uses fresh data.** Review decisions, admin actions and budget changes call `get_user` or
  `get_user_by_slack_id` at the time of the action. `User.groups_cached` is for display only.
- **Sign-in:** reject users without `ganymede:member`. Store `weave_sub`, the display name, email, `slack_id` and
  `groups_cached`.
- **Short cache is fine.** Directory responses can be cached for about 60 seconds to absorb bursts of Slack clicks.
- **Until the Weave changes land,** `WeaveClient` can be backed by a stub: a config file mapping `sub` or email to
  groups, plus a Slack `users.lookupByEmail` lookup. That lets Krater development start now without leaking stub logic
  outside the adapter.
