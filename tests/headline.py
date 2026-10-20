#!/usr/bin/env python3
"""Real PG18 headline calls: source fidelity, strict errors and request reuse."""
import html
import json
import re
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


def headline(session, raw, query="'cafe'::tsquery", options="HighlightAll=true"):
    sql = f"SELECT to_json(ts_headline('public.charabia',{literal(raw)},{query},{literal(options)}))"
    value = json.loads(session.value(sql))
    report.setdefault("calls", []).append({"sql": sql, "result": value})
    checkpoint()
    return value


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
