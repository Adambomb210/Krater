# Weave integration

Weave ([patchworklabsorg/weave](https://github.com/patchworklabsorg/weave)) is Patchwork Labs' identity provider: a
Rails app with Doorkeeper and `doorkeeper-openid_connect`, running at `https://weave.patchworklabs.org`. **Krater uses
it for sign-in only**, through one adapter, `WeaveClient` (`krater/weave/`).

By maintainer decision (2026-09-28), Krater works against Weave as it is on Weave's `main` branch: standard OIDC and
nothing else. Roles, the "disabled" switch and Slack links all live in Krater's own database; see
[SPEC.md, Roles & authentication](SPEC.md#roles--authentication).

## What Krater needs from Weave

Only what Weave's `main` already provides:

| Capability | Where in Weave |
| --- | --- |
| OIDC Authorization Code + PKCE (PKCE is **required**, `force_pkce`) | `config/initializers/doorkeeper.rb` |
| Discovery (`/.well-known/openid-configuration`), JWKS, RS256 id_tokens | `config/routes.rb` (`use_doorkeeper_openid_connect`) |
| `sub`: the user's `p_id`, e.g. `PWL5A1B2C3D4`. Stable and uppercase | `config/initializers/doorkeeper_openid_connect.rb` |
| `name` (`profile` scope), `email` and `email_verified` (`email` scope) | `config/initializers/doorkeeper_openid_connect.rb` |

Krater requests the scopes `openid profile email` and reads four claims: `sub`, `name`, `email`, `email_verified`.
Any other claim in the id_token is ignored.

Heads-up: `docs/OAUTH.md` in Weave documents `GET /api/v1/users/me` and `POST /api/v1/auth/authenticate`. **Neither
exists**, and Krater doesn't need them. Build against `config/routes.rb`, not that doc.

## No longer needed

Earlier versions of Krater depended on Weave work on a `Krater-Integration` branch (handoff patch `0001`, and the
uncommitted `slack_membership` work for [weave#118](https://github.com/patchworklabsorg/weave/issues/118)). **None of
it is required any more:**

| Was | Replaced by, in Krater |
| --- | --- |
| A `groups` claim (`ganymede:member` / `reviewer` / `admin`) | The `user_roles` table, managed at `/admin/users` |
| A `slack_id` claim | `users.slack_user_id`, found by Slack `users.lookupByEmail` with the verified email, or set by an admin |
| The `slack_membership` claim / directory field | Slack `users.info` (guest and deactivated flags), asked directly |
| The directory API (`/api/v1/users`, `by_slack_id`, `:sub`) and its service key | Queries on Krater's own `users` / `user_roles` |
| Weave's `status` / `active` flag, re-checked before every action | `users.disabled_at`, Krater's own switch |
| `KRATER_WEAVE_API_BASE_URL`, `KRATER_WEAVE_SERVICE_KEY` | Removed |

**Still recommended for Weave's own sake:** the security fixes described in the handoff's
`krater-handoff/weave-patches/WEAVE-BUG-REPORT.md`. Patches `0002`/`0003` fix two browser CSP bugs that break OAuth
sign-in for returning users in Chrome and Safari, for *any* external client, Krater included. The other fixes close an
account takeover through unsigned Slack events, admin-panel takeover of owner accounts, locked users still signing in
to OAuth apps, and `/admin` engines reachable after sign-out. Two of those matter to Krater directly:

- Until the lockout fix is merged, a user locked in Weave can still sign in to Krater. Krater's own `disabled_at`
  switch is the way to shut someone out of Krater.
- Until the Slack-events fix is merged, an attacker can take over a Weave account knowing only its email, and so sign
  in to Krater as that person. Krater trusts `email_verified` for pending grants, bootstrap admins given by email, and
  Slack email matching, so prefer Weave subs in `KRATER_BOOTSTRAP_ADMINS` until then.

## Krater-side contract

### Registration

Register Krater in Weave at `/admin/oauth_applications`:

- **Confidential client.**
- Redirect URI: `https://<krater-host>/auth/callback`, plus `http://localhost:<port>/auth/callback` for development.
- Scopes: `openid profile email`.

No service key is needed.

### `WeaveClient` interface

```python
class WeaveClient(Protocol):
    def authorization_url(self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str) -> str: ...
    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> WeaveIdentity: ...
```

```python
@dataclass(frozen=True)
class WeaveIdentity:
    sub: str  # PWL...; Krater's external ID
    name: str
    email: str
    email_verified: bool  # only a JSON `true` counts
```

`exchange_code` verifies the id_token against Weave's JWKS (signature, `iss`, `aud`, `exp`, `nonce`).

### Sign-in rules (`krater.services.users.sign_in`)

1. Upsert the `User` by `weave_sub`: display name, email, and whether Weave said the email is verified.
2. A user an admin has disabled is refused (403 page), and nothing else happens.
3. Bootstrap admins (`KRATER_BOOTSTRAP_ADMINS`: Weave subs, or emails that only match when verified) get
   `ganymede:admin` and `ganymede:member` if missing.
4. Pending grants for the user's email are applied, only if the email is verified, then deleted.
5. Without `ganymede:member`, the user is refused with the "not a member" page. Their row is kept, so an admin can find
   them at `/admin/users` and grant the role.

Every grant, revoke and application writes an `AuditEvent`. Authorization after sign-in never calls Weave: see
`krater.services.roles.authorize`.

### Stub mode

`KRATER_WEAVE_MODE=stub` (refused in production) replaces Weave with `krater/weave/stub_users.json`. Each fixture user
has the standard identity fields plus `groups` and an optional `slack_id`, which a real Weave doesn't carry: stub
sign-in seeds them into `user_roles` and `users.slack_user_id` so dev flows work without an admin granting roles
first. None of that leaks outside `krater.weave` and the stub branch of the sign-in route.
