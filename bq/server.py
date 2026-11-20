"""Build queue server: FIFO slots for Gradle/JVM runs, memory monitor, cleanup, and a localhost page.

One scheduler thread owns every decision (grant, reap, kill). HTTP handlers only enqueue, release and
read, so agents never race each other: the lock serializes them and the tick decides.
"""

import functools
import json
import os
import re
import socket
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
from bq import core, service, settings, system  # noqa: E402
from bq.system import log  # noqa: E402

PORT = int(os.environ.get("BQ_PORT") or core.SETTINGS["port"])  # BQ_PORT wins, so tests never take 8787
DATA = core.DATA
CONFIG = settings.CONFIG
PAGE = Path(__file__).with_name("page.html")
TICK_S = core.SETTINGS["tick_s"]
STARTED = time.time()  # the page waits for this to change after Save & Apply
BIND_WAIT_S = 10  # a restart may find the old process still holding the port for a moment
CURVE_RUNS = 3  # memory curves kept per repo and command
CURVE_LATE_S = 2 * TICK_S + 5  # a curve whose first sample came later than this missed the start: never stored


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


def ticket_pids(gq_pid, tid, procs, serving, kotlin_of):
    """The processes of one queued build: its own tree, the Gradle daemon serving it with that daemon's workers,
    and the Kotlin daemon that daemon talks to."""
    return tree_pids({gq_pid} | {p.pid for p in procs.values() if p.env.get("BQ_TICKET") == str(tid)},
                     procs, serving, kotlin_of)


def tree_pids(tops, procs, serving, kotlin_of):
    """The processes of a build started at these pids, with the daemons serving it (see ticket_pids)."""
    own = set(tops)
    for top in tops:
        own.update(core.descendants(top, procs))
    daemons = {d for d, client in serving.items() if client in own}
    pids = set(own) | daemons
    for d in daemons:
        pids |= set(core.descendants(d, procs)) | kotlin_of.get(d, set())
    return {x for x in pids if x in procs}


def rss_gb(pids, procs):
    return sum(procs[x].rss_kb for x in pids) / 1024 ** 2


def cpu_pct(pids, procs):
    """Share of the whole machine, 0-100."""
    return sum(procs[x].cpu for x in pids) / system.NCPU


def load(pids, procs, total_gb):
    """Memory and CPU of a set of processes, for the page's tooltips."""
    gb = rss_gb(pids, procs)
    return {"gb": round(gb, 2), "ram_pct": round(100 * gb / total_gb, 1) if total_gb else 0,
            "cpu_pct": round(cpu_pct(pids, procs), 1)}


@functools.lru_cache(maxsize=512)  # shortcut: a folder that becomes a repo later keeps its old name until a restart
def project_of(cwd):
    """Name of the git root folder holding cwd, so each worktree keeps its own name; outside git, the cwd's folder."""
    if not cwd:
        return ""
    here = Path(cwd)
    return next((d.name for d in (here, *here.parents) if (d / ".git").exists()), here.name)


@functools.lru_cache(maxsize=512)  # shortcut: same as project_of
def repo_of(cwd):
    """Main checkout of the git repo holding cwd, so every worktree of a repo shares one history; outside git, cwd."""
    if not cwd:
        return ""
    here = Path(cwd)
    for d in (here, *here.parents):
        git = d / ".git"
        if git.is_dir():
            return str(d)
        if git.is_file():  # a worktree: "gitdir: <main>/.git/worktrees/<name>", whose commondir points at <main>/.git
            try:
                gitdir = Path(d, git.read_text().split("gitdir:", 1)[1].strip())
                common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
                return str(common.parent if common.name == ".git" else common)
            except (OSError, IndexError):  # a submodule has no commondir: its own folder
                return str(d)
    return cwd


def script_text(cwd, cmd):
    """Text of the shell script a queued command runs, read when its build starts; None when it runs none."""
    sc = core.script_of(cmd)
    try:
        path = Path(cwd, sc[0]) if sc else None
        return path.read_text(errors="replace") if path and path.stat().st_size < 256 * 1024 else None
    except OSError:
        return None


@functools.lru_cache(maxsize=256)
def root_cwd(pid, lstart):  # lstart keys the cache, so a reused pid is looked up again
    return system.cwd_of(pid)


def root_project(pid, lstart):
    return project_of(root_cwd(pid, lstart))


def outside_and_runner(outside_roots, trees, procs, total_gb):
    """Memory and CPU of the builds running without a slot, each one named, and of the runner's job."""
    outside = set().union(*(trees[f"p{pid}"] for pid in outside_roots))  # a shared daemon counts once
    builds = []
    for pid in outside_roots:
        r, tree = procs[pid], trees[f"p{pid}"]
        builds.append({"pid": pid, "project": root_project(pid, r.lstart), "ctype": core.command_type(r.args),
                       "cwd": root_cwd(pid, r.lstart), "cmd": r.args, "granted": r.started, "status": r.status,
                       "rss_gb": round(rss_gb(tree, procs), 1), "cpu_pct": round(cpu_pct(tree, procs), 1)})
    return {"outside": {**load(outside, procs, total_gb), "n": len(outside_roots), "builds": builds},
            "runner": load(trees["runner"], procs, total_gb)}


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
        self.db.execute("create table if not exists curves (repo text, cmd text, ts real, samples text, ctype text)")
        if "ctype" not in {r[1] for r in self.db.execute("pragma table_info(curves)")}:
            self.db.execute("alter table curves add column ctype text")
            self.db.execute("delete from curves")  # keyed by the raw command line, which one-off scripts never repeat
        self.db.execute("create index if not exists curves_build on curves (repo, cmd)")
        self.db.execute("create index if not exists curves_type on curves (repo, ctype)")
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
        self.curves = {}  # "t<ticket id>" / "p<root pid>" -> {"build": (repo, key, ctype), "lstart", "samples"}
        self.now_gb = {}  # granted ticket id -> memory at the last tick, GB
        self.now_cpu = {}  # active ticket id -> CPU at the last tick, % of the machine
        self.loads = {}  # "outside" / "runner" -> memory and CPU at the last tick
        self.cpu_total = 0.0
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
        peak, curve = self.peak.pop(tid, None), self.curves.pop(f"t{tid}", None)
        cur = self.db.execute("update tickets set state='done', ended=?, end_reason=?, exit_code=?, peak_gb=? "
                              "where id=? and state!='done'", (time.time(), reason, exit_code, peak, tid))
        if cur.rowcount and peak and reason == "released":  # a reaped gq died mid-run: its peak is partial
            ctype = self.db.execute("select ctype from tickets where id=?", (tid,)).fetchone()[0]
            if ctype:
                self.db.execute("insert into peaks values (?,?,?,?)", (ctype, round(peak, 2), time.time(), tid))
            if exit_code == 0:
                self.save_curve(curve)

    def save_curve(self, curve):
        """Keeps a finished run's memory curve, dropping the build's runs older than the last CURVE_RUNS."""
        if not curve or not curve["samples"] or curve["samples"][0][0] > CURVE_LATE_S:
            return
        repo, key, ctype = curve["build"]
        self.db.execute("insert into curves (repo, cmd, ts, samples, ctype) values (?,?,?,?,?)",
                        (repo, key, time.time(), json.dumps(curve["samples"]), ctype))
        self.db.execute("delete from curves where repo=? and cmd=? and rowid not in "
                        "(select rowid from curves where repo=? and cmd=? order by rowid desc limit ?)",
                        (repo, key, repo, key, CURVE_RUNS))

    def history(self, build):
        """The last CURVE_RUNS curves of the same repo and build key; without any, those of the repo and type."""
        repo, key, ctype = build
        for where, args in (("cmd=?", key), ("ctype=?", ctype)):
            rows = self.db.execute(f"select samples from curves where repo=? and {where} order by rowid desc limit ?",
                                   (repo, args, CURVE_RUNS)).fetchall()
            if rows:
                return [json.loads(s) for (s,) in rows]
        return []

    def track(self, key, build, lstart, elapsed, gb):
        """Adds one sample to a running build's curve and returns what its past runs still reached from here on."""
        c = self.curves.get(key)
        if not c or c["lstart"] != lstart:  # a reused pid is another build
            c = self.curves[key] = {"build": build, "lstart": lstart, "samples": []}
        c["samples"].append((round(elapsed, 1), round(gb, 2)))
        return core.remaining_gb(self.history(build), elapsed)

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
        trees = {f"p{r.pid}": tree_pids({r.pid}, procs, serving, kotlin_of) for r in roots}
        workers = {p.pid for p in procs.values() if p.kind == "runner_worker"}
        trees["runner"] = tree_pids(workers, procs, serving, kotlin_of)
        rss = {k: rss_gb(v, procs) for k, v in trees.items()}
        self.cpu_total = system.cpu_total()
        self.swap_samples.append((now, mem["swap_used_gb"]))
        while now - self.swap_samples[0][0] > core.SWAP_WINDOW_S:
            self.swap_samples.popleft()
        swap_gb = core.swap_growth_gb(self.swap_samples, now)
        ceiling = self.ceiling()
        with self.lock:
            tickets = self.active_tickets()
            rss.update(self.measure_tickets(tickets, procs, (serving, kotlin_of)))
            avail = mem["total_gb"] * mem["free_pct"] / 100
            peaks = self.peaks()
            remaining = self.track_all(tickets, roots, procs, rss, now)
            pl = core.plan(procs, roots, tickets, snapshot_ts=snap_ts, now=now, pressure=mem["pressure"],
                           busy_daemons=busy, idle_since=self.idle_since, last_grant_ts=self.last_grant,
                           live_claude_pids=live_claude, last_extra_ts=self.last_extra, avail_gb=avail,
                           estimates=self.estimates(peaks), ceiling=ceiling, swap_gb=swap_gb, rss_gb=rss,
                           peaks=peaks, remaining={k: gb for k, gb in remaining.items() if gb is not None})
            self.end_outside(pl.outside, roots, procs)
            self.loads = outside_and_runner(pl.outside, trees, procs, mem["total_gb"])
            for b in self.loads["outside"]["builds"]:  # the Running table's row of each build without a ticket
                cp = procs[b["pid"]].env.get("CLAUDE_PID", "")
                b["claude_pid"] = int(cp) if cp.isdigit() else None
                b["agent"] = self.agent_label(b["claude_pid"], procs) if b["claude_pid"] else "—"
                b["agent_alive"] = b["claude_pid"] in procs if b["claude_pid"] else None
                b["estimate_gb"] = round(core.estimate_gb(b["ctype"], peaks), 1)
                samples = self.curves.get(f"p{b['pid']}", {}).get("samples") or [(0, 0.0)]
                b["peak_gb"] = round(max(gb for _t, gb in samples), 1)
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

    def track_all(self, tickets, roots, procs, rss, now):
        """Samples every running build's memory; returns, by holder key, what its past runs still reached."""
        out = {}
        for t in tickets:
            key = f"t{t.id}"
            if t.state == "granted" and t.granted and key in rss:
                if key in self.curves:
                    build = self.curves[key]["build"]
                else:
                    cwd, cmd, ctype = self.db.execute("select cwd, cmd, ctype from tickets where id=?",
                                                      (t.id,)).fetchone()
                    cwd, cmd = cwd or "", cmd or ""
                    build = (repo_of(cwd), core.build_key(cmd, script_text(cwd, cmd)), ctype or "other")
                out[key] = self.track(key, build, None, now - t.granted, rss[key])
        for r in roots:  # the plan reads only the roots outside the queue, known after it runs: sample them all
            key = f"p{r.pid}"
            if r.owner in ("agent", "nonagent") and key in rss:
                build = (repo_of(root_cwd(r.pid, r.lstart)), core.build_key(r.args), core.command_type(r.args))
                out[key] = self.track(key, build, r.lstart, now - r.started, rss[key])
        return out

    def end_outside(self, outside, roots, procs):
        """Stores the curve of each build outside the queue whose process ended; forgets the other roots' curves."""
        for pid in outside:
            self.curves[f"p{pid}"]["outside"] = True
        live = {f"p{r.pid}" for r in roots}
        for key in [k for k in self.curves if k.startswith("p") and k not in live]:
            c, p = self.curves.pop(key), procs.get(int(key[1:]))
            if c.get("outside") and (p is None or p.lstart != c["lstart"]):
                self.save_curve(c)

    def measure_tickets(self, tickets, procs, links):
        """Memory and CPU of each live ticket; keeps the peak of a granted one. Returns its memory by "t<id>"."""
        serving, kotlin_of = links
        self.now_gb, self.now_cpu, rss = {}, {}, {}
        for t in (t for t in tickets if t.pid in procs):
            pids = ticket_pids(t.pid, t.id, procs, serving, kotlin_of)
            self.now_cpu[t.id] = cpu_pct(pids, procs)
            if t.state == "granted":
                gb = rss[f"t{t.id}"] = self.now_gb[t.id] = rss_gb(pids, procs)
                self.peak[t.id] = max(self.peak.get(t.id, 0.0), gb)
        return rss

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
                "rss_gb": round(p.rss_kb / 1024 ** 2, 2),
                "ram_pct": round(100 * p.rss_kb / total_kb, 2) if total_kb else 0, "cpu": p.cpu, "etime": p.etime, "status": p.status, "ticket": p.env.get("BQ_TICKET", ""),
                "serving": (serving or {}).get(p.pid), "cmd": p.args[:220]}

    def ticket_rows(self, where, args):
        cols = ["id", "pid", "claude_pid", "ralphy", "cwd", "cmd", "state", "created", "granted", "ended",
                "end_reason", "exit_code", "ctype", "peak_gb", "subagent"]
        exprs = ["round(peak_gb, 1)" if c == "peak_gb" else c for c in cols]
        rows = self.db.execute(f"select {', '.join(exprs)} from tickets where {where} order by id", args)
        return [dict(zip(cols, r)) for r in rows.fetchall()]

    def tickets_view(self, where, args, procs):
        """Ticket rows with what the page shows of them: agent, project, place in line, memory, CPU, estimate."""
        tickets, peaks = self.ticket_rows(where, args), self.peaks()
        line = [tid for (tid,) in self.db.execute("select id from tickets where state='waiting' order by id")]
        for t in tickets:
            t["agent"] = self.agent_label(t["claude_pid"], procs, t["subagent"]) if t["claude_pid"] else "?"
            t["agent_alive"] = t["claude_pid"] in procs if t["claude_pid"] else None
            t["project"] = project_of(t["cwd"])
            t["estimate_gb"] = round(core.estimate_gb(t["ctype"] or "other", peaks), 1)
            t["position"] = line.index(t["id"]) + 1 if t["id"] in line else None
            granted = t["state"] == "granted"
            t["peak_gb"] = round(self.peak.get(t["id"], 0.0), 1) if granted else t["peak_gb"]
            t["rss_gb"] = round(self.now_gb[t["id"]], 1) if t["id"] in self.now_gb else None  # granted ones only
            t["cpu_pct"] = round(self.now_cpu[t["id"]], 1) if t["id"] in self.now_cpu else None
        return tickets

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
                       tree_rss_gb=round(tree_kb / 1024 ** 2, 2), tree_ram_pct=round(100 * tree_kb / total_kb, 2) if total_kb else 0,
                       tickets=self.tickets_view("id=?", (int(p.env["BQ_TICKET"]),), procs)
                       if p.env.get("BQ_TICKET", "").isdigit()  # gq itself has no BQ_TICKET: its live ticket by pid
                       else self.tickets_view("pid=? and state!='done'", (pid,), procs))
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
            rows = sorted((self.proc_row(procs[x], procs, total_kb) for x in mine), key=lambda r: -r["rss_gb"])
            rss_kb = sum(procs[x].rss_kb for x in mine)
            avail_kb = total_kb * mem["free_pct"] / 100
            claude = procs.get(claude_pid)
            m = re.search(r"/sessions/(\d+)\.settings\.json", claude.args) if claude else None
            sid = self.titles.session_of(m.group(1)) if m else None
            tickets = self.tickets_view("claude_pid=? and (state!='done' or ended>?)",
                                        (claude_pid, time.time() - 3600), procs)
            transcript = glob(str(Path.home() / f".claude/projects/*/{sid}.jsonl")) if sid else []
            return {"pid": claude_pid, "label": self.agent_label(claude_pid, procs), "alive": claude is not None,
                    "ralphy": m.group(1) if m else None, "session_id": sid,
                    "transcript": transcript[0] if transcript else None,
                    "etime": claude.etime if claude else None, "lstart": claude.lstart if claude else None,
                    "rss_gb": round(rss_kb / 1024 ** 2, 2), "ram_pct": round(100 * rss_kb / total_kb, 2) if total_kb else 0,
                    "avail_pct": round(100 * rss_kb / avail_kb, 1) if avail_kb else 0,
                    "total_gb": mem["total_gb"], "avail_gb": round(avail_kb / 1024 ** 2, 1),
                    "procs": rows, "tickets": tickets}

    def build_view(self, procs, roots, pl, mem, serving, enforce, would_kill, ceiling, swap_gb):
        agents = {}
        for p in procs.values():
            if p.kind == "claude":
                a = agents.setdefault(p.pid, {"rss": 0, "cpu": 0.0, "procs": 0})
                a["rss"] += p.rss_kb
                a["cpu"] += p.cpu
        relevant = []
        self.sub_tickets = dict(self.db.execute(
            "select id, subagent from tickets where state!='done' and subagent is not null").fetchall())
        total_kb = mem["total_gb"] * 1024 ** 2
        for p in procs.values():
            cp = p.env.get("CLAUDE_PID", "")
            if p.owner == "agent" and cp.isdigit():
                a = agents.setdefault(int(cp), {"rss": 0, "cpu": 0.0, "procs": 0})
                a["rss"] += p.rss_kb
                a["cpu"] += p.cpu
                a["procs"] += 1
            if p.kind in core.AGENT_EXEC_KINDS | core.INFRA_KINDS | {"gq", "runner_worker", "embedded_pg"} \
                    or p.root is not None:
                relevant.append(self.proc_row(p, procs, total_kb, serving))
        relevant.sort(key=lambda r: -r["rss_gb"])
        tickets = self.tickets_view("state!='done' or ended>?", (time.time() - 3600,), procs)
        peaks = self.peaks()
        return {
            "ts": time.strftime("%H:%M:%S"), "enforce": enforce, "used": pl.used,
            "base_slots": core.SLOTS, "max_slots": ceiling, "headroom_gb": core.HEADROOM_GB,
            "avail_gb": round(mem["total_gb"] * mem["free_pct"] / 100, 1), "reserve_gb": round(pl.reserve_gb, 1),
            "swap_growth_gb": round(swap_gb, 1), "swap_growth_limit_gb": core.SWAP_GROWTH_GB,
            "swap_window_s": core.SWAP_WINDOW_S, "peak_history": core.PEAK_HISTORY,
            "cpu_total_pct": round(self.cpu_total, 1), "builds_gb": round(sum(self.now_gb.values()), 1),
            "builds_cpu_pct": round(sum(c for tid, c in self.now_cpu.items() if tid in self.now_gb), 1),
            "outside": self.loads.get("outside", {"n": 0, "builds": []}), "runner_load": self.loads.get("runner"),
            "estimates": {ct: round(core.estimate_gb(ct, peaks), 1) for ct in core.DEFAULT_GB},
            "counts": pl.counts, "blocked": core.render(pl.blocked), "blocked_msg": pl.blocked, "memory": mem,
            "would_kill": would_kill,
            "agents": sorted(({"pid": k, "label": self.agent_label(k, procs), "rss_gb": round(v["rss"] / 1024 ** 2, 2),
                               "ram_pct": round(100 * v["rss"] / total_kb, 1) if total_kb else 0,
                               "cpu_pct": round(v["cpu"] / system.NCPU, 1),
                               "procs": v["procs"], "alive": k in procs} for k, v in agents.items()),
                             key=lambda a: -a["rss_gb"]),
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


def port_free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def config_view():
    """What the settings dialog needs: stored values, defaults, bounds, and the service's start with the system."""
    spec = [{"key": k, "group": g, "kind": kind, **({"choices": list(lo)} if kind == "choice" else
                                                    {"min": lo, "max": hi} if lo is not None else {})}
            for k, (g, _default, kind, lo, hi) in settings.SPEC.items()]
    return {"values": settings.load(), "defaults": settings.defaults(), "spec": spec, "ctypes": list(settings.CTYPES),
            "autostart": service.autostart(), "started": STARTED, "port": PORT, "port_env": "BQ_PORT" in os.environ}


def config_errors(values):
    """(clean settings, {key: error message}): the values' own checks, plus a new port that must be free."""
    clean, errors = settings.validate(values)
    port = clean.get("port", PORT)
    if port != PORT and not port_free(port):
        errors["port"] = core.msg("config.port_taken", port=port)
    return clean, errors


def switch_autostart(want):
    """Error message, or None when the start with the system already matches or was switched."""
    if not isinstance(want, bool) or not service.installed():
        return None
    if want == service.autostart():
        return None
    err = service.set_autostart(want)
    return core.msg("config.autostart", detail=err[:300]) if err else None


def apply_config(body):
    """Validates and saves the dialog's values, switches the start with the system, then restarts the server.

    Returns (reply, HTTP code). Nothing is saved while any value is invalid."""
    values = body.get("values")
    if not isinstance(values, dict):
        return {"error": "values must be an object"}, 400
    clean, errors = config_errors(values)
    if not errors:
        err = switch_autostart(body.get("autostart"))
        errors = {"autostart": err} if err else {}
    if errors:
        return {"errors": errors}, 400
    try:
        settings.save(clean)
    except OSError as e:
        return {"error": f"cannot write {CONFIG}: {e}"}, 500
    log("settings saved from the page; restarting to apply them")
    threading.Thread(target=restart_soon, daemon=True, name="restart").start()
    return {"ok": True, "port": int(os.environ.get("BQ_PORT") or clean["port"])}, 200


def restart_soon():
    time.sleep(0.5)  # lets the reply reach the page first
    service.restart([os.path.abspath(sys.argv[0]), *sys.argv[1:]])


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
        try:
            if u.path == "/":
                return self.reply(PAGE.read_bytes(), ctype="text/html; charset=utf-8")
            route = GET_ROUTES.get(u.path)
            self.reply(*(route(parse_qs(u.query)) if route else ({"error": "not found"}, 404)))
        except (KeyError, ValueError) as e:
            self.reply({"error": f"bad request: {e}"}, 400)

    def same_origin(self):
        """Only the page itself may change settings: a JSON content type forces a CORS preflight this server never
        answers, the Host check stops DNS rebinding, the Origin check any other site."""
        hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        origin = self.headers.get("Origin")
        return (self.headers.get("Host") in hosts and (origin is None or origin in {"http://" + h for h in hosts})
                and (self.headers.get("Content-Type") or "").startswith("application/json"))

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)) or b"{}")
            route = POST_ROUTES.get(u.path)
            self.reply(*(route(self, body) if route else ({"error": "not found"}, 404)))
        except (KeyError, ValueError, TypeError) as e:
            self.reply({"error": f"bad request: {e}"}, 400)


def ticket_status(q):
    with Q.lock:
        return Q.status(int(q["id"][0])), 200


def wake(_q):
    Q.wake.set()
    return {"ok": True}, 200


def post_config(handler, body):
    return apply_config(body) if handler.same_origin() else ({"error": "forbidden"}, 403)


# path -> handler returning (reply, HTTP code); Q is looked up at call time, after main() created it
GET_ROUTES = {
    "/api/state": lambda q: (Q.view, 200),
    "/api/wait": lambda q: (Q.wait(int(q["id"][0]), float(q.get("timeout", ["25"])[0])), 200),
    "/api/ticket": ticket_status,
    "/api/proc": lambda q: (Q.proc_details(int(q["pid"][0])), 200),
    "/api/agent": lambda q: (Q.agent_details(int(q["pid"][0])), 200),
    "/api/config": lambda q: (config_view(), 200),
    "/api/wake": wake,
}
POST_ROUTES = {
    "/api/config": post_config,
    "/api/enqueue": lambda h, body: (Q.enqueue(body), 200),
    "/api/release": lambda h, body: (Q.release(int(body["id"]), body.get("exit_code")), 200),
}


def main():
    global Q
    DATA.mkdir(parents=True, exist_ok=True)
    if sys.stderr is None:  # pythonw on Windows has no console: keep the log in the data dir
        sys.stderr = open(DATA / "server.log", "a", buffering=1)
    Q = Queue()
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps({"enforce": False}, indent=1))
    threading.Thread(target=Q.loop, daemon=True, name="scheduler").start()
    deadline = time.time() + BIND_WAIT_S
    while True:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
            break
        except OSError as e:
            if time.time() > deadline:
                raise SystemExit(f"cannot listen on 127.0.0.1:{PORT}: {e}")
            time.sleep(0.5)
    srv.daemon_threads = True
    log(f"build-queue listening on http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
