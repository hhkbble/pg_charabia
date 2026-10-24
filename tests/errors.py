"""Exercise real encoding rejection and supplemental guarded faults."""
import json
import os
import signal
import sys
import time
from support import Session, json_query, literal, native_config_sql, run, save

def interrupted(signum, _frame):
    raise InterruptedError(f"Received signal {signum}")


signal.signal(signal.SIGTERM, interrupted)

results = {"encoding": {}, "faults": []}
database = "pgcb_ascii_" + str(os.getpid())
run(f"CREATE DATABASE {database} TEMPLATE template0 ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C'")
try:
    installation = run("\\set VERBOSITY verbose\nCREATE EXTENSION pg_charabia",database,check=False)
    assert installation.returncode and "0A000" in installation.stderr,installation.stderr
    assert run("SELECT count(*) FROM pg_extension WHERE extname='pg_charabia'",database)=="0"
    results["encoding"]["linguistic_dictionary_init"] = installation.stderr.strip()
    # Installation is correctly refused by INIT. Register the independent raw
    # parser and diagnostic callbacks to exercise their own encoding preflights.
    run("""
CREATE FUNCTION public.raw_start(internal,integer) RETURNS internal AS 'pg_charabia','parser_start_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION public.raw_gettoken(internal,internal,internal) RETURNS internal AS 'pg_charabia','parser_gettoken_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION public.raw_end(internal) RETURNS void AS 'pg_charabia','parser_end_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION public.raw_lextypes(internal) RETURNS internal AS 'pg_charabia','parser_lextypes_wrapper' LANGUAGE c STRICT;
CREATE TEXT SEARCH PARSER public.raw_parser(START=public.raw_start,GETTOKEN=public.raw_gettoken,END=public.raw_end,LEXTYPES=public.raw_lextypes);
CREATE FUNCTION public.raw_analyze(text) RETURNS TABLE(raw text,kind text,script text,charabia_language text,language text,language_source text,byte_start bigint,byte_end bigint,char_start bigint,char_end bigint,token_type text) AS 'pg_charabia','analyze_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION public.raw_query_any(text,oid) RETURNS tsquery AS 'pg_charabia','query_any_wrapper' LANGUAGE c STRICT;
""",database)
    for name, sql in {
        "start": "SELECT * FROM ts_parse('public.raw_parser','hello')",
        "diagnostic": "SELECT * FROM public.raw_analyze('hello')",
        "query_any": "SELECT public.raw_query_any('hello',0::oid)",
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
assert json_query("SELECT charabia.analysis_info()") ["extension"] == "0.3.1"

DOCUMENT = "The children were running through the garden while the teachers were reading books and sharing their happy memories together. These cats were running."
run("CREATE SCHEMA errors_runtime")
run(native_config_sql("errors_runtime.healthy", stem=False, discard=False))
HEALTHY = "SELECT to_tsvector('errors_runtime.healthy'," + literal(DOCUMENT) + ")"
HEADLINE = "SELECT ts_headline('errors_runtime.healthy'," + literal(DOCUMENT) + ",'running'::tsquery,'HighlightAll=true')"
STRUCTURED_HEALTHY = "SELECT to_tsvector('public.charabia','Running100') @@ 'run'::tsquery"
STRUCTURED_HEADLINE = "SELECT ts_headline('public.charabia','alpha Running100 beta','run'::tsquery,'HighlightAll=true')"

# Exercise the direct dictionary entry, independently of START and analyze.
# Oversized ASCII cannot fail UTF8 conversion in core before reaching LEXIZE.
with Session() as direct:
    assert direct.value("SELECT ts_lexize('errors_runtime.healthy_eng','running')") == "{running}"
    oversized = direct.query("SELECT ts_lexize('errors_runtime.healthy_eng',repeat('x',262145))")
    results["direct_lexize"] = {"oversized": oversized}
    save("errors-results.json", results)
    assert "54000" in oversized and "pg_charabia input exceeds 262144 bytes" in oversized, oversized
    malformed = direct.query("SELECT ts_lexize('errors_runtime.healthy_eng',convert_from(decode('ff','hex'),'SQL_ASCII'))")
    results["direct_lexize"]["malformed_utf8"] = malformed
    # PostgreSQL may reject conversion before it invokes LEXIZE. Preserve that
    # attribution rather than claiming core validation exercised our callback.
    results["direct_lexize"]["malformed_rejection"] = "extension" if "invalid UTF8 input" in malformed else "postgresql_conversion"
    save("errors-results.json", results)
    assert "22021" in malformed, malformed
    assert direct.value("SELECT ts_lexize('errors_runtime.healthy_eng','running')") == "{running}"


def stats(session):
    return json.loads(session.value("SELECT charabia.test_stats()"))


def callback_live(value):
    return value["state_registered"]-value["state_released"]


def registration_failures(session):
    results["registration_faults"]=[]
    for role in ("parser", "dictionary", "structured_dictionary"):
        for point,modes in (("register_alloc",("error","panic","allocation_error")),("register_link",("error","panic","allocation_error"))):
            for mode in modes:
                before=stats(session)
                session.value(f"SELECT charabia.test_set_fault({literal(point)},{literal(mode)})")
                sql=(HEALTHY if role=="parser" else
                     "CREATE TEXT SEARCH DICTIONARY public.registration_fault(TEMPLATE=charabia.structured_template,Kind='numword',Discard=true,Stem=true,Normalize=true)" if role=="structured_dictionary" else
                     "CREATE TEXT SEARCH DICTIONARY public.registration_fault(TEMPLATE=charabia.linguistic_template,Language='eng',Discard=true,Stem=false,Normalize=true,Filter=false)")
                output=session.query(sql)
                after=stats(session)
                counter="dictionary" if role=="structured_dictionary" else role
                record={"role":role,"point":point,"mode":mode,"sql":sql,"output":output,"before":before,"after":after}
                results["registration_faults"].append(record)
                save("errors-results.json",results)
                expected="invalid memory alloc request size" if mode=="allocation_error" else "injected pg_charabia"
                assert "ERROR:" in output and "XX000" in output and expected in output,record
                assert after[counter+"_created"]-before[counter+"_created"]==1,record
                assert after[counter+"_dropped"]-before[counter+"_dropped"]==1,record
                assert after["state_registered"]==before["state_registered"],record
                assert after["state_released"]==before["state_released"],record
                assert after["dictionary_live_fst_bytes"]==before["dictionary_live_fst_bytes"]==0,record
                assert after["dictionary_live_referenced_fst_bytes"]==before["dictionary_live_referenced_fst_bytes"],record
                assert "running" in session.value(HEALTHY)
                final=stats(session)
                assert final["parser_created"]==final["parser_dropped"],record
                assert callback_live(final)==callback_live(before),record
                record["after_context_reset"]=final

    session.value("BEGIN; SAVEPOINT owned_registration")
    before=stats(session)
    session.value("SELECT charabia.test_set_fault('register_alloc','allocation_error')")
    output=session.query(HEALTHY)
    assert "XX000" in output and "invalid memory alloc request size" in output,output
    session.value("ROLLBACK TO owned_registration")
    after=stats(session)
    assert after["parser_created"]-before["parser_created"]==after["parser_dropped"]-before["parser_dropped"]==1,(before,after)
    assert callback_live(after)==callback_live(before),(before,after)
    assert "running" in session.value(HEALTHY)
    session.value("COMMIT")
    results["registration_savepoint"]={"output":output,"before":before,"after":after}


def interrupt(session,reason,cleanup_failure=False,point="gettoken",structured=False):
    import threading
    session.value("SET application_name='pgcb_errors_ready'")
    session.value("SET statement_timeout='300ms'" if reason=="timeout" else "SET statement_timeout=0")
    before=stats(session)
    session.value("SELECT charabia.test_set_fault(" + literal(point) + ",'wait')")
    outcome=[]
    def query():
        try:
            statement=(STRUCTURED_HEADLINE if point.startswith("headline_") else STRUCTURED_HEALTHY) if structured else (HEADLINE if point.startswith("headline_") else HEALTHY)
            outcome.append(session.query(statement))
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
        if cleanup_failure:
            raise AssertionError("injected observer failure")
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
        assert "running" in session.value(HEALTHY)
        if structured:
            assert session.value(STRUCTURED_HEALTHY)=="t"
            assert session.value(STRUCTURED_HEADLINE)=="alpha <b>Running100</b> beta"
        results.setdefault("interrupts",[]).append({"reason":reason,"point":point,"structured":structured,"output":outcome[0],"before":before,"after":after,"pid":session.pid})
    finally:
        primary_error=sys.exception()
        cleanup_errors=[]
        try:
            for operation in ("pg_cancel_backend","pg_terminate_backend"):
                if not worker.is_alive():
                    break
                try:
                    if cleanup_failure and operation=="pg_cancel_backend":
                        # A real psql timeout exercises cleanup's exception
                        # branch without replacing PostgreSQL with a mock.
                        run("SELECT pg_sleep(2)",check=False,timeout=0.05)
                    else:
                        run(f"SELECT {operation}({session.pid})",check=False,timeout=10)
                except BaseException as error:
                    cleanup_errors.append(f"{operation}: {error!r}")
                finally:
                    worker.join(timeout=10)
            if worker.is_alive():
                # If control connections are unavailable, killing this owned
                # psql closes its stdout and releases query()'s reader thread.
                try:
                    session.process.kill()
                except BaseException as error:
                    cleanup_errors.append(f"owned psql kill: {error!r}")
                finally:
                    worker.join(timeout=10)
        finally:
            results.setdefault("interrupt_cleanup",[]).append({
                "reason":reason,"point":point,"failure_probe":cleanup_failure,
                "thread_exited":not worker.is_alive(),"errors":cleanup_errors})
            save("errors-results.json",results)
        if worker.is_alive() or cleanup_errors:
            message=f"Interrupt cleanup: alive={worker.is_alive()}, errors={cleanup_errors}"
            if primary_error is None:
                raise AssertionError(message)
            primary_error.add_note(message)


def real_interrupt(reason,structured=False):
    """Exercise release callbacks under a real statement timeout/user cancel."""
    import threading
    with Session() as session:
        session.value(HEALTHY)
        session.value(HEADLINE)
        if structured:
            assert session.value(STRUCTURED_HEALTHY)=="t"
            assert session.value(STRUCTURED_HEADLINE)=="alpha <b>Running100</b> beta"
        session.value("SET statement_timeout='1s'" if reason == "timeout" else "SET statement_timeout=0")
        query = ("SELECT sum(length(ts_headline('public.charabia','alpha Running100 beta '||g::text,'run'::tsquery,'HighlightAll=true'))) FROM generate_series(1,100000) g" if structured else
                 "SELECT sum(length(ts_headline('errors_runtime.healthy'," + literal(DOCUMENT) + " || g::text,'running'::tsquery,'HighlightAll=true'))) FROM generate_series(1,100000) g")
        outcome = []
        def execute():
            try:
                outcome.append(session.query(query, timeout=30))
            except BaseException as error:
                outcome.append(error)
        worker = threading.Thread(target=execute,name="pgcb-release-"+reason)
        worker.start()
        observed = False
        try:
            deadline = time.monotonic()+15
            while worker.is_alive() and time.monotonic()<deadline:
                active = run(f"SELECT state='active' AND query LIKE '%ts_headline%' FROM pg_stat_activity WHERE pid={session.pid}",timeout=10)
                if active == "t":
                    observed = True
                    break
                time.sleep(0.02)
            if reason == "cancel":
                assert observed, "The real headline query was not observed active"
                time.sleep(0.1)
                assert run(f"SELECT pg_cancel_backend({session.pid})",timeout=10) == "t"
            worker.join(timeout=15)
            assert not worker.is_alive() and outcome and isinstance(outcome[0],str), outcome
            assert "57014" in outcome[0] and ("statement timeout" if reason == "timeout" else "due to user request") in outcome[0], outcome
            session.value("SET statement_timeout=0")
            assert "running" in session.value(HEALTHY)
            assert "<b>running</b>" in session.value(HEADLINE)
            if structured:
                assert session.value(STRUCTURED_HEALTHY)=="t"
                assert session.value(STRUCTURED_HEADLINE)=="alpha <b>Running100</b> beta"
            results.setdefault("release_interrupts",[]).append({"reason":reason,"structured":structured,"query":query,"output":outcome[0],"observed_active":observed,"pid":session.pid})
        finally:
            primary = sys.exception()
            errors = []
            for operation in ("pg_cancel_backend","pg_terminate_backend"):
                if not worker.is_alive():
                    break
                try:
                    run(f"SELECT {operation}({session.pid})",check=False,timeout=10)
                except BaseException as error:
                    errors.append(repr(error))
                worker.join(timeout=10)
            if worker.is_alive():
                session.process.kill()
                worker.join(timeout=10)
            results.setdefault("release_interrupt_cleanup",[]).append({"reason":reason,"thread_exited":not worker.is_alive(),"errors":errors})
            save("errors-results.json",results)
            if worker.is_alive() or errors:
                message = f"Release query cleanup failed: alive={worker.is_alive()}, errors={errors}"
                if primary is None:
                    raise AssertionError(message)
                primary.add_note(message)


try:
    real_interrupt("cancel")
    real_interrupt("timeout")
    real_interrupt("cancel",structured=True)
    real_interrupt("timeout",structured=True)
    if has_hooks:
        with Session() as session:
            session.value(HEALTHY)
            session.value(HEADLINE)
            session.value(STRUCTURED_HEALTHY)
            session.value(STRUCTURED_HEADLINE)
            for point in ("start", "gettoken", "end", "init", "lexize", "headline_selector", "headline_escape"):
                for mode in ("error", "panic"):
                    before=stats(session)
                    session.value(f"SELECT charabia.test_set_fault({literal(point)},{literal(mode)})")
                    query=("CREATE TEXT SEARCH DICTIONARY public.fault_dict(TEMPLATE=charabia.linguistic_template,Language='eng',Discard=true,Stem=true,Normalize=true,Filter=false)") if point=="init" else HEADLINE if point.startswith("headline_") else HEALTHY
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
            for point in ("gettoken", "headline_selector", "headline_escape"):
                interrupt(session,"cancel",point=point)
                interrupt(session,"timeout",point=point)
            for point in ("lexize","structured_emit","structured_emit_string","headline_selector","headline_escape"):
                for mode in ("error","panic"):
                    before=stats(session)
                    session.value(f"SELECT charabia.test_set_fault({literal(point)},{literal(mode)})")
                    statement=STRUCTURED_HEADLINE if point.startswith("headline_") else STRUCTURED_HEALTHY
                    output=session.query(statement)
                    after=stats(session)
                    record={"point":point,"mode":mode,"structured":True,"sql":statement,"output":output,"before":before,"after":after}
                    results["faults"].append(record)
                    save("errors-results.json",results)
                    assert "ERROR:" in output and "injected pg_charabia" in output,record
                    assert after["parser_created"]-before["parser_created"]==after["parser_dropped"]-before["parser_dropped"]==1,record
                    assert callback_live(after)==callback_live(before),record
                    assert session.value(STRUCTURED_HEALTHY)=="t",record
                interrupt(session,"cancel",point=point,structured=True)
                interrupt(session,"timeout",point=point,structured=True)
        try:
            with Session() as probe:
                probe_pid=probe.pid
                probe.value(HEALTHY)
                probe.value(HEADLINE)
                interrupt(probe,"cancel",cleanup_failure=True,point="headline_escape")
        except AssertionError as error:
            assert str(error)=="injected observer failure",repr(error)
            notes=getattr(error,"__notes__",[])
            assert any("TimeoutExpired" in note for note in notes),notes
            assert results["interrupt_cleanup"][-1]["thread_exited"]
            assert results["interrupt_cleanup"][-1]["failure_probe"]
            deadline=time.monotonic()+5
            while run(f"SELECT count(*) FROM pg_stat_activity WHERE pid={probe_pid}")!="0":
                assert time.monotonic()<deadline,"Owned probe backend did not exit"
                time.sleep(0.02)
            results["cleanup_failure_probe"]={"primary_error":str(error),"notes":notes,
                                              "thread_exited":True,"backend_exited":True}
        else:
            raise AssertionError("Cleanup failure probe did not raise its primary failure")
finally:
    save("errors-results.json",results)
    run("DROP SCHEMA errors_runtime CASCADE")
print(json.dumps({"encoding_cases":len(results["encoding"]),"injected_faults":len(results["faults"]),"registration_faults":len(results.get("registration_faults",[])),"test_hooks":has_hooks}))
