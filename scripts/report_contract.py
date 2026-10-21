"""Shared verifier and delivery contract; no environment is started on import."""
import hashlib
import json
from pathlib import Path

FORMAT = 2
RELEASE_VERSION = "0.3.0"
SUITES = ("unit", "smoke", "native", "chain", "bm25", "headline", "config", "lifecycle", "errors", "stress", "recovery", "benchmark")
MAC_DEFAULT = ("unit", "native", "chain", "headline", "config", "lifecycle", "errors")
ARCHIVE_SUITES = ("smoke", "chain", "headline", "config", "recovery")
STATIC_INFO_FIELDS = frozenset(("extension", "pgrx", "charabia", "target", "cargo_lock_sha256", "resource_manifest_sha256", "analysis_revision", "resource_fingerprint", "language_count", "languages", "resources"))
VERIFIED = ("verified", "verified-with-known-quality-failures")
QUALITY_FIELDS = ("input", "query", "config", "expected", "actual")
REQUIRED_QUALITY_CASES = frozenset(("english-den", "code-var", "english-short-phrase"))

def isolated_pg_environment(source):
    """Do not let libpq services/hostaddr override an owned Unix socket.

    Build/test controls are distinct from client connection defaults. In
    particular PGSERVICE must be absent, not an empty service name.
    """
    return {key: value for key, value in source.items()
            if not key.startswith("PG") or key == "PG_CONFIG" or key.startswith(("PGCB_", "PGRX_"))}


def require_container_cleanup(evidence, run_id):
    """Permit removal only after the inner owner proves all PGDATA is gone."""
    reports = list(Path(evidence).glob("*/run.json"))
    if len(reports) != 1:
        raise ValueError("Container retained: expected one final inner cleanup report")
    inner = json.loads(reports[0].read_text())
    if not isinstance(inner, dict) or inner.get("format") != FORMAT or inner.get("run_id") != run_id:
        raise ValueError("Container retained: inner cleanup report identity mismatch")
    if inner.get("status") not in (*VERIFIED, "failed"):
        raise ValueError("Container retained: inner cleanup is not final or failed")
    cleanup = inner.get("cleanup")
    if not isinstance(cleanup, dict) or not all(cleanup.get(k) is True for k in ("marker_matches", "cluster_stopped", "directory_removed")):
        raise ValueError("Container retained: owned PostgreSQL cleanup is unproven")
    if "recovery_children_cleaned" in cleanup or "recovery" in inner:
        if cleanup.get("recovery_children_cleaned") is not True:
            raise ValueError("Container retained: recovery child cleanup is unproven")
        child = json.loads((reports[0].parent / "recovery/results.json").read_text())
        if not isinstance(child, dict) or child.get("format") != FORMAT or child.get("run") != run_id or child.get("status") not in ("passed", "failed"):
            raise ValueError("Container retained: recovery cleanup report identity mismatch")
        child_cleanup = child.get("cleanup")
        if not isinstance(child_cleanup, dict) or not all(child_cleanup.get(k) is True for k in ("marker_matches", "clusters_stopped", "directory_removed")):
            raise ValueError("Container retained: recovery child resources remain")


def digest(path):
    state = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            state.update(chunk)
    return state.hexdigest()


def select_suites(groups, platform, root):
    selected = [suite for group in groups or [] for suite in group.split(",")]
    if not selected:
        selected = list(SUITES if platform == "linux-amd64" else MAC_DEFAULT)
    if selected == ["all"]:
        selected = list(SUITES)
    if not selected or any(s not in SUITES + ("build",) for s in selected):
        raise ValueError("Unknown suite selection")
    if "build" in selected and selected != ["build"]:
        raise ValueError("build must be selected alone")
    if len(selected) != len(set(selected)):
        raise ValueError("Duplicate suite selection")
    if platform == "macos-arm64" and any(s in selected for s in ("smoke", "bm25", "stress", "recovery", "benchmark")):
        raise ValueError("These suites require the Linux verifier; no local BM25 environment is staged")
    for suite in selected:
        if suite not in ("unit", "build") and not any((Path(root) / relative).is_file() for relative in (f"tests/{suite}.py", f"tests/sql/{suite}.sql")):
            raise ValueError(f"Suite {suite!r} is not implemented")
    return selected


def require_static_info(info, expected):
    """Check static identity independently of user configuration or live FSTs."""
    if not isinstance(info, dict) or set(info) != STATIC_INFO_FIELDS:
        raise ValueError("analysis_info must expose exactly the static 0.3.0 identity fields")
    mismatches = {key: {"expected": value, "actual": info.get(key)} for key, value in expected.items() if not same_json(info.get(key), value)}
    if mismatches:
        raise ValueError(f"analysis_info static identity differs from the build inputs: {mismatches}")
    if info["extension"] != RELEASE_VERSION or info["language_count"] != 71:
        raise ValueError("analysis_info requires release 0.3.0 and 71 language dictionaries")
    if not isinstance(info["analysis_revision"], str) or not info["analysis_revision"]:
        raise ValueError("analysis_info requires a nonempty analysis_revision")
    for field in ("cargo_lock_sha256", "resource_manifest_sha256", "resource_fingerprint"):
        value = info[field]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"analysis_info contains an invalid {field}")
    languages = info["languages"]
    if not isinstance(languages, list) or len(languages) != 71:
        raise ValueError("analysis_info requires exactly 71 language capability rows")
    aliases = set()
    for row in languages:
        if not isinstance(row, dict) or set(row) != {"language", "script", "has_stopwords", "stemmer", "defaults"}:
            raise ValueError("Invalid language capability row")
        alias = row["language"]
        if not isinstance(alias, str) or len(alias) != 3 or not alias.isascii() or not alias.islower() or not alias.isalpha() or alias in aliases:
            raise ValueError("Language aliases must be unique three-letter lowercase codes")
        aliases.add(alias)
        if type(row["has_stopwords"]) is not bool or not isinstance(row["script"], str) or not row["script"]:
            raise ValueError("Invalid language script or stopword capability")
        stemmer = row["stemmer"]
        if stemmer is not None and (not isinstance(stemmer, str) or not stemmer):
            raise ValueError("Invalid stemmer capability")
        defaults = row["defaults"]
        wanted = {"discard": row["has_stopwords"], "stem": stemmer is not None, "normalize": True, "filter": False}
        if not same_json(defaults, wanted):
            raise ValueError(f"Invalid defaults for {alias}")
    if "und" not in aliases or sum(row["has_stopwords"] for row in languages) != 52 or sum(row["stemmer"] is not None for row in languages) != 19:
        raise ValueError("Language capability coverage differs from the fixed 71/52/19 contract")


def require_recovery_report(recovery, library_hash):
    """A successful SQL child cannot hide failed cleanup or use an old library."""
    if not isinstance(recovery, dict) or recovery.get("format") != FORMAT or recovery.get("status") != "passed":
        raise ValueError("Missing successful format-2 recovery evidence")
    if not all(recovery.get("cleanup", {}).get(key) is True for key in ("marker_matches", "clusters_stopped", "directory_removed")):
        raise ValueError("Recovery child cleanup did not pass")
    if not isinstance(library_hash, str) or len(library_hash) != 64 or any(c not in "0123456789abcdef" for c in library_hash) or recovery.get("loaded_library_sha256") != library_hash:
        raise ValueError("Recovery did not load the currently verified extension library")


def frozen_quality_cases():
    # Read only the reviewed source fixture, never a signature from run evidence.
    path = Path(__file__).resolve().parent.parent / "tests/fixtures/quality-known.json"
    document = json.loads(path.read_text())
    cases = document.get("cases")
    if document.get("format") != FORMAT or not isinstance(cases, dict) or set(cases) != REQUIRED_QUALITY_CASES:
        raise ValueError("The reviewed quality fixture must contain exactly the three frozen cases")
    for case in cases.values():
        if not isinstance(case, dict) or set(case) != set(QUALITY_FIELDS) or case["expected"] is not True or case["actual"] is not False:
            raise ValueError("Invalid frozen domain quality signature")
    return cases


def same_json(left, right):
    return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(right, sort_keys=True, separators=(",", ":"), allow_nan=False)


def quality_case(case_id, *, input, query, config, expected, actual, known_signature=None):
    case = dict(id=case_id, input=input, query=query, config=config, expected=expected, actual=actual)
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("A quality case needs a stable nonempty id")
    frozen = frozen_quality_cases().get(case_id)
    if frozen is not None:
        if not same_json(known_signature, frozen):
            raise ValueError(f"Quality case {case_id} requires its reviewed frozen signature")
        case["known_signature"] = frozen
        exact = all(same_json(case[field], frozen[field]) for field in QUALITY_FIELDS)
        case["status"] = "known-failure" if exact else "regression"
    elif known_signature is not None:
        raise ValueError(f"Quality case {case_id} is not an eligible known failure")
    else:
        case["status"] = "passed" if same_json(expected, actual) else "regression"
    return case


def validate_quality(document):
    if not isinstance(document, dict) or document.get("format") != FORMAT or not isinstance(document.get("cases"), list):
        raise ValueError("Invalid quality-results.json format")
    seen = set()
    counts = {"passed": 0, "known-failure": 0, "regression": 0}
    for case in document["cases"]:
        if not isinstance(case, dict) or case.get("id") in seen:
            raise ValueError("Invalid or duplicate quality case")
        try:
            checked = quality_case(case["id"], **{key: case[key] for key in QUALITY_FIELDS}, known_signature=case.get("known_signature"))
        except (KeyError, TypeError) as exc:
            raise ValueError("Incomplete quality case") from exc
        if case.get("status") != checked["status"]:
            raise ValueError(f"Quality case {case['id']} has an invalid status")
        seen.add(case["id"])
        counts[case["status"]] += 1
    return {"format": FORMAT, "counts": counts, "cases": document["cases"],
            "known_failures": [c["id"] for c in document["cases"] if c["status"] == "known-failure"],
            "regressions": [c["id"] for c in document["cases"] if c["status"] == "regression"]}


def read_quality(path):
    path = Path(path)
    return validate_quality(json.loads(path.read_text()) if path.is_file() else {"format": FORMAT, "cases": []})


def require_known_quality_cases(quality):
    missing = REQUIRED_QUALITY_CASES - {case["id"] for case in quality["cases"]}
    if missing:
        raise ValueError(f"Missing frozen domain quality cases: {sorted(missing)}")


def verified_status(quality):
    if quality["regressions"]:
        return "failed"
    return "verified-with-known-quality-failures" if quality["known_failures"] else "verified"


def verify_checksums(package):
    package = Path(package).absolute()
    # copytree follows directory symlinks. Reject them before checking the file
    # manifest so no unverified alias or outside directory can enter delivery.
    if any(path.is_symlink() for path in (package, *package.parents)):
        raise ValueError("Package path contains a symbolic link")
    if any(path.is_symlink() for path in package.rglob("*")):
        raise ValueError("Package contains a symbolic link")
    package = package.resolve()
    checksums = package / "SHA256SUMS"
    if not checksums.is_file():
        raise ValueError("The verified package lacks SHA256SUMS")
    entries = set()
    for line in checksums.read_text().splitlines():
        expected, relative = line.split("  ", 1)
        path = package / relative
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise ValueError(f"Invalid checksum for {relative}")
        if relative in entries or not path.resolve().is_relative_to(package) or not path.is_file():
            raise ValueError(f"Invalid package manifest path: {relative}")
        entries.add(relative)
        if digest(path) != expected:
            raise ValueError(f"Package checksum mismatch: {relative}")
    actual = {str(p.relative_to(package)) for p in package.rglob("*") if p.is_file() and p != checksums}
    if actual != entries:
        raise ValueError(f"Package manifest is incomplete: {sorted(actual ^ entries)}")
    return entries


def require_release_report(report):
    """Reject old or partial evidence before reading a package or invoking tools."""
    if report.get("format") != FORMAT or report.get("status") not in VERIFIED or report.get("platform") != "linux-amd64":
        raise ValueError("A verified format-2 Linux AMD64 report is required")
    if report.get("build_profile") != "release" or report.get("test_hooks") is not False:
        raise ValueError("Debug or test-hook builds cannot be packaged for delivery")
    if set(report.get("suites", [])) != set(SUITES) or len(report["suites"]) != len(SUITES):
        raise ValueError("All required release suites must pass in the same run")
    suite_results = report.get("suite_results", {})
    if set(suite_results) != set(SUITES) or any(v.get("status") != "passed" for v in suite_results.values()):
        raise ValueError("Missing successful mechanism suite evidence")
    if not all(report.get("cleanup", {}).get(key) for key in ("marker_matches", "cluster_stopped", "directory_removed")):
        raise ValueError("Cluster cleanup did not pass")
    quality = validate_quality(report.get("quality", {}))
    require_known_quality_cases(quality)
    if report["status"] != verified_status(quality):
        raise ValueError("Quality failures and report status disagree")
    if not report.get("analysis_info_verified") or not report.get("fts_callback_abi_verified"):
        raise ValueError("Runtime identity and FTS ABI were not verified")
    require_static_info(report.get("analysis_info"), {"extension": RELEASE_VERSION})
    if not report.get("public_api_abi_verified") or not report.get("default_objects_verified"):
        raise ValueError("Public diagnostic ABI and default text search objects were not verified")
    require_recovery_report(report.get("recovery"), report.get("loaded_library_sha256"))
    if not report.get("pg_textsearch", {}).get("loaded_backend_verified"):
        raise ValueError("The actual pg_textsearch backend library was not verified")
    commands = {item["label"]: item for item in report.get("commands", [])}
    for label in ("unit", "fmt", "clippy", "schema", "package", "fts-callback-abi", "public-api-abi", "default-objects", "analysis-info", "documentation-examples", "runner-contract"):
        if commands.get(label, {}).get("exit_code") != 0:
            raise ValueError(f"Missing successful {label} evidence")
    for suite in set(SUITES) - {"unit"}:
        labels = [key for key in commands if key.startswith(f"suite-{suite}-")]
        if not labels or any(commands[key]["exit_code"] for key in labels):
            raise ValueError(f"Missing successful {suite} suite command evidence")


def require_release_identity(report, *, commit, status, lock_hash, resources_hash, library_hash):
    if report.get("git_status") != "" or status:
        raise ValueError("A clean tested checkout and clean current checkout are required")
    if commit != report.get("git_commit"):
        raise ValueError("The current commit differs from the tested commit")
    if library_hash != report.get("library", {}).get("sha256") or library_hash != report.get("loaded_library_sha256"):
        raise ValueError("Packaged and actually loaded library hashes differ")
    if lock_hash != report.get("cargo_lock_sha256"):
        raise ValueError("Current Cargo.lock differs from the tested build")
    if resources_hash != report.get("resources_sha256"):
        raise ValueError("Current resource manifest differs from the tested build")
