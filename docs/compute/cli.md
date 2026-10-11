# The `krater` command

Krater Compute from a terminal: submit jobs and boxes, watch them, SSH into a box, manage a project's secrets. It's a
small separate package in `cli/` (Python 3.10+, httpx only) that talks to Krater's API (`/api/v1/`,
`krater.web.routers.api`) and signs in with Weave.

## For members

Install it (once it's published, or from a checkout):

```bash
uv tool install "git+https://github.com/patchworklabsorg/krater#subdirectory=cli"
```

Sign in, then work:

```bash
krater login --url https://krater.patchworklabs.org
krater projects
krater submit <project> --name train --image ghcr.io/me/train:v3 --command "python train.py" --gpu "RTX 4090" --target 0.30 --max 0.45 --max-runtime 240
krater jobs <project>
krater show <job>
krater cancel <job>
krater submit <project> --name notebook --image ubuntu:24.04 --box --gpu "RTX 3060" --target 0.07 --max 0.10
krater ssh <job> -- -L 8888:localhost:8888
krater secrets set <project> HF_TOKEN
krater prices
```

`krater login` prints a Weave link (and opens it). Sign in there, and Weave shows a code: paste it into the terminal.
That works over SSH too. The CLI keeps its sign-in in `credentials.json` under `%APPDATA%\krater` on Windows or
`~/.config/krater` elsewhere, readable by you only, and refreshes it as needed; `krater logout` deletes it.
`krater secrets set` reads the value from standard input or asks for it without echoing, so it never lands in your
shell history. Every command takes `--json`.

## How sign-in works

The CLI is a public OAuth client of Weave (it can't keep a secret), so it uses the authorization code flow with PKCE
(S256), which Weave requires of public clients:

1. `GET /api/v1/cli-config` tells the CLI Weave's issuer, the CLI app's client id, the scopes (`openid profile`) and
   the redirect: Doorkeeper's out-of-band `urn:ietf:wg:oauth:2.0:oob`, for which Weave shows the code.
2. The member signs in at Weave's authorize page; the CLI swaps the code (plus its PKCE verifier) for an access token
   (two hours) and a refresh token.
3. Each API request carries `Authorization: Bearer <access token>`. Krater checks it with Weave using the token
   itself: `GET /oauth/token/info` must name the CLI app (`KRATER_WEAVE_CLI_CLIENT_ID`), and `GET /oauth/userinfo`
   gives the member's `sub` and applies Weave's app-access rules. (Doorkeeper doesn't let a token introspect itself,
   so introspection isn't used.) The answer is cached for a minute. Then Krater authorizes the member through Weave's
   directory exactly as for a page: roles are re-checked live for anything that changes state.
4. A token for any other Weave app is refused, and the API never accepts the browser's session cookie, so it needs
   no CSRF protection and a web page can't call it on a member's behalf.

A member who has never signed in to Krater in a browser is asked to do that once: Krater creates its user record
from Weave's id_token at that first sign-in.

## Setting it up (admins)

In Weave, create an OAuth application **Krater CLI**:

- **Confidential:** no (a public client; the CLI holds no secret).
- **Redirect URI:** `urn:ietf:wg:oauth:2.0:oob`.
- **Scopes:** `openid profile`.
- **Who may use it:** the same app-access restriction as Krater's own app, so only Ganymede members can sign in.

Then set `KRATER_WEAVE_CLI_CLIENT_ID` to its client id in Krater's environment. The API exists only with that set and
`KRATER_COMPUTE_MODE=vast`.

**A nicer sign-in, later:** with a loopback redirect the browser hands the code straight back to the CLI. Doorkeeper
already matches `http://127.0.0.1/callback` on any port, but Weave in production refuses plain-http redirect URIs
(`force_ssl_in_redirect_uri`). A one-line Weave change allows loopback addresses:

```ruby
force_ssl_in_redirect_uri { |uri| !IPAddr.new(uri.host).loopback? rescue true }
```

After that, add `http://127.0.0.1/callback` to the app's redirect URIs and set `KRATER_WEAVE_CLI_LOOPBACK=true`.

## Developing it

```bash
cd cli
uv run --group dev pytest
```

The tests run the CLI against a fake Krater and Weave (`httpx.MockTransport`). The API's own tests are in
`tests/web/test_api.py`, and the Weave token check's in `tests/weave/test_live_client.py`.
