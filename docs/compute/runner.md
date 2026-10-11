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
6. reports the exit code. The scheduler destroys the machine on its next pass.

## What a member's job sees

| Variable | Meaning |
| --- | --- |
| `KRATER_RESUMED` | `1` when this is a resume (a later attempt, or a restart after a pause): load the latest checkpoint |
| `KRATER_CHECKPOINT_DIR` | Save checkpoints here. Restored before the command starts |
| `KRATER_OUTPUT_DIR` | Put results here. Uploaded when the command exits |
| `KRATER_JOB_ID`, `KRATER_ATTEMPT` | For logs |

Write a checkpoint under a temporary name ending in `.tmp` or `.partial` and rename it when it's complete: the
runner skips those names, so it never uploads a half-written file. The image needs a POSIX `sh` and one of curl, wget
or Python; its own ENTRYPOINT is not run, so put any setup in the command.

## Security

The runner holds one secret, `KRATER_JOB_TOKEN`. It works for this instance only and only while it is live (Krater
stores its sha256). Storage is reached through presigned URLs for this job's own keys
(`projects/<project>/jobs/<job>/checkpoints|outputs/`), single PUTs up to 64 MB and multipart parts above that, so no
request body passes Cloudflare's cap in front of the storage host. The organization's Vast key never reaches a
machine.

## Tests

```bash
docker run --rm -v "$PWD/runner:/src" -w /src golang:1.23 go test -race ./...
```
