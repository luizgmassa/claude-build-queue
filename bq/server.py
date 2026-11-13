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
DATA = core.DATA
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
    busy, serving, kotlin_of = set(), {}, {}
    for pid, _local, remote in est:
        client, server = procs.get(pid), procs.get(port_owner.get(remote))
        if not client or not server or client.pid == server.pid:
            continue
        if server.kind == "gradle_daemon" and client.kind == "gradle_client":
            busy.add(server.pid)
            serving[server.pid] = client.pid
        elif server.kind == "kotlin_daemon" and client.kind == "gradle_daemon":
            busy.add(server.pid)
            kotlin_of.setdefault(client.pid, set()).add(server.pid)
    return busy, serving, kotlin_of


def ticket_rss_gb(gq_pid, tid, procs, serving, kotlin_of):
    """Memory of one queued build: its own tree, the Gradle daemon serving it with that daemon's workers,
    and the Kotlin daemon that daemon talks to."""
    own = {gq_pid, *core.descendants(gq_pid, procs)} | {p.pid for p in procs.values()
                                                       if p.env.get("BQ_TICKET") == str(tid)}
    daemons = {d for d, client in serving.items() if client in own}
    pids = set(own) | daemons
    for d in daemons:
        pids |= set(core.descendants(d, procs)) | kotlin_of.get(d, set())
    return sum(procs[x].rss_kb for x in pids if x in procs) / 1024 ** 2


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


def cwd_of(pid):
    for line in run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=5).splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""


def read_calls():
    """The hook's recent `gq run` records; trims the file once it passes 1 MB."""
    try:
        with open(core.CALLS, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - 65536))
            lines = f.read().decode(errors="ignore").splitlines()[1:] if size > 65536 else \
                f.read().decode(errors="ignore").splitlines()
    except OSError:
        return []
    if size > 1 << 20:  # shortcut: a hook append racing this rewrite is lost; it only costs one label
        tmp = core.CALLS.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, core.CALLS)
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


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
                    if m:  # subagent lines carry the parent session id too
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
        if "ctype" not in {r[1] for r in self.db.execute("pragma table_info(tickets)")}:
            self.db.execute("alter table tickets add column ctype text")
            self.db.execute("alter table tickets add column peak_gb real")
        if "subagent" not in {r[1] for r in self.db.execute("pragma table_info(tickets)")}:
            self.db.execute("alter table tickets add column subagent text")
        self.db.execute("create table if not exists peaks (ctype text, peak_gb real, ts real, ticket int)")
        for tid, cmd in self.db.execute("select id, cmd from tickets where ctype is null and state!='done'").fetchall():
            self.db.execute("update tickets set ctype=? where id=?", (core.command_type(cmd or ""), tid))
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.wake = threading.Event()
        self.events = deque(maxlen=300)
        self.idle_since = {}
        self.pending_kills = {}  # pid -> (lstart, sigkill deadline, reason)
        self.last_grant = None
        self.view = {}
        self.blocked = ""
        self.limit = core.SLOTS  # restarts at the base: running builds above it just block new ones
        self.holders = set()
        self.last_raise = None
        self.peak = {}  # granted ticket id -> largest memory seen, GB
        self.taken_calls = set()  # hook records already matched to a ticket
        self.sub_tickets = {}  # active ticket id -> subagent, so its processes carry the subagent label
        self.procs, self.mem = {}, {"total_gb": 0, "free_pct": 0}  # last snapshot, for the detail dialogs
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
        peak = self.peak.pop(tid, None)
        cur = self.db.execute("update tickets set state='done', ended=?, end_reason=?, exit_code=?, peak_gb=? "
                              "where id=? and state!='done'", (time.time(), reason, exit_code, peak, tid))
        if cur.rowcount and peak and reason == "released":  # a reaped gq died mid-run: its peak is partial
            ctype = self.db.execute("select ctype from tickets where id=?", (tid,)).fetchone()[0]
            if ctype:
                self.db.execute("insert into peaks values (?,?,?,?)", (ctype, round(peak, 2), time.time(), tid))

    def peaks(self):
        out = {}
        for ctype, gb in self.db.execute("select ctype, peak_gb from peaks order by ts"):
            out.setdefault(ctype, []).append(gb)
        return out

    def estimates(self):
        peaks = self.peaks()
        rows = self.db.execute("select id, coalesce(ctype, 'other') from tickets where state='waiting'")
        return {tid: (ctype, core.estimate_gb(ctype, peaks)) for tid, ctype in rows}

    # ------------------------------------------------------------ scheduler tick

    def tick(self):
        snap_ts = time.time()
        procs = snapshot_procs()
        listen, est = tcp_links()
        mem = memory()
        roots = core.classify(procs, protected_pids={os.getpid()})
        busy, serving, kotlin_of = busy_daemons(procs, listen, est)
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
            for t in tickets:
                if t.state == "granted" and t.pid in procs:
                    gb = ticket_rss_gb(t.pid, t.id, procs, serving, kotlin_of)
                    self.peak[t.id] = max(self.peak.get(t.id, 0.0), gb)
            avail = mem["total_gb"] * mem["free_pct"] / 100
            pl = core.plan(procs, roots, tickets, snapshot_ts=snap_ts, now=now, pressure=mem["pressure"],
                           busy_daemons=busy, idle_since=self.idle_since, last_grant_ts=self.last_grant,
                           live_claude_pids=live_claude, limit=self.limit, prev_holders=self.holders,
                           last_raise_ts=self.last_raise, avail_gb=avail, estimates=self.estimates())
            if pl.lowered and pl.limit < self.limit:
                self.event("limite", f"limite {self.limit}→{pl.limit}: {pl.lowered} execução(ões) terminou(aram)")
            if pl.raised:
                self.last_raise = now
                self.event("limite", pl.raised)
            self.limit, self.holders = pl.limit, pl.holders
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
            self.procs, self.mem, self.serving = procs, mem, serving
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

    def agent_label(self, claude_pid, procs, subagent=None):
        sub = " subagent" if subagent else ""
        p = procs.get(claude_pid)
        if not p:
            return f"claude {claude_pid}{sub} (encerrado)"
        m = re.search(r"/sessions/(\d+)\.settings\.json", p.args)
        if not m:
            return f"claude {claude_pid}{sub}"
        sid = self.titles.session_of(m.group(1))
        title = self.titles.title_of(sid) if sid else ""
        return f"ralphy #{m.group(1)}{sub}" + (f" · {title}" if title else "")

    def proc_row(self, p, procs, total_kb, serving=None):
        cp = p.env.get("CLAUDE_PID", "")
        agent_pid = p.pid if p.kind == "claude" else int(cp) if p.owner == "agent" and cp.isdigit() else None
        tid = p.env.get("BQ_TICKET", "")
        sub = self.sub_tickets.get(int(tid)) if tid.isdigit() and p.kind != "claude" else None
        owner = self.agent_label(agent_pid, procs, sub) if agent_pid else p.owner
        return {"pid": p.pid, "ppid": p.ppid, "kind": p.kind, "owner": owner, "agent_pid": agent_pid,
                "rss_mb": p.rss_kb // 1024, "ram_pct": round(100 * p.rss_kb / total_kb, 2) if total_kb else 0,
                "cpu": p.cpu, "etime": p.etime, "status": p.status, "ticket": p.env.get("BQ_TICKET", ""),
                "serving": (serving or {}).get(p.pid), "cmd": p.args[:220]}

    def ticket_rows(self, where, args):
        cols = ["id", "pid", "claude_pid", "ralphy", "cwd", "cmd", "state", "created", "granted", "ended",
                "end_reason", "exit_code", "ctype", "peak_gb", "subagent"]
        rows = self.db.execute(f"select {', '.join(cols)} from tickets where {where} order by id", args)
        return [dict(zip(cols, r)) for r in rows.fetchall()]

    def proc_details(self, pid):
        """Everything known about one process, for the page's dialog."""
        with self.lock:
            procs, mem, serving = self.procs, self.mem, getattr(self, "serving", {})
            p = procs.get(pid)
            if p is None:
                return {"error": f"PID {pid} não existe mais"}
            total_kb = mem["total_gb"] * 1024 ** 2
            tree = [p] + [procs[d] for d in core.descendants(pid, procs)]
            out = self.proc_row(p, procs, total_kb, serving)
            tree_kb = sum(q.rss_kb for q in tree)
            out.update(cmd=p.args, lstart=p.lstart, env=p.env, root=p.root,
                       ancestors=[self.proc_row(a, procs, total_kb, serving) for a in core.ancestors(pid, procs)],
                       tree=[self.proc_row(q, procs, total_kb, serving) for q in tree],
                       tree_rss_mb=tree_kb // 1024, tree_ram_pct=round(100 * tree_kb / total_kb, 2) if total_kb else 0,
                       tickets=self.ticket_rows("id=?", (int(p.env["BQ_TICKET"]),))
                       if p.env.get("BQ_TICKET", "").isdigit() else [])
        out["cwd"] = cwd_of(pid)  # lsof outside the lock: it can take a moment
        return out

    def agent_details(self, claude_pid):
        """One agent: session, every process it owns with its memory share, and its tickets."""
        with self.lock:
            procs, mem = self.procs, self.mem
            total_kb = mem["total_gb"] * 1024 ** 2
            mine = {q.pid for q in procs.values() if q.env.get("CLAUDE_PID") == str(claude_pid)}
            if claude_pid in procs:
                mine |= {claude_pid, *core.descendants(claude_pid, procs)}
            rows = sorted((self.proc_row(procs[x], procs, total_kb) for x in mine), key=lambda r: -r["rss_mb"])
            rss_kb = sum(procs[x].rss_kb for x in mine)
            avail_kb = total_kb * mem["free_pct"] / 100
            claude = procs.get(claude_pid)
            m = re.search(r"/sessions/(\d+)\.settings\.json", claude.args) if claude else None
            sid = self.titles.session_of(m.group(1)) if m else None
            tickets = self.ticket_rows("claude_pid=? and (state!='done' or ended>?)",
                                       (claude_pid, time.time() - 3600))
            transcript = glob(str(Path.home() / f".claude/projects/*/{sid}.jsonl")) if sid else []
            return {"pid": claude_pid, "label": self.agent_label(claude_pid, procs), "alive": claude is not None,
                    "ralphy": m.group(1) if m else None, "session_id": sid,
                    "transcript": transcript[0] if transcript else None,
                    "etime": claude.etime if claude else None, "lstart": claude.lstart if claude else None,
                    "rss_mb": rss_kb // 1024, "ram_pct": round(100 * rss_kb / total_kb, 2) if total_kb else 0,
                    "avail_pct": round(100 * rss_kb / avail_kb, 1) if avail_kb else 0,
                    "total_gb": mem["total_gb"], "avail_gb": round(avail_kb / 1024 ** 2, 1),
                    "procs": rows, "tickets": tickets}

    def build_view(self, procs, roots, pl, mem, serving, enforce, would_kill):
        agents = {}
        for p in procs.values():
            if p.kind == "claude":
                agents.setdefault(p.pid, {"rss": 0, "procs": 0})["rss"] += p.rss_kb
        relevant = []
        self.sub_tickets = dict(self.db.execute(
            "select id, subagent from tickets where state!='done' and subagent is not null").fetchall())
        total_kb = mem["total_gb"] * 1024 ** 2
        for p in procs.values():
            cp = p.env.get("CLAUDE_PID", "")
            if p.owner == "agent" and cp.isdigit():
                a = agents.setdefault(int(cp), {"rss": 0, "procs": 0})
                a["rss"] += p.rss_kb
                a["procs"] += 1
            if p.kind in core.AGENT_EXEC_KINDS | core.INFRA_KINDS | {"gq", "runner_worker", "embedded_pg"} \
                    or p.root is not None:
                relevant.append(self.proc_row(p, procs, total_kb, serving))
        relevant.sort(key=lambda r: -r["rss_mb"])
        tickets = self.ticket_rows("state!='done' or ended>?", (time.time() - 3600,))
        peaks = self.peaks()
        for t in tickets:
            t["agent"] = self.agent_label(t["claude_pid"], procs, t["subagent"]) if t["claude_pid"] else "?"
            t["estimate_gb"] = round(core.estimate_gb(t["ctype"] or "other", peaks), 1)
            if t["state"] == "granted":
                t["peak_gb"] = round(self.peak.get(t["id"], 0.0), 1)
        return {
            "ts": time.strftime("%H:%M:%S"), "enforce": enforce, "slots": pl.limit, "used": pl.used,
            "base_slots": core.SLOTS, "max_slots": core.MAX_SLOTS, "headroom_gb": core.HEADROOM_GB,
            "avail_gb": round(mem["total_gb"] * mem["free_pct"] / 100, 1),
            "estimates": {ct: round(core.estimate_gb(ct, peaks), 1) for ct in core.DEFAULT_GB},
            "counts": pl.counts, "blocked": pl.blocked, "memory": mem, "would_kill": would_kill,
            "agents": sorted(({"pid": k, "label": self.agent_label(k, procs), "rss_mb": v["rss"] // 1024,
                               "ram_pct": round(100 * v["rss"] / total_kb, 1) if total_kb else 0,
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
        now = time.time()
        records = read_calls()
        with self.lock:
            call = core.match_call(records, body.get("session_id"), now, self.taken_calls) or {}
            if call:
                self.taken_calls.add(call.get("id"))
            subagent = (call.get("agent_type") or call.get("agent_id")) if call.get("agent_id") else None
            cur = self.db.execute(
                "insert into tickets (pid, claude_pid, ralphy, session_id, cwd, cmd, state, created, ctype, subagent) "
                "values (?,?,?,?,?,?, 'waiting', ?, ?, ?)",
                (int(body["pid"]), body.get("claude_pid"), body.get("ralphy"), body.get("session_id"),
                 str(body.get("cwd", ""))[:300], str(body.get("cmd", ""))[:1000], now,
                 core.command_type(str(body.get("cmd", ""))), subagent))
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
            elif u.path == "/api/proc":
                self.reply(Q.proc_details(int(q["pid"][0])))
            elif u.path == "/api/agent":
                self.reply(Q.agent_details(int(q["pid"][0])))
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
