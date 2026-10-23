#!/usr/bin/env python3
"""Load a delivery archive in an owned Linux AMD64 PG18 cluster; never build."""
import argparse
import datetime
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

from report_contract import ARCHIVE_SUITES, DEFAULT_OBJECTS_QUERY, FORMAT, QUALITY_WARNING_REVIEW, RELEASE_VERSION, STATIC_INFO_FIELDS, VERIFIED, digest, isolated_pg_environment, read_quality, require_default_objects, require_recovery_report, require_release_report, require_static_info, same_json, verified_status, verify_checksums


def unpack_archive(archive, destination):
    """Accept regular files and directories only, with exactly one root."""
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        roots = set()
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or str(path) != member.name.rstrip("/") or member.name in names or not (member.isfile() or member.isdir()):
                raise ValueError(f"Unsafe delivery archive entry: {member.name}")
            roots.add(path.parts[0])
            names.add(member.name)
        if len(roots) != 1 or not members:
            raise ValueError("Delivery archive must have exactly one root directory")
        # All members have been checked; no links or special files can escape.
        source.extractall(destination)
    root = Path(destination) / roots.pop()
    if not root.is_dir():
        raise ValueError("Delivery archive root is not a directory")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
        parser.error("Delivery verification requires Linux AMD64; no platform fallback")
    if not args.archive.is_file():
        parser.error("Delivery archive does not exist")
    run_id = os.environ.get("PGCB_RUN_ID", datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}-delivery")
    evidence = Path(os.environ.get("PGCB_ARTIFACTS", "artifacts")) / run_id
    evidence.mkdir(parents=True, exist_ok=False)
    temporary = Path(tempfile.mkdtemp(prefix="pgcb-delivery.", dir="/tmp"))
    marker = temporary / ".pg-charabia-owner"
    marker.write_text(run_id)
    result = {"format": FORMAT, "run_id": run_id, "platform": "linux-amd64", "mode": "delivery-archive",
              "archive_sha256": digest(args.archive), "temporary_directory": str(temporary), "commands": [], "suites": list(ARCHIVE_SUITES), "suite_results": {}, "status": "running"}
    env = dict(isolated_pg_environment(os.environ), PYTHONDONTWRITEBYTECODE="1")
    result["inherited_libpq_environment_keys_removed"] = sorted(set(os.environ) - set(env))
    pg_config = Path(env.get("PG_CONFIG", "/usr/lib/postgresql/18/bin/pg_config"))
    pg_bin = None
    started = False
    recovery_attempted = False
    failure = None

    def checkpoint():
        (evidence / "run.json").write_text(json.dumps(result, indent=2) + "\n")

    def command(argv, label, capture=False, check=True):
        print(f"[{run_id}] {label}", flush=True)
        before = time.monotonic()
        log = evidence / (label + ".log")
        with log.open("w") as output:
            process = subprocess.Popen([str(x) for x in argv], env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait()
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
        result["commands"].append({"label": label, "argv": [str(x) for x in argv], "exit_code": process.returncode, "seconds": round(time.monotonic() - before, 3), "log": log.name})
        checkpoint()
        if process.returncode and check:
            raise RuntimeError(f"{label} failed ({process.returncode}); preserved {log}")
        return log.read_text() if capture else process.returncode

    def interrupted(signum, _frame):
        raise InterruptedError(f"Received signal {signum}")

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        delivery = unpack_archive(args.archive, temporary / "unpacked")
        verify_checksums(delivery)
        resource_manifest=json.loads((delivery/"resources/manifest.json").read_text())
        for source in resource_manifest["rebuild_source_archives"]:
            if source["package_path"]!=f"dependency-sources/{source['crate']}-{source['version']}.crate":
                raise ValueError("Invalid corresponding-source package path")
            if digest(delivery/source["package_path"])!=source["sha256"]:
                raise ValueError("Missing or changed corresponding source archive")
        for required in ("Cargo.toml","Cargo.lock","build.rs","src/lib.rs","sql/pg_charabia.sql","docker/verify.Dockerfile","scripts/generate-english.py","scripts/rebuild-german","resources/english/words.txt","resources/english/attribution/headword-pages.txt.gz"):
            if not (delivery/required).is_file():
                raise ValueError("Missing rebuild/relink material: " + required)
        manifest = json.loads((delivery / "delivery-manifest.json").read_text())
        original = json.loads((delivery / "verification/run.json").read_text())
        require_release_report(original)
        if manifest.get("verification_format") != FORMAT or manifest.get("verification_status") != original["status"] or manifest.get("target") != "x86_64-unknown-linux-gnu" or manifest.get("test_hooks") is not False or manifest.get("source_dirty") is not False or manifest.get("version") != RELEASE_VERSION:
            raise RuntimeError("Delivery manifest is not a verified Linux AMD64 release")
        if manifest.get("source_commit") != original.get("git_commit") or manifest.get("library_sha256") != original.get("loaded_library_sha256"):
            raise RuntimeError("Delivery manifest and original loaded library identity disagree")
        result["delivery_manifest"] = manifest
        if not same_json(manifest.get("quality"), original["quality"]):
            raise RuntimeError("Delivery manifest changes the verified quality results or warnings")
        if digest(delivery / QUALITY_WARNING_REVIEW["file"]) != QUALITY_WARNING_REVIEW["sha256"]:
            raise RuntimeError("Delivery archive changes or omits the original warning evidence")
        result["original_quality"] = original["quality"]
        if manifest.get("structured_token_quality_costs") != original.get("structured_token_quality_costs"):
            raise RuntimeError("Delivery manifest omits or changes the recorded structured precision costs")
        result["structured_token_quality_costs"] = original["structured_token_quality_costs"]
        version = command([pg_config, "--version"], "pg-version", True).strip()
        if not version.startswith("PostgreSQL 18.") or version != manifest["postgres_build"]:
            raise RuntimeError(f"Expected the packaged PostgreSQL build {manifest['postgres_build']}, got {version}")
        pg_bin = Path(command([pg_config, "--bindir"], "pg-bindir", True).strip())
        env["PATH"] = str(pg_bin) + os.pathsep + env.get("PATH", "")
        stage = temporary / "stage/extension"
        shutil.copytree(delivery / "extension", stage)
        library = stage / "pg_charabia.so"
        if digest(library) != manifest["library_sha256"]:
            raise RuntimeError("Extracted library differs from the delivery manifest")
        architecture = command(["file", library], "library-file", True)
        if "ELF 64-bit" not in architecture or "x86-64" not in architecture:
            raise RuntimeError("Delivery library is not Linux AMD64 ELF")
        dependencies = command(["ldd", library], "library-dependencies", True)
        if "not found" in dependencies:
            raise RuntimeError("Delivery library has an unresolved dependency")
        command(["readelf", "-h", library], "library-elf-header")
        result["library"] = {"staged_path": str(library), "sha256": digest(library), "file": architecture}
        (temporary / "socket").mkdir()
        command([pg_bin / "initdb", "-D", temporary / "data", "--encoding=UTF8", "--locale=C", "--auth=trust", "--no-instructions"], "initdb")
        with (temporary / "data/postgresql.conf").open("a") as config:
            config.write("\n" + "\n".join(["listen_addresses = ''", f"unix_socket_directories = '{temporary}/socket'", "port = 55432", f"extension_control_path = '{temporary}/stage:$system'", f"dynamic_library_path = '{stage}:$libdir'", "shared_preload_libraries = 'pg_textsearch'"]) + "\n")
        started = True
        command([pg_bin / "pg_ctl", "-D", temporary / "data", "-l", temporary / "server.log", "-w", "-t", "60", "start"], "pg-start")
        env.update(PGHOST=str(temporary / "socket"), PGPORT="55432", PGDATABASE="postgres", PGUSER=command(["id", "-un"], "database-user", True).strip(), PGCB_RUN_DIR=str(evidence), PGCB_TEST_HOOKS="0", PGCB_TEMP_DIR=str(temporary), PGCB_PLATFORM="linux-amd64", PG_CONFIG=str(pg_config),
                   PGCB_EXTENSION_STAGE=str(stage), PGCB_LIBRARY_PATH=str(library), PGCB_LIBRARY_SHA256=digest(library), PGCB_PG_BIN=str(pg_bin))
        psql = [pg_bin / "psql", "-X", "-v", "ON_ERROR_STOP=1"]
        command(psql + ["-c", "CREATE EXTENSION pg_charabia; CREATE EXTENSION pg_textsearch;"], "create-extensions")
        upstream_library = Path(command([pg_config, "--pkglibdir"], "pg-library-directory", True).strip()) / "pg_textsearch.so"
        if digest(upstream_library) != original["pg_textsearch"]["library_sha256"]:
            raise RuntimeError("pg_textsearch library differs from the original verified runtime")
        upstream_version = command(psql + ["-Atq", "-c", "SELECT extversion FROM pg_extension WHERE extname='pg_textsearch';"], "pg-textsearch-version", True).strip()
        if upstream_version != original["pg_textsearch"]["version"]:
            raise RuntimeError("pg_textsearch extension version differs from the original verified runtime")
        info = json.loads(command(psql + ["-Atq", "-c", "SELECT charabia.analysis_info();"], "analysis-info", True))
        require_static_info(info, original["analysis_info"])
        for field in STATIC_INFO_FIELDS:
            if info.get(field) != original["analysis_info"].get(field):
                raise RuntimeError(f"Extracted library analysis_info differs for {field}")
        defaults = json.loads(command(psql + ["-Atq", "-c", DEFAULT_OBJECTS_QUERY], "default-objects", True))
        require_default_objects(defaults, info)
        result["default_objects"] = defaults
        result["default_objects_verified"] = True
        with (evidence / "loaded-backend.stderr").open("w") as errors:
            backend = subprocess.Popen([str(x) for x in psql] + ["-Atq"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, env=env, text=True)
            try:
                backend.stdin.write("LOAD 'pg_charabia'; SELECT pg_backend_pid();\n")
                backend.stdin.flush()
                pid = int(backend.stdout.readline().strip())
                maps = Path(f"/proc/{pid}/maps").read_text()
                (evidence / "loaded-backend-maps.txt").write_text(maps)
                if str(library) not in maps:
                    raise RuntimeError("Backend did not map the extracted delivery library")
                if str(upstream_library) not in maps:
                    raise RuntimeError("Backend did not map the verified pg_textsearch library")
                result["pg_textsearch"] = dict(original["pg_textsearch"], loaded_backend_verified=True)
                result["loaded_backend_pid"] = pid
                result["loaded_library_sha256"] = digest(library)
                backend.stdin.write("\\q\n")
                backend.stdin.flush()
                backend.communicate(timeout=15)
                if backend.returncode:
                    raise RuntimeError("Loaded-library identity backend failed")
            finally:
                if backend.poll() is None:
                    backend.kill()
                    backend.wait()
        examples = delivery / "tests/examples.py"
        if not examples.is_file():
            raise RuntimeError("Delivery archive lacks its executable documentation examples")
        command([sys.executable, examples], "documentation-examples")
        for suite in ARCHIVE_SUITES:
            paths = [(delivery / "tests/sql" / f"{suite}.sql", psql + ["-f"]), (delivery / "tests" / f"{suite}.py", [sys.executable])]
            existing = [(path, argv) for path, argv in paths if path.is_file()]
            if not existing:
                raise RuntimeError(f"Delivery archive lacks {suite} verification")
            if suite == "recovery":
                recovery_attempted = True
            for path, argv in existing:
                command(argv + [path], f"suite-{suite}-{path.suffix[1:]}")
            if suite == "recovery":
                recovery = json.loads((evidence / "recovery/results.json").read_text())
                require_recovery_report(recovery, result["loaded_library_sha256"])
                result["recovery"] = recovery
            result["suite_results"][suite] = {"status": "passed", "commands": [item["label"] for item in result["commands"] if item["label"].startswith(f"suite-{suite}-")]}
        result["archive_test_quality"] = read_quality(evidence / "quality-results.json")
        if result["archive_test_quality"]["regressions"]:
            raise RuntimeError("Extracted delivery examples introduced a quality regression")
        # Extracted-package checks retain all warnings and frozen failures.
        result["quality"] = original["quality"]
        result["status"] = "mechanisms-passed"
    except BaseException as exc:
        failure = exc
        result.update(status="failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        cleanup = {"marker_matches": marker.is_file() and marker.read_text() == run_id, "cluster_stopped": not started, "directory_removed": False}
        if (temporary / "server.log").is_file():
            shutil.copy2(temporary / "server.log", evidence / "server.log")
        if started and pg_bin:
            try:
                stop = command([pg_bin / "pg_ctl", "-D", temporary / "data", "-m", "fast", "-w", "-t", "60", "stop"], "pg-stop", check=False)
                status = command([pg_bin / "pg_ctl", "-D", temporary / "data", "status"], "pg-final-status", check=False)
                cleanup["cluster_stopped"] = stop == 0 and status != 0 and not (temporary / "data/postmaster.pid").exists()
                if (temporary / "server.log").is_file():
                    shutil.copy2(temporary / "server.log", evidence / "server.log")
            except BaseException as exc:
                cleanup["error"] = str(exc)
        if recovery_attempted:
            try:
                child = json.loads((evidence / "recovery/results.json").read_text())
                result["recovery"] = child
                cleanup["recovery_children_cleaned"] = all(child.get("cleanup", {}).get(key) is True for key in ("marker_matches", "clusters_stopped", "directory_removed"))
            except (OSError, ValueError, TypeError) as exc:
                cleanup["recovery_children_cleaned"] = False
                cleanup["recovery_error"] = str(exc)
        if cleanup["marker_matches"] and cleanup["cluster_stopped"] and cleanup.get("recovery_children_cleaned", True):
            try:
                shutil.rmtree(temporary)
                cleanup["directory_removed"] = not temporary.exists()
            except OSError as exc:
                cleanup["error"] = str(exc)
        if not cleanup["directory_removed"]:
            result["status"] = "cleanup-failed"
            print(f"CLEANUP FAILED; preserved owned cluster: {temporary}", file=sys.stderr)
        elif result["status"] == "mechanisms-passed":
            result["status"] = verified_status(result["quality"])
        result["cleanup"] = cleanup
        checkpoint()
        print(f"[{run_id}] {result['status']}; evidence: {evidence}", flush=True)
    return 0 if result["status"] in VERIFIED else 130 if isinstance(failure, InterruptedError) else 1


if __name__ == "__main__":
    sys.exit(main())
