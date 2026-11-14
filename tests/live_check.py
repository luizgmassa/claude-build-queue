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
log = Path(os.environ["BQ_DATA"]) / "runs.log"
job = [sys.executable, "-c", "import os, sys, time\n"
       "def mark(kind):\n"
       "    with open(sys.argv[1], 'a') as f: f.write(f'{kind} {os.environ[\"N\"]} {time.time()}\\n')\n"
       "mark('start'); time.sleep(3); mark('end')", str(log)]
procs = []
for n in range(1, 6):
    procs.append(subprocess.Popen(gq + job, env={**env, "N": str(n)}, stderr=subprocess.PIPE))
    time.sleep(0.4)  # deterministic enqueue order
time.sleep(1)
procs[3].kill()  # job 4 dies while waiting: its ticket must be reaped, not block the queue
codes = [p.wait(timeout=60) for p in procs]

events = [line.split() for line in log.read_text().splitlines()]
starts = {n: float(t) for kind, n, t in events if kind == "start"}
ends = {n: float(t) for kind, n, t in events if kind == "end"}
order = sorted(starts, key=starts.get)
peak = max(sum(1 for n in starts if starts[n] <= t < ends[n]) for t in starts.values())
print("start order:", order, "| peak concurrency:", peak, "| exit codes:", codes)
assert order == ["1", "2", "3", "5"], order
assert peak == 2, peak
assert codes[3] != 0 and all(c == 0 for i, c in enumerate(codes) if i != 3), codes

nested = subprocess.run(gq + [sys.executable, str(ROOT / "bq/gq.py"), "run", "--",
                              sys.executable, "-c", "print('nested-ok')"],
                        env=env, capture_output=True, text=True, timeout=30)
assert "nested-ok" in nested.stdout, nested
with server.Q.lock:
    left = server.Q.db.execute("select count(*) from tickets where state!='done'").fetchone()[0]
assert left == 0, left
print("live check OK: FIFO order, max 2 concurrent, dead waiter reaped, nested run reuses the slot")
