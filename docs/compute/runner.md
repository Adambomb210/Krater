# Krater Compute runner

The runner (`runner/`, Go, standard library only) is the only Krater code on a rented machine. The Dockerfile's
`runner` stage builds it as a static linux/amd64 binary, and the portal serves it at `/runner/v1/krater-runner`.

## How a job starts

Krater creates the instance in Vast's `args` mode with `onstart: "sh"` (Vast's entrypoint override) and a bootstrap
script as `args` (`krater/compute/launch.py`). The bootstrap:

1. downloads the runner from `$KRATER_RUNNER_URL` with curl, wget or Python, whichever the image has;
2. checks its sha256 (when the image has `sha256sum`) and refuses to run a mismatch;
3. runs it;
4. waits forever afterwards. Vast restarts a container whose main process exits and keeps billing, so only Krater
   ends a machine, by destroying it.

Checked locally on 2026-10-11 with alpine (busybox wget), python:3.12-slim (Python only) and debian:bookworm-slim
(none: it prints why and waits, and Krater's runner start deadline fails the job).

The runner then:

1. calls `POST /runner/v1/start` with its token and gets the command and whether this is a resume;
2. restores the job's checkpoints into `$KRATER_CHECKPOINT_DIR` (default `/krater/checkpoints`);
3. runs the command with `sh -c`;
4. sends a heartbeat every minute, with the mean GPU use from `nvidia-smi` sampled every 15 s, and stops the
   command (SIGTERM, then SIGKILL after 30 s) when Krater answers `stop`;
5. uploads changed checkpoints every 10 minutes, and checkpoints and outputs (`$KRATER_OUTPUT_DIR`, default
   `/krater/outputs`) when the command exits;
6. saves the command's stdout and stderr, with its own lines, to the job's log (`logs/attempt-<n>.log`, up to
   16 MB; the rest still reaches Vast's container log) and uploads it every minute and at exit;
7. reports the exit code. The scheduler destroys the machine on its next pass.

The job's team reads the log, outputs and checkpoints on the job page (Output and Files; downloads are redirects to
URLs signed for 15 minutes) or with `krater logs` and `krater download`. Reviewers see the job but not its files.

## Boxes

A box launches in Vast's SSH mode (`runtype: ssh_direct`): Vast's own init runs sshd and is the main process, and the
bootstrap runs as its startup script, starting the runner in the background. The runner then:

1. writes the box owner's SSH keys (from `/start`) as the whole of `~/.ssh/authorized_keys` (0600, `.ssh` 0700);
2. restores `/workspace` (`$KRATER_WORKSPACE_DIR`) from the job's stored workspace;
3. sends a heartbeat every minute with GPU use and the number of established connections to port 22, read from
   `/proc/net/tcp` and `tcp6`;
4. saves `/workspace` every 10 minutes, and when a heartbeat reply says `sync` (Krater is about to stop the box),
   then confirms with `POST /runner/v1/synced`.

Checked on staging (2026-10-11), plain `ubuntu:24.04` in SSH mode: Vast installs and runs sshd itself, runs the
startup script, and the runner starts within a minute of the rental. `authorized_keys` held only the member's key
(the account had no SSH keys, and Vast's `SSH_PUBLIC_KEY` was empty). 22/tcp is mapped to a public port
(`VAST_TCP_PORT_22`), which is what the job page shows; Vast's own SSH proxy (`sshN.vast.ai`) works too. Krater
refuses to start boxes while the Vast account has any SSH keys, since Vast may install them in every instance.

What a member can read inside their box: the container's environment (`/proc/1/environ`) has `KRATER_JOB_TOKEN`
(this instance's runner token: it can only report for this box), Vast's `CONTAINER_API_KEY` (it can stop or destroy
this one instance; Krater treats that like any unexpected stop) and a `JUPYTER_TOKEN` Vast generates. Boxes are
SSH-only in v1 (Adam, 2026-10-11): members run Jupyter themselves through an SSH tunnel.

## What a member's job sees

| Variable | Meaning |
| --- | --- |
| `KRATER_RESUMED` | `1` when this is a resume (a later attempt, or a restart after a pause): load the latest checkpoint |
| `KRATER_CHECKPOINT_DIR` | Save checkpoints here. Restored before the command starts |
| `KRATER_OUTPUT_DIR` | Put results here. Uploaded when the command exits |
| `KRATER_JOB_ID`, `KRATER_ATTEMPT` | For logs |
| the project's secrets | Such as `HF_TOKEN`, set on the project's Compute page |

Write a checkpoint under a temporary name ending in `.tmp` or `.partial` and rename it when it's complete: the
runner skips those names, so it never uploads a half-written file. The image needs a POSIX `sh` and one of curl, wget
or Python; its own ENTRYPOINT is not run, so put any setup in the command. An image whose `USER` isn't root can't
create `/krater/...`, so the runner uses `/tmp/krater/checkpoints` and `/tmp/krater/outputs` instead (the variables
say which). If it can't make its directories at all it reports exit code 125.

## Project secrets

A project's team sets secrets on its Compute page (`krater.services.project_secrets`): write-only, encrypted with
the keys in `KRATER_PROJECT_SECRETS_KEY_FILE` (Fernet, one per line, newest first), which only the portal reads. The
runner gets them in its `start` reply and puts them in the command's environment; for a box it writes
`/etc/profile.d/krater-secrets.sh` (mode 0600, outside `/workspace`, so never synced), which login shells read. It
logs how many, never a value. Names are capital letters, digits and underscores; `KRATER_`, `VAST_`, `CONTAINER_`,
`LD_`, `PYTHON*`, `PATH`, `HOME` and the like are refused.

What this protects and what it doesn't: values never go into Vast's create request, so they don't sit in Vast's
instance records. But the runner fetches them with `KRATER_JOB_TOKEN`, which is in that request's environment, so
anyone holding the organization's Vast key could read the token while the machine runs and fetch them too. And the
machine's host can read anything on the machine. Members should use tokens they can scope and revoke.

## Security

The runner holds one secret, `KRATER_JOB_TOKEN`. It works for this instance only and only while it is live (Krater
stores its sha256). Storage is reached through presigned URLs for this job's own keys
(`projects/<project>/jobs/<job>/checkpoints|outputs/`), single PUTs up to 64 MB and multipart parts above that, so no
request body passes Cloudflare's cap in front of the storage host. Each URL is signed for its exact size, so a
machine can't upload more than it asked for, and Krater refuses URLs past the project's storage quota (an admin
setting, 50 GB by default; replacing a file counts only the difference). Checked against SeaweedFS 4.47 on
2026-10-11: a body of the signed size gets 200, a bigger or smaller one 403, for single PUTs and parts alike.
Unfinished multipart uploads are aborted hourly once their job has ended (SeaweedFS lists no start time, so age
alone can't decide). The organization's Vast key never reaches a machine.

## Tests

```bash
docker run --rm -v "$PWD/runner:/src" -w /src golang:1.23 go test -race ./...
```
