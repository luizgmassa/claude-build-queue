"""Build queue server: FIFO slots for Gradle/JVM runs, memory monitor, cleanup, and a localhost page.

One scheduler thread owns every decision (grant, reap, kill). HTTP handlers only enqueue, release and
read, so agents never race each other: the lock serializes them and the tick decides.
"""

import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from glob import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bq import core, system  # noqa: E402
from bq.system import log  # noqa: E402

PORT = int(os.environ.get("BQ_PORT", "8787"))
DATA = core.DATA
CONFIG = DATA / "config.json"
PAGE = Path(__file__).with_name("page.html")
TICK_S = 2.0


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
    return tree_rss_gb({gq_pid} | {p.pid for p in procs.values() if p.env.get("BQ_TICKET") == str(tid)},
                       procs, serving, kotlin_of)


def tree_rss_gb(tops, procs, serving, kotlin_of):
    """Memory of a build started at these pids, with the daemons serving it (see ticket_rss_gb)."""
    own = set(tops)
    for top in tops:
        own.update(core.descendants(top, procs))
    daemons = {d for d, client in serving.items() if client in own}
    pids = set(own) | daemons
    for d in daemons:
        pids |= set(core.descendants(d, procs)) | kotlin_of.get(d, set())
    return sum(procs[x].rss_kb for x in pids if x in procs) / 1024 ** 2


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
        try:
            tmp.write_text("\n".join(lines) + "\n")
            os.replace(tmp, core.CALLS)
        except OSError:
            pass  # Windows refuses the replace while a hook holds the file open; the next read retries
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
        self.blocked = None  # message
        self.last_extra = None
        self.swap_samples = deque()  # (ts, swap used GB) within core.SWAP_WINDOW_S
        self.peak = {}  # granted ticket id -> largest memory seen, GB
        self.taken_calls = set()  # hook records already matched to a ticket
        self.sub_tickets = {}  # active ticket id -> subagent, so its processes carry the subagent label
        self.procs, self.mem = {}, {"total_gb": 0, "free_pct": 0}  # last snapshot, for the detail dialogs
        self.titles = Titles()

    def event(self, kind, m):
        """Keeps the message for the page, which translates it, and logs it in English."""
        self.events.appendleft({"ts": time.strftime("%H:%M:%S"), "kind": kind, "msg": m})
        log(f"{kind}: {core.render(m)}")

    def config(self):
        try:
            cfg = json.loads(CONFIG.read_text())
            return cfg if isinstance(cfg, dict) else {}
        except (OSError, ValueError):
            return {}

    def enforce(self):
        return bool(self.config().get("enforce", False))

    def ceiling(self):
        """Configured max slots (`gq config max N`), or None when off."""
        n = self.config().get("max")
        return max(core.SLOTS, n) if isinstance(n, int) and not isinstance(n, bool) else None

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

    def estimates(self, peaks):
        rows = self.db.execute("select id, coalesce(ctype, 'other') from tickets where state in ('waiting','granted')")
        return {tid: (ctype, core.estimate_gb(ctype, peaks)) for tid, ctype in rows}

    # ------------------------------------------------------------ scheduler tick

    def tick(self):
        snap_ts = time.time()
        procs = system.snapshot_procs()
        listen, est = system.tcp_links(procs)
        mem = system.memory()
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
        rss = {f"p{r.pid}": tree_rss_gb({r.pid}, procs, serving, kotlin_of) for r in roots}
        workers = {p.pid for p in procs.values() if p.kind == "runner_worker"}
        rss["runner"] = tree_rss_gb(workers, procs, serving, kotlin_of)
        self.swap_samples.append((now, mem["swap_used_gb"]))
        while now - self.swap_samples[0][0] > core.SWAP_WINDOW_S:
            self.swap_samples.popleft()
        swap_gb = core.swap_growth_gb(self.swap_samples, now)
        ceiling = self.ceiling()
        with self.lock:
            tickets = self.active_tickets()
            for t in tickets:
                if t.state == "granted" and t.pid in procs:
                    gb = ticket_rss_gb(t.pid, t.id, procs, serving, kotlin_of)
                    rss[f"t{t.id}"] = gb
                    self.peak[t.id] = max(self.peak.get(t.id, 0.0), gb)
            avail = mem["total_gb"] * mem["free_pct"] / 100
            peaks = self.peaks()
            pl = core.plan(procs, roots, tickets, snapshot_ts=snap_ts, now=now, pressure=mem["pressure"],
                           busy_daemons=busy, idle_since=self.idle_since, last_grant_ts=self.last_grant,
                           live_claude_pids=live_claude, last_extra_ts=self.last_extra, avail_gb=avail,
                           estimates=self.estimates(peaks), ceiling=ceiling, swap_gb=swap_gb, rss_gb=rss,
                           peaks=peaks)
            if pl.extra:
                self.last_extra = now
                self.event("extra", pl.extra)
            for tid, start in pl.pid_starts.items():
                self.db.execute("update tickets set pid_start=? where id=?", (start, tid))
            for tid, reason in pl.reaps:
                self.finish(tid, reason)
                self.event("reap", core.msg("event.reap", id=tid, reason=core.msg("end." + reason)))
            for tid in pl.grants:
                self.db.execute("update tickets set state='granted', granted=? where id=?", (now, tid))
                self.last_grant = now
                self.event("grant", core.msg("event.grant", id=tid))
            if pl.grants or pl.reaps:
                self.cond.notify_all()
            self.blocked = pl.blocked
            would_kill = []
            for pid, reason in pl.kills:
                if enforce:
                    self.kill(pid, reason, procs)
                else:
                    would_kill.append({"pid": pid, "reason": reason})  # reason is a message
            self.escalate(procs)
            self.procs, self.mem, self.serving = procs, mem, serving
            self.view = self.build_view(procs, roots, pl, mem, serving, enforce, would_kill, ceiling, swap_gb)

    def kill(self, root, reason, procs):
        tree = [root] + core.descendants(root, procs)
        for pid in reversed(tree):
            p = procs.get(pid)
            if p is None or pid in (1, os.getpid()) or p.kind == "claude" or p.owner in ("runner", "protected"):
                continue
            if pid in self.pending_kills:
                continue
            err = system.stop(pid)
            if err:
                self.event("error", err)
            else:
                self.pending_kills[pid] = (p.lstart, time.time() + core.TERM_GRACE_S, reason)
        p = procs.get(root)
        self.event("kill", core.msg("event.sigterm", pid=root, kind=p.kind if p else "?", reason=reason))

    def escalate(self, procs):
        now = time.time()
        for pid, (start, deadline, reason) in list(self.pending_kills.items()):
            p = procs.get(pid)
            if p is None or p.lstart != start:
                del self.pending_kills[pid]
            elif now >= deadline:
                err = system.stop(pid, hard=True)
                self.event("error" if err else "kill",
                           err or core.msg("event.sigkill", pid=pid, s=core.TERM_GRACE_S, reason=reason))
                del self.pending_kills[pid]

    # ------------------------------------------------------------ view

    def agent_label(self, claude_pid, procs, subagent=None):
        """Language-neutral label; whether the agent ended travels as a separate flag the page translates."""
        sub = " subagent" if subagent else ""
        p = procs.get(claude_pid)
        if not p:
            return f"claude {claude_pid}{sub}"
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
                "agent_alive": agent_pid in procs if agent_pid else None,
                "rss_mb": p.rss_kb // 1024, "ram_pct": round(100 * p.rss_kb / total_kb, 2) if total_kb else 0,
                "cpu": p.cpu, "etime": p.etime, "status": p.status, "ticket": p.env.get("BQ_TICKET", ""),
                "serving": (serving or {}).get(p.pid), "cmd": p.args[:220]}

    def ticket_rows(self, where, args):
        cols = ["id", "pid", "claude_pid", "ralphy", "cwd", "cmd", "state", "created", "granted", "ended",
                "end_reason", "exit_code", "ctype", "peak_gb", "subagent"]
        exprs = ["round(peak_gb, 1)" if c == "peak_gb" else c for c in cols]
        rows = self.db.execute(f"select {', '.join(exprs)} from tickets where {where} order by id", args)
        return [dict(zip(cols, r)) for r in rows.fetchall()]

    def proc_details(self, pid):
        """Everything known about one process, for the page's dialog."""
        with self.lock:
            procs, mem, serving = self.procs, self.mem, getattr(self, "serving", {})
            p = procs.get(pid)
            if p is None:
                m = core.msg("error.pid_gone", pid=pid)
                return {"error": core.render(m), "error_msg": m}
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
        out["cwd"] = system.cwd_of(pid)  # outside the lock: it can take a moment
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

    def build_view(self, procs, roots, pl, mem, serving, enforce, would_kill, ceiling, swap_gb):
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
            t["agent_alive"] = t["claude_pid"] in procs if t["claude_pid"] else None
            t["estimate_gb"] = round(core.estimate_gb(t["ctype"] or "other", peaks), 1)
            if t["state"] == "granted":
                t["peak_gb"] = round(self.peak.get(t["id"], 0.0), 1)
        return {
            "ts": time.strftime("%H:%M:%S"), "enforce": enforce, "used": pl.used,
            "base_slots": core.SLOTS, "max_slots": ceiling, "headroom_gb": core.HEADROOM_GB,
            "avail_gb": round(mem["total_gb"] * mem["free_pct"] / 100, 1), "reserve_gb": round(pl.reserve_gb, 1),
            "swap_growth_gb": round(swap_gb, 1), "swap_growth_limit_gb": core.SWAP_GROWTH_GB,
            "estimates": {ct: round(core.estimate_gb(ct, peaks), 1) for ct in core.DEFAULT_GB},
            "counts": pl.counts, "blocked": core.render(pl.blocked), "blocked_msg": pl.blocked, "memory": mem,
            "would_kill": would_kill,
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
        self.event("enqueue", core.msg("event.enqueue", id=tid, pid=body["pid"], cmd=str(body.get("cmd", ""))[:100]))
        self.wake.set()
        return {"id": tid}

    def status(self, tid):
        row = self.db.execute("select state, end_reason from tickets where id=?", (tid,)).fetchone()
        if not row:
            return {"state": "unknown"}
        ahead = self.db.execute("select count(*) from tickets where state='waiting' and id<?", (tid,)).fetchone()
        waiting = self.db.execute("select count(*) from tickets where state='waiting'").fetchone()
        return {"state": row[0], "reason": row[1], "position": ahead[0] + 1, "waiting": waiting[0],
                "blocked": core.render(self.blocked)}  # English text: gq prints it

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
        self.event("release", core.msg("event.release", id=tid, exit=exit_code))
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
        try:
            self.end_headers()  # flushes the headers: a dead client fails here already
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
    DATA.mkdir(parents=True, exist_ok=True)
    if sys.stderr is None:  # pythonw on Windows has no console: keep the log in the data dir
        sys.stderr = open(DATA / "server.log", "a", buffering=1)
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
