#!/usr/bin/env python3
"""Real native structured ASCII token contracts, independent of pg_textsearch installation."""
import html
import json
import os
from pathlib import Path
import sys

from support import Session, literal, save


KINDS = ("url", "host", "email", "file", "numword")
LANGUAGES = ("eng", "deu", "nld", "rus", "spa", "fra", "por", "ita", "ara",
             "tur", "cmn", "jpn", "kor", "vie", "tha", "und")
REPORT = {
    "status": "running", "checks": [], "errors": [], "plans": [],
    "source": {
        "literal": "Explicit ASCII grammar, raw case, and bounded expansion requirements; not extension-generated goldens",
        "english": "Existing English prepare/stop rules and Snowball running -> run upstream example",
        "grouping": "PG18 pushval_morph: separate nvariant groups OR, entries in one group AND; flags=0 retains one position",
        "index": "Independent fixed document IDs and real GIN scans versus sequential execution",
        "headline": "Original-source fidelity and PostgreSQL matching against the real dictionary chain",
    },
}


def checkpoint():
    save("structured-results.json", REPORT)


def check(condition, label, evidence=None):
    REPORT["checks"].append({"label": label, "passed": bool(condition), "evidence": evidence})
    checkpoint()
    if not condition:
        raise AssertionError(label + ": " + repr(evidence))


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def main():
    schema = "pgcb_structured_" + str(os.getpid())
    evidence = Path(os.environ["PGCB_RUN_DIR"]) / "structured"
    evidence.mkdir(exist_ok=False)
    sequence = 0
    created = False

    def sql(session, statement, label, parse=False, allow_error=False):
        nonlocal sequence
        sequence += 1
        stem = f"{sequence:03d}-{label}"
        (evidence / (stem + ".sql")).write_text(statement + ";\n")
        output = session.query(statement)
        (evidence / (stem + ".output")).write_text(output + "\n")
        REPORT["commands"] = sequence
        checkpoint()
        if not allow_error and "ERROR:" in output:
            raise AssertionError(label + ": " + output)
        return json.loads(output) if parse else output

    def error(session, statement, state, label):
        output = sql(session, statement, label, allow_error=True)
        REPORT["errors"].append({"label": label, "sqlstate": state, "output": output})
        check("ERROR:" in output and state in output, label, output)
        check(sql(session, "SELECT 1", label + "-recovery") == "1",
              "same backend remains usable after " + label)

    def direct(session, dictionary, raw, label):
        return sql(session, "SELECT coalesce(to_jsonb(ts_lexize(" + literal(dictionary) + "," +
                   literal(raw) + ")),\'null\'::jsonb)", label, True)

    def positions(session, config, raw, label):
        return sql(session, "SELECT coalesce(jsonb_object_agg(lexeme,positions),'{}') FROM "
                   "unnest(to_tsvector(" + literal(config) + "," + literal(raw) + "))", label, True)

    def matches(session, config, raw, query, label):
        return sql(session, "SELECT to_tsvector(" + literal(config) + "," + literal(raw) +
                   ") @@ (" + query + ")", label) == "t"

    def headline(session, config, raw, query, label):
        return sql(session, "SELECT to_jsonb(ts_headline(" + literal(config) + "," + literal(raw) +
                   "," + query + ",'HighlightAll=true'))", label, True)

    try:
        with Session() as session:
            catalog = sql(session, "SELECT jsonb_build_object('types',"
                "(SELECT jsonb_object_agg(tokid,alias) FROM ts_token_type('charabia.structured_parser')),"
                "'dictionaries',(SELECT jsonb_agg(d.dictname ORDER BY d.dictname) FROM pg_ts_dict d "
                "JOIN pg_namespace n ON n.oid=d.dictnamespace WHERE n.nspname='charabia'),"
                "'mappings',(SELECT count(*) FROM pg_ts_config_map WHERE mapcfg='public.charabia_structured'::regconfig),"
                "'template',to_regclass('pg_catalog.pg_ts_template') IS NOT NULL AND EXISTS("
                "SELECT 1 FROM pg_ts_template t JOIN pg_namespace n ON n.oid=t.tmplnamespace "
                "WHERE n.nspname='charabia' AND t.tmplname='structured_template'))", "catalog", True)
            check(len(catalog["types"]) == 23 and all(catalog["types"].get(str(i + 19)) == kind
                  for i, kind in enumerate(KINDS)), "Parser installs the five fixed structured type IDs", catalog)
            check(set(catalog["dictionaries"]) == set(LANGUAGES + KINDS) and catalog["mappings"] == 21
                  and catalog["template"], "default installation owns 16 linguistic and five structured dictionaries", catalog)

            sql(session, "CREATE SCHEMA " + schema, "schema")
            created = True
            config = schema + ".general"
            sql(session, "CREATE TEXT SEARCH DICTIONARY " + schema + ".words("
                "TEMPLATE=charabia.linguistic_template,Language='eng',Discard=true,Stem=false,Normalize=true,"
                "Filter=false,English=false);CREATE TEXT SEARCH CONFIGURATION " + config +
                "(COPY=public.charabia_structured)", "private-config")
            for language in LANGUAGES:
                sql(session, "ALTER TEXT SEARCH CONFIGURATION " + config + " ALTER MAPPING FOR " +
                    language + " WITH " + schema + ".words", "ordinary-" + language)

            # Hand-written structures assert exact raw ownership and the selected Kind.
            raw_cases = [
                ("url", "https://Example.com/Running?q=100ms"),
                ("url", "https://[::1]:8080/A#Part"),
                ("url", "https://example.com?"),
                ("url", "https://example_com/%2F"),
                ("url", "https://exa%4Dple.com/A"),
                ("host", "example.com"),
                ("email", "User+tag@Example.com"),
                ("file", "/Src/RunningTests.rs"),
                ("file", "C:\\Src\\RunningTests.rs"),
                ("numword", "100ms"),
                ("numword", "0x5fff"),
                ("numword", "v1.2.3"),
            ]
            for ordinal, (kind, raw) in enumerate(raw_cases):
                text = "前" + raw + "后"
                rows = sql(session, "SELECT jsonb_agg(to_jsonb(a) ORDER BY a.byte_start) "
                    "FROM charabia.analyze(" + literal(text) + ",'public.charabia_structured')a", f"raw-{ordinal}", True)
                parsed = sql(session, "SELECT jsonb_agg(jsonb_build_array(tokid,token) ORDER BY ord) "
                    "FROM ts_parse('charabia.structured_parser'," + literal(text) +
                    ") WITH ORDINALITY p(tokid,token,ord)", f"parse-{ordinal}", True)
                check("".join(row[1] for row in parsed) == text and "".join(row["raw"] for row in rows) == text,
                      "Parser and diagnostics reconstruct the complete Unicode source", {"raw": text, "rows": rows})
                selected = [row for row in rows if row["token_type"] == kind]
                check(len(selected) == 1 and selected[0]["raw"] == raw and
                      selected[0]["language"] is None and selected[0]["byte_start"] == 3 and
                      selected[0]["byte_end"] == 3 + len(raw.encode()),
                      "one original ASCII envelope has one structured record and no language", {"kind": kind, "rows": rows})
                previous = 0
                for row in rows:
                    check(row["byte_start"] == previous and
                          text.encode()[row["byte_start"]:row["byte_end"]].decode() == row["raw"],
                          "raw spans are continuous UTF-8 byte ranges", row)
                    previous = row["byte_end"]
            for ordinal, text in enumerate((
                '<a href="https://Example.com/Running?q=100ms">search &amp; ordinary</a>',
                "https://example.com/a|b tail", "https://example.com/%ZZ tail",
                "https://example.com/A\u0301 tail", "éhttps://example.com/Running中文",
                "C++ C# plainCamelCase plain_word tail",
            )):
                rows = sql(session, "SELECT jsonb_agg(to_jsonb(a) ORDER BY byte_start) "
                    "FROM charabia.analyze(" + literal(text) + ",'public.charabia_structured')a", f"fallback-{ordinal}", True)
                check("".join(row["raw"] for row in rows) == text,
                      "markup, failed envelopes, and combining boundaries never swallow raw text", {"raw": text, "rows": rows})
                if ordinal == 0:
                    check(any(row["token_type"] == "url" and row["raw"] ==
                              "https://Example.com/Running?q=100ms" for row in rows) and
                          matches(session, config, text, "'search & ordinary'::tsquery", "markup-words"),
                          "tag and entity shapes hide neither the URL nor ordinary inner words", rows)
                if ordinal in (1, 2, 3):
                    check(not any(row["token_type"] == "url" for row in rows),
                          "an invalid whole envelope never becomes a valid URL prefix", rows)

            for ordinal, raw in enumerate((
                ".bad@example.com", "bad..local@example.com", "a@b@example.com", "a@@b@example.com",
                "a@example.com:80",
                "a@example.com:abc", "a@example.com..", "a@example.com+tag",
            )):
                rows = sql(session, "SELECT jsonb_agg(to_jsonb(a) ORDER BY byte_start) "
                    "FROM charabia.analyze(" + literal(raw) + ",'public.charabia_structured')a", f"malformed-email-{ordinal}", True)
                check("".join(row["raw"] for row in rows) == raw and
                      not any(row["token_type"] == "email" for row in rows),
                      "a malformed mailbox retains all source text without a shortened email token", rows)
                check(direct(session, "charabia.email", raw, f"malformed-email-direct-{ordinal}") is None,
                      "the email dictionary rejects the same malformed full mailbox", raw)
            for ordinal, raw in enumerate((
                "a@example.com", "a@example.com tail", "a@example.com,tail",
                "(a@example.com)", "a@example.com.", "a@example.com: text",
            )):
                rows = sql(session, "SELECT jsonb_agg(to_jsonb(a) ORDER BY byte_start) "
                    "FROM charabia.analyze(" + literal(raw) + ",'public.charabia_structured')a", f"email-boundary-{ordinal}", True)
                check("".join(row["raw"] for row in rows) == raw and
                      [row["raw"] for row in rows if row["token_type"] == "email"] == ["a@example.com"],
                      "normal punctuation and whitespace retain the complete email token", rows)

            full_cases = [
                ("url", "HTTPS://Example.COM:00080/A/../%7e?q=%2F#Part",
                 "https://example.com:00080/A/../%7e?q=%2F#Part"),
                ("host", "Example.COM:00080", "example.com:00080"),
                ("email", "User+tag@Example.COM", "User+tag@example.com"),
                ("file", "/Src/RunningTests.rs", "/Src/RunningTests.rs"),
                ("numword", "Item123", "Item123"),
            ]
            for ordinal, (kind, raw, full) in enumerate(full_cases):
                terms = direct(session, "charabia." + kind, raw, f"full-{ordinal}")
                check(full in terms and len(terms) == len(set(terms)) and all(terms),
                      "terminal terms retain the precise literal full form and have no empty duplicates", {
                          "kind": kind, "raw": raw, "expected_full": full, "actual": terms})
            for raw in ("0x5fff", "5fff", "f5ff"):
                body = raw[2:] if raw.startswith("0x") else raw
                terms = direct(session, "charabia.numword", raw, "hex-" + raw)
                check(set(terms) == {body, "0x" + body}, "qualified hex emits exactly its two literal spellings", terms)
            for ordinal, raw in enumerate(("dead", "cafe", "face", "1234", "5ff", "100ms")):
                terms = direct(session, "charabia.numword", raw, "not-hex-" + str(ordinal))
                check(not terms or not any(term.startswith("0x") for term in terms),
                      "ordinary words, decimal-only and short forms do not invent hex aliases", {"raw": raw, "terms": terms})
            for ordinal, raw in enumerate(("1234", "+1.25e-3", "v1.2.3", "1e10", "1E10", "12e3", "1e1234")):
                check(direct(session, "charabia.numword", raw, "numeric-" + str(ordinal)) == [raw],
                      "numbers and versions retain one literal form without numeric conversion", raw)

            # The full and the complete component conjunction are distinct routes.
            plain = "plainto_tsquery(" + literal(config) + ",'100ms')"
            for literal_vector, expected in (("'100ms':1", True), ("'100':1 'ms':1", True),
                                             ("'100':1", False), ("'ms':1", False)):
                actual = sql(session, "SELECT " + literal(literal_vector) + "::tsvector @@ " + plain,
                             "independent-group-vector") == "t"
                check(actual == expected,
                      "independently written vectors establish singleton-OR versus component-AND query groups", {
                          "vector": literal_vector, "expected": expected, "actual": actual})
            for literal_vector in ("'5fff':1", "'0x5fff':1"):
                check(sql(session, "SELECT " + literal(literal_vector) + "::tsvector @@ "
                          "plainto_tsquery(" + literal(config) + ",'5fff')", "hex-singleton-variant") == "t",
                      "each complete hex spelling independently satisfies the OR query", literal_vector)
            for raw, expected in (("100ms", True), ("100 ms", True), ("100", False), ("ms", False), ("200ms", False)):
                actual = matches(session, config, raw, plain, "query-group")
                check(actual == expected,
                      "full OR component-AND admits the exact independent match set", {
                          "raw": raw, "expected": expected, "actual": actual})
            scattered_actual = matches(session, config, "100 ms", plain, "scattered-domain-observation")
            REPORT["quality_observations"] = [{
                "mode": "structured", "input": "100 ms", "query": "100ms", "domain_expected": False,
                "native_actual": scattered_actual, "config":config, "classification": "disclosed scattered-component false positive",
                "basis": "A tsquery conjunction does not require component co-location; this is not a quality exemption",
            }]
            checkpoint()
            check(matches(session, config, "ms", "charabia.query_any('100ms'," + literal(config) + ")", "query-any"),
                  "query_any explicitly widens the component conjunction to OR")
            vector = positions(session, config, "alpha the 100ms beta", "atomic-positions")
            check(all(vector.get(term) == [3] for term in ("100ms", "100", "ms")) and
                  vector.get("alpha") == [1] and vector.get("beta") == [4],
                  "structured alternatives share one position while an outside Stop leaves one hole", vector)
            check(matches(session, config, "alpha the 100ms beta", "'alpha <2> 100ms <-> beta'::tsquery", "outer-phrase") and
                  not matches(session, config, "100ms", "'100 <-> ms'::tsquery", "inner-phrase"),
                  "one atomic structured token preserves the outside distance and has no internal phrase positions")
            check(matches(session, config, "100ms", "to_tsquery(" + literal(config) + ",'100m:*')", "prefix"),
                  "prefix flags apply to the real analyzed structured token operands")
            prefix_query = "to_tsquery(" + literal(config) + ",'100mill:*')"
            for literal_vector, expected in (("'100':1 'millisecond':1", True), ("'1000':1 'millisecond':1", True),
                                             ("'100':1", False), ("'1000':1", False),
                                             ("'millisecond':1", False)):
                actual = sql(session, "SELECT " + literal(literal_vector) + "::tsvector @@ " + prefix_query,
                             "component-prefix-vector") == "t"
                check(actual == expected,
                      "component-only prefix matching still requires every member of the AND group", {
                          "vector": literal_vector, "expected": expected, "actual": actual})

            # Native ALTER preserves omitted parameters and recomputes removed defaults.
            sql(session, "CREATE TEXT SEARCH DICTIONARY " + schema + ".parts("
                "TEMPLATE=charabia.structured_template,Kind='numword',Discard=false,Stem=false,Normalize=false)", "parts")
            terms = direct(session, schema + ".parts", "Running100", "parts-no-stem")
            check("running" in terms and "run" not in terms and "Running100" in terms,
                  "Stem=false preserves the prepared surface and the literal full form", terms)
            sql(session, "ALTER TEXT SEARCH DICTIONARY " + schema + ".parts(Stem=true)", "parts-enable-stem")
            check("run" in direct(session, schema + ".parts", "Running100", "parts-stemmed"),
                  "Stem adds the upstream English run result without changing Kind")
            sql(session, "ALTER TEXT SEARCH DICTIONARY " + schema + ".parts(Stem=false,Stem=true)", "parts-last-wins")
            check("run" in direct(session, schema + ".parts", "Running100", "parts-last-wins-result"),
                  "native ALTER duplicate merge keeps the last same-spelling value")
            sql(session, "ALTER TEXT SEARCH DICTIONARY " + schema + ".parts(Stem)", "parts-remove-stem")
            check("run" in direct(session, schema + ".parts", "Running100", "parts-default-stem"),
                  "removing Stem restores the structured default")
            check("the" in direct(session, schema + ".parts", "the100", "parts-no-discard"),
                  "Discard=false retains the English stop component")
            sql(session, "ALTER TEXT SEARCH DICTIONARY " + schema + ".parts(Discard=true)", "parts-enable-discard")
            stopped = direct(session, schema + ".parts", "the100", "parts-discarded")
            check("the" not in stopped and "the100" in stopped and "100" in stopped,
                  "Discard removes only the stop component and retains the full and numeric forms", stopped)
            accepted = 0
            for kind in KINDS:
                for mask in range(8):
                    name = schema + ".matrix"
                    options = ",".join(key + "=" + ("true" if mask & (1 << index) else "false")
                                       for index, key in enumerate(("Discard", "Stem", "Normalize")))
                    sql(session, "CREATE TEXT SEARCH DICTIONARY " + name +
                        "(TEMPLATE=charabia.structured_template,Kind=" + literal(kind) + "," + options +
                        ");DROP TEXT SEARCH DICTIONARY " + name, "parameter-matrix")
                    accepted += 1
            check(accepted == 40, "all five kinds accept all eight explicit Boolean combinations", accepted)
            for label, options, state in (
                ("missing-kind", "Stem=true", "22023"),
                ("invalid-kind", "Kind='URL'", "22023"),
                ("unknown-filter", "Kind='url',Filter=false", "22023"),
                ("unknown-language", "Kind='url',Language='eng'", "22023"),
                ("unknown-english", "Kind='url',English=false", "22023"),
                ("bad-boolean", "Kind='url',Normalize='notbool'", "42601"),
                ("duplicate-kind", "Kind='url',\"Kind\"='host'", "22023"),
            ):
                error(session, "CREATE TEXT SEARCH DICTIONARY " + schema + ".bad("
                      "TEMPLATE=charabia.structured_template," + options + ")", state, label)

            sql(session, "CREATE TEXT SEARCH DICTIONARY " + schema + ".filter("
                "TEMPLATE=charabia.linguistic_template,Language='eng',Discard=false,Stem=false,Normalize=true,"
                "Filter=true,English=false);CREATE TEXT SEARCH CONFIGURATION " + schema +
                ".filtered(COPY=" + config + ");ALTER TEXT SEARCH CONFIGURATION " + schema +
                ".filtered ALTER MAPPING FOR numword WITH " + schema + ".filter,charabia.numword;"
                "CREATE TEXT SEARCH CONFIGURATION " + schema + ".fallback(COPY=" + config +
                ");ALTER TEXT SEARCH CONFIGURATION " + schema +
                ".fallback ALTER MAPPING FOR numword WITH charabia.url,pg_catalog.simple;"
                "CREATE TEXT SEARCH CONFIGURATION " + schema + ".terminal(COPY=" + config +
                ");ALTER TEXT SEARCH CONFIGURATION " + schema +
                ".terminal ALTER MAPPING FOR numword WITH charabia.numword,pg_catalog.simple", "chains")
            check(direct(session, "charabia.url", "100ms", "direct-reject") is None,
                  "a structured dictionary independently rejects input outside its Kind")
            fallback = positions(session, schema + ".fallback", "100ms", "fallback-vector")
            check(fallback == {"100ms": [1]}, "NULL rejection passes the actual current input to simple", fallback)
            check(headline(session, schema + ".fallback", "100ms", "'100ms'::tsquery", "external-terminal-headline") == "<b>100ms</b>",
                  "an external simple terminal dictionary highlights the original structured token")
            filtered = positions(session, schema + ".filtered", "RUNNING100", "filter-vector")
            check("running100" in filtered and "RUNNING100" not in filtered and "run" in filtered,
                  "a preceding real Filter changes bytes before independent structured validation", filtered)
            terminal = positions(session, schema + ".terminal", "100ms", "terminal-vector")
            check(terminal == {"100ms": [1], "100": [1], "ms": [1]},
                  "a successful structured token is terminal while all emitted terms share one position", terminal)

            sql(session, "CREATE TABLE " + schema + ".docs(id int PRIMARY KEY,body text NOT NULL);INSERT INTO " +
                schema + ".docs VALUES(1,'100ms'),(2,'100'),(3,'ms'),(4,'100 ms'),(5,'200ms'),"
                "(6,'alpha 100ms beta');CREATE INDEX structured_gin ON " + schema +
                ".docs USING gin(to_tsvector(" + literal(config) + ",body));ANALYZE " + schema + ".docs", "gin-setup")

            def indexed(expected, label):
                query = ("SELECT id FROM " + schema + ".docs WHERE to_tsvector(" + literal(config) +
                         ",body) @@ plainto_tsquery(" + literal(config) + ",'100ms')")
                result_query = "SELECT coalesce(jsonb_agg(id ORDER BY id),'[]') FROM (" + query + ") found"
                default = sql(session, "EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON,COSTS OFF) " + result_query,
                              label + "-default-plan", True)
                sql(session, "SET enable_seqscan=off;SET enable_indexscan=off;SET enable_indexonlyscan=off;"
                    "SET enable_bitmapscan=on", label + "-index-settings")
                plan = sql(session, "EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON,COSTS OFF) " + result_query,
                           label + "-index-plan", True)
                actual = sql(session, result_query, label + "-indexed", True)
                sql(session, "SET enable_bitmapscan=off;SET enable_seqscan=on", label + "-seq-settings")
                sequential = sql(session, result_query, label + "-sequential", True)
                sql(session, "RESET enable_seqscan;RESET enable_indexscan;RESET enable_indexonlyscan;"
                    "RESET enable_bitmapscan", label + "-reset-settings")
                REPORT["plans"].append({"label": label, "sql": query, "default": default, "index": plan,
                                        "expected_ids": expected, "indexed": actual, "sequential": sequential})
                check(any(node.get("Index Name") == "structured_gin" and node.get("Actual Loops", 0) > 0
                          for node in nodes(plan)), "real GIN executes the expression index after " + label, plan)
                check(actual == sequential == expected, "GIN and heap return independent IDs after " + label,
                      {"expected": expected, "indexed": actual, "heap": sequential})

            indexed([1, 4, 6], "initial")
            sql(session, "INSERT INTO " + schema + ".docs VALUES(7,'100ms')", "gin-insert")
            indexed([1, 4, 6, 7], "insert")
            sql(session, "UPDATE " + schema + ".docs SET body='300ms' WHERE id=1", "gin-update")
            indexed([4, 6, 7], "update")
            sql(session, "DELETE FROM " + schema + ".docs WHERE id=7", "gin-delete")
            indexed([4, 6], "delete")

            raw = "alpha https://Example.com/Running?q=100ms&x=2 beta"
            expected = "alpha <b>https://Example.com/Running?q=100ms&amp;x=2</b> beta"
            actual = headline(session, config, raw, "'run'::tsquery", "headline-url-component")
            check(actual == expected, "a stem component highlights the whole original URL exactly once", actual)
            check(html.unescape(actual.replace("<b>", "").replace("</b>", "")) == raw,
                  "headline markers and source escaping preserve every original byte", actual)
            check(headline(session, config, "alpha 100ms beta", "'100 | ms | 100ms'::tsquery", "headline-repeated") ==
                  "alpha <b>100ms</b> beta", "multiple matching operands do not repeat the original structured token token")
            for typ in ("json", "jsonb"):
                document = {"content": ["100ms & <tag>", "", "100ms"], "number": 7}
                output = sql(session, "SELECT ts_headline(" + literal(config) + "," + literal(json.dumps(document)) +
                    "::" + typ + ",'ms'::tsquery,'HighlightAll=true')", "headline-" + typ, True)
                check(output == {"content": ["<b>100ms</b> &amp; &lt;tag&gt;", "", "<b>100ms</b>"], "number": 7},
                      "each JSON string uses the same original-text structured headline callback", output)
            error(session, "SELECT ts_headline(" + literal(config) +
                  ",'100ms','ms'::tsquery,'UnknownSymbolOption=true')", "22023", "headline-unknown-option")
            distinct = ["https://unique.example/item?q=" + str(i) for i in range(100)]
            block = " ".join(distinct)
            block_positions = positions(session, config, block, "distinct-structured-block")
            check(all(block_positions.get(raw) == [index + 1] for index, raw in enumerate(distinct)),
                  "many distinct full structured tokens survive with one consecutive position each", block_positions)
            error(session, "SELECT ts_lexize('charabia.numword',repeat('a',262145))", "54000", "direct-input-limit")
            # Count before Stop and deduplication: 31 English leaves plus one
            # numeric leaf are 32 units even though all repeated 'the' stop.
            within_units = "the_" * 31 + "1"
            over_units = "the_" * 32 + "1"
            terms = direct(session, "charabia.numword", within_units, "unit-boundary-32")
            check(within_units in terms and "1" in terms and "the" not in terms,
                  "exactly 32 pre-stop expansion units retain their full and numeric forms", terms)
            error(session, "SELECT ts_lexize('charabia.numword'," + literal(over_units) + ")",
                  "54000", "unit-boundary-33")
            check(direct(session, "charabia.numword", "100ms", "post-budget-recovery") is not None,
                  "a budget failure does not poison the cached structured dictionary")
            long_raw = "https://example.com/" + "x" * 2047
            rows = sql(session, "SELECT jsonb_agg(to_jsonb(a) ORDER BY byte_start) FROM charabia.analyze(" +
                       literal(long_raw) + ",'public.charabia_structured')a", "long-envelope", True)
            check("".join(row["raw"] for row in rows) == long_raw and
                  not any(row["token_type"] == "url" for row in rows),
                  "a long envelope returns to the complete ordinary/display path without a short URL", rows)
            check(headline(session, config, long_raw, "'absent'::tsquery", "long-headline") == long_raw,
                  "long display fallback remains byte-faithful in headline")
            sql(session, "DROP SCHEMA " + schema + " CASCADE", "schema-cleanup")
            created = False
        REPORT["status"] = "passed"
    except BaseException as error_value:
        REPORT["status"] = "failed"
        REPORT["error"] = repr(error_value)
        raise
    finally:
        primary = sys.exception()
        try:
            if created:
                with Session() as cleanup:
                    cleanup.value("DROP SCHEMA " + schema + " CASCADE")
                created = False
            REPORT["cleanup"] = {"schema_removed": not created}
        except BaseException as cleanup_error:
            REPORT["cleanup_error"] = repr(cleanup_error)
            REPORT["status"] = "failed"
            if primary is None:
                raise
            primary.add_note("structured schema cleanup also failed: " + repr(cleanup_error))
        finally:
            checkpoint()


if __name__ == "__main__":
    main()
