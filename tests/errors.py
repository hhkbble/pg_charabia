"""Exercise real encoding rejection and supplemental guarded faults."""
import json
import os
import sys
import time
from support import Session, json_query, literal, run, save

results = {"encoding": {}, "faults": []}
database = "pgcb_ascii_" + str(os.getpid())
run(f"CREATE DATABASE {database} TEMPLATE template0 ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C'")
try:
    installation = run("\\set VERBOSITY verbose\nCREATE EXTENSION pg_charabia",database,check=False)
    assert installation.returncode and "0A000" in installation.stderr,installation.stderr
    assert run("SELECT count(*) FROM pg_extension WHERE extname='pg_charabia'",database)=="0"
    results["encoding"]["dictionary_init"] = installation.stderr.strip()
    # Installation is correctly refused by INIT. Register the independent raw
    # parser and diagnostic callbacks to exercise their own encoding preflights.
    run("""
CREATE FUNCTION public.raw_start(internal,integer) RETURNS internal AS 'pg_charabia','parser_start_v1_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION public.raw_gettoken(internal,internal,internal) RETURNS internal AS 'pg_charabia','parser_gettoken_v1_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION public.raw_end(internal) RETURNS void AS 'pg_charabia','parser_end_v1_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION public.raw_lextypes(internal) RETURNS internal AS 'pg_charabia','parser_lextypes_v1_wrapper' LANGUAGE c STRICT;
CREATE TEXT SEARCH PARSER public.raw_parser(START=public.raw_start,GETTOKEN=public.raw_gettoken,END=public.raw_end,LEXTYPES=public.raw_lextypes);
CREATE FUNCTION public.raw_analyze(text) RETURNS TABLE(raw text,lemma text,kind text,script text,language text,byte_start bigint,byte_end bigint,char_start bigint,char_end bigint,route text) AS 'pg_charabia','analyze_v1_wrapper' LANGUAGE c STRICT;
""",database)
    for name, sql in {
        "start": "SELECT * FROM ts_parse('public.raw_parser','hello')",
        "diagnostic": "SELECT * FROM public.raw_analyze('hello')",
    }.items():
        result = run("\\set VERBOSITY verbose\n"+sql, database, check=False)
        assert result.returncode != 0 and "0A000" in result.stderr, result.stderr
        results["encoding"][name] = result.stderr.strip()
finally:
    run(f"DROP DATABASE {database}")

has_hooks = os.environ.get("PGCB_TEST_HOOKS") == "1"
registered = int(run("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='charabia' AND p.proname LIKE 'test_%'"))
assert registered == (2 if has_hooks else 0), registered
assert json_query("SELECT charabia.analysis_info_v1()") ["test_hooks"] == has_hooks

if has_hooks:
    with Session() as session:
        session.value("SELECT to_tsvector('charabia.default_v1','hello world')")
        for point in ("start", "gettoken", "end", "init", "lexize"):
            for mode in ("error", "panic"):
                before = json.loads(session.value("SELECT charabia.test_stats_v1()"))
                session.value(f"SELECT charabia.test_set_fault_v1({literal(point)},{literal(mode)})")
                query = "CREATE TEXT SEARCH DICTIONARY public.fault_dict(TEMPLATE=charabia.normalize_v1,Route='latin')" if point == "init" else "SELECT to_tsvector('charabia.default_v1','hello world')"
                output = session.query(query)
                assert "ERROR:" in output and "injected pg_charabia" in output, output
                assert session.value("SELECT 1") == "1"
                after = json.loads(session.value("SELECT charabia.test_stats_v1()"))
                assert after["parser_created"] == after["parser_dropped"], after
                assert after["dictionary_created"]-before["dictionary_created"] == after["dictionary_dropped"]-before["dictionary_dropped"], (before,after)
                results["faults"].append({"point":point,"mode":mode,"output":output,"before":before,"after":after})
        session.value("SELECT charabia.test_set_fault_v1('gettoken','wait')")
        outcome = []
        import threading
        worker = threading.Thread(target=lambda: outcome.append(session.query("SELECT to_tsvector('charabia.default_v1','cancel after state allocation')")))
        worker.start()
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            if run(f"SELECT application_name FROM pg_stat_activity WHERE pid={session.pid}") == "pgcb_hook_wait":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Fault wait point was not reached")
        assert run(f"SELECT pg_cancel_backend({session.pid})") == "t"
        worker.join(timeout=15)
        assert not worker.is_alive() and outcome and "57014" in outcome[0], outcome
        after = json.loads(session.value("SELECT charabia.test_stats_v1()"))
        assert after["parser_created"] == after["parser_dropped"], after
        assert session.value("SELECT to_tsvector('charabia.default_v1','hello')") == "'hello':1"
        results["cancel"] = {"output":outcome[0],"after":after,"pid":session.pid}
save("errors-results.json",results)
print(json.dumps({"encoding_cases":len(results["encoding"]),"injected_faults":len(results["faults"]),"test_hooks":has_hooks}))
