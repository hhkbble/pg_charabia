"""Small real-psql helpers; no simulated PostgreSQL behavior."""
import json
import os
from pathlib import Path
import queue
import sys
import subprocess
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from report_contract import FORMAT, quality_case, validate_quality

if not __debug__:
    raise RuntimeError("Test assertions must not be disabled")

PSQL = str(Path(subprocess.check_output([os.environ["PG_CONFIG"], "--bindir"], text=True).strip()) / "psql")
EVIDENCE = Path(os.environ["PGCB_RUN_DIR"])

def literal(value):
    return "'" + value.replace("'", "''") + "'"

def run(sql, database=None, check=True, timeout=120, env=None):
    argv = [PSQL, "-XAtq", "-v", "ON_ERROR_STOP=1"]
    if database:
        argv += ["-d", database]
    result = subprocess.run(argv, input=sql, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    if check and result.returncode:
        raise AssertionError(f"psql failed ({result.returncode}): {result.stderr}\n{sql[:1000]}")
    return result if not check else result.stdout.strip()

def json_query(sql):
    return json.loads(run(sql))

class Session:
    """Keep one real backend alive across statements, errors and observations."""
    def __init__(self, database=None, env=None):
        argv = [PSQL, "-XAtq", "-v", "ON_ERROR_STOP=0", "-v", "VERBOSITY=verbose"]
        if database:
            argv += ["-d",database]
        self.environment = env
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True, env=env)
        self.lines = queue.Queue()
        def read():
            for line in self.process.stdout:
                self.lines.put(line.rstrip("\n"))
            self.lines.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.database = database
        self.pid = None
        try:
            self.query("SET standard_conforming_strings=on")
            self.pid = int(self.query("SELECT pg_backend_pid()"))
        except BaseException:
            self.process.kill()
            self.process.wait()
            self.reader.join(timeout=5)
            raise

    def query(self, sql, timeout=60):
        marker = "pgcb_" + uuid.uuid4().hex
        self.process.stdin.write(sql + ";\n\\echo " + marker + "\n")
        self.process.stdin.flush()
        lines = []
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.01, deadline-time.monotonic()))
            except queue.Empty as error:
                self.cancel()
                raise AssertionError("psql statement timed out: " + "\n".join(lines)) from error
            if line is None:
                raise AssertionError("Backend/psql exited before statement marker: " + "\n".join(lines))
            if line == marker:
                return "\n".join(lines).strip()
            lines.append(line)

    def value(self, sql):
        output = self.query(sql)
        if "ERROR:" in output:
            raise AssertionError(output)
        return output

    def cancel(self):
        if self.process.poll() is None and self.pid is not None:
            result = run(f"SELECT pg_cancel_backend({int(self.pid)})", database=self.database, check=False, timeout=10, env=self.environment)
            if result.returncode:
                raise AssertionError(f"Failed to cancel backend {self.pid}: {result.stderr}")

    def close(self):
        cleanup_error = None
        if self.process.poll() is None:
            try:
                self.cancel()
                self.process.stdin.write("\\q\n")
                self.process.stdin.flush()
                self.process.wait(timeout=15)
            except BaseException as error:
                cleanup_error = error
            finally:
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait()
        self.reader.join(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()
        if self.reader.is_alive():
            raise AssertionError("psql reader thread did not join")
        if cleanup_error is not None:
            raise AssertionError("psql failed to exit after backend cancellation") from cleanup_error
        if self.process.returncode:
            raise AssertionError(f"psql exited {self.process.returncode}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self.close()
        except BaseException as cleanup_error:
            if args[1] is None:
                raise
            args[1].add_note(f"psql cleanup also failed: {cleanup_error}")

def save(name, value):
    (EVIDENCE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def rss(pid):
    if os.environ["PGCB_PLATFORM"] == "linux-amd64":
        with open(f"/proc/{pid}/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"): return int(line.split()[1])*1024
        raise AssertionError("Missing backend RSS")
    return int(subprocess.check_output(["ps","-o","rss=","-p",str(pid)],text=True).strip())*1024


def record_quality_case(case_id, *, input, query, config, expected, actual, known_signature=None):
    """Save exact domain expectations separately from mechanism assertions.

    Only the three reviewed source fixtures may be known failures. A caller
    signature is redundant evidence and cannot redefine their fields. A changed
    result (including an unexpected improvement) is a regression until reviewed
    in the fixture. This helper never catches test failures.
    """
    path = EVIDENCE / "quality-results.json"
    document = json.loads(path.read_text()) if path.exists() else {"format": FORMAT, "cases": []}
    validate_quality(document)
    if any(case["id"] == case_id for case in document["cases"]):
        raise AssertionError(f"Quality case recorded twice: {case_id}")
    case = quality_case(case_id, input=input, query=query, config=config, expected=expected, actual=actual, known_signature=known_signature)
    document["cases"].append(case)
    validate_quality(document)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
    if case["status"] == "regression":
        raise AssertionError(f"Unreviewed quality result for {case_id}; see {path}")
    return case


def native_config_sql(name, stem=False, discard=False, normalize=True):
    """Explicit native DDL test fixture; each language owns a normal dictionary."""
    import re
    if not re.fullmatch(r'[a-z_][a-z_0-9]{0,35}\.[a-z_][a-z_0-9]{0,35}', name):
        raise ValueError('Fixture name must be a short qualified SQL identifier')
    schema, base = name.split('.')
    boolean = lambda flag: 'true' if flag else 'false'
    return f"""CREATE TEXT SEARCH CONFIGURATION {name}(PARSER=charabia.parser);
DO $fixture$ DECLARE l jsonb; BEGIN
 FOR l IN SELECT value FROM jsonb_array_elements(charabia.analysis_info()->'languages') LOOP
  EXECUTE format('CREATE TEXT SEARCH DICTIONARY %I.%I(TEMPLATE=charabia.template,Language=%L,Discard=%s,Stem=%s,Normalize={boolean(normalize)},Filter=false)',
   {literal(schema)},{literal(base)}||'_'||(l->>'language'),l->>'language',
   ({boolean(discard)} AND (l->>'has_stopwords')::boolean)::text,
   ({boolean(stem)} AND l->>'stemmer' IS NOT NULL)::text);
  EXECUTE format('ALTER TEXT SEARCH CONFIGURATION {name} ADD MAPPING FOR %I WITH %I.%I',l->>'language',{literal(schema)},{literal(base)}||'_'||(l->>'language'));
 END LOOP;
END $fixture$;"""
