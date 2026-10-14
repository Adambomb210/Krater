# Compute providers

Krater Compute rents from one provider today: Vast.ai. This page covers three things:

- The seam another provider plugs into (`krater/providers/`), and what is still shaped like Vast.
- How the generic clouds (AWS, GCP) map onto it.
- How the OSPool could, written for the talks with OSG.

Anything not checked against a provider's own system is marked **unverified**.

## The seam (built 2026-10-14)

- **`krater.providers.Capabilities`** says what a provider can do. Krater branches on these flags, never on a
  provider's name:

  | Flag | Means | Without it |
  | --- | --- | --- |
  | `on_demand` | fixed-price machines | on-demand jobs and the `on_demand` escalation are refused |
  | `interruptible` | cheaper machines the provider can take back | interruptible jobs are refused |
  | `bids` | Krater sets the interruptible price and can raise it | limit orders and `raise_bid` are refused |
  | `outbid_pauses` | an interrupted machine pauses with its disk | limit orders refused; an interruption is a lost machine (requeue) |
  | `stop_start` + `ssh` | a machine can stop, keep its disk, resume; members can SSH in | boxes are refused |
  | `self_stop` | the runner can stop its own machine when Krater is gone | the runner's switch is off (`self_stop_after_seconds: -1`) |
  | `charges` | the provider reports what it billed per machine | the hourly reconcile doesn't run; Krater's estimate is the spend |
  | `search_quota` | searches count against a quota the provider reports | (nothing to pace on) |

- **`krater.providers.ComputeProvider`** is the protocol for the calls the scheduler, the reconciler and the price
  history make:
  - search offers;
  - create, show, list (by label prefix), stop, start, change bid and destroy an instance;
  - charges;
  - account SSH keys.

  `krater.vast.VastClient` implements it.
- **Neutral errors** (`krater.providers.errors`). The scheduler, the reconciler and the price history catch only these:
  - `ProviderUnavailableError`: retry later.
  - `ProviderRateLimitedError`.
  - `ProviderSearchQuotaError`.
  - `ProviderRequestFailedError`: refused. Its `may_have_happened` tells the scheduler to settle a create by its label instead of trying again.

  Vast's errors subclass them.
- **The registry.** `KRATER_COMPUTE_MODE` names the provider (`off` for none). `providers.active(settings)` and
  `providers.get_provider(settings)` are how the worker and the portal find it; `Settings.compute_on` replaces the old
  `compute_mode == "vast"` checks.
- **`compute_instances.provider`.** Every rental records its provider. A scheduler pass watches, settles and
  reconciles only its own provider's rows, and the provider's instance id is unique per provider.
- **Job validation** (`krater.services.compute_jobs`) refuses whatever the active provider can't do, with a message
  naming the provider ("Vast.ai has no interruptible machines.").

Vast behaves exactly as before: the full suite passes unchanged apart from the worker tests' patch target.

### Adding a provider

1. **A package of its own** (`krater/<provider>/`), the only code that knows its URLs and wire shapes. Its errors
   subclass `krater.providers.errors`. Its credentials are read only by the worker, from a file setting like
   `KRATER_VAST_API_KEY_FILE`.
2. **A fake** that serves the provider's API to its real client in tests, the way `FakeVast` is an
   `httpx.MockTransport`. Copy the behaviours that cost money to get wrong.
3. **A client implementing `ComputeProvider`.** It needs:
   - instances labelled at create, and listable by label prefix (the lost-create settle and the sweep depend on it);
   - `show_instance` answering `None` once an instance is gone;
   - the instance's state readable as Vast's `is_running` / `intended_status` (see "Still Vast-shaped" below).
4. **Register it** (`providers.register(ProviderInfo(...))`) with honest capabilities. Add its name to
   `Settings.compute_mode`.
5. **Run the scheduler suite against its fake.** Then do a paid round on staging as [README.md](README.md#paid-checks)
   describes before production.

### Still Vast-shaped (to change with the second provider)

These were left alone on purpose: each touches stored data or the runner protocol, and no second provider exists yet
to shape the change.

- **Instance ids.** The column `vast_instance_id` (BigInteger) is unique per provider. Rename it to
  `provider_instance_id` as a string: AWS ids (`i-0abc…`) and HTCondor ids (`1234.0`) aren't integers. The same goes
  for `offer_id` and `machine_id`.
- **The shapes in `krater.vast.types`.** `Offer`, `Instance`, `CreateInstance` and `Charge` carry Vast's fields and
  `from_json`. Move the neutral parts (GPU model and count, GPU memory, prices, disk, reliability, location, label, a
  neutral state) to `krater.providers`, and keep the parsing in each provider's package.
- **The instance's state.** The scheduler reads `is_running` and Vast's `intended_status` / `cur_state` (three places:
  outbid vs. self-stop, a member restarting their own box, resume). Replace these with a neutral phase on `Instance`:
  - provisioning
  - running
  - interrupted (paused)
  - stopped
  - gone
- **Launch** (`krater/compute/launch.py`). It builds Vast's `CreateInstance` (args/SSH modes, `onstart`). The neutral
  part is the bootstrap script and the environment; each provider turns those into its own create (EC2 user data, a GCE
  startup script, an HTCondor submit file).
- **The runner's self-stop.** `StartInfo.vast_instance_id` and `vast_api_url` tell the runner how to stop its machine
  through Vast's API. Make it a descriptor: `{"method": "vast_api", "url", "instance_id"}`, `{"method": "shutdown"}`
  (a cloud VM stops itself with `shutdown -h now`), or nothing.
- **Search quota and price history.** The table is `vast_search_quota`, and `/pricing/history` samples Vast's market.
  The public catalog behind the budget estimator is SkyPilot's Vast CSV.
- **Words members read.** Some templates and reasons say "Vast" ("Vast didn't confirm the rental"). Take these from
  `ProviderInfo.title`.

## Generic clouds: AWS and GCP

Their GPU VMs fit the machine-market model. The differences are in pricing, interruption and billing. These are facts
known from the providers' documentation, **unverified against their APIs here**.

| | Vast | AWS EC2 | GCP Compute Engine |
| --- | --- | --- | --- |
| Offers | a market of hosts, searched | instance types per region/zone (fixed GPU shapes); spot price history API | machine types + attached GPUs per zone |
| Interruptible price | **a bid** Krater sets and raises | spot price set by AWS (an optional max price only caps it) | spot price set by Google |
| Capabilities | everything | `bids=False`, `outbid_pauses=False`* | `bids=False`, `outbid_pauses=False`* |
| When interrupted | pauses with its disk, resumes itself | terminate (default), or stop / hibernate for persistent requests; 2-minute notice in instance metadata | stop or delete (chosen at create); about 30 s notice |
| Stop/start | yes | yes (EBS kept, billed) | yes (disk kept, billed) |
| SSH | Vast's proxy or a direct port | the instance's public IP; Krater installs the member's key | the same |
| Find by label | label | tags (`krater-label`) | labels |
| Self-stop | Vast API with the container key | `shutdown -h now` (stop, if the instance's shutdown behaviour is stop) | `shutdown -h now` |
| Charges | per instance, within the hour | Cost and Usage Report by tag, hours to a day late | billing export to BigQuery by label, hours late |
| Quotas | daily search rows | per-region GPU vCPU quotas (a create fails until raised) | per-region GPU quotas |

\* A spot VM set to *stop* on interruption keeps its disk. A provider could report `outbid_pauses=True` if its client
starts the VM again when capacity returns, but the clouds don't do that themselves, so `False` is the honest start.

Fit:
- **Pricing.** The clouds set the spot price, so a job's target/max becomes "rent if the current spot price fits"
  (`bids=False`), and limit orders are Vast's only.
- **Interruption.** An interruption is a lost machine. The job requeues and resumes from its last checkpoint in Krater's
  storage, which the scheduler already does for a machine that disappears.
- **Charges.** Reconcile stays useful but lags by hours; the 80%/100% alerts keep running on Krater's estimate.
- **Credentials.** Each provider's credentials (an IAM role or access key, a service-account key) belong in the worker
  only, like the Vast key.

## The OSPool: integration report

### What it is

The OSPool (OSG, run with the University of Wisconsin–Madison's CHTC) is a shared HTCondor pool of donated, mostly
opportunistic capacity, CPUs and GPUs. Work goes in as HTCondor jobs from an Access Point (AP); nothing is rented and
there is no price. Facts from OSG's documentation (2026-10-14):

- **Free.** OSG's own material says resources are free of charge, with no allocation needed. That comes from a 2023
  slide deck, so confirm it.
- **Who may use it** (the [AP acceptable use policy](https://osg-htc.org/docs/submit/ap-ospool-aup/)). The work must
  be research or education at a U.S. academic, government or non-profit institution, or by external collaborators of
  one doing such work. **The AP operator is responsible for its users' behaviour.** Accounting is per user and per
  project. The policy says nothing about platforms submitting for many users, or about minors.
- **Jobs are interrupted.** They run on backfill with no runtime guarantee, and HTCondor re-runs an interrupted job
  ([job planning](https://osg-htc.org/osg-portal-documentation/htc_workloads/workload_planning/roadmap/)).
  - **Runtime.** 10 hours per execution is suggested and 20 hours is the most. Longer work must checkpoint.
  - **HTCondor's checkpointing.** A job exits with code 85 (`checkpoint_exit_code`), and HTCondor saves its
    `transfer_checkpoint_files`, then requeues it
    ([checkpointing](https://osg-htc.org/osg-portal-documentation/htc_workloads/submitting_workloads/checkpointing-on-OSPool/)).
- **GPUs.** Jobs ask with `request_gpus` and filter on:
  - `gpus_minimum_capability` / `gpus_maximum_capability`: compute capability, not CUDA version;
  - `gpus_minimum_memory`: in MB.

  Listed types include V100, 2080 Ti, A100 (40/80 GB), A10, RTX 3090 and RTX 4090
  ([GPU jobs](https://osg-htc.org/osg-portal-documentation/htc_workloads/specific_resource/gpu-jobs/)). OSG gives no
  counts or wait times. Some contributors add GPUs only when there is demand.
- **Containers.** Docker and Apptainer images both work. The job is the command run in the container, so the image's
  entrypoint is ignored, as Krater's launch already assumes. Large images go through OSDF.
- **Running an AP** ([install an OSPool AP](https://osg-htc.org/docs/submit/install-ospool-ap/)):
  - a planning consultation with OSG first;
  - a host with 6 cores, 12 GB of RAM and 1 TB of disk, a public IP with forward and reverse DNS, and inbound TCP 9618;
  - registration in OSG Topology, and a COManage-approved admin contact;
  - an OSPool token;
  - the `ospool-ap` package (HTCondor plus Gratia accounting);
  - every job carries a registered `+ProjectName`.
- **Submitting from code.** HTCondor's Python bindings (`htcondor2`) submit, query and remove jobs on a schedd, and
  authenticate remotely with IDTOKENS
  ([API](https://htcondor.readthedocs.io/en/main/apis/python-bindings/api/version2/htcondor2/index.html)).

**Unverified, and the first questions for OSG:**
- whether jobs on OSPool execution points may open outbound HTTPS connections to an arbitrary host (Krater's runner
  needs to reach Krater and its storage throughout the job);
- whether a platform may submit for its members under one AP, and how members must be identified;
- whether minors may be among them (Ganymede has members under 18: see SPEC "Guardian verification");
- whether Patchwork Labs qualifies as an eligible non-profit, or needs an academic sponsor;
- current GPU availability for 1-GPU jobs of a few hours.

### How it maps onto Krater

The OSPool is a queue, not a market, but it fits the existing protocol closely enough to need no new scheduler.

| Krater | OSPool |
| --- | --- |
| `search_offers` | One "offer" per GPU class the pool has (by capability and memory, from `condor_status -const 'GPUs > 0'`), at $0. Searching per job is unnecessary; cache the classes for an hour. |
| `create_instance` | Submit one HTCondor job: `container_image` = the member's image, `request_gpus`, `gpus_minimum_capability` / `gpus_minimum_memory` from the job, `request_disk`, `+ProjectName`, `JobBatchName` = Krater's label, the bootstrap as the executable, Krater's runner environment, `allowed_execute_duration` = the job's max runtime. |
| `show_instance` / `list_instances` | `condor_q` by `JobBatchName`; idle → provisioning, running → running, held → stopped, gone from the queue → gone. |
| `destroy_instance` | `condor_rm` |
| Interruption | HTCondor re-runs the job by itself, on another slot. The runner starts again, `/start` says it resumes, and it restores the last checkpoint from Krater's storage, as on a moved Vast job. |
| Capabilities | `interruptible=True` (every job can be evicted), and everything else `False`: no on demand, no bids, no outbid pauses, no stop/start or SSH (so no boxes), no self-stop, no charges, no search quota. |
| Krater down | No self-stop is needed. Nothing bills, and `allowed_execute_duration` ends abandoned work. |
| Spend | $0. |

What has to be decided or built beyond the protocol:

1. **Budgets.** At $0, a project's budget never runs out, so it would not ration a free, shared pool.
   - **Option:** charge a shadow price (for example, a GPU-hour rate per class) into Krater's spend, so the existing
     ceilings and alerts work.
   - **Option:** give projects a separate GPU-hours allowance.

   Either way it is a policy decision for Patchwork.
2. **Choosing the provider per job.** With Vast and the OSPool both live, `KRATER_COMPUTE_MODE` naming a single
   provider is not enough.
   - **Plan:** jobs gain a `provider` (members choose "free, may wait and be interrupted" vs. "paid, starts now").
   - The scheduler runs one pass per provider.
   - Each provider's credentials stay in the worker.
3. **Runtime limit.** The 20-hour per-execution limit applies to members' jobs, and a job that doesn't checkpoint
   restarts from zero after each eviction.
   - Krater's checkpoint directory and resume flag already exist.
   - **Option:** the runner could also translate a member's own checkpoint signal into exit code 85, so HTCondor
     requeues the job without counting it as a failure.
4. **Submitting.**
   - **Option A (likely):** Patchwork runs its own OSPool AP, with OSG's consultation. Krater's worker talks to its
     schedd with an IDTOKEN through `htcondor2`.
   - **Option B:** a service account on an OSG-hosted AP. Check whether the policy allows this for a multi-user
     platform.
5. **Identity and accounting.**
   - OSG accounts per user per project; Krater can map each Ganymede project to a registered OSG `ProjectName`.
   - Whether members need OSG accounts of their own is the second question above.
6. **Images.**
   - Members' Docker images are pulled by Apptainer on the execution point.
   - Big images should go through OSDF. Image size limits and pull rates are **unverified**.
   - Krater's bootstrap needs a POSIX `sh` in the image, as on Vast.

### Suggested order

1. Ask OSG the unverified questions above, outbound networking and eligibility first: a no to either ends this design.
2. Change "Still Vast-shaped": string instance ids, the neutral instance phase, the self-stop descriptor, and a
   provider per job.
3. Build `krater/ospool/`: an `htcondor2` client, and a fake schedd serving the same calls.
4. Test on a test AP with CPU-only jobs and the real runner, before any GPU work.
