"""Waiting on a job: `krater logs --follow` and `krater wait`. Both poll the API; nothing is pushed.

Finished states mirror `FINISHED_JOB_STATES` in `krater/models/compute_job.py`; the CLI is its own package, so it
can't import them.
"""

from __future__ import annotations

from collections.abc import Callable

from krater_cli.api import Krater

FINISHED_STATES = frozenset({"succeeded", "failed", "cancelled"})
#: The API serves at most this much of a log per request.
MAX_LOG_BYTES = 1024 * 1024
#: Extra bytes asked for beyond the new ones, see `_new_text`.
FIRST_OVERLAP = 4096

Say = Callable[[str], None]


def state_line(job: dict) -> str:
    return f"{job['name']}: {job['state']}" + (f" - {job['state_reason']}" if job.get("state_reason") else "")


def exit_code_for(job: dict) -> int:
    """0 if it succeeded, the command's own exit code if it failed with one, else 1 (cancelled, or no code)."""
    if job["state"] == "succeeded":
        return 0
    code = job.get("exit_code")
    return code if job["state"] == "failed" and isinstance(code, int) and 1 <= code <= 255 else 1


def wait(
    krater: Krater,
    job_id: str,
    *,
    interval: float,
    timeout_minutes: float | None,
    say: Say,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    quiet: bool = False,
) -> tuple[dict, bool]:
    """Poll until the job is finished or the time is up. Returns (the job, whether it finished)."""
    deadline = None if timeout_minutes is None else clock() + timeout_minutes * 60
    last_state = None
    while True:
        job = krater.get(f"/jobs/{job_id}")
        if job["state"] != last_state and not quiet:
            say(state_line(job))
        last_state = job["state"]
        if job["state"] in FINISHED_STATES:
            return job, True
        if deadline is not None and clock() >= deadline:
            return job, False
        sleep(interval)


def _new_text(krater: Krater, base: str, seen: int, size: int) -> tuple[str, int, int | None]:
    """The log's bytes after the first `seen`: (text, the log's size now, its attempt).

    The API serves the *end* of a log, and drops the partial first line of a window that doesn't reach the start of
    the file. So ask for the new bytes plus some overlap, keep the last `size - seen` bytes of what comes back, and
    widen the overlap if the dropped line ate into the new bytes. The size in each answer is the truth: the log may
    have grown since we last looked.
    """
    overlap = FIRST_OVERLAP
    while True:
        want = min(max(size - seen, 0) + overlap, MAX_LOG_BYTES)
        data = krater.get(f"{base}?bytes={want}")
        size = data["size_bytes"]
        gap = size - seen
        raw = data["text"].encode("utf-8", errors="replace")
        if data["attempt"] is None or gap <= 0 or len(raw) >= gap:
            return raw[-gap:].decode("utf-8", errors="replace") if gap > 0 else "", size, data["attempt"]
        if want >= min(size, MAX_LOG_BYTES):  # can't ask for more
            return raw.decode("utf-8", errors="replace"), size, data["attempt"]
        overlap *= 4


def follow_logs(
    krater: Krater,
    job_id: str,
    *,
    tail_bytes: int,
    interval: float,
    say: Say,
    sleep: Callable[[float], None],
) -> dict:
    """Print the end of the log, then whatever is added, until the job is finished. Returns the finished job."""
    base = f"/jobs/{job_id}/logs"
    attempt: int | None = None
    seen = 0
    first = True
    while True:
        job = krater.get(f"/jobs/{job_id}")  # before the log, so a finished job's last output is still read
        if first:
            data = krater.get(f"{base}?bytes={tail_bytes}")
            if data["text"]:
                if data["truncated"]:
                    say(f"[the last part of attempt {data['attempt']}'s output; krater download {job_id} --only logs]")
                say(data["text"].rstrip("\n"))
            attempt, seen = data["attempt"], data["size_bytes"]
            first = False
        else:
            probe = krater.get(f"{base}?bytes=1")
            if probe["attempt"] is not None and probe["attempt"] != attempt:
                if attempt is not None:
                    say(f"--- attempt {probe['attempt']} ---")
                attempt, seen = probe["attempt"], 0
            if probe["size_bytes"] < seen:  # the log started over
                seen = 0
            if probe["size_bytes"] > seen:
                text, size, got_attempt = _new_text(krater, base, seen, probe["size_bytes"])
                if got_attempt != attempt and got_attempt is not None:
                    attempt, seen = got_attempt, 0  # a new attempt began between the two calls; read it from the top
                    say(f"--- attempt {got_attempt} ---")
                    continue
                gap = size - seen
                skipped = gap - len(text.encode("utf-8", errors="replace"))
                if skipped > 0:
                    say(f"[... skipped {skipped} bytes ...]")
                if text:
                    say(text.rstrip("\n"))
                seen = size
        if job["state"] in FINISHED_STATES:
            say(state_line(job))
            return job
        sleep(interval)
