"""End-to-end check with real gq processes against an isolated server (port 8788, temp data dir).

The server only sees this script's own process subtree and normal memory pressure, so the result does not
depend on what else runs on the machine. Run: python3 tests/live_check.py
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["BQ_DATA"] = tempfile.mkdtemp(prefix="bq-live-")
os.environ["BQ_PORT"] = "8788"
sys.path.insert(0, str(ROOT))
from bq import core, server  # noqa: E402

real_snapshot = server.system.snapshot_procs


def own_subtree():  # no ancestors: on a CI runner they include Runner.Worker, which takes a slot
    procs = real_snapshot()
    keep = {os.getpid()} | set(core.descendants(os.getpid(), procs))
    return {pid: p for pid, p in procs.items() if pid in keep}


server.system.snapshot_procs = own_subtree
server.system.memory = lambda: {"pressure": 1, "free_pct": 50, "total_gb": 0, "app_gb": 0, "wired_gb": 0,
                         "compressed_gb": 0, "cached_gb": 0, "free_gb": 0, "swap_used_gb": 0, "swap_total_gb": 0}
server.Q = server.Queue()
threading.Thread(target=server.Q.loop, daemon=True).start()
httpd = server.ThreadingHTTPServer(("127.0.0.1", 8788), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

env = {**os.environ, "BQ_URL": "http://127.0.0.1:8788", "CLAUDE_PID": "", "BQ_TICKET": ""}
gq = [sys.executable, str(ROOT / "bq/gq.py"), "run", "--"]
marks = Path(os.environ["BQ_DATA"]) / "marks"
marks.mkdir()
job = [sys.executable, "-c", "import os, sys, time\n"  # one file per mark: Windows appends are not atomic
       "def mark(kind):\n"
       "    open(os.path.join(sys.argv[1], kind + '-' + os.environ['N']), 'w').write(str(time.time()))\n"
       "mark('start'); time.sleep(3); mark('end')", str(marks)]
procs = []
for n in range(1, 6):
    procs.append(subprocess.Popen(gq + job, env={**env, "N": str(n)}, stderr=subprocess.PIPE))
    time.sleep(0.4)  # deterministic enqueue order
time.sleep(1)
procs[3].kill()  # job 4 dies while waiting: its ticket must be reaped, not block the queue
codes = [p.wait(timeout=60) for p in procs]

times = {f.name: float(f.read_text()) for f in marks.iterdir()}
starts = {k[6:]: t for k, t in times.items() if k.startswith("start-")}
ends = {k[4:]: t for k, t in times.items() if k.startswith("end-")}
order = sorted(starts, key=starts.get)
peak = max(sum(1 for n in starts if starts[n] <= t < ends[n]) for t in starts.values())
print("start order:", order, "| peak concurrency:", peak, "| exit codes:", codes)
# FIFO by the server's grant times: jobs granted in the same tick may write their start marks in any order
with server.Q.lock:
    granted = dict(server.Q.db.execute("select id, granted from tickets where id <= 5").fetchall())
assert set(order[:2]) == {"1", "2"} and set(order[2:]) == {"3", "5"}, order
assert granted[1] <= granted[3] and granted[2] <= granted[3] <= granted[5] and granted[4] is None, granted
assert peak == 2, peak
assert codes[3] != 0 and all(c == 0 for i, c in enumerate(codes) if i != 3), codes

nested = subprocess.run(gq + [sys.executable, str(ROOT / "bq/gq.py"), "run", "--",
                              sys.executable, "-c", "print('nested-ok')"],
                        env=env, capture_output=True, text=True, timeout=30)
assert "nested-ok" in nested.stdout, nested
if os.name != "nt":  # an alarm armed outside gq (perl -e 'alarm N; exec @ARGV' gq run -- ...) must stop the child
    pid_file = marks / "alarm-child"
    alarmed = subprocess.run([sys.executable, "-c", "import os, signal, sys; signal.alarm(2); "
                              "os.execv(sys.executable, [sys.executable] + sys.argv[1:])", *gq[1:],
                              sys.executable, "-c", "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); "
                              "time.sleep(60)", str(pid_file)], env=env, timeout=30)
    child = int(pid_file.read_text())
    time.sleep(0.5)
    try:
        os.kill(child, 0)
        child_alive = True
    except ProcessLookupError:
        child_alive = False
    assert alarmed.returncode != 0 and not child_alive, (alarmed.returncode, child)

with server.Q.lock:
    left = server.Q.db.execute("select count(*) from tickets where state!='done'").fetchone()[0]
assert left == 0, left
print("live check OK: FIFO order, max 2 concurrent, dead waiter reaped, nested run reuses the slot, "
      "outside alarm stops the child")
