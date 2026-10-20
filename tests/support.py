"""Small real-psql helpers; no simulated PostgreSQL behavior."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

if not __debug__:
    raise RuntimeError("Test assertions must not be disabled")

PSQL = str(Path(subprocess.check_output([os.environ["PG_CONFIG"], "--bindir"], text=True).strip()) / "psql")
EVIDENCE = Path(os.environ["PGCB_RUN_DIR"])

def literal(value):
    return "'" + value.replace("'", "''") + "'"

def run(sql, database=None, check=True, timeout=120):
    argv = [PSQL, "-XAtq", "-v", "ON_ERROR_STOP=1"]
    if database:
        argv += ["-d", database]
    result = subprocess.run(argv, input=sql, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        raise AssertionError(f"psql failed ({result.returncode}): {result.stderr}\n{sql[:1000]}")
    return result if not check else result.stdout.strip()

def json_query(sql):
    return json.loads(run(sql))

class Session:
    """Keep one real backend alive across statements, errors and observations."""
    def __init__(self, database=None):
        argv = [PSQL, "-XAtq", "-v", "ON_ERROR_STOP=0", "-v", "VERBOSITY=verbose"]
        if database:
            argv += ["-d",database]
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.lines = queue.Queue()
        def read():
            for line in self.process.stdout:
                self.lines.put(line.rstrip("\n"))
            self.lines.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.query("SET standard_conforming_strings=on")
        self.pid = int(self.query("SELECT pg_backend_pid()"))

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

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.write("\\q\n")
            self.process.stdin.flush()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise AssertionError("psql failed to exit")
        self.reader.join(timeout=2)
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
