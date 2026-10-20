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
    with Session(database) as session:
        output=session.query("SELECT * FROM public.raw_analyze(convert_from(decode('ff','hex'),'SQL_ASCII'))")
        results["encoding"]["diagnostic_invalid_utf8"]=output
        save("errors-results.json",results)
        assert "0A000" in output,output
        assert session.value("SELECT 1")=="1"
finally:
    run(f"DROP DATABASE {database}")

has_hooks = os.environ.get("PGCB_TEST_HOOKS") == "1"
registered = int(run("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='charabia' AND p.proname LIKE 'test_%'"))
assert registered == (2 if has_hooks else 0), registered
assert json_query("SELECT charabia.analysis_info_v1()") ["test_hooks"] == has_hooks

def stats(session):
    return json.loads(session.value("SELECT charabia.test_stats_v1()"))


def callback_live(value):
    return value["state_registered"]-value["state_released"]


def registration_failures(session):
    results["registration_faults"]=[]
    for role in ("parser", "dictionary"):
        for point,modes in (("register_alloc",("error","panic","allocation_error")),("register_link",("error","panic"))):
            for mode in modes:
                before=stats(session)
                session.value(f"SELECT charabia.test_set_fault_v1({literal(point)},{literal(mode)})")
                sql=("SELECT to_tsvector('charabia.default_v1',repeat('hello world ',64))" if role=="parser" else
                     "CREATE TEXT SEARCH DICTIONARY public.registration_fault(TEMPLATE=charabia.normalize_v1,Route='latin',StopWords='[\"registrationcanary\"]')")
                output=session.query(sql)
                after=stats(session)
                record={"role":role,"point":point,"mode":mode,"output":output,"before":before,"after":after}
                results["registration_faults"].append(record)
                save("errors-results.json",results)
                expected="invalid memory alloc request size" if mode=="allocation_error" else "injected pg_charabia"
                assert "ERROR:" in output and "XX000" in output and expected in output,record
                assert after[role+"_created"]-before[role+"_created"]==1,record
                assert after[role+"_dropped"]-before[role+"_dropped"]==1,record
                assert after["state_registered"]==before["state_registered"],record
                assert after["state_released"]==before["state_released"],record
                assert after["dictionary_live_fst_bytes"]==before["dictionary_live_fst_bytes"],record
                assert session.value("SELECT to_tsvector('charabia.default_v1','hello')")=="'hello':1"
                final=stats(session)
                assert final["parser_created"]==final["parser_dropped"],record
                assert callback_live(final)==callback_live(before),record
                record["after_context_reset"]=final

    session.value("BEGIN; SAVEPOINT owned_registration")
    before=stats(session)
    session.value("SELECT charabia.test_set_fault_v1('register_alloc','allocation_error')")
    output=session.query("SELECT to_tsvector('charabia.default_v1','savepoint cleanup')")
    assert "XX000" in output and "invalid memory alloc request size" in output,output
    session.value("ROLLBACK TO owned_registration")
    after=stats(session)
    assert after["parser_created"]-before["parser_created"]==after["parser_dropped"]-before["parser_dropped"]==1,(before,after)
    assert callback_live(after)==callback_live(before),(before,after)
    assert session.value("SELECT to_tsvector('charabia.default_v1','hello')")=="'hello':1"
    session.value("COMMIT")
    results["registration_savepoint"]={"output":output,"before":before,"after":after}


def interrupt(session,reason):
    import threading
    session.value("SET application_name='pgcb_errors_ready'")
    session.value("SET statement_timeout='300ms'" if reason=="timeout" else "SET statement_timeout=0")
    before=stats(session)
    session.value("SELECT charabia.test_set_fault_v1('gettoken','wait')")
    outcome=[]
    def query():
        try:
            outcome.append(session.query("SELECT to_tsvector('charabia.default_v1','cancel after state allocation')"))
        except BaseException as error:
            outcome.append(error)
    worker=threading.Thread(target=query,name="pgcb-interrupt-"+reason)
    worker.start()
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if run(f"SELECT application_name FROM pg_stat_activity WHERE pid={session.pid}",timeout=10)=="pgcb_hook_wait":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Fault wait point was not reached")
        if reason=="cancel":
            assert run(f"SELECT pg_cancel_backend({session.pid})",timeout=10)=="t"
        worker.join(timeout=15)
        assert not worker.is_alive() and outcome and isinstance(outcome[0],str),outcome
        assert "57014" in outcome[0],outcome
        assert ("statement timeout" if reason=="timeout" else "due to user request") in outcome[0],outcome
        session.value("SET statement_timeout=0")
        after=stats(session)
        assert after["parser_created"]-before["parser_created"]==after["parser_dropped"]-before["parser_dropped"]==1,(before,after)
        assert callback_live(after)==callback_live(before),(before,after)
        assert session.value("SELECT to_tsvector('charabia.default_v1','hello')")=="'hello':1"
        results[reason]={"output":outcome[0],"before":before,"after":after,"pid":session.pid}
    finally:
        if worker.is_alive():
            run(f"SELECT pg_cancel_backend({session.pid})",check=False,timeout=10)
            worker.join(timeout=10)
        if worker.is_alive():
            run(f"SELECT pg_terminate_backend({session.pid})",check=False,timeout=10)
            worker.join(timeout=10)
        assert not worker.is_alive(),"Owned query thread failed to exit"
        save("errors-results.json",results)


try:
    if has_hooks:
        with Session() as session:
            session.value("SELECT to_tsvector('charabia.default_v1','hello world')")
            for point in ("start", "gettoken", "end", "init", "lexize"):
                for mode in ("error", "panic"):
                    before=stats(session)
                    session.value(f"SELECT charabia.test_set_fault_v1({literal(point)},{literal(mode)})")
                    query="CREATE TEXT SEARCH DICTIONARY public.fault_dict(TEMPLATE=charabia.normalize_v1,Route='latin')" if point=="init" else "SELECT to_tsvector('charabia.default_v1','hello world')"
                    output=session.query(query)
                    after=stats(session)
                    record={"point":point,"mode":mode,"output":output,"before":before,"after":after}
                    results["faults"].append(record)
                    save("errors-results.json",results)
                    assert "ERROR:" in output and "injected pg_charabia" in output,record
                    assert session.value("SELECT 1")=="1"
                    assert after["parser_created"]==after["parser_dropped"],record
                    assert after["dictionary_created"]-before["dictionary_created"]==after["dictionary_dropped"]-before["dictionary_dropped"],record
                    assert callback_live(after)==callback_live(before),record
            registration_failures(session)
            interrupt(session,"cancel")
            interrupt(session,"timeout")
finally:
    save("errors-results.json",results)
print(json.dumps({"encoding_cases":len(results["encoding"]),"injected_faults":len(results["faults"]),"registration_faults":len(results.get("registration_faults",[])),"test_hooks":has_hooks}))
