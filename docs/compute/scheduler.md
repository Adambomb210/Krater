# Krater Compute scheduler

`krater/compute/scheduler.py` is the only code that rents or destroys Vast machines. The worker runs one pass a
minute (`compute_schedule`), under a procrastinate lock so passes never overlap, and only with
`KRATER_COMPUTE_MODE=vast` and a usable Vast client. In production `vast` requires `KRATER_VAST_MODE=live`.

## A pass

1. **List** the account's instances that carry Krater's label prefix (`KRATER_COMPUTE_LABEL_PREFIX`, default
   `krater-job-`).
2. **Settle lost creates.** Each rental's `compute_instances` row is committed, with a unique label
   (`<prefix><job id>-<attempt>`), *before* Vast is called. If the create times out, the next pass finds the
   machine by its label. Not found within 5 minutes, the row is marked `create_failed` and the job requeued.
3. **Sweep.** An instance with Krater's prefix and no live row is destroyed: a create that showed up late, a
   destroy that didn't take, or a machine a member restarted with its own container key.
4. **Watch** every live instance:
   - pulling → `running` once Vast says so (the GPU bills from then);
   - running → `paused` when it stops without Krater stopping it (outbid);
   - paused past the pause grace → the bid rises by the bid margin each pass, up to the job's max price;
   - paused past "move after" → destroyed, and the job requeued (attempt + 1, `KRATER_RESUMED=1`);
   - running past the job's max runtime → job failed, machine destroyed;
   - never running within 30 minutes → job failed (check the image), machine destroyed;
   - gone from Vast unexpectedly → job requeued;
   - job finished (cancelled, failed, succeeded) → machine destroyed.
5. **Start** queued jobs, oldest first. For each, search offers for its shape (GPU models or minimum VRAM, GPU
   count, disk, the reliability floor), price every offer at full hourly cost (the bid, min bid plus the margin, or
   `dph_base` on demand, plus disk), and take the cheapest at or under the target (several GPU models: best
   performance per dollar). After `max_wait_minutes` the job does what the member chose: keep waiting, accept up to
   its max price, switch to on demand up to its max price, or cancel.

Every price limit is also capped by the project's hourly cap. A rental needs budget for at least one hour, under
the project's row lock; it then holds the cost of its max runtime (or whatever is left), so two jobs can't spend
the same dollars.

Every destroy is confirmed with `show_instance(...) is None`. Until it is, the row keeps `destroy_requested_at` and
the next pass tries again.

## States

Jobs: `queued` → `starting` → `running` ⇄ `paused`, ending `succeeded`, `failed` or `cancelled`. Instances:
`creating` → `provisioning` → `running` ⇄ `paused` → `destroyed`, or `create_failed`. `compute_events` logs every
step for the member and admins.

## Not yet

- The runner: until it exists, the container runs the member's command in a shell that then waits forever (Vast
  restarts a container whose main process exits, and keeps billing). Jobs therefore don't report success yet.
- The spend loop (live estimate into `SpendSnapshot`, the 100% hard stop) and hourly reconciliation with
  `/api/v0/charges`.
- Boxes (SSH, Jupyter, idle stop), the member UI and the CLI.
