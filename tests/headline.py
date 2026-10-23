#!/usr/bin/env python3
"""Real PG18 headline calls: source fidelity, strict errors and request reuse."""
import html
import json
import os
import re
import sys
from support import Session, literal, save

report = {"status": "running", "checks": [], "errors": [], "source": {
    "matching": "PG18 ts_parse.c hlfinditem and retained Linux probe observations",
    "rendering": "Original-source byte preservation, strict options, and PG18 headline selector contracts",
    "stem": "upstream English Snowball run golden in unambiguous preserved English paragraph",
}}


def checkpoint():
    save("headline-results.json", report)


def check(condition, label, evidence):
    report["checks"].append({"label": label, "passed": bool(condition), "evidence": evidence})
    checkpoint()
    if not condition:
        raise AssertionError(label + ": " + repr(evidence))


def headline(session, raw, query="'cafe'::tsquery", options="HighlightAll=true", config="public.charabia"):
    sql = f"SELECT to_json(ts_headline({literal(config)},{literal(raw)},{query},{literal(options)}))"
    value = json.loads(session.value(sql))
    report.setdefault("calls", []).append({"sql": sql, "result": value})
    checkpoint()
    return value


def fragment_positions(session):
    """Exercise selection, which HighlightAll deliberately bypasses."""
    schema = "pgcb_headline_" + str(os.getpid())
    config = schema + ".positions"
    session.value("CREATE SCHEMA " + schema)
    try:
        # A fixed Dictionary Language makes the Stop hole independent of the
        # Parser's statistical language choice; ordinary native mappings suffice.
        session.value("CREATE TEXT SEARCH DICTIONARY " + schema + ".words ("
                      "TEMPLATE=charabia.linguistic_template,Language='eng',Discard=true,"
                      "Stem=true,Normalize=true,Filter=false,English=false)")
        session.value("CREATE TEXT SEARCH CONFIGURATION " + config + "(PARSER=charabia.parser)")
        routes = json.loads(session.value(
            "SELECT json_agg(alias ORDER BY tokid) FROM ts_token_type('charabia.parser') "
            "WHERE alias NOT IN ('blank','display_only')"))
        for route in routes:
            session.value("ALTER TEXT SEARCH CONFIGURATION " + config +
                          " ADD MAPPING FOR " + route + " WITH " + schema + ".words")
        raw = "one two three four five six seven eight nine ten alpha the beta tail final"
        options = "MinWords=2,MaxWords=3,MaxFragments=1"
        # Independent position contract: one Stop consumes exactly one position.
        for query, matches, expected in (
            ("'alpha <2> beta'::tsquery", True, "<b>alpha</b> the <b>beta</b>"),
            ("'alpha <-> beta'::tsquery", False, "one two"),
        ):
            native = session.value("SELECT to_tsvector(" + literal(config) + "," +
                                   literal(raw) + ") @@ " + query)
            check(native == ("t" if matches else "f"),
                  "explicit Stop hole defines the native phrase distance", native)
            actual = headline(session, raw, query, options, config)
            check(actual == expected,
                  "position-aware fragment selection distinguishes true and false phrases", {
                      "query": query, "expected": expected, "actual": actual,
                      "basis": "PG18 hlCover and no-cover first MinWords fallback; explicit Stop hole"})

        # An unambiguous English prefix makes run/running two operands for one
        # source word. The three-word budget counts that word once and Stop once.
        raw = ("The children were running through the garden while the teachers were reading "
               "books and sharing their happy memories together. lead running the tail next final")
        positions = json.loads(session.value(
            "SELECT jsonb_object_agg(lexeme,positions) FROM unnest(to_tsvector(" +
            literal(config) + "," + literal(raw) + ")) WHERE lexeme IN ('lead','run','running')"))
        lead = positions.get("lead", [])
        check(len(lead) == 1 and all(lead[0] + 1 in positions.get(term, []) for term in ("run", "running")),
              "late fragment word actually has surface and stem at the same position", positions)
        # Choose the only late match using a phrase anchor to exclude the prefix's
        # earlier running; both variants still match the same raw late token.
        actual = headline(session, raw, "'lead <-> (run | running)'::tsquery", options, config)
        check(actual == "<b>lead</b> <b>running</b> the",
              "fragment budget counts a Stop once and repeated variants zero times", {
                  "expected": "<b>lead</b> <b>running</b> the", "actual": actual,
                  "basis": "PG18 get_next_fragment/mark_hl_fragments; two content words plus one Stop"})
    finally:
        primary = sys.exception()
        try:
            session.value("DROP SCHEMA " + schema + " CASCADE")
        except BaseException as cleanup_error:
            report.setdefault("cleanup_errors", []).append(repr(cleanup_error))
            checkpoint()
            if primary is None:
                raise
            primary.add_note("headline fixture cleanup also failed: " + repr(cleanup_error))


def external_dictionary_cases(session):
    """Match PG-produced terms while rendering original bytes, without our dictionary."""
    schema = "pgcb_headline_external_" + str(os.getpid())
    session.value("CREATE SCHEMA " + schema)
    try:
        session.value("CREATE EXTENSION IF NOT EXISTS unaccent")
        session.value("CREATE TEXT SEARCH DICTIONARY " + schema + ".synonyms("
                      "TEMPLATE=pg_catalog.synonym,SYNONYMS='synonym_sample')")
        session.value("CREATE TEXT SEARCH DICTIONARY " + schema + ".filter("
                      "TEMPLATE=charabia.linguistic_template,Language='eng',Discard=false,"
                      "Stem=false,Normalize=true,Filter=true,English=false)")
        session.value("CREATE TEXT SEARCH DICTIONARY " + schema + ".fra_plain("
                      "TEMPLATE=charabia.linguistic_template,Language='fra',Discard=false,"
                      "Stem=false,Normalize=false,Filter=false,English=false)")
        aliases = json.loads(session.value(
            "SELECT json_agg(alias ORDER BY tokid) FROM ts_token_type('charabia.parser') "
            "WHERE alias NOT IN ('blank','display_only')"))
        for name, chain in (("synonym", schema + ".synonyms,pg_catalog.simple"),
                            ("filter_synonym", schema + ".filter," + schema + ".synonyms,pg_catalog.simple"),
                            ("simple", "pg_catalog.simple"),
                            ("unaccent", "public.unaccent," + schema + ".fra_plain")):
            config = schema + "." + name
            session.value("CREATE TEXT SEARCH CONFIGURATION " + config + "(PARSER=charabia.parser)")
            for alias in aliases:
                session.value("ALTER TEXT SEARCH CONFIGURATION " + config +
                              " ADD MAPPING FOR " + alias + " WITH " + chain)
        for name in ("synonym", "filter_synonym"):
            config = schema + "." + name
            check(session.value("SELECT to_tsvector(" + literal(config) + ",'PostgreSQL')::text") == "'pgsql':1",
                  "PG synonym fixture changes the term before headline", {"config": config})
            actual = headline(session, "PostgreSQL & <tag>", "plainto_tsquery(" +
                              literal(config) + ",'PostgreSQL')", config=config)
            check(actual == "<b>PostgreSQL</b> &amp; &lt;tag&gt;",
                  "external synonym and optional filter highlight original spelling", actual)
        config = schema + ".synonym"
        check(headline(session, "indices", "'index:*'::tsquery", config=config) == "<b>indices</b>",
              "external synonym prefix result matches the whole original span", "synonym_sample indices -> index*")
        config = schema + ".unaccent"
        check(headline(session, "HÔTEL & hôtel", "plainto_tsquery(" + literal(config) + ",'hotel')", config=config) ==
              "<b>HÔTEL</b> &amp; <b>hôtel</b>",
              "external filtering dictionary supplies actual terms to core matching", "unaccent.rules HÔTEL -> HOTEL")
        config = schema + ".simple"
        raw = "x" * 2047 + " CAFÉ"
        actual = headline(session, raw, "plainto_tsquery(" + literal(config) + ",'CAFÉ')", config=config)
        check(actual == "x" * 2047 + " <b>CAFÉ</b>",
              "long display preserves source and external simple uses its own locale-correct query", actual)
        check(session.value("SELECT charabia.query_any('PostgreSQL'," + literal(schema + ".synonym") + ")::text") == "'pgsql'",
              "query_any accepts an ordinary external-dictionary configuration", schema + ".synonym")
    finally:
        primary = sys.exception()
        try:
            session.value("DROP SCHEMA " + schema + " CASCADE")
        except BaseException as cleanup_error:
            report.setdefault("cleanup_errors", []).append(repr(cleanup_error))
            checkpoint()
            if primary is None:
                raise
            primary.add_note("external headline cleanup also failed: " + repr(cleanup_error))


def merge_and_parallel_cases(session):
    """Independent source/position expectations with explicit dictionary rules."""
    schema = "pgcb_headline_round4_" + str(os.getpid())
    session.value("CREATE SCHEMA " + schema)
    try:
        session.value("CREATE TEXT SEARCH DICTIONARY " + schema + ".words("
                      "TEMPLATE=charabia.linguistic_template,Language='eng',Discard=false,"
                      "Stem=false,Normalize=false,Filter=false,English=false);")
        session.value("CREATE TEXT SEARCH DICTIONARY " + schema + ".parallel("
                      "TEMPLATE=charabia.linguistic_template,Language='tur',Discard=false,"
                      "Stem=true,Normalize=false,Filter=false,English=true);")
        aliases = json.loads(session.value(
            "SELECT json_agg(alias ORDER BY tokid) FROM ts_token_type('charabia.parser') "
            "WHERE alias NOT IN ('blank','display_only')"))
        for name in ("words", "parallel"):
            config = schema + "." + name
            session.value("CREATE TEXT SEARCH CONFIGURATION " + config + "(PARSER=charabia.parser)")
            for alias in aliases:
                session.value("ALTER TEXT SEARCH CONFIGURATION " + config +
                              " ADD MAPPING FOR " + alias + " WITH " + config)
        config = schema + ".words"
        for raw in ("PostgreSQL", "postgreSQL", "POSTGRESQL"):
            rows = json.loads(session.value(
                "SELECT json_agg(row_to_json(a)) FROM (SELECT raw,kind,language,language_source,"
                "byte_start,byte_end FROM charabia.analyze(" + literal(raw) + "))a"))
            report.setdefault("parser_calls", []).append({"raw": raw, "rows": rows})
            check(len(rows) == 1 and rows[0]["raw"] == raw and rows[0]["kind"] == "word" and
                  rows[0]["byte_start"] == 0 and rows[0]["byte_end"] == len(raw.encode()),
                  "complete English word keeps one exact original span", {"raw": raw, "rows": rows})
            if rows[0]["language_source"] == "english-merge":
                check(rows[0]["language"] == "eng", "an actual whole-word merge selects eng", rows)
            check(session.value("SELECT to_tsvector(" + literal(config) + "," + literal(raw) +
                                ")::text") == "'postgresql':1",
                  "restored English whole word has one index position", raw)
            actual = headline(session, "left " + raw + " right", "'postgresql'::tsquery", config=config)
            check(actual == "left <b>" + raw + "</b> right",
                  "whole-word matching emits original case exactly once", actual)
        raw = "left PostgreSQL right"
        check(session.value("SELECT to_tsvector(" + literal(config) + "," + literal(raw) +
                            ") @@ 'left <-> postgresql <-> right'::tsquery") == "t",
              "merged word does not leave replaced fragment positions in adjacent phrase", raw)
        check(session.value("SELECT to_tsvector(" + literal(config) + ",'PostgreSQL') @@ "
                            "'postgre:*'::tsquery AND NOT(to_tsvector(" + literal(config) +
                            ",'PostgreSQL') @@ 'sql:*'::tsquery)") == "t",
              "prefix sees the complete restored word without an inner component posting", raw)
        check(headline(session, "PostgreSQL", "'postgre:*'::tsquery", config=config) == "<b>PostgreSQL</b>",
              "prefix highlights the complete restored original word", raw)
        for raw in ("post greSQL", "postgreSQL2", "éPostgreSQL", "PostgreSQLé", "PostgreSQL\u0301", "databaseAPI"):
            rows = json.loads(session.value(
                "SELECT json_agg(row_to_json(a)) FROM (SELECT raw,language_source,byte_start,byte_end "
                "FROM charabia.analyze(" + literal(raw) + "))a"))
            report.setdefault("parser_calls", []).append({"raw": raw, "rows": rows})
            valid_boundary = (all(not any(character.isspace() for character in row["raw"])
                                  for row in rows if row["language_source"] == "english-merge")
                              if raw == "post greSQL" else
                              all(row["language_source"] != "english-merge" for row in rows))
            check("".join(row["raw"] for row in rows) == raw and valid_boundary,
                  "whole-word restoration never crosses delimiters or searches an inner ASCII substring", {
                      "raw": raw, "rows": rows})
            actual = headline(session, raw, "'postgre:*'::tsquery", config=config)
            check(html.unescape(actual.replace("<b>", "").replace("</b>", "")) == raw,
                  "nonmerged boundary cases retain every original byte in headline", actual)
        check(session.value("SELECT NOT(to_tsvector(" + literal(config) +
                            ",'post greSQL') @@ 'postgresql'::tsquery)") == "t",
              "space-separated near spelling never creates a full-word posting", "post greSQL")
        config = schema + ".parallel"
        for term in ("abbrevıate", "abbrevıa", "abbreviate", "abbrevi"):
            check(headline(session, "ABBREVIATE & <tag>", literal(term) + "::tsquery", config=config) ==
                  "<b>ABBREVIATE</b> &amp; &lt;tag&gt;",
                  "each Turkish/English alternative matches one original headline span", term)
        for typ in ("json", "jsonb"):
            raw = {"original": ["ABBREVIATE & <tag>", "ABBREVIATE"]}
            expected = {"original": ["<b>ABBREVIATE</b> &amp; &lt;tag&gt;", "<b>ABBREVIATE</b>"]}
            actual = json.loads(session.value("SELECT ts_headline(" + literal(config) + "," +
                literal(json.dumps(raw)) + "::" + typ +
                ",'abbrevıate | abbrevıa | abbreviate | abbrevi'::tsquery,'HighlightAll=true')"))
            check(actual == expected, "four-variant JSON callbacks render each original source exactly once", {
                "type": typ, "expected": expected, "actual": actual})
    finally:
        primary = sys.exception()
        try:
            session.value("DROP SCHEMA " + schema + " CASCADE")
        except BaseException as cleanup_error:
            report.setdefault("cleanup_errors", []).append(repr(cleanup_error))
            checkpoint()
            if primary is None:
                raise
            primary.add_note("round-4 headline cleanup also failed: " + repr(cleanup_error))


try:
    with Session() as session:
        # No oracle is obtained from analyze()/lexize(): raw equality is a separate
        # invariant and fixed expected highlights originate in upstream/probe facts.
        for raw in ("中文 café cafe\u0301 CAFÉ & < > \" '", "\t\n café\r\n\t",
                    "x" * 2046 + " café", "x" * 2047 + " café",
                    "x" * 2048 + " café", " " * 4096 + " café",
                    "😀" * 700 + " café", "ﷺ" * 400 + " café"):
            actual = headline(session, raw)
            restored = html.unescape(actual.replace("<b>", "").replace("</b>", ""))
            check(restored == raw, "HighlightAll preserves every source byte after inverse escaping", {
                "raw_bytes": len(raw.encode()), "actual": actual, "restored": restored})

        paragraph = ("The children were running through the garden while the teachers were reading "
                     "books and sharing their happy memories together. These cats were running.")
        check(session.value(f"SELECT to_tsvector('public.charabia',{literal(paragraph)}) @@ 'run'::tsquery") == "t",
              "English Snowball run is available in the retained paragraph", paragraph)
        actual = headline(session, paragraph, "'run | running'::tsquery")
        check(actual.count("<b>running</b>") == 2, "two matching variants do not duplicate original tokens", actual)
        check(html.unescape(re.sub(r"</?b>", "", actual)) == paragraph,
              "repeated operand entries do not duplicate source bytes", actual)

        for operator in ("'run & !fox'::tsquery", "'run | fox'::tsquery"):
            actual = headline(session, paragraph, operator)
            check(html.unescape(actual.replace("<b>", "").replace("</b>", "")) == paragraph,
                  "Boolean selector path retains source without acting as a match filter", actual)

        text = "needle " + "alpha beta gamma delta " * 30 + "needle final"
        fragments = headline(session, text, "'needle'::tsquery",
                             'MinWords=2,MaxWords=6,MaxFragments=2,FragmentDelimiter="[cut]"')
        check(fragments.count("<b>needle</b>") == 2 and "[cut]" in fragments and len(fragments) < len(text),
              "selector returns both separated covers in source order", fragments)
        fragment_positions(session)
        external_dictionary_cases(session)
        merge_and_parallel_cases(session)

        # PG's deserialize_deflist owns CSV/quoting; the adapter receives parsed values.
        actual = headline(session, "CAFÉ", options='HighlightAll=YES,StartSel="[a,b]",StopSel="[/a,b]"')
        check(actual == "[a,b]CAFÉ[/a,b]", "option commas remain quoted PG values", actual)
        actual = headline(session, "CAFÉ", options='HighlightAll=true,StartSel="",StopSel=""')
        check(actual == "CAFÉ", "empty explicit markers are legal", actual)

        failures = [
            ("Unknown=true", "22023"),
            ("Escape=perhaps", "22023"),
            ("HighlightAll=garbage", "22023"),
            ("StartSel=a,startsel=b", "22023"),
            ("ShortWord=1", "0A000"),
            ("MinWords=0", "22023"),
            ("MinWords=15,MaxWords=15", "22023"),
            ("MaxWords=10001", "22023"),
            ("MaxWords=-1", "22023"),
            ("MaxFragments=101", "22023"),
            ("HighlightAll=true,MinWords=15", "22023"),
            ("HighlightAll=true,MaxWords=35", "22023"),
            ("HighlightAll=true,MaxFragments=1", "22023"),
            ("StartSel=" + "x" * 257, "54000"),
        ]
        for options, state in failures:
            sql = f"SELECT ts_headline('public.charabia','café','cafe'::tsquery,{literal(options)})"
            output = session.query(sql)
            report["errors"].append({"sql": sql, "expected_state": state, "output": output})
            checkpoint()
            check("ERROR:" in output and state in output, "unsupported/invalid options fail explicitly", output)
            check(headline(session, "CAFÉ") == "<b>CAFÉ</b>", "same backend recovers after headline error", output)

        output = session.query("SELECT ts_headline('public.charabia','café','cafe:A'::tsquery)")
        check("ERROR:" in output and "0A000" in output, "weighted operands are explicitly unsupported", output)
        output = session.query("SELECT ts_headline('public.charabia',repeat('x ',131072),'x'::tsquery," +
                               literal("HighlightAll=true,StartSel=" + "s" * 256 + ",StopSel=" + "e" * 256) + ")")
        check("ERROR:" in output and "54000" in output and "8388608" in output,
              "actual selected markers cannot expand output beyond 8 MiB", output)
        check(headline(session, "café") == "<b>café</b>", "same backend recovers after output budget error", output)
        # JSON and JSONB reuse one HeadlineParsedText for each string value.
        for _ in range(20):
            raw = {"CAFÉ": ["CAFÉ & cafe", "", "<tag>", None, 3], "another": "cafe"}
            expected = {"CAFÉ": ["<b>CAFÉ</b> &amp; <b>cafe</b>", "", "&lt;tag&gt;", None, 3],
                        "another": "<b>cafe</b>"}
            for typ in ("json", "jsonb"):
                actual = json.loads(session.value("SELECT ts_headline('public.charabia'," +
                    literal(json.dumps(raw, ensure_ascii=False)) + f"::{typ},'cafe'::tsquery,'HighlightAll=true')"))
                check(actual == expected, "JSON callback reuse resets entries and markers for each source string", actual)
    report["status"] = "passed"
except BaseException as error:
    report["status"] = "failed"
    report["error"] = repr(error)
    raise
finally:
    checkpoint()
