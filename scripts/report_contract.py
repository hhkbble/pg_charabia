"""Shared verifier and delivery contract; no environment is started on import."""
import hashlib
import json
from pathlib import Path
import re

FORMAT = 2
RELEASE_VERSION = "0.3.0"
ANALYSIS_REVISION = "route16-ascii-symbol-1"
LANGUAGE_CODES = ("eng", "deu", "nld", "rus", "spa", "fra", "por", "ita", "ara", "tur", "cmn", "jpn", "kor", "vie", "tha", "und")
ENGLISH_ELIGIBLE = frozenset(("deu", "nld", "spa", "fra", "por", "ita", "tur", "vie"))
LANGUAGE_SCRIPTS = dict(zip(LANGUAGE_CODES, ("Latin", "Latin", "Latin", "Cyrillic", "Latin", "Latin", "Latin", "Latin", "Arabic", "Latin", "Cj", "Cj", "Hangul", "Latin", "Thai", "Other")))
LANGUAGE_STEMMERS = dict(zip(LANGUAGE_CODES[:10], ("English", "German", "Dutch", "Russian", "Spanish", "French", "Portuguese", "Italian", "Arabic", "Turkish")))
SUITES = ("unit", "smoke", "native", "symbol", "chain", "bm25", "headline", "config", "lifecycle", "errors", "stress", "recovery", "benchmark")
MAC_DEFAULT = ("unit", "native", "symbol", "chain", "headline", "config", "lifecycle", "errors")
ARCHIVE_SUITES = ("smoke", "symbol", "chain", "headline", "config", "recovery")
STATIC_INFO_FIELDS = frozenset(("extension", "pgrx", "charabia", "target", "cargo_lock_sha256", "resource_manifest_sha256", "analysis_revision", "resource_fingerprint", "language_count", "languages", "symbols", "resources"))
VERIFIED = ("verified", "verified-with-known-quality-failures")
QUALITY_FIELDS = ("input", "query", "config", "expected", "actual")
REQUIRED_QUALITY_CASES = frozenset(("english-den", "code-var", "english-short-phrase"))
# Reviewed against the unchanged domain inputs, real native vectors and BM25
# candidates of the route16 implementation. This closes failures, never adds one.
REVIEWED_QUALITY_RESOLUTIONS = {
    "english-den": "The nld route retains den through the English branch; the unchanged fox-den document matches.",
    "code-var": "The tur route retains var through the English branch; the unchanged technical document matches.",
}


# Native catalog inspection also runs against the extracted package. Keep this
# query shared so a second runner cannot omit explicit installation defaults.
DEFAULT_OBJECTS_QUERY = """SELECT json_build_object(
    'parser',(SELECT json_build_object('schema',n.nspname,'name',p.prsname) FROM pg_ts_config c JOIN pg_ts_parser p ON p.oid=c.cfgparser JOIN pg_namespace n ON n.oid=p.prsnamespace WHERE c.oid='public.charabia'::regconfig),
    'types',(SELECT json_agg(json_build_object('id',tokid,'alias',alias) ORDER BY tokid) FROM ts_token_type('charabia.parser')),
    'dictionaries',(SELECT json_agg(d.dictname ORDER BY d.dictname) FROM pg_ts_dict d JOIN pg_namespace n ON n.oid=d.dictnamespace WHERE n.nspname='charabia'),
    'dictionary_options',(SELECT json_agg(json_build_object('dictionary',d.dictname,'template_schema',tn.nspname,'template',t.tmplname,
        'options',d.dictinitoption) ORDER BY d.dictname)
        FROM pg_ts_dict d JOIN pg_namespace n ON n.oid=d.dictnamespace JOIN pg_ts_template t ON t.oid=d.dicttemplate JOIN pg_namespace tn ON tn.oid=t.tmplnamespace WHERE n.nspname='charabia'),
    'mappings',(SELECT json_agg(json_build_object('type',m.maptokentype,'order',m.mapseqno,'dictionary_schema',n.nspname,'dictionary',d.dictname) ORDER BY m.maptokentype,m.mapseqno) FROM pg_ts_config_map m JOIN pg_ts_dict d ON d.oid=m.mapdict JOIN pg_namespace n ON n.oid=d.dictnamespace WHERE m.mapcfg='public.charabia'::regconfig));"""


def language_capabilities():
    """Independent release expectations, ordered by the public token type IDs."""
    return [{"language": alias, "script": LANGUAGE_SCRIPTS[alias],
             "has_stopwords": alias != "und", "stemmer": LANGUAGE_STEMMERS.get(alias),
             "english_eligible": alias in ENGLISH_ELIGIBLE,
             "defaults": {"discard": alias != "und",
                          "stem": alias in LANGUAGE_STEMMERS or alias in ENGLISH_ELIGIBLE,
                          "normalize": True, "filter": False, "english": alias in ENGLISH_ELIGIBLE}}
            for alias in LANGUAGE_CODES]


def symbol_identity():
    """Independent static rules and object expectations for ASCII symbols."""
    return {"rule_revision":"ascii-atomic-full-or-component-bundle-1", "dictionary_count":5,
            "ordinary_ascii_delimiters":["<",">","="],
            "types":[{"id":index,"kind":kind,"defaults":{"discard":True,"stem":True,"normalize":True}}
                     for index,kind in enumerate(("url","host","email","file","numword"),19)],
            "limits":{"units":32,"lexemes":66,"bytes":16384},
            "dependencies":{"linkify":"0.11.0","iri-string":"0.7.14"}}


def default_catalog():
    """Expected standard objects, independent of runtime installation output."""
    symbols=symbol_identity()["types"]
    options=[{"dictionary":row["language"],"template_schema":"charabia","template":"template",
              "options":", ".join(f"{key} = '{value}'" for key,value in
                 {"language":row["language"],**{key:str(value).lower() for key,value in row["defaults"].items()}}.items())}
             for row in language_capabilities()]
    options += [{"dictionary":row["kind"],"template_schema":"charabia","template":"symbol_template",
                 "options":f"kind = '{row['kind']}', discard = 'true', stem = 'true', normalize = 'true'"} for row in symbols]
    mappings=[{"type":index,"order":1,"dictionary_schema":"charabia","dictionary":alias}
              for index,alias in enumerate(LANGUAGE_CODES,1)]
    mappings += [{"type":row["id"],"order":1,"dictionary_schema":"charabia","dictionary":row["kind"]} for row in symbols]
    return {"parser":{"schema":"charabia","name":"parser"},
            "types":[{"id":index,"alias":alias} for index,alias in enumerate((*LANGUAGE_CODES,"blank","display_only"),1)] +
                    [{"id":row["id"],"alias":row["kind"]} for row in symbols],
            "dictionaries":sorted((*LANGUAGE_CODES,*(row["kind"] for row in symbols))),
            "dictionary_options":sorted(options,key=lambda row:row["dictionary"]),"mappings":mappings}


def require_default_objects(document, info):
    """Check all installed types, mappings, templates and explicit defaults."""
    require_static_info(info,{})
    expected=default_catalog()
    if not isinstance(document,dict) or set(document)!=set(expected):
        raise ValueError("Missing default Parser/Dictionary/Configuration catalog evidence")
    for key,message in (("parser","Default Configuration must use charabia.parser"),
                        ("types","Installed Parser types differ from the 23 ordered types"),
                        ("dictionaries","Expected exactly the 21 default dictionaries"),
                        ("mappings","Default mappings must use language and symbol dictionaries, blank/display unmapped")):
        if not same_json(document[key],expected[key]):raise ValueError(message)
    def parse_options(raw):
        if not isinstance(raw,str):raise ValueError("Missing Dictionary options")
        options={}
        for entry in raw.split(","):
            match=re.fullmatch(r"\s*(?:\"([a-z]+)\"|([a-z]+))\s*=\s*'([a-z]+)'\s*",entry)
            if match is None:raise ValueError("Invalid explicit default Dictionary option")
            name=match[1] or match[2]
            if name in options:raise ValueError("Duplicate explicit default Dictionary option")
            options[name]=match[3]
        return options
    rows=document["dictionary_options"]
    if not isinstance(rows,list) or len(rows)!=21:raise ValueError("Missing default Dictionary option rows")
    for actual,wanted in zip(rows,expected["dictionary_options"]):
        if not isinstance(actual,dict) or set(actual)!=set(wanted):raise ValueError("Invalid default Dictionary row")
        if any(actual[key]!=wanted[key] for key in ("dictionary","template_schema","template")):
            raise ValueError("Default Dictionary template or identity mismatch")
        if parse_options(actual["options"])!=parse_options(wanted["options"]):
            raise ValueError("Default Dictionary options differ from explicit defaults")


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
    if info["extension"] != RELEASE_VERSION or type(info["language_count"]) is not int or info["language_count"] != len(LANGUAGE_CODES):
        raise ValueError("analysis_info requires release 0.3.0 and 16 language dictionaries")
    if info["analysis_revision"] != ANALYSIS_REVISION:
        raise ValueError("analysis_info requires the current analysis_revision")
    for field in ("cargo_lock_sha256", "resource_manifest_sha256", "resource_fingerprint"):
        value = info[field]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"analysis_info contains an invalid {field}")
    if not same_json(info["symbols"], symbol_identity()):
        raise ValueError("Symbol types, defaults, dependencies or limits differ from the fixed contract")
    languages = info["languages"]
    if not isinstance(languages, list) or len(languages) != len(LANGUAGE_CODES):
        raise ValueError("analysis_info requires exactly 16 language capability rows")
    aliases = set()
    for row in languages:
        if not isinstance(row, dict) or set(row) != {"language", "script", "has_stopwords", "stemmer", "english_eligible", "defaults"}:
            raise ValueError("Invalid language capability row")
        alias = row["language"]
        if not isinstance(alias, str) or len(alias) != 3 or not alias.isascii() or not alias.islower() or not alias.isalpha() or alias in aliases:
            raise ValueError("Language aliases must be unique three-letter lowercase codes")
        aliases.add(alias)
        if type(row["has_stopwords"]) is not bool or type(row["english_eligible"]) is not bool or not isinstance(row["script"], str) or not row["script"]:
            raise ValueError("Invalid language script or stopword capability")
        stemmer = row["stemmer"]
        if stemmer is not None and (not isinstance(stemmer, str) or not stemmer):
            raise ValueError("Invalid stemmer capability")
        defaults = row["defaults"]
        wanted = {"discard": row["has_stopwords"], "stem": stemmer is not None or row["english_eligible"], "normalize": True, "filter": False, "english": row["english_eligible"]}
        if not same_json(defaults, wanted):
            raise ValueError(f"Invalid defaults for {alias}")
    if not same_json(languages, language_capabilities()):
        raise ValueError("Language order or capability coverage differs from the fixed 16/15/10/8 contract")


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
        if case_id in REVIEWED_QUALITY_RESOLUTIONS:
            unchanged=all(same_json(case[field],frozen[field]) for field in QUALITY_FIELDS if field!="actual")
            case["status"]="passed" if unchanged and same_json(actual,expected) else "regression"
            case["resolution"]={"analysis_revision":ANALYSIS_REVISION,"basis":REVIEWED_QUALITY_RESOLUTIONS[case_id],"tests":["tests/bm25.py","tests/chain.py","src/analysis.rs"]}
        else:
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
        if not same_json(case.get("resolution"),checked.get("resolution")):
            raise ValueError(f"Quality case {case['id']} has invalid resolution evidence")
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
    costs=report.get("symbol_quality_costs")
    if not isinstance(costs,list) or not any(isinstance(item,dict) and item.get("input")=="100 ms" and item.get("query")=="100ms" and item.get("domain_expected") is False and item.get("native_actual") is True for item in costs):
        raise ValueError("Missing explicit non-PASS symbol precision-cost evidence")
    if report["status"] != verified_status(quality):
        raise ValueError("Quality failures and report status disagree")
    if not report.get("analysis_info_verified") or not report.get("fts_callback_abi_verified"):
        raise ValueError("Runtime identity and FTS ABI were not verified")
    require_static_info(report.get("analysis_info"), {"extension": RELEASE_VERSION})
    if not report.get("public_api_abi_verified") or not report.get("default_objects_verified"):
        raise ValueError("Public diagnostic ABI and default text search objects were not verified")
    require_default_objects(report.get("default_objects"), report["analysis_info"])
    require_recovery_report(report.get("recovery"), report.get("loaded_library_sha256"))
    if not report.get("pg_textsearch", {}).get("loaded_backend_verified"):
        raise ValueError("The actual pg_textsearch backend library was not verified")
    commands = {item["label"]: item for item in report.get("commands", [])}
    for label in ("unit", "fmt", "clippy", "schema", "package", "fts-callback-abi", "public-api-abi", "default-objects", "analysis-info", "documentation-examples", "runner-contract", "word-generator-contract", "resource-verification", "normal-cargo-tree"):
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
