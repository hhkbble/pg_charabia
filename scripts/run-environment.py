#!/usr/bin/env python3
"""Build and exercise the actual extension in an owned PostgreSQL 18 cluster."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib

from report_contract import DEFAULT_OBJECTS_QUERY, FORMAT, RELEASE_VERSION, SUITES, VERIFIED, isolated_pg_environment, read_quality, require_bm25_report, require_default_objects, require_known_quality_cases, require_recovery_report, require_static_info, same_json, select_suites, verified_status

ROOT = Path(__file__).resolve().parent.parent

def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("macos-arm64", "linux-amd64"))
    parser.add_argument("--suite", action="append", help="Suite, comma-separated suites, all, or build")
    parser.add_argument("--test-hooks", action="store_true", help="Supplemental fault-injection build")
    parser.add_argument("--debug", action="store_true", help="Debug packaging; Linux defaults to release")
    args = parser.parse_args()
    try:
        selected = select_suites(args.suite, args.platform, ROOT)
    except ValueError as exc:
        parser.error(str(exc))
    all_suites = list(SUITES)
    expected = ("Darwin", "arm64") if args.platform == "macos-arm64" else ("Linux", "x86_64")
    if (platform.system(), platform.machine()) != expected:
        parser.error(f"Expected {expected}, got {(platform.system(), platform.machine())}; no platform fallback")
    if "DOCS_RS" in os.environ:
        parser.error("DOCS_RS would enable dummy dictionaries and is prohibited")
    run_id = os.environ.get("PGCB_RUN_ID", datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}")
    artifact_base = Path(os.environ.get("PGCB_ARTIFACTS", ROOT / "artifacts"))
    evidence = artifact_base / run_id
    evidence.mkdir(parents=True, exist_ok=False)
    test_dir = Path(tempfile.mkdtemp(prefix="pgcb.", dir="/tmp"))
    marker = test_dir / ".pg-charabia-owner"
    marker.write_text(run_id)
    (test_dir / "socket").mkdir()
    env = isolated_pg_environment(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    pg_config = Path(env.get("PG_CONFIG", "/opt/homebrew/opt/postgresql@18/bin/pg_config" if args.platform == "macos-arm64" else "/usr/lib/postgresql/18/bin/pg_config"))
    result = {"format": FORMAT, "run_id": run_id, "platform": args.platform, "suites": selected, "test_hooks": args.test_hooks, "build_profile": "debug" if args.debug or args.platform == "macos-arm64" else "release", "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "temporary_directory": str(test_dir), "commands": [], "suite_results": {}, "status": "running"}
    result["inherited_libpq_environment_keys_removed"] = sorted(set(os.environ) - set(env))
    pg_bin = None
    cluster_started = False
    recovery_attempted = False
    failure = None

    def checkpoint():
        (evidence / "run.json").write_text(json.dumps(result, indent=2) + "\n")

    def command(argv, label, capture=False, check=True, command_env=None):
        print(f"[{run_id}] {label}", flush=True)
        started = time.monotonic()
        path = evidence / f"{label}.log"
        with path.open("w") as log:
            proc = subprocess.Popen([str(x) for x in argv], cwd=ROOT, env=command_env or env, stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            try:
                proc.wait()
            except BaseException:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise
        item = {"label": label, "argv": [str(x) for x in argv], "exit_code": proc.returncode, "seconds": round(time.monotonic() - started, 3), "log": path.name}
        result["commands"].append(item)
        checkpoint()
        output = path.read_text(errors="replace")
        if proc.returncode and check:
            sys.stderr.write(output[-12000:])
            raise RuntimeError(f"{label} failed ({proc.returncode}); preserved {path}")
        return output if capture else proc.returncode

    def interrupted(signum, _frame):
        raise InterruptedError(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        if not pg_config.is_file():
            raise RuntimeError(f"Missing explicit PostgreSQL pg_config: {pg_config}")
        version = command([pg_config, "--version"], "pg-version", True).strip()
        if not version.startswith("PostgreSQL 18."):
            raise RuntimeError(f"PG18 required, got {version}")
        pg_bin = Path(command([pg_config, "--bindir"], "pg-bindir", True).strip())
        env["PATH"] = str(pg_bin) + os.pathsep + env.get("PATH", "")
        rust_version = command(["rustc", "-Vv"], "rust-version", True)
        if "release: 1.97.1\n" not in rust_version:
            raise RuntimeError("The locked Rust 1.97.1 toolchain is required")
        target_triple = "aarch64-apple-darwin" if args.platform == "macos-arm64" else "x86_64-unknown-linux-gnu"
        if f"host: {target_triple}\n" not in rust_version:
            raise RuntimeError(f"Expected Rust host {target_triple}")
        lock_hash = sha256(ROOT / "Cargo.lock")
        feature_string = "pg18,test-hooks" if args.test_hooks else "pg18"
        cache_key = f"{target_triple}-pg18-rust1971-pgrx0193-{lock_hash[:16]}-{feature_string.replace(',', '-')}"
        cache = Path(env.get("PGCB_CACHE", Path.home() / ".cache/pg_charabia"))
        env["CARGO_TARGET_DIR"] = str(Path(env.get("PGCB_TARGET_BASE", cache / "target")) / cache_key)
        env["PGRX_HOME"] = str(test_dir / "pgrx")
        Path(env["PGRX_HOME"]).mkdir()
        env["PGRX_PG_CONFIG_PATH"] = str(pg_config)
        env["PG_CONFIG"] = str(pg_config)
        env["CARGO_BUILD_JOBS"] = env.get("CARGO_BUILD_JOBS", "2")
        env.setdefault("LINDERA_BUILD_DICTIONARY_CACHE_DIR", str(cache / "lindera" / target_triple))
        env.setdefault("LIBCLANG_PATH", "/opt/homebrew/opt/llvm/lib" if args.platform == "macos-arm64" else "/usr/lib/llvm-19/lib")
        if args.platform == "macos-arm64":
            cli_root = cache / "tools/pgrx-0.19.3"
            cli = cli_root / "bin/cargo-pgrx"
            if not cli.is_file():
                install_env = dict(env, CARGO_TARGET_DIR=str(cache / "tools-target/pgrx-0.19.3"))
                command(["cargo", "install", "cargo-pgrx", "--version", "0.19.3", "--locked", "--root", cli_root], "install-cargo-pgrx", command_env=install_env)
            env["PATH"] = str(cli.parent) + os.pathsep + env["PATH"]
        cli_version = command(["cargo", "pgrx", "--version"], "pgrx-version", True)
        if "0.19.3" not in cli_version:
            raise RuntimeError("cargo-pgrx 0.19.3 is required")
        crate_version = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
        if crate_version != RELEASE_VERSION:
            raise RuntimeError(f"Expected extension version {RELEASE_VERSION}, got {crate_version}")
        result.update({"postgres": version, "rust": rust_version, "cargo_lock_sha256": lock_hash, "resources_sha256": sha256(ROOT / "resources/manifest.json"), "target_triple": target_triple, "features": feature_string, "target_cache": env["CARGO_TARGET_DIR"], "git_commit": command(["git", "-c", f"safe.directory={ROOT}", "rev-parse", "HEAD"], "git-commit", True).strip(), "git_status": command(["git", "-c", f"safe.directory={ROOT}", "status", "--porcelain=v1"], "git-status", True)})
        command([sys.executable, ROOT / "scripts/prepare-resources.py", "--cache", env["LINDERA_BUILD_DICTIONARY_CACHE_DIR"]], "resource-verification")
        command(["cargo", "tree", "--locked", "--no-default-features", "--features", feature_string], "cargo-tree")
        command(["cargo", "tree", "--locked", "-e", "features", "--no-default-features", "--features", feature_string], "cargo-feature-tree")
        normal_tree=command(["cargo","tree","--locked","-e","normal","--no-default-features","--features",feature_string],"normal-cargo-tree",True)
        if "stop-words v" in normal_tree:
            raise RuntimeError("Raw stop-words arrays remain in runtime dependencies")
        if "unit" in selected:
            command([sys.executable, ROOT / "scripts/check-report-contract"], "runner-contract")
            command([sys.executable, ROOT / "scripts/check-english-generator"], "word-generator-contract")
            command(["cargo", "test", "--locked", "--lib", "--no-default-features", "--features", feature_string], "unit")
            result["suite_results"]["unit"] = {"status": "passed", "commands": ["runner-contract", "unit"]}
            checkpoint()
        if args.platform == "linux-amd64" and selected == all_suites:
            command(["cargo", "fmt", "--all", "--", "--check"], "fmt")
            command(["cargo", "clippy", "--locked", "--all-targets", "--no-default-features", "--features", feature_string, "--", "-D", "warnings"], "clippy")
        package_cmd = ["cargo", "pgrx", "package", "--pg-config", pg_config, "--out-dir", test_dir / "stage", "--prefix-dir", "extension", "--no-default-features", "--features", feature_string.replace(",", " "), "--cargo=--locked"]
        if args.debug or args.platform == "macos-arm64":
            package_cmd.append("--debug")
        command(package_cmd, "package")
        schema_cmd = ["cargo", "pgrx", "schema", "--pg-config", pg_config, "--skip-build", "--out", evidence / "schema.sql", "--dot", evidence / "schema.dot", "--no-default-features", "--features", feature_string.replace(",", " "), "--cargo=--locked"]
        if result["build_profile"] == "release":
            schema_cmd.append("--release")
        command(schema_cmd, "schema")
        command(["cargo", "metadata", "--locked", "--format-version", "1", "--no-default-features", "--features", feature_string], "cargo-metadata")
        for fingerprint in ("/opt/pgcb-packages.txt", "/opt/pgcb-pg-textsearch-source.sha256", "/opt/pgcb-pg-textsearch-LICENSE"):
            if Path(fingerprint).is_file():
                shutil.copy2(fingerprint, evidence / Path(fingerprint).name)
        stage = test_dir / "stage/extension"
        libraries = list(stage.glob("pg_charabia*.so")) + list(stage.glob("pg_charabia*.dylib"))
        if len(libraries) != 1:
            raise RuntimeError(f"Expected one staged extension library in {stage}, found {libraries}")
        library = libraries[0]
        library_info = command(["file", library], "library-file", True)
        if args.platform == "linux-amd64":
            if "ELF 64-bit" not in library_info or "x86-64" not in library_info:
                raise RuntimeError(f"Wrong extension architecture: {library_info}")
            deps = command(["ldd", library], "library-dependencies", True)
            if "not found" in deps:
                raise RuntimeError("Unresolved library dependency")
            command(["readelf", "-h", library], "library-elf-header")
        else:
            if "arm64" not in library_info or "Mach-O" not in library_info:
                raise RuntimeError(f"Wrong extension architecture: {library_info}")
            command(["otool", "-L", library], "library-dependencies")
        package_out = evidence / "package"
        shutil.copytree(test_dir / "stage", package_out)
        shutil.copytree(ROOT / "resources", package_out / "resources")
        metadata = json.loads((evidence / "cargo-metadata.log").read_text())
        resource_manifest = json.loads((ROOT / "resources/manifest.json").read_text())
        charabia_package = next(p for p in metadata["packages"] if p["name"] == "charabia" and p["version"] == resource_manifest["charabia"])
        charabia_node = next(n for n in metadata["resolve"]["nodes"] if n["id"] == charabia_package["id"])
        enabled_features = set(charabia_node["features"])
        if enabled_features != set(resource_manifest["resolved_charabia_features"]):
            raise RuntimeError(f"Resolved Charabia features differ from the resource manifest: {sorted(enabled_features)}")
        result["charabia_features"] = sorted(enabled_features)
        actual_resources = []
        for item in resource_manifest["embedded_data"]:
            package = next(p for p in metadata["packages"] if p["name"] == item["crate"] and p["version"] == item["version"])
            data_path = Path(package["manifest_path"]).parent / item["path"]
            actual_hash = sha256(data_path)
            if actual_hash != item["sha256"]:
                raise RuntimeError(f"Embedded resource hash mismatch: {data_path}")
            actual_resources.append(dict(item, verified_sha256=actual_hash))
        (evidence / "embedded-resources.json").write_text(json.dumps(actual_resources, indent=2) + "\n")
        build_sources=[]
        for item in resource_manifest["build_source_files"]:
            package_metadata=next(p for p in metadata["packages"] if p["name"]==item["crate"] and p["version"]==item["version"])
            data_path=Path(package_metadata["manifest_path"]).parent/item["path"]
            if sha256(data_path)!=item["sha256"]:
                raise RuntimeError("Build-only source identity mismatch: "+str(data_path))
            build_sources.append(dict(item,verified_sha256=sha256(data_path)))
        (evidence/"build-source-files.json").write_text(json.dumps(build_sources,indent=2)+"\n")
        # Keep the exact corresponding upstream source in the binary delivery.
        source_archives=[]
        for entry in resource_manifest["rebuild_source_archives"]:
            package_metadata=next(p for p in metadata["packages"] if p["name"]==entry["crate"] and p["version"]==entry["version"])
            source_root=Path(package_metadata["manifest_path"]).parent
            archive=source_root.parents[2]/"cache"/source_root.parent.name/(entry["crate"]+"-"+entry["version"]+".crate")
            if sha256(archive)!=entry["sha256"]:
                raise RuntimeError("Rebuild source archive checksum mismatch: "+str(archive))
            if entry["crate"]=="charabia":
                import tarfile
                with tarfile.open(archive) as contents:
                    for relative,wanted in (("dictionaries/txt/german/words.txt",resource_manifest["german_rebuild"]["source_sha256"]),("dictionaries/fst/german/words.fst",resource_manifest["german_rebuild"]["fst_sha256"])):
                        source=contents.extractfile("charabia-0.10.0/"+relative)
                        if source is None or hashlib.sha256(source.read()).hexdigest()!=wanted:
                            raise RuntimeError("German corresponding source differs: "+relative)
            destination=package_out/entry["package_path"]
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(archive,destination)
            source_archives.append(dict(entry,verified_sha256=sha256(destination)))
        result["rebuild_source_archives"]=source_archives
        (evidence/"rebuild-source-archives.json").write_text(json.dumps(source_archives,indent=2)+"\n")
        licenses = [{"name": p["name"], "version": p["version"], "license": p.get("license"), "license_file": p.get("license_file"), "source": p.get("source")} for p in metadata["packages"]]
        (package_out / "dependency-licenses.json").write_text(json.dumps(licenses, indent=2) + "\n")
        license_dir = package_out / "dependency-notices"
        license_dir.mkdir()
        for package in metadata["packages"]:
            crate_dir = Path(package["manifest_path"]).parent
            candidates = set()
            for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*"):
                candidates.update(p for p in crate_dir.glob(pattern) if p.is_file())
            if package.get("license_file"):
                candidate = crate_dir / package["license_file"]
                if candidate.is_file():
                    candidates.add(candidate)
            if candidates:
                crate_notices = license_dir / f"{package['name']}-{package['version']}"
                crate_notices.mkdir()
                for candidate in candidates:
                    shutil.copy2(candidate, crate_notices / candidate.name)
        shutil.copy2(ROOT / "Cargo.lock", package_out / "Cargo.lock")
        checksums = []
        for artifact in sorted(package_out.rglob("*")):
            if artifact.is_file():
                checksums.append(f"{sha256(artifact)}  {artifact.relative_to(package_out)}")
        (package_out / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
        result["library"] = {"staged_path": str(library), "canonical_path": str(library.resolve()), "sha256": sha256(library), "file": library_info, "package_path": str(package_out)}
        controls = list(stage.glob("*.control"))
        if not controls or not list(stage.glob("*.sql")):
            raise RuntimeError("Package lacks control or schema SQL")
        if args.platform == "linux-amd64":
            environment_lock = json.loads((ROOT / "docker/environment.lock.json").read_text())
            source_fingerprint = Path("/opt/pgcb-pg-textsearch-source.sha256").read_text().split()[0]
            if source_fingerprint != environment_lock["pg_textsearch_archive_sha256"]:
                raise RuntimeError("The image's pg_textsearch source archive does not match environment.lock.json")
            upstream_library = Path(command([pg_config, "--pkglibdir"], "pg-library-directory", True).strip()) / "pg_textsearch.so"
            upstream_file = command(["file", upstream_library], "pg-textsearch-library-file", True)
            if "ELF 64-bit" not in upstream_file or "x86-64" not in upstream_file:
                raise RuntimeError("pg_textsearch is not an x86-64 ELF shared library")
            upstream_dependencies = command(["ldd", upstream_library], "pg-textsearch-dependencies", True)
            if "not found" in upstream_dependencies:
                raise RuntimeError("pg_textsearch has an unresolved dynamic dependency")
            result["pg_textsearch"] = {"version": environment_lock["pg_textsearch"], "commit": environment_lock["pg_textsearch_commit"], "source_archive_sha256": source_fingerprint, "library_path": str(upstream_library), "library_sha256": sha256(upstream_library)}
        # No installation into the PostgreSQL distribution is performed.
        command([pg_bin / "initdb", "-D", test_dir / "data", "--encoding=UTF8", "--locale=C", "--auth=trust", "--no-instructions"], "initdb")
        config = test_dir / "data/postgresql.conf"
        config_lines = ["listen_addresses = ''", f"unix_socket_directories = '{test_dir}/socket'", "port = 55432", f"extension_control_path = '{test_dir}/stage:$system'", f"dynamic_library_path = '{stage}:$libdir'", "log_min_messages = notice", "max_connections = 24"]
        needs_bm25 = any(s in selected for s in ("smoke", "bm25", "stress", "recovery", "benchmark")) or (args.platform == "linux-amd64" and any(s in selected for s in ("lifecycle", "chain")))
        if needs_bm25:
            # Container-only pg_textsearch installation belongs to the pinned image.
            if args.platform != "linux-amd64":
                raise RuntimeError("Local BM25 suite requires an explicitly staged upstream build; use Linux verifier")
            config_lines.append("shared_preload_libraries = 'pg_textsearch'")
        with config.open("a") as handle:
            handle.write("\n" + "\n".join(config_lines) + "\n")
        cluster_started = True
        command([pg_bin / "pg_ctl", "-D", test_dir / "data", "-l", test_dir / "server.log", "-w", "-t", "60", "start"], "pg-start")
        env.update({"PGHOST": str(test_dir / "socket"), "PGPORT": "55432", "PGDATABASE": "postgres", "PGUSER": command(["id", "-un"], "database-user", True).strip(), "PGCB_RUN_DIR": str(evidence), "PGCB_TEST_HOOKS": "1" if args.test_hooks else "0", "PGCB_TEMP_DIR": str(test_dir), "PGCB_PLATFORM": args.platform,
                    "PGCB_EXTENSION_STAGE": str(stage), "PGCB_LIBRARY_PATH": str(library), "PGCB_LIBRARY_SHA256": sha256(library), "PGCB_PG_BIN": str(pg_bin)})
        psql = [pg_bin / "psql", "-X", "-v", "ON_ERROR_STOP=1"]
        command(psql + ["-c", "CREATE EXTENSION pg_charabia;"], "create-extension")
        if needs_bm25:
            command(psql + ["-c", "CREATE EXTENSION pg_textsearch;"], "create-pg-textsearch")
        if needs_bm25:
            upstream_version = command(psql + ["-Atq", "-c", "SELECT extversion FROM pg_extension WHERE extname='pg_textsearch';"], "pg-textsearch-version", True).strip()
            if upstream_version != result["pg_textsearch"]["version"]:
                raise RuntimeError(f"Unexpected loaded pg_textsearch version: {upstream_version}")
        analysis_info = json.loads(command(psql + ["-Atq", "-c", "SELECT charabia.analysis_info();"], "analysis-info", True))
        expected_info = {
            "extension": tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"],
            "charabia": resource_manifest["charabia"], "pgrx": "0.19.3",
            "target": target_triple,
            "cargo_lock_sha256": lock_hash, "resource_manifest_sha256": result["resources_sha256"],
            "resources": resource_manifest,
        }
        try:
            require_static_info(analysis_info, expected_info)
        except ValueError as exc:
            (evidence / "analysis-info-mismatches.json").write_text(json.dumps({"expected": expected_info, "actual": analysis_info, "error": str(exc)}, indent=2) + "\n")
            raise RuntimeError("Loaded analysis_info static identity does not match build inputs; see actual and expected evidence") from exc
        result["analysis_info"] = analysis_info
        result["analysis_info_verified"] = True
        defaults = json.loads(command(psql + ["-Atq", "-c", DEFAULT_OBJECTS_QUERY], "default-objects", True))
        require_default_objects(defaults, analysis_info)
        result["default_objects_verified"] = True
        result["default_objects"] = defaults
        abi_query = """SELECT json_agg(json_build_object('name',p.proname,
            'arguments',oidvectortypes(p.proargtypes),'returns',format_type(p.prorettype,NULL),
            'strict',p.proisstrict) ORDER BY p.proname)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='charabia' AND p.proname IN
            ('parser_start','parser_gettoken','parser_end','parser_lextypes','parser_headline',
             'linguistic_dictionary_init','linguistic_dictionary_lexize','structured_dictionary_init','structured_dictionary_lexize');"""
        actual_abi = json.loads(command(psql + ["-Atq", "-c", abi_query], "fts-callback-abi", True))
        callback_types = {
            "parser_start": ("internal, integer", "internal"),
            "parser_gettoken": ("internal, internal, internal", "internal"),
            "parser_end": ("internal", "void"),
            "parser_lextypes": ("internal", "internal"),
            "parser_headline": ("internal, internal, tsquery", "internal"),
            "linguistic_dictionary_init": ("internal", "internal"),
            "structured_dictionary_init": ("internal", "internal"),
            "structured_dictionary_lexize": ("internal, internal, internal, internal", "internal"),
            "linguistic_dictionary_lexize": ("internal, internal, internal, internal", "internal"),
        }
        expected_abi = [{"name": name, "arguments": types[0], "returns": types[1], "strict": True} for name, types in sorted(callback_types.items())]
        if actual_abi != expected_abi:
            raise RuntimeError(f"The installed FTS callback ABI differs from the design: {actual_abi}")
        result["fts_callback_abi_verified"] = True
        public_query = """SELECT json_agg(json_build_object('name',p.proname,
            'arguments',oidvectortypes(p.proargtypes),'returns',format_type(p.prorettype,NULL),
            'security_definer',p.prosecdef,'volatility',p.provolatile,'parallel',p.proparallel,
            'columns',(SELECT json_agg(json_build_object('name',p.proargnames[i],'type',format_type(p.proallargtypes[i],NULL)) ORDER BY i) FROM generate_subscripts(p.proallargtypes,1) i WHERE p.proargmodes[i]='t')) ORDER BY p.proname)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='charabia' AND p.proname IN ('analysis_info','analyze','query_any','create_config','alter_config','drop_config');"""
        public_abi = json.loads(command(psql + ["-Atq", "-c", public_query], "public-api-abi", True))
        analyze_columns = [{"name": name, "type": "bigint" if name in ("byte_start", "byte_end", "char_start", "char_end") else "text"} for name in ("raw", "kind", "script", "charabia_language", "language", "language_source", "byte_start", "byte_end", "char_start", "char_end", "token_type")]
        expected_public = [{"name": name, "arguments": arguments, "returns": returns, "security_definer": False, "volatility": "s", "parallel": "r", "columns": columns} for name, arguments, returns, columns in (
            ("analysis_info", "", "jsonb", None), ("analyze", "text", "record", analyze_columns), ("query_any", "text, regconfig", "tsquery", None))]
        if public_abi != expected_public:
            raise RuntimeError(f"Installed public diagnostic ABI differs from the release contract: {public_abi}")
        result["public_api_abi_verified"] = True
        command(psql + ["-c", "SELECT version(); SELECT extname, extversion FROM pg_extension ORDER BY 1; SHOW dynamic_library_path; SHOW extension_control_path;"], "database-identity")
        # A persistent backend loads this exact staged library; its mappings prove identity.
        with (evidence / "loaded-backend.stderr").open("w") as error_log:
            backend = subprocess.Popen([str(x) for x in psql] + ["-Atq"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error_log, env=env, text=True, bufsize=1)
            try:
                backend.stdin.write("LOAD 'pg_charabia'; SELECT pg_backend_pid();\n")
                backend.stdin.flush()
                backend_pid = int(backend.stdout.readline().strip())
                if args.platform == "linux-amd64":
                    maps = Path(f"/proc/{backend_pid}/maps").read_text()
                else:
                    with (evidence / "loaded-backend-maps.stderr").open("w") as maps_errors:
                        maps = subprocess.check_output(["lsof", "-p", str(backend_pid)], text=True, stderr=maps_errors)
                (evidence / "loaded-backend-maps.txt").write_text(maps)
                if not any(path in maps for path in (str(library), str(library.resolve()))):
                    raise RuntimeError("Backend mappings do not contain the staged library")
                if needs_bm25 and result["pg_textsearch"]["library_path"] not in maps:
                    raise RuntimeError("Backend mappings do not contain the fingerprinted pg_textsearch library")
                if needs_bm25:
                    result["pg_textsearch"]["loaded_backend_verified"] = True
                result["loaded_backend_pid"] = backend_pid
                result["loaded_library_sha256"] = sha256(library)
                backend.stdin.write("SELECT 1;\n\\q\n")
                backend.stdin.flush()
                backend.communicate(timeout=15)
                if backend.returncode:
                    raise RuntimeError("Loaded-library identity backend failed")
            finally:
                if backend.poll() is None:
                    backend.kill()
                    backend.wait()
        for suite in selected:
            if suite in ("unit", "build"):
                continue
            sql = ROOT / "tests/sql" / f"{suite}.sql"
            python = ROOT / "tests" / f"{suite}.py"
            if suite == "recovery":
                recovery_attempted = True
            if sql.is_file():
                command(psql + ["-f", sql], f"suite-{suite}-sql")
            if python.is_file():
                command([sys.executable, python], f"suite-{suite}-python")
            if suite == "bm25":
                result["bm25_quality"] = require_bm25_report(json.loads((evidence / "bm25-results.json").read_text()))
            if suite == "recovery":
                recovery_path = evidence / "recovery/results.json"
                if not recovery_path.is_file():
                    raise RuntimeError("The recovery suite omitted its child lifecycle report")
                recovery = json.loads(recovery_path.read_text())
                require_recovery_report(recovery, result["loaded_library_sha256"])
                result["recovery"] = recovery
            result["suite_results"][suite] = {"status": "passed", "commands": [item["label"] for item in result["commands"] if item["label"].startswith(f"suite-{suite}-")]}
            checkpoint()
        if args.platform == "linux-amd64" and selected == all_suites:
            command([sys.executable, ROOT / "tests/examples.py"], "documentation-examples")
        probe = env.get("PGCB_CLEANUP_PROBE")
        if probe == "sql-error":
            command(psql + ["-c", "SELECT 1/0;"], "cleanup-probe-sql-error")
        elif probe == "sigterm":
            os.kill(os.getpid(), signal.SIGTERM)
        elif probe:
            raise RuntimeError(f"Unknown PGCB_CLEANUP_PROBE: {probe}")
        result["quality"] = read_quality(evidence / "quality-results.json")
        if "bm25" in selected:
            require_known_quality_cases(result["quality"])
            if not same_json(result["bm25_quality"], result["quality"]):
                raise RuntimeError("BM25 quality summary differs from the shared quality registry")
        if result["quality"]["regressions"]:
            raise RuntimeError(f"Unreviewed quality regressions: {result['quality']['regressions']}")
        result["status"] = "mechanisms-passed"
    except BaseException as exc:
        failure = exc
        result["status"] = "failed"
        result["error"] = str(exc)
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Preserve evidence before touching owned resources; never remove a live cluster.
        if (test_dir / "server.log").is_file():
            shutil.copy2(test_dir / "server.log", evidence / "server.log")
        cleanup = {"marker_matches": marker.is_file() and marker.read_text() == run_id, "cluster_stopped": not cluster_started, "directory_removed": False}
        if cluster_started and pg_bin:
            try:
                rc = command([pg_bin / "pg_ctl", "-D", test_dir / "data", "-m", "fast", "-w", "-t", "60", "stop"], "pg-stop", check=False)
                status_rc = command([pg_bin / "pg_ctl", "-D", test_dir / "data", "status"], "pg-final-status", check=False)
                cleanup["cluster_stopped"] = rc == 0 and status_rc != 0 and not (test_dir / "data/postmaster.pid").exists()
                if (test_dir / "server.log").is_file():
                    shutil.copy2(test_dir / "server.log", evidence / "server.log")
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
                shutil.rmtree(test_dir)
                cleanup["directory_removed"] = not test_dir.exists()
            except OSError as exc:
                cleanup["error"] = str(exc)
        if not cleanup["directory_removed"]:
            result["status"] = "cleanup-failed"
            print(f"CLEANUP FAILED; preserved owned cluster: {test_dir}", file=sys.stderr)
        result["cleanup"] = cleanup
        if result["status"] == "mechanisms-passed":
            result["status"] = verified_status(result["quality"])
        if "quality" not in result:
            try:
                result["quality"] = read_quality(evidence / "quality-results.json")
            except (ValueError, json.JSONDecodeError) as exc:
                result["quality_error"] = str(exc)
        structured_report=evidence/"structured-results.json"
        if structured_report.is_file():
            try:
                structured_data=json.loads(structured_report.read_text())
                result["structured_token_quality_costs"]=structured_data.get("quality_observations",[])
            except (ValueError,TypeError,AttributeError) as exc:
                result["structured_token_quality_cost_error"]=str(exc)
                if result["status"] != "cleanup-failed":
                    result["status"]="failed"
                if failure is None:
                    failure=exc
        result["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        checkpoint()
        print(f"[{run_id}] {result['status']}; evidence: {evidence}", flush=True)
    return 0 if result["status"] in VERIFIED else 130 if isinstance(failure, InterruptedError) else 1

if __name__ == "__main__":
    sys.exit(main())
