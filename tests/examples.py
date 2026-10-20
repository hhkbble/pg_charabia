"""Run the published SQL examples verbatim in an owned empty database."""
import hashlib
import os
from pathlib import Path
import re
from support import run, save

readme=Path(__file__).resolve().parent.parent/"README.md"
examples=re.findall(r"```sql\n(.*?)```",readme.read_text(),re.S)
assert len(examples)==3,len(examples)
database="pgcb_examples_"+str(os.getpid())
results={"readme_sha256":hashlib.sha256(readme.read_bytes()).hexdigest(),"examples":[]}
run(f"CREATE DATABASE {database} TEMPLATE template0")
try:
    for number,sql in enumerate(examples):
        output=run(sql,database)
        results["examples"].append({"number":number+1,"sql":sql,"output":output})
    assert run("SELECT count(*) FROM pg_ts_config_map WHERE mapcfg='public.app_search'::regconfig",database)=="18"
    assert run("SELECT to_tsvector('public.app_search','the The')",database)=="'the':2"
    assert run("SELECT id FROM documents WHERE to_tsvector('charabia.default_v1',body) @@ plainto_tsquery('charabia.default_v1','POSTGRESQL')",database)=="1"
finally:
    save("documentation-examples.json",results)
    run(f"DROP DATABASE {database}")
print("README SQL examples passed verbatim in an owned database")
