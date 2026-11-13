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

real_snapshot = server.snapshot_procs


def own_subtree():
    procs = real_snapshot()
    keep = {os.getpid()} | set(core.descendants(os.getpid(), procs))
    keep |= {a.pid for pid in list(keep) for a in core.ancestors(pid, procs)}
    return {pid: p for pid, p in procs.items() if pid in keep}


server.snapshot_procs = own_subtree
server.memory = lambda: {"pressure": 1, "free_pct": 50, "total_gb": 0, "app_gb": 0, "wired_gb": 0,
                         "compressed_gb": 0, "cached_gb": 0, "free_gb": 0, "swap_used_gb": 0, "swap_total_gb": 0}
server.Q = server.Queue()
threading.Thread(target=server.Q.loop, daemon=True).start()
httpd = server.ThreadingHTTPServer(("127.0.0.1", 8788), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

env = {**os.environ, "BQ_URL": "http://127.0.0.1:8788", "CLAUDE_PID": "", "BQ_TICKET": ""}
gq = [sys.executable, str(ROOT / "bq/gq.py"), "run", "--"]
log = Path(os.environ["BQ_DATA"]) / "runs.log"
job = f'echo "start $N $(python3 -c "import time;print(time.time())")" >> {log}; sleep 3; ' \
      f'echo "end $N $(python3 -c "import time;print(time.time())")" >> {log}'
procs = []
for n in range(1, 6):
    procs.append(subprocess.Popen(gq + ["bash", "-c", job], env={**env, "N": str(n)}, stderr=subprocess.PIPE))
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

nested = subprocess.run(gq + [sys.executable, str(ROOT / "bq/gq.py"), "run", "--", "echo", "nested-ok"],
                        env=env, capture_output=True, text=True, timeout=30)
assert "nested-ok" in nested.stdout, nested
with server.Q.lock:
    left = server.Q.db.execute("select count(*) from tickets where state!='done'").fetchone()[0]
assert left == 0, left
print("live check OK: FIFO order, max 2 concurrent, dead waiter reaped, nested run reuses the slot")
