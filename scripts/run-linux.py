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

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "pg-charabia-verify:pg18-pgrx0193"

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append")
    parser.add_argument("--test-hooks", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-build-image", action="store_true")
    parser.add_argument("--cleanup-probe", choices=("sql-error", "sigterm"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}-amd64"
    evidence = ROOT / "artifacts" / run_id
    evidence.mkdir(parents=True, exist_ok=False)
    name = "pgcb-" + run_id.lower()
    cid = None
    exit_code = 1
    result = {"run_id": run_id, "container_name": name, "platform": "linux/amd64", "status": "running"}
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
        if args.cleanup_probe:
            argv[argv.index(IMAGE):argv.index(IMAGE)] = ["--env", f"PGCB_CLEANUP_PROBE={args.cleanup_probe}"]
        for suite in args.suite or []:
            argv += ["--suite", suite]
        if args.test_hooks:
            argv.append("--test-hooks")
        if args.debug:
            argv.append("--debug")
        cid = subprocess.check_output(argv, text=True).strip()
        result["container_id"] = cid
        (evidence / "container-id").write_text(cid + "\n")
        exit_code = run(["docker", "start", "--attach", cid], "container", check=False)
        # docker start's transport status is checked against the actual container exit.
        state = json.loads(subprocess.check_output(["docker", "inspect", cid], text=True))[0]
        (evidence / "container-inspect.json").write_text(json.dumps(state, indent=2) + "\n")
        exit_code = int(state["State"]["ExitCode"]) if exit_code == 0 else exit_code
        result["status"] = "passed" if exit_code == 0 else "failed"
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
        if cid is None:
            probe = subprocess.run(["docker", "inspect", name], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                candidate = json.loads(probe.stdout)[0]
                if candidate.get("Config", {}).get("Labels", {}).get("pg_charabia.run_id") == run_id:
                    cid = candidate["Id"]
                    result["container_id"] = cid
        cleanup = {"container_removed": cid is None, "volumes": []}
        if cid:
            try:
                state = json.loads(subprocess.check_output(["docker", "inspect", cid], text=True))[0]
                volumes = [m["Name"] for m in state.get("Mounts", []) if m.get("Type") == "volume"]
                cleanup["volumes"] = volumes
                if state["State"]["Running"]:
                    subprocess.run(["docker", "stop", "--time", "65", cid], stdout=subprocess.DEVNULL, check=True)
                run(["docker", "logs", cid], "container-final", check=False)
                (evidence / "container-final-inspect.json").write_text(subprocess.check_output(["docker", "inspect", cid], text=True))
                subprocess.run(["docker", "rm", "--volumes", cid], stdout=subprocess.DEVNULL, check=True)
                cleanup["container_removed"] = subprocess.run(["docker", "inspect", cid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
                cleanup["volumes_removed"] = all(subprocess.run(["docker", "volume", "inspect", v], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0 for v in volumes)
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
