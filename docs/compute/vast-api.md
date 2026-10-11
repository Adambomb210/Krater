# Vast.ai API: what Krater Compute relies on

Krater Compute rents Vast machines itself (design: "Krater Compute v1: design", a Claude Doc shared by Adam). This
page lists the API facts `krater/vast/` is built on. Most were measured on the live API in the 2026-10-11 spike
(staging test key, one interruptible RTX 3060 for 16 minutes, $0.012). `FakeVast` (`krater/vast/fake.py`) copies
each behavior that would cost money to get wrong, and `tests/vast/test_client.py` checks the client against it.

## Calls

| What | Call | Notes |
| --- | --- | --- |
| Search offers | `POST /api/v0/bundles/` | At most **512** offers per search, whatever `limit` says. Sorted server-side by `order` (`[["dph_total","asc"]]`) |
| Create | `PUT /api/v0/asks/{offer}/` | `price` makes it an interruptible bid. The response has `new_contract` (the id) and `instance_api_key` |
| Show one | `GET /api/v0/instances/{id}/?owner=me` | `{"instances": null}` for an unknown id |
| List | `GET /api/v1/instances/` | 25 per page; `next_token` → `after_token`. The v0 list answers 410 |
| Change bid | `PUT /api/v0/instances/bid_price/{id}/` | `{"client_id": "me", "price": 0.064}` |
| Stop / start | `PUT /api/v0/instances/{id}/` | `{"state": "stopped"}` / `{"state": "running"}` |
| Destroy | `DELETE /api/v0/instances/{id}/` | 404 `no_such_instance` once gone |
| Logs | `PUT /api/v0/instances/request_logs/{id}/` | Returns `result_url` on another host; fetch it without the Authorization header |
| Charges | `GET /api/v0/charges/` | `select_filters` with a `day` range is required; up to 500 per page; `next_token` → `after_token` |

## Rules

- **Every path ends with a slash.** Without it Vast answers **308 and does nothing**, DELETE included. The client never
  follows redirects and treats any 3xx as an error.
- **Prices.** `dph_total` is the GPU price (`dph_base`) plus disk at the search's `allocated_storage` (about 8.1 GB
  when left out). `min_bid` is the GPU alone. Krater always searches with the job's real disk size and compares offers
  by GPU price (the bid for interruptible, `dph_base` on demand) plus `storage_cost` (per GB-month) × disk / 730.
  Bids came out 17–64% cheaper than on-demand, like for like.
- **Storage cost varies a lot by host**: $0.13 to $0.87 per GB-month for the same GPU.
- **Units.** Vast sends dollars as JSON floats. The client parses them with `Decimal(str(x))`. Krater stores hourly
  rates as integer micro-dollars (`*_microusd`), because bids need sub-cent precision; budgets and spend stay in cents.
- **A container whose command exits is restarted** by Vast, and keeps billing. The runner must never exit.
- **Billing.** The GPU is billed only while the instance is `running`; during the image pull only disk is billed
  (spike: 0.194 h of GPU against 0.252 h of disk). Charges appear within a minute and are rounded to $0.001; a row can
  span several days, so reconcile on an instance's cumulative total.
- **`gpu_util` is slow**: it rose 50–105 s after a GPU burst and flickered when idle. Use the runner's own `nvidia-smi`
  sample; Vast's figure is the fallback.
- **The instance's own key.** Vast injects `CONTAINER_API_KEY` into the container (it can start, stop or destroy that
  one instance) and returns it from create as `instance_api_key`. Never log a create response. Budget and runtime stops
  destroy, never just stop, since a member could start a stopped instance again.
- **One account can't outbid itself** ("GPU conflict: requesting already rented gpus"), so a real pause is still
  untested.
- **Open:** the bill showed $0.059/h after the bid was raised to $0.064. Is a bid charged at its own price or at a
  clearing price? And what does a paused instance's `actual_status` say?

## Not checked live yet

- `gpu_ram` in a search filter: sent in MB, as Vast's CLI does (it multiplies a `gpu_ram>=24` query by 1000).
- Rate limits: Vast answers 429 with a `detail`; how many calls a minute it allows isn't documented.
