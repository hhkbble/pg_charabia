#!/usr/bin/env python3
"""Own a Linux AMD64 verifier container and preserve evidence before cleanup."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from report_contract import FORMAT, VERIFIED, digest, require_container_cleanup, select_suites

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "pg-charabia-verify:pg18-pgrx0193"


def git_metadata_mounts(root):
    """Make an isolated worktree's existing Git identity readable in Docker."""
    root = Path(root).resolve()
    if not (root / ".git").is_file():
        return []
    def git_path(option):
        return Path(subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", option],
            text=True).strip()).resolve()
    common = git_path("--git-common-dir")
    administrative = git_path("--git-dir")
    if not administrative.is_relative_to(common):
        raise ValueError("Worktree metadata must belong to its Git common directory")
    return ["--mount", f"type=bind,src={common},dst={common},readonly"]


def inspect_resource(kind, name, observations):
    """Return the object, or None only when Docker explicitly reports absence."""
    argv = ["docker", "inspect", name] if kind == "container" else ["docker", "volume", "inspect", name]
    probe = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    observations.append({"argv": argv, "exit_code": probe.returncode,
                         "stdout": probe.stdout, "stderr": probe.stderr})
    if probe.returncode == 0:
        value = json.loads(probe.stdout)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise ValueError(f"Invalid Docker {kind} inspection for {name}")
        return value[0]
    missing = (f"error: no such object: {name}",
               f"error: no such container: {name}",
               f"error response from daemon: no such container: {name}") if kind == "container" else (
               f"error response from daemon: get {name}: no such volume",)
    if probe.returncode == 1 and probe.stdout.strip() in ("", "[]") and probe.stderr.strip().casefold() in {message.casefold() for message in missing}:
        return None
    raise RuntimeError(f"Cannot establish Docker {kind} state for {name}: exit {probe.returncode}; {probe.stderr.strip()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append")
    parser.add_argument("--test-hooks", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-build-image", action="store_true")
    parser.add_argument("--archive", type=Path, help="Load and verify an existing delivery archive without compiling")
    parser.add_argument("--cleanup-probe", choices=("sql-error", "sigterm", "recovery-sql-error", "recovery-sigterm"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.archive:
        if args.suite or args.test_hooks or args.debug or args.cleanup_probe:
            parser.error("--archive cannot be combined with suites, hooks, debug or cleanup probes")
        args.archive = args.archive.resolve()
        if not args.archive.is_file():
            parser.error("Delivery archive does not exist")
        selected = []
        args.no_build_image = True
    else:
        try:
            selected = select_suites(args.suite, "linux-amd64", ROOT)
        except ValueError as exc:
            parser.error(str(exc))
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}-amd64"
    evidence = ROOT / "artifacts" / run_id
    evidence.mkdir(parents=True, exist_ok=False)
    name = "pgcb-" + run_id.lower()
    cid = None
    create_attempted = False
    exit_code = 1
    result = {"format": FORMAT, "run_id": run_id, "container_name": name, "platform": "linux/amd64", "suites": selected, "status": "running"}
    def interrupted(signum, _frame):
        raise InterruptedError(f"Received signal {signum}")
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    def run(argv, label, check=True):
        print(f"[{run_id}] {label}", flush=True)
        with (evidence / f"{label}.log").open("w") as handle:
            proc = subprocess.Popen(argv, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                proc.wait()
            except BaseException:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise
        if check and proc.returncode:
            raise RuntimeError(f"{label} failed ({proc.returncode}); see {evidence / (label + '.log')}")
        return proc.returncode
    try:
        if not args.no_build_image:
            run(["docker", "build", "--platform", "linux/amd64", "--progress", "plain", "-f", "docker/verify.Dockerfile", "-t", IMAGE, "."], "image-build")
        inspect = subprocess.check_output(["docker", "image", "inspect", IMAGE], text=True)
        (evidence / "image-inspect.json").write_text(inspect)
        image = json.loads(inspect)[0]
        if image["Architecture"] != "amd64" or image["Os"] != "linux":
            raise RuntimeError("The verifier image is not Linux AMD64")
        result["image_id"] = image["Id"]
        # Explicit persistent caches are target-specific and never contain PGDATA.
        cache_root = Path(os.environ.get("PGCB_LINUX_CACHE", Path.home() / ".cache/pg_charabia/linux-amd64"))
        cache_root.mkdir(parents=True, exist_ok=True)
        # These owned cache/evidence directories must be writable by container postgres
        # on both Docker Desktop and native Linux bind mounts.
        cache_root.chmod(0o777)
        evidence.chmod(0o777)
        for part in ("cargo", "target", "lindera", "resources"):
            (cache_root / part).mkdir(exist_ok=True)
            (cache_root / part).chmod(0o777)
        host_archives = Path.home() / ".cache/pg_charabia/resources"
        manifest = json.loads((ROOT / "resources/manifest.json").read_text())
        for item in manifest["archives"]:
            source = host_archives / item["name"]
            target = cache_root / "resources" / item["name"]
            if source.is_file() and not target.exists():
                shutil.copyfile(source, target)
        argv = ["docker", "create", "--platform", "linux/amd64", "--name", name, "--label", "pg_charabia.verifier=true", "--label", f"pg_charabia.run_id={run_id}", "--mount", f"type=bind,src={ROOT},dst=/workspace,readonly", "--mount", f"type=bind,src={cache_root},dst=/cache", "--mount", f"type=bind,src={evidence},dst=/evidence", "--tmpfs", "/var/lib/postgresql:rw", "--env", f"PGCB_RUN_ID={run_id}-inner", IMAGE]
        metadata_mounts = git_metadata_mounts(ROOT)
        argv[argv.index(IMAGE):argv.index(IMAGE)] = metadata_mounts
        result["git_metadata_mounts"] = metadata_mounts
        if args.archive:
            index = argv.index(IMAGE)
            argv[index:index] = ["--entrypoint", "python3", "--mount", f"type=bind,src={args.archive},dst=/delivery.tar.gz,readonly"]
            argv += ["/workspace/scripts/check-delivery.py", "--archive", "/delivery.tar.gz"]
            result["archive"] = {"path": str(args.archive), "sha256": digest(args.archive)}
        if args.cleanup_probe:
            argv[argv.index(IMAGE):argv.index(IMAGE)] = ["--env", f"PGCB_CLEANUP_PROBE={args.cleanup_probe}"]
        for suite in args.suite or []:
            argv += ["--suite", suite]
        if args.test_hooks:
            argv.append("--test-hooks")
        if args.debug:
            argv.append("--debug")
        create_attempted = True
        cid = subprocess.check_output(argv, text=True).strip()
        result["container_id"] = cid
        (evidence / "container-id").write_text(cid + "\n")
        exit_code = run(["docker", "start", "--attach", cid], "container", check=False)
        # docker start's transport status is checked against the actual container exit.
        state = json.loads(subprocess.check_output(["docker", "inspect", cid], text=True))[0]
        (evidence / "container-inspect.json").write_text(json.dumps(state, indent=2) + "\n")
        exit_code = int(state["State"]["ExitCode"]) if exit_code == 0 else exit_code
        reports = list(evidence.glob("*/run.json"))
        if exit_code == 0:
            if len(reports) != 1:
                raise RuntimeError("Expected exactly one inner verifier report")
            inner = json.loads(reports[0].read_text())
            if inner.get("format") != FORMAT or inner.get("status") not in VERIFIED:
                raise RuntimeError("Container exit zero without a verified format-3 inner report")
            if not all(inner.get("cleanup", {}).get(k) for k in ("marker_matches", "cluster_stopped", "directory_removed")):
                raise RuntimeError("Inner cleanup did not pass")
            result["status"] = inner["status"]
            result["quality"] = inner.get("quality")
        else:
            result["status"] = "failed"
    except BaseException as exc:
        exit_code = 130 if isinstance(exc, InterruptedError) else 1
        result["error"] = str(exc)
        result["status"] = "failed"
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # A signal can arrive after Docker creates the container but before the ID
        # reaches Python. Recover only the exact name with this run's ownership label.
        cleanup = {"container_removed": cid is None and not create_attempted,
                   "volumes": [], "inspection_probes": []}
        if cid is None and create_attempted:
            try:
                candidate = inspect_resource("container", name, cleanup["inspection_probes"])
                if candidate is None:
                    cleanup["container_removed"] = True
                else:
                    if candidate.get("Config", {}).get("Labels", {}).get("pg_charabia.run_id") == run_id:
                        cid = candidate["Id"]
                        result["container_id"] = cid
                    else:
                        raise RuntimeError("Refusing cleanup of a container without this run's ownership label")
            except BaseException as exc:
                cleanup["container_removed"] = False
                cleanup["error"] = str(exc)
        if cid:
            try:
                state = json.loads(subprocess.check_output(["docker", "inspect", cid], text=True))[0]
                volumes = [m["Name"] for m in state.get("Mounts", []) if m.get("Type") == "volume"]
                cleanup["volumes"] = volumes
                cleanup["volumes_removed"] = not volumes
                if state["State"]["Running"]:
                    subprocess.run(["docker", "stop", "--time", "65", cid], stdout=subprocess.DEVNULL, check=True)
                run(["docker", "logs", cid], "container-final", check=False)
                (evidence / "container-final-inspect.json").write_text(subprocess.check_output(["docker", "inspect", cid], text=True))
                try:
                    require_container_cleanup(evidence, run_id + "-inner")
                except (OSError, ValueError, TypeError) as exc:
                    cleanup["container_retained"] = True
                    cleanup["retention_reason"] = str(exc)
                    cleanup["volumes_removed"] = not volumes
                else:
                    subprocess.run(["docker", "rm", "--volumes", cid], stdout=subprocess.DEVNULL, check=True)
                    cleanup["container_removed"] = inspect_resource("container", cid, cleanup["inspection_probes"]) is None
                    volume_absence = [inspect_resource("volume", v, cleanup["inspection_probes"]) is None for v in volumes]
                    cleanup["volumes_removed"] = all(volume_absence)
            except BaseException as exc:
                cleanup["error"] = str(exc)
                result["status"] = "cleanup-failed"
                exit_code = 1
        if not cleanup["container_removed"] or not cleanup.get("volumes_removed", True):
            result["status"] = "cleanup-failed"
            exit_code = 1
        result["cleanup"] = cleanup
        (evidence / "outer-run.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"[{run_id}] {result['status']}; evidence: {evidence}", flush=True)
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
