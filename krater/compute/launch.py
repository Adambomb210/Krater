"""What Krater asks Vast to start in a job's container.

Krater launches in Vast's `args` mode and replaces the image's ENTRYPOINT with `sh`: in that mode Vast's `onstart`
field *is* the entrypoint override (Vast's own CLI sends `--entrypoint X` as `"onstart": X`), and `args` are passed
to it. Without the override, an image with its own ENTRYPOINT would get `sh -c ...` as arguments, exit, and be
restarted by Vast in a billing loop. So an image needs a POSIX `sh`, and a member whose image's entrypoint does setup
(activating an environment, say) puts that in the command. Not yet checked on a paid launch.

Until the injected runner exists (a small static binary fetched at start; see the design), the main process is a
shell that runs the member's command and then waits forever. It must never exit: Vast restarts a container whose
main process ends, which would rerun the job and keep billing (spike, 2026-10-11). Krater destroys the instance
instead.
"""

from __future__ import annotations

from krater.models import ComputeJob

_WAIT_FOREVER = 'echo "krater: waiting for Krater to stop this machine"\nwhile :; do sleep 3600; done'


#: Vast's `onstart` in `args` mode: the entrypoint that replaces the image's own.
ENTRYPOINT = "sh"


def startup_args(job: ComputeJob) -> tuple[str, ...]:
    """The arguments to `ENTRYPOINT`."""
    if job.command:
        script = f'{job.command}\ncode=$?\necho "krater: command exited with code $code"\n{_WAIT_FOREVER}'
    else:
        script = _WAIT_FOREVER
    return ("-c", script)


def job_env(job: ComputeJob, attempt: int) -> dict[str, str]:
    """Variables every job sees. `KRATER_RESUMED=1` tells the member's code to restore its last checkpoint."""
    return {
        "KRATER_JOB_ID": str(job.id),
        "KRATER_ATTEMPT": str(attempt),
        "KRATER_RESUMED": "1" if attempt > 1 else "0",
    }
