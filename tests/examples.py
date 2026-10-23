"""Execute README and reference SQL verbatim in an owned Linux database."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from support import run, save

ROOT = Path(__file__).resolve().parent.parent
readme = ROOT / "README.md"
documents = ((readme, 3), (ROOT / "docs/reference.md", 2))
examples = []
document_sha256 = {}
for document, expected_blocks in documents:
    source = str(document.relative_to(ROOT))
    contents = document.read_bytes()
    blocks = re.findall(r"```sql\n(.*?)```", contents.decode("utf-8"), re.S)
    assert len(blocks) == expected_blocks, (source, len(blocks))
    document_sha256[source] = hashlib.sha256(contents).hexdigest()
    examples.extend((source, number + 1, sql) for number, sql in enumerate(blocks))
assert os.environ["PGCB_PLATFORM"] == "linux-amd64", "The README BM25 examples require the Linux verifier and preloaded pg_textsearch"
database = "pgcb_examples_" + str(os.getpid())
results = {"status":"running","readme_sha256":document_sha256["README.md"],"document_sha256":document_sha256,"examples":[],"checks":[],"source":"README and reference SQL plus fixed PG/Charabia contracts and independent document IDs/source text"}
created = False


def checkpoint():
    save("documentation-examples.json",results)


def check(ok,label,evidence):
    results["checks"].append({"label":label,"passed":bool(ok),"evidence":evidence})
    checkpoint()
    if not ok:
        raise AssertionError(label + ": " + repr(evidence))


def json_lines(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def nodes(value):
    if isinstance(value,dict):
        yield value
        for item in value.values():
            yield from nodes(item)
    elif isinstance(value,list):
        for item in value:
            yield from nodes(item)


try:
    run(f"CREATE DATABASE {database} TEMPLATE template0")
    created = True
    for number,(source,source_block,sql) in enumerate(examples):
        record = {"number":number+1,"source":source,"source_block":source_block,"sql":sql,"status":"running"}
        results["examples"].append(record)
        checkpoint()
        record["output"] = run(sql,database)
        record["status"] = "passed"
        checkpoint()
    first_info = [item for item in json_lines(results["examples"][0]["output"]) if "extension" in item]
    check(len(first_info)==1 and first_info[0]["extension"]=="0.3.0" and first_info[0]["language_count"]==16,
          "README loads this native interface release",first_info)
    check(first_info[0].get("symbols",{}).get("dictionary_count")==5 and
          [row["kind"] for row in first_info[0]["symbols"]["types"]]==["url","host","email","file","numword"],
          "README loads the actual five-symbol interface",first_info[0].get("symbols"))
    unit_headline=run("SELECT ts_headline('app.search','alpha 100ms beta',plainto_tsquery('app.search','ms'),'HighlightAll=true')",database)
    check(unit_headline=="alpha <b>100ms</b> beta","reference custom symbol dictionary highlights the whole source token",unit_headline)
    capabilities = first_info[0]["languages"]
    check([row["language"] for row in capabilities] ==
          ["eng","deu","nld","rus","spa","fra","por","ita","ara","tur","cmn","jpn","kor","vie","tha","und"] and
          sum(row["defaults"]["english"] for row in capabilities) == 8,
          "README analysis identity lists the ordered supported routes and English defaults", capabilities)
    options=run("SELECT dictinitoption FROM pg_ts_dict WHERE oid='app.search_eng'::regdictionary",database)
    check("stem = true" in options.replace("'","") and "discard = true" in options.replace("'",""),
          "native ALTER keeps other stored options",options)
    check(run("SELECT mapdict=(SELECT mapdict FROM pg_ts_config_map WHERE mapcfg='public.charabia'::regconfig AND maptokentype=6) FROM pg_ts_config_map WHERE mapcfg='app.search'::regconfig AND maptokentype=6",database)=="t",
          "COPY shares unchanged French dictionary",options)
    ids = run("SELECT id FROM documents WHERE to_tsvector('public.charabia',body) @@ plainto_tsquery('public.charabia','POSTGRESQL') ORDER BY id",database)
    check(ids == "1","native case normalization finds the independent Chinese document",ids)
    stem_ids = run("SELECT id FROM documents WHERE to_tsvector('public.charabia',body) @@ plainto_tsquery('public.charabia','run') ORDER BY id",database)
    check(stem_ids == "2","English upstream running->run golden is present in the long English document",stem_ids)
    native_headline = run("SELECT ts_headline('public.charabia',body,plainto_tsquery('public.charabia','run'),'HighlightAll=true') FROM documents WHERE id=2",database)
    check(native_headline.count("<b>running</b>") == 2,
          "native headline marks both original running words from a run query",native_headline)
    filtered = run("SELECT to_tsvector('app.filtered',body)::text FROM documents WHERE id=2",database)
    check("'running':" in filtered and "'the':" in filtered and "'run':" not in filtered,
          "filter followed by simple keeps surface and stopwords with Stem and Discard disabled",filtered)
    ranked = "SELECT id FROM documents ORDER BY body <@> to_bm25query('全文','public.documents_bm25') LIMIT 10"
    default_plan = json.loads(run("EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF) " + ranked,database))
    forced_plan = json.loads(run("SET enable_seqscan=off; EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF) " + ranked,database))
    results["bm25_plans"] = {"query":ranked,"default":default_plan,"index":forced_plan}
    check(any(node.get("Node Type") == "Index Scan" and node.get("Index Name") == "documents_bm25" and node.get("Actual Loops",0) > 0 for node in nodes(forced_plan)),
          "README ranking executes the actual target BM25 index",forced_plan)
    results["bm25_default_ids"] = run(ranked,database)
    bm_ids = run("SET enable_seqscan=off;" + ranked,database)
    results["bm25_index_ids"] = bm_ids
    check(bm_ids == "1","BM25 candidate meets the independent original-text expectation",bm_ids)
    highlighted = run("SELECT ts_headline('public.charabia',body,charabia.query_any('全文','public.charabia'),'MaxFragments=2') FROM documents WHERE id=1",database)
    check("<b>全文</b>" in highlighted and "PostgreSQL" in highlighted,"standard headline keeps original source bytes around the hit",highlighted)
    json_highlight = json.loads(run("SELECT ts_headline('public.charabia','{\"text\":\"CAFÉ & cafe\"}'::jsonb,plainto_tsquery('public.charabia','cafe'),'HighlightAll=true')",database))
    check(json_highlight == {"text":"<b>CAFÉ</b> &amp; <b>cafe</b>"},"JSONB uses the same original-text and escape contract",json_highlight)
    results["status"] = "passed"
except BaseException as error:
    results["status"] = "failed"
    results["error"] = repr(error)
    raise
finally:
    primary = sys.exception()
    checkpoint()
    if created:
        try:
            run(f"DROP DATABASE {database}")
            results["owned_database_removed"] = True
        except BaseException as error:
            results["owned_database_removed"] = False
            results["cleanup_error"] = repr(error)
            results["status"] = "cleanup-failed"
            if primary is None:
                raise
            primary.add_note(f"Owned documentation database cleanup also failed: {error}")
        finally:
            checkpoint()
print("README and reference SQL examples passed verbatim in an owned Linux database")
