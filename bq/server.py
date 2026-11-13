"""Build queue server: FIFO slots for Gradle/JVM runs, memory monitor, cleanup, and a localhost page.

One scheduler thread owns every decision (grant, reap, kill). HTTP handlers only enqueue, release and
read, so agents never race each other: the lock serializes them and the tick decides.
"""

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from glob import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bq import core  # noqa: E402

PORT = int(os.environ.get("BQ_PORT", "8787"))
DATA = Path(os.environ.get("BQ_DATA", Path.home() / ".local/share/build-queue"))
CONFIG = DATA / "config.json"
PAGE = Path(__file__).with_name("page.html")
TICK_S = 2.0


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError) as e:
        log(f"command failed: {cmd[0]}: {e}")
        return ""


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, file=sys.stderr, flush=True)


def snapshot_procs():
    fmt = "pid=,ppid=,rss=,pcpu=,etime=,stat=,lstart=,command="
    with_env = run(["ps", "-axEww", "-o", fmt])
    plain = run(["ps", "-axww", "-o", fmt])
    envs = {}
    for line in with_env.splitlines():
        parts = line.split(None, 11)
        if len(parts) == 12:
            envs[int(parts[0])] = parts[11]
    procs = {}
    for line in plain.splitlines():
        parts = line.split(None, 11)
        if len(parts) < 12 or parts[5].startswith("Z"):  # a zombie is dead even before its parent reaps it
            continue
        pid = int(parts[0])
        args = parts[11]
        full = envs.get(pid, "")
        env_text = full[len(args):] if full.startswith(args) else ""
        procs[pid] = core.Proc(pid=pid, ppid=int(parts[1]), rss_kb=int(parts[2]), cpu=float(parts[3]),
                               etime=parts[4], lstart=" ".join(parts[6:11]), args=args,
                               env=core.parse_env(env_text))
    return procs


def tcp_links():
    """Returns (listen ports by pid, established (pid, local, remote) triples) for java processes."""
    out = run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN,ESTABLISHED", "-a", "-c", "java", "-F", "pnT"])
    listen, est, pid, name = {}, [], None, None
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid = int(val)
        elif tag == "n":
            name = val
        elif tag == "T" and val.startswith("ST=") and pid and name:
            if val == "ST=LISTEN":
                listen.setdefault(pid, set()).add(name.rsplit(":", 1)[-1])
            elif val == "ST=ESTABLISHED" and "->" in name:
                local, remote = name.split("->", 1)
                est.append((pid, local.rsplit(":", 1)[-1], remote.rsplit(":", 1)[-1]))
    return listen, est


def busy_daemons(procs, listen, est):
    """A daemon is busy while a client of the right kind holds a connection to one of its listen ports."""
    port_owner = {port: pid for pid, ports in listen.items() for port in ports}
    busy, serving = set(), {}
    for pid, _local, remote in est:
        client, server = procs.get(pid), procs.get(port_owner.get(remote))
        if not client or not server or client.pid == server.pid:
            continue
        if server.kind == "gradle_daemon" and client.kind == "gradle_client":
            busy.add(server.pid)
            serving[server.pid] = client.pid
        elif server.kind == "kotlin_daemon" and client.kind == "gradle_daemon":
            busy.add(server.pid)
    return busy, serving


def memory():
    names = ["hw.memsize", "vm.swapusage", "kern.memorystatus_vm_pressure_level", "kern.memorystatus_level",
             "hw.pagesize"]
    vals = {}
    for line in run(["sysctl"] + names).splitlines():
        k, _, v = line.partition(":")
        vals[k.strip()] = v.strip()
    swap = dict(re.findall(r"(total|used|free) = ([\d.]+)M", vals.get("vm.swapusage", "")))
    page = int(vals.get("hw.pagesize", "16384") or 16384)
    vm = {}
    for line in run(["vm_stat"]).splitlines():
        m = re.match(r'"?([^":]+)"?:\s+(\d+)', line)
        if m:
            vm[m.group(1).strip()] = int(m.group(2)) * page
    gb = 1024 ** 3
    total = int(vals.get("hw.memsize", "0") or 0)
    app = vm.get("Anonymous pages", 0) - vm.get("Pages purgeable", 0)
    return {
        "total_gb": round(total / gb, 1),
        "app_gb": round(app / gb, 1),
        "wired_gb": round(vm.get("Pages wired down", 0) / gb, 1),
        "compressed_gb": round(vm.get("Pages occupied by compressor", 0) / gb, 1),
        "cached_gb": round(vm.get("File-backed pages", 0) / gb, 1),
        "free_gb": round(vm.get("Pages free", 0) / gb, 1),
        "free_pct": int(vals.get("kern.memorystatus_level", "0") or 0),
        "pressure": int(vals.get("kern.memorystatus_vm_pressure_level", "1") or 1),
        "swap_used_gb": round(float(swap.get("used", 0)) / 1024, 1),
        "swap_total_gb": round(float(swap.get("total", 0)) / 1024, 1),
    }


class Titles:
    """Agent labels: ralphy session number and the Claude session title, cached by transcript size."""

    def __init__(self):
        self.cache = {}

    def session_of(self, ralphy):
        path = Path.home() / f".ralphy/sessions/{ralphy}.agent-status.jsonl"
        try:
            with open(path, "rb") as f:
                f.seek(max(0, path.stat().st_size - 8192))
                for line in reversed(f.read().decode(errors="ignore").splitlines()):
                    m = re.search(r'"session_id":"([0-9a-f-]{36})"', line)
                    if m and '"agent_id":null' in line:
                        return m.group(1)
        except OSError:
            pass
        return None

    def title_of(self, sid):
        files = glob(str(Path.home() / f".claude/projects/*/{sid}.jsonl"))
        if not files:
            return ""
        path = files[0]
        size = os.path.getsize(path)
        hit = self.cache.get(sid)
        if hit and hit[0] == size:
            return hit[1]
        title, offset = (hit[1], hit[2]) if hit else ("", 0)
        with open(path, "rb") as f:
            f.seek(offset)
            for m in re.finditer(rb'"customTitle":"([^"]*)"', f.read()):
                title = m.group(1).decode(errors="ignore")
        self.cache[sid] = (size, title, size)
        return title


class Queue:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DATA / "queue.db", check_same_thread=False, isolation_level=None)
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("""create table if not exists tickets (
            id integer primary key autoincrement, pid int, pid_start text, claude_pid int, ralphy text,
            session_id text, cwd text, cmd text, state text, created real, granted real, ended real,
            end_reason text, exit_code int)""")
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.wake = threading.Event()
        self.events = deque(maxlen=300)
        self.idle_since = {}
        self.pending_kills = {}  # pid -> (lstart, sigkill deadline, reason)
        self.last_grant = None
        self.view = {}
        self.blocked = ""
        self.titles = Titles()

    def event(self, kind, text):
        self.events.appendleft({"ts": time.strftime("%H:%M:%S"), "kind": kind, "text": text})
        log(f"{kind}: {text}")

    def enforce(self):
        try:
            return bool(json.loads(CONFIG.read_text()).get("enforce", False))
        except (OSError, ValueError):
            return False

    def active_tickets(self):
        rows = self.db.execute("select id, pid, state, created, granted, pid_start from tickets "
                               "where state in ('waiting','granted') order by id").fetchall()
        return [core.Ticket(*r) for r in rows]

    def finish(self, tid, reason, exit_code=None):
        self.db.execute("update tickets set state='done', ended=?, end_reason=?, exit_code=? "
                        "where id=? and state!='done'", (time.time(), reason, exit_code, tid))

    # ------------------------------------------------------------ scheduler tick

    def tick(self):
        snap_ts = time.time()
        procs = snapshot_procs()
        listen, est = tcp_links()
        mem = memory()
        roots = core.classify(procs, protected_pids={os.getpid()})
        busy, serving = busy_daemons(procs, listen, est)
        now = time.time()
        for p in procs.values():
            if p.kind in ("gradle_daemon", "kotlin_daemon"):
                if p.pid in busy:
                    self.idle_since.pop(p.pid, None)
                else:
                    self.idle_since.setdefault(p.pid, now)
        for pid in list(self.idle_since):
            if pid not in procs:
                del self.idle_since[pid]
        live_claude = {p.pid for p in procs.values() if p.kind == "claude"}
        enforce = self.enforce()
        with self.lock:
            tickets = self.active_tickets()
            pl = core.plan(procs, roots, tickets, snapshot_ts=snap_ts, now=now, pressure=mem["pressure"],
                           busy_daemons=busy, idle_since=self.idle_since, last_grant_ts=self.last_grant,
                           live_claude_pids=live_claude, slots=core.SLOTS)
            for tid, start in pl.pid_starts.items():
                self.db.execute("update tickets set pid_start=? where id=?", (start, tid))
            for tid, reason in pl.reaps:
                self.finish(tid, reason)
                self.event("reap", f"ticket #{tid}: {reason}")
            for tid in pl.grants:
                self.db.execute("update tickets set state='granted', granted=? where id=?", (now, tid))
                self.last_grant = now
                self.event("grant", f"ticket #{tid} entrou na vaga")
            if pl.grants or pl.reaps:
                self.cond.notify_all()
            self.blocked = pl.blocked
            would_kill = []
            for pid, reason in pl.kills:
                if enforce:
                    self.kill(pid, reason, procs)
                else:
                    would_kill.append({"pid": pid, "reason": reason})
            self.escalate(procs)
            self.view = self.build_view(procs, roots, pl, mem, serving, enforce, would_kill)

    def kill(self, root, reason, procs):
        tree = [root] + core.descendants(root, procs)
        for pid in reversed(tree):
            p = procs.get(pid)
            if p is None or pid in (1, os.getpid()) or p.kind == "claude" or p.owner in ("runner", "protected"):
                continue
            if pid in self.pending_kills:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                self.pending_kills[pid] = (p.lstart, time.time() + core.TERM_GRACE_S, reason)
            except ProcessLookupError:
                pass
            except PermissionError as e:
                self.event("erro", f"sem permissão para matar {pid}: {e}")
        p = procs.get(root)
        self.event("kill", f"SIGTERM {root} ({p.kind if p else '?'}): {reason}")

    def escalate(self, procs):
        now = time.time()
        for pid, (start, deadline, reason) in list(self.pending_kills.items()):
            p = procs.get(pid)
            if p is None or p.lstart != start:
                del self.pending_kills[pid]
            elif now >= deadline:
                try:
                    os.kill(pid, signal.SIGKILL)
                    self.event("kill", f"SIGKILL {pid} após {core.TERM_GRACE_S}s: {reason}")
                except ProcessLookupError:
                    pass
                del self.pending_kills[pid]

    # ------------------------------------------------------------ view

    def agent_label(self, claude_pid, procs):
        p = procs.get(claude_pid)
        if not p:
            return f"claude {claude_pid} (encerrado)"
        m = re.search(r"/sessions/(\d+)\.settings\.json", p.args)
        if not m:
            return f"claude {claude_pid}"
        sid = self.titles.session_of(m.group(1))
        title = self.titles.title_of(sid) if sid else ""
        return f"ralphy #{m.group(1)}" + (f" · {title}" if title else "")

    def build_view(self, procs, roots, pl, mem, serving, enforce, would_kill):
        agents = {}
        for p in procs.values():
            if p.kind == "claude":
                agents.setdefault(p.pid, {"rss": 0, "procs": 0})["rss"] += p.rss_kb
        relevant = []
        for p in procs.values():
            cp = p.env.get("CLAUDE_PID", "")
            if p.owner == "agent" and cp.isdigit():
                a = agents.setdefault(int(cp), {"rss": 0, "procs": 0})
                a["rss"] += p.rss_kb
                a["procs"] += 1
            if p.kind in core.AGENT_EXEC_KINDS | core.INFRA_KINDS | {"gq", "runner_worker", "embedded_pg"} \
                    or p.root is not None:
                owner = p.owner
                if p.owner == "agent" and cp.isdigit():
                    owner = self.agent_label(int(cp), procs)
                relevant.append({"pid": p.pid, "ppid": p.ppid, "kind": p.kind, "owner": owner,
                                 "rss_mb": p.rss_kb // 1024, "cpu": p.cpu, "etime": p.etime,
                                 "status": p.status, "ticket": p.env.get("BQ_TICKET", ""),
                                 "serving": serving.get(p.pid), "cmd": p.args[:220]})
        relevant.sort(key=lambda r: -r["rss_mb"])
        rows = self.db.execute("select id, pid, claude_pid, ralphy, cwd, cmd, state, created, granted, ended, "
                               "end_reason, exit_code from tickets where state!='done' or ended>? "
                               "order by id", (time.time() - 3600,)).fetchall()
        cols = ["id", "pid", "claude_pid", "ralphy", "cwd", "cmd", "state", "created", "granted", "ended",
                "end_reason", "exit_code"]
        tickets = [dict(zip(cols, r)) for r in rows]
        for t in tickets:
            t["agent"] = self.agent_label(t["claude_pid"], procs) if t["claude_pid"] else "?"
        return {
            "ts": time.strftime("%H:%M:%S"), "enforce": enforce, "slots": core.SLOTS, "used": pl.used,
            "counts": pl.counts, "blocked": pl.blocked, "memory": mem, "would_kill": would_kill,
            "agents": sorted(({"pid": k, "label": self.agent_label(k, procs), "rss_mb": v["rss"] // 1024,
                               "procs": v["procs"], "alive": k in procs} for k, v in agents.items()),
                             key=lambda a: -a["rss_mb"]),
            "processes": relevant, "tickets": tickets, "events": list(self.events)[:80],
        }

    def loop(self):
        while True:
            self.wake.wait(TICK_S)
            self.wake.clear()
            try:
                self.tick()
            except Exception as e:  # the scheduler must survive one bad snapshot
                log(f"tick failed: {e!r}")

    # ------------------------------------------------------------ API

    def enqueue(self, body):
        with self.lock:
            cur = self.db.execute(
                "insert into tickets (pid, claude_pid, ralphy, session_id, cwd, cmd, state, created) "
                "values (?,?,?,?,?,?, 'waiting', ?)",
                (int(body["pid"]), body.get("claude_pid"), body.get("ralphy"), body.get("session_id"),
                 str(body.get("cwd", ""))[:300], str(body.get("cmd", ""))[:1000], time.time()))
            tid = cur.lastrowid
        self.event("enqueue", f"ticket #{tid} pid {body['pid']}: {str(body.get('cmd', ''))[:100]}")
        self.wake.set()
        return {"id": tid}

    def status(self, tid):
        row = self.db.execute("select state, end_reason from tickets where id=?", (tid,)).fetchone()
        if not row:
            return {"state": "unknown"}
        ahead = self.db.execute("select count(*) from tickets where state='waiting' and id<?", (tid,)).fetchone()
        waiting = self.db.execute("select count(*) from tickets where state='waiting'").fetchone()
        return {"state": row[0], "reason": row[1], "position": ahead[0] + 1, "waiting": waiting[0],
                "blocked": self.blocked}

    def wait(self, tid, timeout):
        deadline = time.time() + min(timeout, 30)
        with self.cond:
            while True:
                st = self.status(tid)
                left = deadline - time.time()
                if st["state"] != "waiting" or left <= 0:
                    return st
                self.cond.wait(left)

    def release(self, tid, exit_code):
        with self.lock:
            self.finish(tid, "released", exit_code)
            self.cond.notify_all()
        self.event("release", f"ticket #{tid} liberou a vaga (exit {exit_code})")
        self.wake.set()
        return {"ok": True}


Q = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, code=200, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the waiting gq died; the scheduler reaps its ticket

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                self.reply(PAGE.read_bytes(), ctype="text/html; charset=utf-8")
            elif u.path == "/api/state":
                self.reply(Q.view)
            elif u.path == "/api/wait":
                self.reply(Q.wait(int(q["id"][0]), float(q.get("timeout", ["25"])[0])))
            elif u.path == "/api/ticket":
                with Q.lock:
                    self.reply(Q.status(int(q["id"][0])))
            elif u.path == "/api/wake":
                Q.wake.set()
                self.reply({"ok": True})
            else:
                self.reply({"error": "not found"}, 404)
        except (KeyError, ValueError) as e:
            self.reply({"error": f"bad request: {e}"}, 400)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)) or b"{}")
            if u.path == "/api/enqueue":
                self.reply(Q.enqueue(body))
            elif u.path == "/api/release":
                self.reply(Q.release(int(body["id"]), body.get("exit_code")))
            else:
                self.reply({"error": "not found"}, 404)
        except (KeyError, ValueError, TypeError) as e:
            self.reply({"error": f"bad request: {e}"}, 400)


def main():
    global Q
    Q = Queue()
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps({"enforce": False}, indent=1))
    threading.Thread(target=Q.loop, daemon=True, name="scheduler").start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    log(f"build-queue listening on http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
