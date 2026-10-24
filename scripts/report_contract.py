"""Shared verifier and delivery contract; no environment is started on import."""
import hashlib
from functools import lru_cache
import json
from pathlib import Path
import re

FORMAT = 3
PROVENANCE_FORMAT = 2
MODES = ("linguistic", "structured")
RELEASE_VERSION = "0.4.0"
ANALYSIS_REVISION = "route16-dual-mode-1"
LANGUAGE_CODES = ("eng", "deu", "nld", "rus", "spa", "fra", "por", "ita", "ara", "tur", "cmn", "jpn", "kor", "vie", "tha", "und")
ENGLISH_ELIGIBLE = frozenset(("deu", "nld", "spa", "fra", "por", "ita", "tur", "vie"))
LANGUAGE_SCRIPTS = dict(zip(LANGUAGE_CODES, ("Latin", "Latin", "Latin", "Cyrillic", "Latin", "Latin", "Latin", "Latin", "Arabic", "Latin", "Cj", "Cj", "Hangul", "Latin", "Thai", "Other")))
LANGUAGE_STEMMERS = dict(zip(LANGUAGE_CODES[:10], ("English", "German", "Dutch", "Russian", "Spanish", "French", "Portuguese", "Italian", "Arabic", "Turkish")))
SUITES = ("unit", "smoke", "native", "structured", "chain", "bm25", "headline", "config", "lifecycle", "errors", "stress", "recovery", "benchmark")
MAC_DEFAULT = ("unit", "native", "structured", "chain", "headline", "config", "lifecycle", "errors")
ARCHIVE_SUITES = ("smoke", "structured", "chain", "headline", "config", "recovery")
STATIC_INFO_FIELDS = frozenset(("extension", "pgrx", "charabia", "target", "cargo_lock_sha256", "resource_manifest_sha256", "analysis_revision", "resource_fingerprint", "language_count", "languages", "structured_tokens", "resources", "modes", "configurations", "default_configuration"))
VERIFIED = ("verified", "verified-with-known-quality-failures", "verified-with-quality-warnings")
QUALITY_KNOWN_REVIEW = {"file":"tests/fixtures/quality-known.json","sha256":"0973fee0fedcc90cb7021521b3afb2c862f8ffb219f5bd9ceba41bee406f2566"}
QUALITY_FIELDS = ("input", "query", "config", "expected", "actual")
REQUIRED_QUALITY_CASES = frozenset(("english-den", "code-var", "english-short-phrase"))
# Reviewed against the unchanged domain inputs, real native vectors and BM25
# candidates of the route16 implementation. This closes failures, never adds one.
REVIEWED_QUALITY_RESOLUTIONS = {
    "english-den": "The nld route retains den through the English branch; the unchanged fox-den document matches.",
    "code-var": "The tur route retains var through the English branch; the unchanged technical document matches.",
}


# Unchanged warnings introduced in v0.3.0: exact reviewed records only, never a name pattern.
QUALITY_WARNING_REVIEW = {
    "file": "tests/fixtures/quality-warnings-original.json",
    "sha256": "ae335da2f89aae7eb10e59ab86aaf3f8233488cddafa06371b6ae9a2d8243b22",
    "source_run": "20261023T221121Z-73050-amd64-inner",
}
REVIEWED_QUALITY_WARNINGS = frozenset((
    'bm25-domain-surface_running',
    'bm25-domain-french_accents',
    'bm25-domain-swedish_boundary',
    'bm25-domain-bulk-on-force_generic_plan-surface_running',
    'bm25-domain-bulk-on-force_generic_plan-french_accents',
    'bm25-domain-bulk-on-force_generic_plan-swedish_boundary',
    'bm25-domain-bulk-off-force_custom_plan-surface_running',
    'bm25-domain-bulk-off-force_custom_plan-french_accents',
    'bm25-domain-bulk-off-force_custom_plan-swedish_boundary',
    'bm25-domain-bulk-off-force_generic_plan-surface_running',
    'bm25-domain-bulk-off-force_generic_plan-french_accents',
    'bm25-domain-bulk-off-force_generic_plan-swedish_boundary',
    'english-short-phrase-no-stop-bag',
    'german-die-precision',
    'bm25-topk-domain',
    'bm25-domain-insert-on-force_custom_plan-surface_running',
    'bm25-domain-insert-on-force_generic_plan-surface_running',
    'bm25-domain-insert-off-force_custom_plan-surface_running',
    'bm25-domain-insert-off-force_generic_plan-surface_running',
    'bm25-domain-update-on-force_custom_plan-surface_running',
    'bm25-domain-update-on-force_generic_plan-surface_running',
    'bm25-domain-update-off-force_custom_plan-surface_running',
    'bm25-domain-update-off-force_generic_plan-surface_running',
    'bm25-domain-delete-on-force_custom_plan-surface_running',
    'bm25-domain-delete-on-force_generic_plan-surface_running',
    'bm25-domain-delete-off-force_custom_plan-surface_running',
    'bm25-domain-delete-off-force_generic_plan-surface_running',
    'bm25-domain-vacuum-on-force_custom_plan-surface_running',
    'bm25-domain-vacuum-on-force_generic_plan-surface_running',
    'bm25-domain-vacuum-off-force_custom_plan-surface_running',
    'bm25-domain-vacuum-off-force_generic_plan-surface_running',
    'bm25-domain-spill-on-force_custom_plan-surface_running',
    'bm25-domain-spill-on-force_generic_plan-surface_running',
    'bm25-domain-spill-off-force_custom_plan-surface_running',
    'bm25-domain-spill-off-force_generic_plan-surface_running',
    'bm25-domain-post-spill-insert-on-force_custom_plan-surface_running',
    'bm25-domain-post-spill-insert-on-force_generic_plan-surface_running',
    'bm25-domain-post-spill-insert-off-force_custom_plan-surface_running',
    'bm25-domain-post-spill-insert-off-force_generic_plan-surface_running',
    'bm25-domain-restart-on-force_custom_plan-surface_running',
    'bm25-domain-restart-on-force_generic_plan-surface_running',
    'bm25-domain-restart-off-force_custom_plan-surface_running',
    'bm25-domain-restart-off-force_generic_plan-surface_running',
    'bm25-domain-reindex-on-force_custom_plan-surface_running',
    'bm25-domain-reindex-on-force_generic_plan-surface_running',
    'bm25-domain-reindex-off-force_custom_plan-surface_running',
    'bm25-domain-reindex-off-force_generic_plan-surface_running',
))

# Native catalog inspection also runs against the extracted package. Keep this
# query shared so a second runner cannot omit explicit installation defaults.
DEFAULT_OBJECTS_QUERY = """SELECT json_build_object(
 'parsers',(SELECT json_agg(json_build_object('schema',n.nspname,'name',p.prsname,
   'types',(SELECT json_agg(json_build_object('id',tokid,'alias',alias) ORDER BY tokid) FROM ts_token_type(p.oid))) ORDER BY p.prsname)
   FROM pg_ts_parser p JOIN pg_namespace n ON n.oid=p.prsnamespace WHERE n.nspname='charabia'),
 'templates',(SELECT json_agg(t.tmplname ORDER BY t.tmplname) FROM pg_ts_template t JOIN pg_namespace n ON n.oid=t.tmplnamespace WHERE n.nspname='charabia'),
 'configurations',(SELECT json_agg(json_build_object('name','public.'||c.cfgname,
   'parser',json_build_object('schema',pn.nspname,'name',p.prsname),
   'mappings',(SELECT json_agg(json_build_object('type',m.maptokentype,'order',m.mapseqno,'dictionary_schema',dn.nspname,'dictionary',d.dictname) ORDER BY m.maptokentype,m.mapseqno)
    FROM pg_ts_config_map m JOIN pg_ts_dict d ON d.oid=m.mapdict JOIN pg_namespace dn ON dn.oid=d.dictnamespace WHERE m.mapcfg=c.oid)) ORDER BY c.cfgname)
   FROM pg_ts_config c JOIN pg_namespace n ON n.oid=c.cfgnamespace JOIN pg_ts_parser p ON p.oid=c.cfgparser JOIN pg_namespace pn ON pn.oid=p.prsnamespace
   WHERE n.nspname='public' AND c.cfgname IN ('charabia','charabia_linguistic','charabia_structured')),
 'dictionaries',(SELECT json_agg(d.dictname ORDER BY d.dictname) FROM pg_ts_dict d JOIN pg_namespace n ON n.oid=d.dictnamespace WHERE n.nspname='charabia'),
 'dictionary_options',(SELECT json_agg(json_build_object('dictionary',d.dictname,'template_schema',tn.nspname,'template',t.tmplname,'options',d.dictinitoption) ORDER BY d.dictname)
   FROM pg_ts_dict d JOIN pg_namespace n ON n.oid=d.dictnamespace JOIN pg_ts_template t ON t.oid=d.dicttemplate JOIN pg_namespace tn ON tn.oid=t.tmplnamespace WHERE n.nspname='charabia'));"""



def language_capabilities():
    """Independent release expectations, ordered by the public token type IDs."""
    return [{"language": alias, "script": LANGUAGE_SCRIPTS[alias],
             "has_stopwords": alias != "und", "stemmer": LANGUAGE_STEMMERS.get(alias),
             "english_eligible": alias in ENGLISH_ELIGIBLE,
             "defaults": {"discard": alias != "und",
                          "stem": alias in LANGUAGE_STEMMERS or alias in ENGLISH_ELIGIBLE,
                          "normalize": True, "filter": False, "english": alias in ENGLISH_ELIGIBLE}}
            for alias in LANGUAGE_CODES]


def structured_token_identity():
    """Independent static rules and object expectations for structured ASCII tokens."""
    return {"rule_revision":"ascii-atomic-full-or-component-bundle-1", "dictionary_count":5,
            "ordinary_ascii_delimiters":["<",">","="],
            "types":[{"id":index,"kind":kind,"defaults":{"discard":True,"stem":True,"normalize":True}}
                     for index,kind in enumerate(("url","host","email","file","numword"),19)],
            "limits":{"units":32,"lexemes":66,"bytes":16384},
            "dependencies":{"linkify":"0.11.0","iri-string":"0.7.14"}}


def mode_identity():
    ordinary=[{"id":index,"alias":alias} for index,alias in enumerate((*LANGUAGE_CODES,"blank","display_only"),1)]
    structured=[{"id":row["id"],"alias":row["kind"]} for row in structured_token_identity()["types"]]
    return [{"mode":mode,"parser":f"charabia.{mode}_parser","types":ordinary+(structured if mode=="structured" else [])} for mode in MODES]


def configuration_identity():
    return [{"name":"public.charabia","mode":"linguistic"},
            {"name":"public.charabia_linguistic","mode":"linguistic"},
            {"name":"public.charabia_structured","mode":"structured"}]


DELIVERY_ANALYSIS_FIELDS = ("analysis_revision","modes","configurations","default_configuration")


PUBLIC_API_QUERY = "SET search_path=pg_catalog;SELECT json_agg(json_build_object('name',p.proname,\n            'arguments',oidvectortypes(p.proargtypes),'returns',format_type(p.prorettype,NULL),\n            'strict',p.proisstrict,'configuration',p.proconfig,'default_count',p.pronargdefaults,'defaults',pg_get_expr(p.proargdefaults,0),'security_definer',p.prosecdef,'volatility',p.provolatile,'parallel',p.proparallel,\n            'columns',(SELECT json_agg(json_build_object('name',p.proargnames[i],'type',format_type(p.proallargtypes[i],NULL)) ORDER BY i) FROM generate_subscripts(p.proallargtypes,1) i WHERE p.proargmodes[i]='t')) ORDER BY p.proname)\n            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace\n            WHERE n.nspname='charabia' AND p.proname IN ('analysis_info','analyze','query_any','create_config','alter_config','drop_config');"


def public_api_abi():
    columns=[{"name":name,"type":"bigint" if name in ("byte_start","byte_end","char_start","char_end") else "text"} for name in ("raw","kind","script","charabia_language","language","language_source","byte_start","byte_end","char_start","char_end","token_type")]
    return [{"name":name,"arguments":arguments,"returns":returns,"security_definer":False,"volatility":"s","parallel":"r","columns":output,
             "strict":name!="analysis_info","configuration":["search_path=pg_catalog"],"default_count":0 if name=="analysis_info" else 1,
             "defaults":None if name=="analysis_info" else "'public.charabia'::regconfig"}
            for name,arguments,returns,output in (("analysis_info","","jsonb",None),("analyze","text, regconfig","record",columns),("query_any","text, regconfig","tsquery",None))]


def require_public_api_abi(actual):
    if not same_json(actual,public_api_abi()):
        raise ValueError("Public diagnostic ABI, privileges, search_path or defaults differ")


def require_installation_files(stage):
    actual=sorted(path.name for path in Path(stage).glob("pg_charabia--*.sql"))
    if actual!=[f"pg_charabia--{RELEASE_VERSION}.sql"]:
        raise ValueError("Only the fresh current installation SQL can enter staging or delivery")


def require_delivery_analysis_identity(manifest,info):
    require_static_info(info,{})
    if any(not same_json(manifest.get(field),info[field]) for field in DELIVERY_ANALYSIS_FIELDS):
        raise ValueError("Delivery analysis identity differs from the verified modes and configurations")


def default_catalog():
    """Expected native objects from the independent product contract."""
    structured_tokens=structured_token_identity()["types"]
    options=[{"dictionary":row["language"],"template_schema":"charabia","template":"linguistic_template",
              "options":", ".join(f"{key} = '{value}'" for key,value in
                 {"language":row["language"],**{key:str(value).lower() for key,value in row["defaults"].items()}}.items())}
             for row in language_capabilities()]
    options += [{"dictionary":row["kind"],"template_schema":"charabia","template":"structured_template",
                 "options":f"kind = '{row['kind']}', discard = 'true', stem = 'true', normalize = 'true'"} for row in structured_tokens]
    ordinary=[{"type":index,"order":1,"dictionary_schema":"charabia","dictionary":alias} for index,alias in enumerate(LANGUAGE_CODES,1)]
    structured=[{"type":row["id"],"order":1,"dictionary_schema":"charabia","dictionary":row["kind"]} for row in structured_tokens]
    return {"parsers":[{"schema":"charabia","name":f"{row['mode']}_parser","types":row["types"]} for row in mode_identity()],
            "templates":["linguistic_template","structured_template"],
            "configurations":[{"name":row["name"],"parser":{"schema":"charabia","name":f"{row['mode']}_parser"},
                               "mappings":ordinary+(structured if row["mode"]=="structured" else [])} for row in configuration_identity()],
            "dictionaries":sorted((*LANGUAGE_CODES,*(row["kind"] for row in structured_tokens))),
            "dictionary_options":sorted(options,key=lambda row:row["dictionary"])}


def require_default_objects(document, info):
    """Check both Parsers and all three complete Configuration mappings."""
    require_static_info(info,{})
    expected=default_catalog()
    if not isinstance(document,dict) or set(document)!=set(expected):
        raise ValueError("Missing default Parser/Dictionary/Configuration catalog evidence")
    for key in ("parsers","templates","configurations","dictionaries"):
        if not same_json(document[key],expected[key]):
            raise ValueError(f"Default {key} differ from the dual-mode contract")
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
        if not isinstance(child_cleanup, dict) or not all(child_cleanup.get(k) is True for k in ("marker_matches", "clusters_stopped", "directory_removed", "clients_joined")):
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
        raise ValueError("analysis_info must expose exactly the static release identity fields")
    mismatches = {key: {"expected": value, "actual": info.get(key)} for key, value in expected.items() if not same_json(info.get(key), value)}
    if mismatches:
        raise ValueError(f"analysis_info static identity differs from the build inputs: {mismatches}")
    if info["extension"] != RELEASE_VERSION or type(info["language_count"]) is not int or info["language_count"] != len(LANGUAGE_CODES):
        raise ValueError("analysis_info requires the current release and 16 language dictionaries")
    if info["analysis_revision"] != ANALYSIS_REVISION:
        raise ValueError("analysis_info requires the current analysis_revision")
    for field in ("cargo_lock_sha256", "resource_manifest_sha256", "resource_fingerprint"):
        value = info[field]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"analysis_info contains an invalid {field}")
    if not same_json(info["structured_tokens"], structured_token_identity()):
        raise ValueError("Structured types, defaults, dependencies or limits differ from the fixed contract")
    if info["default_configuration"] != "public.charabia" or not same_json(info["modes"],mode_identity()) or not same_json(info["configurations"],configuration_identity()):
        raise ValueError("Analysis modes or default Configurations differ from the product contract")
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
    """Require both target histories and all owned recovery process exits."""
    if not isinstance(recovery,dict) or recovery.get("format")!=FORMAT or recovery.get("status")!="passed" or recovery.get("analysis_revision")!=ANALYSIS_REVISION:
        raise ValueError("Missing successful current recovery evidence")
    if not all(recovery.get("cleanup",{}).get(key) is True for key in ("marker_matches","clusters_stopped","directory_removed")):
        raise ValueError("Recovery child cleanup did not pass")
    if not isinstance(library_hash,str) or len(library_hash)!=64 or any(c not in "0123456789abcdef" for c in library_hash) or recovery.get("loaded_library_sha256")!=library_hash:
        raise ValueError("Recovery did not load the currently verified extension library")
    subreaper=recovery.get("child_subreaper",{})
    if subreaper.get("enabled") is not True or subreaper.get("current")!=1 or [row.get("option") for row in subreaper.get("calls",[])]!=[37,36,37] or any(row.get("result")!=0 or row.get("errno")!=0 for row in subreaper.get("calls",[])):
        raise ValueError("Recovery child subreaper was not verified")
    proofs=recovery.get("cluster_exit_proofs")
    if not isinstance(proofs,list) or len({row.get("cluster") for row in proofs})<2 or any(
        row.get("status_exit")!=3 or row.get("pid_file_absent") is not True or row.get("remaining_owned_pids")!=[] or not row.get("processes") for row in proofs):
        raise ValueError("Recovery cluster process exit proofs are incomplete")
    if recovery["cleanup"].get("clients_joined") is not True:
        raise ValueError("Recovery clients were not joined")
    clients=recovery.get("client_exit_proofs",[])
    commands=recovery.get("commands",[])
    if not clients or len(clients)!=len(commands) or any(
        proof.get("label")!=command.get("label") or proof.get("argv")!=command.get("argv") or
        proof.get("signal_handoff_registered") is not True or
        proof.get("direct_waited") is not True or proof.get("group_empty") is not True or
        proof.get("cleanup_complete") is not True or proof.get("remaining_owned_pids")!=[] or
        proof.get("errors")!=[] or proof.get("direct_exit_code")!=command.get("exit")
        for proof,command in zip(clients,commands)):
        raise ValueError("Recovery client process exit proofs are incomplete")
    if not recovery.get("checks") or any(row.get("ok") is not True for row in recovery["checks"]):
        raise ValueError("Recovery assertions did not all pass")
    modes=recovery.get("mode_evidence",{})
    required_linguistic={"gin_linguistic_literal","gin_stored_linguistic_literal","bm25_linguistic_literal",
                         "gin_linguistic_missing_component","bm25_linguistic_missing_component","gin_linguistic_phrase",
                         "gin_linguistic_stem","gin_stored_linguistic_stem","bm25_linguistic_stem"}
    for stage,target in (("target",True),("crash",False),("restored",True)):
        rows=modes.get(stage,{})
        if set(rows)!=set(MODES):raise ValueError("Recovery mode evidence is incomplete")
        for mode in MODES:
            row=rows[mode]
            if row.get("config")!=f"lab.{mode}_cfg" or row.get("parser")!=f"charabia.{mode}_parser" or row.get("target_state") is not target or row.get("catalog",{}).get("stored_consistent") is not True:
                raise ValueError("Recovery mode identity or target state differs")
            kinds=row.get("query_kinds",[])
            if not isinstance(kinds,list) or len(set(kinds))!=len(kinds):raise ValueError("Invalid recovery query coverage")
            if mode=="linguistic":
                if not required_linguistic<=set(kinds) or row.get("headline")!="alpha foo_<b>bar</b>.rs beta" or any(row.get("positions",{}).get(term)!=[position] for term,position in (("foo",2),("bar",3),("rs",4),("beta",5))):
                    raise ValueError("Recovery linguistic positions, headline or index coverage is incomplete")
            elif row.get("whole_source_headline_checked") is not True or row.get("atomic_positions_checked") is not target or not all(any(kind.startswith(prefix) for kind in kinds) for prefix in ("gin_structured_","gin_stored_structured_","bm25_structured_")):
                raise ValueError("Recovery structured position, headline or index coverage is incomplete")



def frozen_quality_cases():
    # Read only the reviewed source fixture, never a signature from run evidence.
    path = Path(__file__).resolve().parent.parent / QUALITY_KNOWN_REVIEW["file"]
    contents=path.read_bytes()
    if hashlib.sha256(contents).hexdigest()!=QUALITY_KNOWN_REVIEW["sha256"]:
        raise ValueError("Reviewed frozen quality evidence changed")
    document = json.loads(contents)
    cases = document.get("cases")
    if document.get("format") != PROVENANCE_FORMAT or not isinstance(cases, dict) or set(cases) != REQUIRED_QUALITY_CASES:
        raise ValueError("The reviewed quality fixture must contain exactly the three frozen cases")
    for case in cases.values():
        if not isinstance(case, dict) or set(case) != set(QUALITY_FIELDS) or case["expected"] is not True or case["actual"] is not False:
            raise ValueError("Invalid frozen domain quality signature")
    return cases


def same_json(left, right):
    return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(right, sort_keys=True, separators=(",", ":"), allow_nan=False)


def reviewed_quality_warnings():
    """Load the immutable original report and select explicitly reviewed IDs."""
    path = Path(__file__).resolve().parent.parent / QUALITY_WARNING_REVIEW["file"]
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != QUALITY_WARNING_REVIEW["sha256"]:
        raise ValueError("Reviewed quality warning evidence changed")
    document = json.loads(contents)
    if document.get("format") != PROVENANCE_FORMAT or not isinstance(document.get("cases"), list):
        raise ValueError("Invalid original quality warning report")
    selected = {}
    for row in document["cases"]:
        if row.get("id") not in REVIEWED_QUALITY_WARNINGS:
            continue
        if row["id"] in selected or row.get("status") != "regression":
            raise ValueError("Invalid reviewed quality warning record")
        fields = {key: row[key] for key in QUALITY_FIELDS}
        if same_json(fields["expected"], fields["actual"]):
            raise ValueError("A reviewed warning must retain its original quality difference")
        if isinstance(fields["expected"], list) and (
            not isinstance(fields["actual"], list) or not set(fields["expected"]).issubset(fields["actual"])
        ):
            raise ValueError("Complete candidate loss cannot be a quality warning")
        selected[row["id"]] = fields
    if set(selected) != REVIEWED_QUALITY_WARNINGS:
        raise ValueError("Missing reviewed quality warning records")
    return selected


# Added only after per-mode source review and current-library observations.
MODE_QUALITY_REVIEW = {"file":"tests/fixtures/quality-mode-review.json", "sha256":"1f9d5242e3e9b503aaaf1c04c73ef0c3883089f4e36e9dbdf56fd9f44cfe2002"}
DEFAULT_CONFIGS = {"linguistic":"public.charabia", "structured":"public.charabia_structured"}


def mode_quality_reviews():
    """No historical exception transfers to a new mode without an exact review."""
    path=Path(__file__).resolve().parent.parent/MODE_QUALITY_REVIEW["file"]
    contents=path.read_bytes()
    if hashlib.sha256(contents).hexdigest()!=MODE_QUALITY_REVIEW["sha256"]:
        raise ValueError("Mode quality review evidence changed")
    document=json.loads(contents)
    if document.get("analysis_revision")!=ANALYSIS_REVISION or document.get("format")!=FORMAT:
        raise ValueError("Mode quality review has an outdated identity")
    sources={**frozen_quality_cases(),**reviewed_quality_warnings()}
    result={}
    for row in document["cases"]:
        key=(row["mode"],row["id"])
        source=sources.get(row["id"])
        if key in result or row["mode"] not in MODES or source is None or not row.get("basis"):
            raise ValueError("Invalid or duplicate mode quality review")
        source_hash=hashlib.sha256(json.dumps(source,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        if row["source_sha256"]!=source_hash:
            raise ValueError("Mode quality review does not identify the original signature")
        if row["id"] in ("english-short-phrase","german-die-precision","bm25-topk-domain") and not isinstance(row.get("observed_signature"),dict):
            raise ValueError("Boolean quality failures require an exact observed signature")
        result[key]=row
    return result


@lru_cache(maxsize=1)
def _quality_source_json():
    # Cache immutable signatures, not mutable case dictionaries. Every emitted
    # case receives fresh decoded values. Source hashes are checked on first use.
    return ({key:json.dumps(value,sort_keys=True,separators=(",",":")) for key,value in frozen_quality_cases().items()},
            {key:json.dumps(value,sort_keys=True,separators=(",",":")) for key,value in reviewed_quality_warnings().items()})


@lru_cache(maxsize=1)
def _mode_review_json():
    return {key:json.dumps(value,sort_keys=True,separators=(",",":")) for key,value in mode_quality_reviews().items()}


def quality_case(case_id, *, input, query, config, expected, actual, mode, analysis_revision=ANALYSIS_REVISION, known_signature=None, observed_signature=None):
    if mode not in MODES or analysis_revision!=ANALYSIS_REVISION:
        raise ValueError("Quality case requires an explicit current analysis mode and revision")
    installed={row["name"]:row["mode"] for row in configuration_identity()}
    if isinstance(config,str) and config in installed and installed[config]!=mode:
        raise ValueError("Quality configuration conflicts with its analysis mode")
    case=dict(id=case_id,input=input,query=query,config=config,expected=expected,actual=actual,
              mode=mode,analysis_revision=analysis_revision)
    if not isinstance(case_id,str) or not case_id:
        raise ValueError("A quality case needs a stable nonempty id")
    if observed_signature is not None:
        case["observed_signature"]=observed_signature
    if not isinstance(config,str) or not config:
        case["status"]="regression"
        return case
    frozen_sources,warning_sources=_quality_source_json()
    frozen=json.loads(frozen_sources[case_id]) if case_id in frozen_sources else None
    reviewed=json.loads(warning_sources[case_id]) if case_id in warning_sources else None
    if frozen is not None:
        if not same_json(known_signature,frozen):
            raise ValueError(f"Quality case {case_id} requires its reviewed frozen signature")
        case["known_signature"]=frozen
        if case_id in REVIEWED_QUALITY_RESOLUTIONS:
            unchanged=all(same_json(case[field],frozen[field]) for field in QUALITY_FIELDS if field not in ("actual","config"))
            case["status"]="passed" if unchanged and config==DEFAULT_CONFIGS[mode] and same_json(actual,expected) else "regression"
            case["resolution"]={"analysis_revision":ANALYSIS_REVISION,"basis":REVIEWED_QUALITY_RESOLUTIONS[case_id],"tests":["tests/bm25.py","tests/chain.py","src/analysis.rs"]}
            return case
    elif known_signature is not None:
        raise ValueError(f"Quality case {case_id} is not an eligible known failure")
    source=frozen or reviewed
    if source is not None:
        approval_json=_mode_review_json().get((mode,case_id))
        approval=json.loads(approval_json) if approval_json is not None else None
        observation_matches=approval is not None and ("observed_signature" not in approval or same_json(observed_signature,approval["observed_signature"]))
        exact=observation_matches and config==approval["config"] and all(
            same_json(case[field],source[field]) for field in QUALITY_FIELDS if field!="config")
        case["status"]=("known-failure" if frozen else "warning") if exact else "regression"
        if exact:case["mode_review"]={"source_sha256":approval["source_sha256"],"basis":approval["basis"]}
    else:
        case["status"]="passed" if same_json(expected,actual) else "regression"
    return case


def validate_quality(document):
    if not isinstance(document,dict) or document.get("format")!=FORMAT or not isinstance(document.get("cases"),list):
        raise ValueError("Invalid quality-results.json format")
    seen=set()
    statuses=("passed","known-failure","warning","regression")
    counts={key:0 for key in statuses}
    counts_by_mode={mode:{key:0 for key in statuses} for mode in MODES}
    for case in document["cases"]:
        if not isinstance(case,dict) or not isinstance(case.get("id"),str) or (case.get("mode"),case.get("id")) in seen:
            raise ValueError("Invalid or duplicate quality case")
        try:
            checked=quality_case(case["id"],**{key:case[key] for key in QUALITY_FIELDS},mode=case["mode"],
                                 analysis_revision=case["analysis_revision"],known_signature=case.get("known_signature"),observed_signature=case.get("observed_signature"))
        except (KeyError,TypeError) as exc:
            raise ValueError("Incomplete quality case") from exc
        for field in ("resolution","mode_review"):
            if not same_json(case.get(field),checked.get(field)):
                raise ValueError(f"Quality case {case['id']} has invalid {field} evidence")
        if case.get("status")!=checked["status"]:
            raise ValueError(f"Quality case {case['id']} has an invalid status")
        seen.add((case["mode"],case["id"]))
        counts[case["status"]]+=1
        counts_by_mode[case["mode"]][case["status"]]+=1
    def identities(status):
        return [{"mode":case["mode"],"id":case["id"]} for case in document["cases"] if case["status"]==status]
    summary={"format":FORMAT,"counts":counts,"counts_by_mode":counts_by_mode,"cases":document["cases"],
             "known_failures":identities("known-failure"),"warnings":identities("warning"),
             "warning_review":dict(QUALITY_WARNING_REVIEW),"mode_review":dict(MODE_QUALITY_REVIEW),
             "regressions":identities("regression")}
    for key in ("counts","counts_by_mode","known_failures","warnings","warning_review","mode_review","regressions"):
        if key in document and not same_json(document[key],summary[key]):
            raise ValueError(f"Quality summary {key} disagrees with its cases or reviewed policy")
    return summary


def read_quality(path):
    path=Path(path)
    return validate_quality(json.loads(path.read_text()) if path.is_file() else {"format":FORMAT,"cases":[]})


def required_bm25_case_ids():
    # Product/domain case coverage, independent of the runtime registry.
    bulk=("chinese","english_case","mixed_or_terms","surface_running","stem_run","surface_cats",
          "french_accents","french_surface","turkish_surface","turkish_casing","swedish_accents",
          "swedish_boundary","swedish_fold","korean","cyrillic","japanese","latin_normalization",
          "identifier","url","mixed_script","unknown","expression-boundary","array-boundary",
          "frequency-cap","position-cap","frequency-ranking")
    phases=("insert","update","delete","vacuum","spill","post-spill-insert","restart","reindex")
    plans=("force_custom_plan","force_generic_plan")
    cases=set()
    for cache in ("on","off"):
        for plan in plans:
            for name in bulk:
                cases.add("bm25-domain-"+name if (cache,plan)==("on","force_custom_plan") else f"bm25-domain-bulk-{cache}-{plan}-{name}")
            for phase in phases:
                for name in ("chinese","english_case","surface_running","unknown"):
                    cases.add(f"bm25-domain-{phase}-{cache}-{plan}-{name}")
    cases.update("bm25-analysis-"+name for name in ("empty","whitespace","stop_only"))
    for name in REQUIRED_QUALITY_CASES:
        cases.update((name,name+"-no_stop_docs",name+"-no-stop-bag"))
    cases.update(("german-die-precision","bm25-bag-independent-candidates","bm25-topk-domain"))
    return frozenset(cases)


def require_known_quality_cases(quality):
    seen={(case["mode"],case["id"]) for case in quality["cases"]}
    missing={(mode,case_id) for mode in MODES for case_id in REQUIRED_QUALITY_CASES}-seen
    if missing:raise ValueError(f"Missing frozen domain quality cases: {sorted(missing)}")


def require_bm25_case_coverage(quality):
    seen={(case["mode"],case["id"]) for case in quality["cases"]}
    wanted={(mode,case_id) for mode in MODES for case_id in required_bm25_case_ids()}
    if seen!=wanted:
        raise ValueError(f"Incomplete two-mode BM25 quality coverage: missing={sorted(wanted-seen)}, unexpected={sorted(seen-wanted)}")


def verified_status(quality):
    if quality["regressions"]:return "failed"
    if quality["warnings"]:return "verified-with-quality-warnings"
    return "verified-with-known-quality-failures" if quality["known_failures"] else "verified"


def require_bm25_report(report):
    """Both complete mode reports must agree with the full shared registry."""
    if report.get("format")!=FORMAT or report.get("analysis_revision")!=ANALYSIS_REVISION:
        raise ValueError("BM25 report format or analysis identity differs")
    quality=validate_quality({"format":FORMAT,"cases":report.get("domain_quality")})
    if report.get("mechanism_status")!="passed" or report.get("status")!="passed":
        raise ValueError("BM25 mechanisms did not pass")
    if quality["regressions"] or report.get("quality_status")!=verified_status(quality):
        raise ValueError("BM25 quality status disagrees with its cases")
    if not same_json(report.get("quality"),quality):
        raise ValueError("BM25 quality summary disagrees with its cases")
    require_bm25_case_coverage(quality)
    children=report.get("mode_reports")
    if not isinstance(children,dict) or set(children)!=set(MODES):
        raise ValueError("BM25 mode reports are incomplete")
    combined=[]
    for mode in MODES:
        child=children[mode]
        if child.get("format")!=FORMAT or child.get("mode")!=mode or child.get("config")!=DEFAULT_CONFIGS[mode] or child.get("analysis_revision")!=ANALYSIS_REVISION:
            raise ValueError("BM25 child mode identity differs")
        if child.get("status")!="passed" or child.get("mechanism_status")!="passed" or child.get("schema_removed") is not True:
            raise ValueError("BM25 child mechanisms or cleanup did not pass")
        child_quality=validate_quality({"format":FORMAT,"cases":child.get("domain_quality")})
        if any(case["mode"]!=mode for case in child_quality["cases"]) or not same_json(child.get("quality"),child_quality):
            raise ValueError("BM25 child quality differs from its mode")
        if child.get("quality_status")!=verified_status(child_quality):raise ValueError("BM25 child quality status differs")
        combined.extend(child_quality["cases"])
    if not same_json(combined,quality["cases"]):raise ValueError("BM25 child registries disagree with the aggregate")
    cost=children["structured"].get("structured_tokens",{}).get("precision_cost",{})
    if cost.get("input")!="100 appendix ms" or cost.get("query")!="100ms" or cost.get("domain_expectation_for_structural_equality") is not False or cost.get("actual_native_plain") is not True:
        raise ValueError("BM25 structured precision cost is missing or falsely passed")
    return quality


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
        raise ValueError("A verified format-3 Linux AMD64 report is required")
    if report.get("git_status") != "":
        raise ValueError("A clean tested source checkout is required")
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
    require_bm25_case_coverage(quality)
    bm25_quality = validate_quality(report.get("bm25_quality", {}))
    if not same_json(bm25_quality, quality):
        raise ValueError("BM25 quality cases disagree with the release report")
    require_known_quality_cases(bm25_quality)
    for key in ("counts", "counts_by_mode", "warnings", "warning_review", "mode_review", "known_failures", "regressions"):
        if key not in report["quality"]:
            raise ValueError(f"Missing quality warning disclosure: {key}")
    costs=report.get("structured_token_quality_costs")
    if not isinstance(costs,list) or any(not isinstance(item,dict) or item.get("mode")!="structured" or not item.get("config") for item in costs) or not any(isinstance(item,dict) and item.get("input")=="100 ms" and item.get("query")=="100ms" and item.get("domain_expected") is False and item.get("native_actual") is True for item in costs):
        raise ValueError("Missing explicit non-PASS structured precision-cost evidence")
    if report["status"] != verified_status(quality):
        raise ValueError("Quality failures and report status disagree")
    if not report.get("analysis_info_verified") or not report.get("fts_callback_abi_verified"):
        raise ValueError("Runtime identity and FTS ABI were not verified")
    require_static_info(report.get("analysis_info"), {"extension": RELEASE_VERSION})
    if not report.get("public_api_abi_verified") or not report.get("default_objects_verified"):
        raise ValueError("Public diagnostic ABI and default text search objects were not verified")
    require_public_api_abi(report.get("public_api_abi"))
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
