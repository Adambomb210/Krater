"""`krater`: Krater Compute from a terminal.

    krater login --url https://krater.example.org
    krater projects
    krater submit <project> --name train --image ghcr.io/me/train:v3 --command "python train.py" \\
        --gpu "RTX 4090" --target 0.30 --max 0.45 --max-runtime 240
    krater jobs <project>
    krater show <job>
    krater ssh <job> -- -L 8888:localhost:8888
    krater secrets set <project> HF_TOKEN        (reads the value from stdin, or asks without echoing it)

Prices are full hourly cost in dollars (GPU plus disk). `--json` prints Krater's answer as JSON.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
from collections.abc import Callable, Sequence

import httpx

from krater_cli import __version__, auth, store
from krater_cli.api import Krater, KraterError

ESCALATIONS = ("keep_waiting", "raise_bid", "on_demand", "cancel")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="krater", description="Krater Compute from a terminal.")
    parser.add_argument("--version", action="version", version=f"krater {__version__}")
    parser.add_argument("--json", action="store_true", help="print Krater's answer as JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="sign in with Weave")
    login.add_argument("--url", default=os.environ.get("KRATER_URL"), help="Krater's address (or $KRATER_URL)")
    login.add_argument("--no-browser", action="store_true", help="don't open a browser; paste the code instead")
    commands.add_parser("logout", help="forget the stored sign-in")
    commands.add_parser("whoami", help="who you're signed in as")
    commands.add_parser("projects", help="your projects")

    jobs = commands.add_parser("jobs", help="a project's jobs and boxes, newest first")
    jobs.add_argument("project")

    submit = commands.add_parser("submit", help="queue a job or a box")
    submit.add_argument("project")
    submit.add_argument("--name", required=True)
    submit.add_argument("--image", required=True, help="your container image; it needs a POSIX sh")
    submit.add_argument("--command", dest="job_command", help="what the job runs (not for a box)")
    submit.add_argument("--box", action="store_true", help="an interactive SSH box instead of a job")
    submit.add_argument("--gpu", action="append", default=[], help="a GPU model you accept (repeatable)")
    submit.add_argument("--min-vram", type=int, help="or: any GPU with at least this many GB")
    submit.add_argument("--gpus", type=int, default=1)
    submit.add_argument("--disk", type=int, default=32, help="GB")
    submit.add_argument("--target", required=True, help="start at or under this, $/h")
    submit.add_argument("--max", required=True, help="never pay more than this, $/h")
    submit.add_argument("--on-demand", action="store_true", help="rent on demand instead of bidding")
    submit.add_argument("--max-wait", type=int, default=12 * 60, help="minutes to wait for the target price")
    submit.add_argument("--on-max-wait", choices=ESCALATIONS, default="keep_waiting")
    submit.add_argument("--max-runtime", type=int, default=8 * 60, help="minutes")

    show = commands.add_parser("show", help="a job's state, machine and recent events")
    show.add_argument("job")
    for action, text in (
        ("cancel", "cancel a job or box (its machine is destroyed)"),
        ("stop", "stop a box (it saves /workspace first)"),
        ("resume", "start a stopped box again on the same machine"),
        ("move", "start a stopped box on another machine from its saved /workspace"),
    ):
        command = commands.add_parser(action, help=text)
        command.add_argument("job")

    ssh = commands.add_parser("ssh", help="SSH into a running box (extra arguments after -- go to ssh)")
    ssh.add_argument("job")
    ssh.add_argument("ssh_args", nargs=argparse.REMAINDER)

    secrets = commands.add_parser("secrets", help="a project's secrets (values can't be read back)")
    secret_commands = secrets.add_subparsers(dest="secrets_command", required=True)
    secrets_list = secret_commands.add_parser("list")
    secrets_list.add_argument("project")
    secrets_set = secret_commands.add_parser("set", help="reads the value from stdin, or asks for it")
    secrets_set.add_argument("project")
    secrets_set.add_argument("name")
    secrets_rm = secret_commands.add_parser("rm")
    secrets_rm.add_argument("project")
    secrets_rm.add_argument("name")

    commands.add_parser("prices", help="what an hour costs now, per GPU model")
    return parser


def _table(rows: list[list[str]], say: Callable[[str], None]) -> None:
    if not rows:
        return
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        say("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())


def _money(value: str | None) -> str:
    return f"${value}" if value is not None else "-"


def _read_secret(name: str) -> str:
    if not sys.stdin.isatty():
        return sys.stdin.read().rstrip("\n")
    return getpass.getpass(f"Value for {name} (not shown): ")


def run(
    argv: Sequence[str],
    *,
    http: httpx.Client,
    say: Callable[[str], None] = print,
    prompt: Callable[[str], str] = input,
    read_secret: Callable[[str], str] = _read_secret,
    exec_ssh: Callable[[list[str]], None] | None = None,
) -> int:
    args = _parser().parse_args(list(argv))

    if args.command == "login":
        if not args.url:
            say("Give Krater's address: krater login --url https://<your Krater>")
            return 2
        creds = auth.login(args.url, http=http, open_browser=not args.no_browser, prompt=prompt, say=say)
        me = Krater(http).get("/me")
        say(f"Signed in to {creds['url']} as {me['name']}.")
        return 0
    if args.command == "logout":
        say("Signed out." if store.clear() else "Not signed in.")
        return 0

    krater = Krater(http)

    def out(data: dict, render: Callable[[], None]) -> None:
        if args.json:
            say(json.dumps(data, indent=2))
        else:
            render()

    if args.command == "whoami":
        me = krater.get("/me")
        roles = [role for role in ("member", "reviewer", "admin") if me.get(role)]
        out(me, lambda: say(f"{me['name']} <{me['email']}> ({', '.join(roles)}) on {krater.creds['url']}"))
    elif args.command == "projects":
        data = krater.get("/projects")
        rows = [["ID", "STATUS", "TITLE"]] + [
            [p["id"], p["status"] + ("" if p["can_launch"] else " (can't launch)"), p["title"] or "Untitled"]
            for p in data["projects"]
        ]
        out(data, lambda: _table(rows, say) if data["projects"] else say("You're not on any project's team."))
    elif args.command == "jobs":
        data = krater.get(f"/projects/{args.project}/jobs")
        rows = [["ID", "KIND", "STATE", "TARGET/MAX", "SPENT", "NAME"]] + [
            [
                j["id"],
                j["kind"],
                j["state"],
                f"{_money(j['target_usd_per_hour'])}/{_money(j['max_usd_per_hour'])}",
                f"${j['spend_cents'] / 100:.2f}",
                j["name"],
            ]
            for j in data["jobs"]
        ]
        out(data, lambda: _table(rows, say) if data["jobs"] else say("No jobs yet."))
    elif args.command == "submit":
        body = {
            "name": args.name,
            "image": args.image,
            "command": args.job_command,
            "kind": "box" if args.box else "job",
            "gpu_names": args.gpu,
            "min_gpu_ram_gb": args.min_vram,
            "num_gpus": args.gpus,
            "disk_gb": args.disk,
            "target_usd_per_hour": args.target,
            "max_usd_per_hour": args.max,
            "interruptible": not args.on_demand,
            "max_wait_minutes": args.max_wait,
            "on_max_wait": args.on_max_wait,
            "max_runtime_minutes": args.max_runtime,
        }
        job = krater.post(f"/projects/{args.project}/jobs", body)
        out(job, lambda: say(f"Queued {job['kind']} {job['id']}: {job['state_reason'] or job['state']}"))
    elif args.command == "show":
        job = krater.get(f"/jobs/{args.job}")

        def render() -> None:
            say(
                f"{job['name']} ({job['kind']}): {job['state']}"
                + (f" - {job['state_reason']}" if job["state_reason"] else "")
            )
            say(
                f"  image {job['image']}; target {_money(job['target_usd_per_hour'])}/h, max {_money(job['max_usd_per_hour'])}/h"
            )
            say(
                f"  spent ${job['spend_cents'] / 100:.2f}"
                + (f"; exit code {job['exit_code']}" if job["exit_code"] is not None else "")
            )
            machine = job.get("machine")
            if machine:
                say(f"  machine: {machine['gpu']} at {_money(machine['usd_per_hour'])}/h, {machine['state']}")
                if machine.get("ssh_host"):
                    say(f"  ssh: krater ssh {job['id']}  (ssh -p {machine['ssh_port']} root@{machine['ssh_host']})")
            for event in job.get("events", [])[:10]:
                say(f"  {event['at'][:19].replace('T', ' ')}  {event['message']}")

        out(job, render)
    elif args.command in ("cancel", "stop", "resume", "move"):
        job = krater.post(f"/jobs/{args.job}/{args.command}")
        out(
            job,
            lambda: say(
                f"{job['name']}: {job['state']}" + (f" - {job['state_reason']}" if job["state_reason"] else "")
            ),
        )
    elif args.command == "ssh":
        job = krater.get(f"/jobs/{args.job}")
        machine = job.get("machine") or {}
        if job["kind"] != "box" or not machine.get("ssh_host"):
            say(f"{job['name']} has no SSH address yet ({job['state']}).")
            return 1
        extra = [a for a in args.ssh_args if a != "--"]
        command = ["ssh", "-p", str(machine["ssh_port"]), *extra, f"root@{machine['ssh_host']}"]
        if exec_ssh is not None:
            exec_ssh(command)
        else:
            if shutil.which("ssh") is None:
                say("No ssh command found. Run: " + " ".join(command))
                return 1
            os.execvp("ssh", command)
    elif args.command == "secrets":
        base = f"/projects/{args.project}/secrets"
        if args.secrets_command == "list":
            data = krater.get(base)
            rows = [["NAME", "UPDATED"]] + [
                [s["name"], s["updated_at"][:16].replace("T", " ")] for s in data["secrets"]
            ]
            out(data, lambda: _table(rows, say) if data["secrets"] else say("No secrets."))
        elif args.secrets_command == "set":
            value = read_secret(args.name)
            if not value:
                say("No value given; nothing changed.")
                return 1
            data = krater.put(f"{base}/{args.name}", {"value": value})
            out(data, lambda: say(f"Saved {data['name']}. Jobs get it when they next start."))
        else:
            data = krater.delete(f"{base}/{args.name}")
            out(data, lambda: say(f"Deleted {args.name}."))
    elif args.command == "prices":
        data = krater.get("/prices")
        rows = [["GPU", "RENTAL", "CHEAPEST", "MEDIAN", "OFFERS"]] + [
            [p["gpu"], p["rental"], _money(p["min_usd_per_hour"]), _money(p["median_usd_per_hour"]), p["offers"]]
            for p in data["prices"]
        ]
        out(data, lambda: _table(rows, say) if data["prices"] else say("No prices yet."))
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    with httpx.Client(timeout=30.0) as http:
        try:
            code = run(sys.argv[1:] if argv is None else argv, http=http)
        except auth.AuthError as exc:
            print(f"krater: {exc}", file=sys.stderr)
            code = 1
        except KraterError as exc:
            print(f"krater: {exc}", file=sys.stderr)
            for field, problem in exc.fields.items():
                print(f"  {field}: {problem}", file=sys.stderr)
            code = 1
        except httpx.HTTPError as exc:
            print(f"krater: couldn't reach Krater or Weave ({exc.__class__.__name__}).", file=sys.stderr)
            code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
