"""Pure logic of the build queue: process classification, scheduling and the hook's command analysis.

Nothing here touches the system, so every rule is unit-testable with synthetic input.
"""

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

SLOTS = 2  # base limit, runner included
MAX_SLOTS = 4  # ceiling when memory allows extra slots
HEADROOM_GB = 6.0  # available memory that must remain after the next item's estimate
RAISE_GAP_S = 90  # a new build needs time to reach its peak before memory is judged again
PEAK_HISTORY = 5  # estimate = largest of the last N measured peaks of the same command type
# Starting estimates (GB of the whole build: client, daemon, workers, Kotlin daemon) before any measurement.
DEFAULT_GB = {"gradle-check": 8.0, "gradle-backend": 6.0, "gradle-ios": 6.0, "gradle-test": 5.0,
              "gradle-lint": 3.0, "gradle": 4.0, "xcodebuild": 6.0, "java": 2.0, "pod": 1.0, "uv": 1.0,
              "other": 4.0}
IDLE_GRACE_S = 20
KOTLIN_IDLE_S = 60  # idle Kotlin daemon dies even mid-build: KGP falls back to in-process compile
GRANT_QUIET_S = 20
TERM_GRACE_S = 10
CALL_WINDOW_S = 60  # a `gq run` reaches the server within this long after the hook saw its Bash call

DATA = Path(os.environ.get("BQ_DATA", Path.home() / ".local/share/build-queue"))
CALLS = DATA / "calls.jsonl"  # the hook's record of each `gq run` Bash call: session and subagent

PRESSURE_NORMAL, PRESSURE_WARN, PRESSURE_CRITICAL = 1, 2, 4

ENV_KEYS = ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "RALPHY_STATUS_FILE", "BQ_TICKET", "GITHUB_ACTIONS")
_ENV_RE = re.compile(r"(?:^|\s)(" + "|".join(ENV_KEYS) + r")=(\S*)")

AGENT_EXEC_KINDS = {"gradle_client", "xcodebuild", "pod", "uv_run", "kotlinc", "java"}
NONAGENT_EXEC_KINDS = {"gradle_client", "xcodebuild", "pod"}
# Processes below these belong to a build already accounted for; they are never roots.
INFRA_KINDS = {"gradle_daemon", "kotlin_daemon", "gradle_worker"}


@dataclass
class Proc:
    pid: int
    ppid: int
    rss_kb: int
    cpu: float
    etime: str
    lstart: str
    args: str
    env: dict = field(default_factory=dict)
    kind: str = "other"
    owner: str = "nonagent"  # agent | runner | protected | nonagent
    root: int | None = None  # pid of the exec root this process runs under
    status: str = ""


def parse_env(env_text):
    return {k: v for k, v in _ENV_RE.findall(env_text)}


def kind_of(args):
    first = args.split(" ", 1)[0]
    exe = os.path.basename(first)
    words = args.split()
    if "org.gradle.launcher.daemon.bootstrap.GradleDaemon" in args:
        return "gradle_daemon"
    if "org.jetbrains.kotlin.daemon.KotlinCompileDaemon" in args:
        return "kotlin_daemon"
    if "GradleWorkerMain" in args or "org.gradle.internal.worker.tmpdir" in args:
        return "gradle_worker"
    if any(m in args for m in ("org.gradle.appname=gradlew", "GradleWrapperMain", "gradle-wrapper.jar",
                               "org.gradle.launcher.GradleMain")):
        return "gradle_client"
    if "Runner.Worker" in args:
        return "runner_worker"
    if exe == "claude" or "/.local/share/claude/versions/" in first:
        return "claude"
    if re.search(r"/gq run(\s|$)", args) or args.startswith("gq run"):
        return "gq"
    if exe == "xcodebuild":
        return "xcodebuild"
    if re.search(r"(^|[\s/])pod (install|update)\b", args):
        return "pod"
    if exe == "uv" and len(words) > 1 and words[1] == "run":
        return "uv_run"
    if exe in ("kotlinc", "kotlinc-jvm", "kotlin") or "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler" in args:
        return "kotlinc"
    if exe == "java":
        return "java"
    if exe == "postgres" and "embedded-pg" in args:
        return "embedded_pg"
    return "other"


def command_type(cmd):
    """Memory class of a queued command line, used to pick its estimate."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    names = [os.path.basename(w) for w in words]
    if "xcodebuild" in names:
        return "xcodebuild"
    if "pod" in names and names[names.index("pod") + 1:][:1] in (["install"], ["update"]):
        return "pod"
    if "gradlew" in names or "gradle" in names:
        i = names.index("gradlew") if "gradlew" in names else names.index("gradle")
        args = words[i + 1:]
        if any(a == "-p" and args[k + 1:k + 2] == ["backend"] or a in ("-pbackend", "--project-dir=backend")
               for k, a in enumerate(args)):
            return "gradle-backend"
        tasks = [a.rsplit(":", 1)[-1] for a in args if not a.startswith("-")]
        if any("ios" in t.lower() for t in tasks):
            return "gradle-ios"
        if not tasks or any(t in ("check", "build", "koverXmlReport", "koverVerify") for t in tasks):
            return "gradle-check"
        if any("test" in t.lower() for t in tasks):
            return "gradle-test"
        if all(t.startswith(("ktlint", "detekt")) for t in tasks):
            return "gradle-lint"
        return "gradle"
    if "uv" in names and names[names.index("uv") + 1:][:1] == ["run"]:
        return "uv"
    if any(n in ("java", "kotlinc", "kotlinc-jvm", "kotlin") for n in names):
        return "java"
    return "other"


def estimate_gb(ctype, peaks):
    """Largest recent measured peak of this command type, or the starting table value."""
    recent = peaks.get(ctype, [])[-PEAK_HISTORY:]
    return max(recent) if recent else DEFAULT_GB.get(ctype, DEFAULT_GB["other"])


def ancestors(pid, procs):
    seen = set()
    p = procs.get(pid)
    while p and p.ppid in procs and p.ppid not in seen and p.ppid != p.pid:
        seen.add(p.ppid)
        p = procs[p.ppid]
        yield p


def descendants(pid, procs):
    children = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    out, stack = [], list(children.get(pid, []))
    while stack:
        c = stack.pop()
        if c in out or c == pid:
            continue
        out.append(c)
        stack.extend(children.get(c, []))
    return out


def classify(procs, protected_pids=()):
    """Sets kind, owner and root on every process. Returns the exec roots."""
    for p in procs.values():
        p.kind = kind_of(p.args)
    for p in procs.values():
        anc = list(ancestors(p.pid, procs))
        if "CLAUDE_PID" not in p.env:  # Gradle starts some workers with a clean env; the parent tells the owner
            src = next((a for a in anc if "CLAUDE_PID" in a.env), None)
            if src:
                p.env = {**src.env, **p.env}
        if p.pid in protected_pids or p.kind == "claude" or "/.health-hub-local/" in p.args:
            p.owner = "protected"
        elif (p.env.get("GITHUB_ACTIONS") == "true" or p.kind == "runner_worker"
              or any(a.kind == "runner_worker" for a in anc)
              or ".gradle-health-hub-ci" in p.args or "/actions-runner/" in p.args):
            p.owner = "runner"
        elif "CLAUDE_PID" in p.env:
            p.owner = "agent"
        else:
            p.owner = "nonagent"
    roots = []
    for p in procs.values():
        kinds = AGENT_EXEC_KINDS if p.owner == "agent" else NONAGENT_EXEC_KINDS if p.owner == "nonagent" else set()
        if p.kind not in kinds:
            continue
        anc = list(ancestors(p.pid, procs))
        if any(a.kind in AGENT_EXEC_KINDS | INFRA_KINDS or a.owner in ("runner", "protected") and a.kind != "claude"
               for a in anc):
            continue
        roots.append(p)
    for r in roots:
        r.root = r.pid
        for d in descendants(r.pid, procs):
            procs[d].root = r.pid
    return roots


@dataclass
class Ticket:
    id: int
    pid: int
    state: str  # waiting | granted | done
    created: float
    granted: float | None = None
    pid_start: str | None = None


@dataclass
class Plan:
    grants: list = field(default_factory=list)
    kills: list = field(default_factory=list)  # (pid, reason)
    reaps: list = field(default_factory=list)  # (ticket id, reason)
    pid_starts: dict = field(default_factory=dict)  # ticket id -> lstart seen first
    blocked: str = ""
    used: int = 0
    runner_busy: bool = False
    counts: dict = field(default_factory=dict)
    limit: int = SLOTS
    raised: str = ""  # explanation of this tick's raise, if any
    lowered: int = 0  # slot holders that ended since the last tick
    holders: set = field(default_factory=set)  # slot holders after this tick's grants


def raise_refusal(*, used, limit, pressure, now, last_raise_ts, avail_gb, est):
    """Why one more slot cannot open now, or "" when it can."""
    ctype, gb = est
    if used + 1 > MAX_SLOTS:
        return f"teto de {MAX_SLOTS} vagas"
    if pressure != PRESSURE_NORMAL:
        return "vaga extra só com pressão normal"
    if last_raise_ts is not None and now - last_raise_ts < RAISE_GAP_S:
        return f"próxima vaga extra em {int(RAISE_GAP_S - (now - last_raise_ts))}s"
    if avail_gb - gb < HEADROOM_GB:
        return (f"vaga extra recusada: {avail_gb:.1f} GB disponíveis − {gb:.1f} GB estimados ({ctype}) "
                f"< {HEADROOM_GB:.0f} GB de folga")
    return ""


def plan(procs, roots, tickets, *, snapshot_ts, now, pressure, busy_daemons, idle_since, last_grant_ts,
         live_claude_pids, limit=SLOTS, prev_holders=frozenset(), last_raise_ts=None, avail_gb=0.0,
         estimates=None):
    """One scheduling tick. Pure: the caller applies the result under its lock.

    The limit rises one slot at a time above SLOTS when memory allows the head item, and drops one slot
    per slot holder that ended, never below SLOTS.
    """
    out = Plan()
    estimates = estimates or {}
    live = []
    for t in tickets:
        p = procs.get(t.pid)
        if t.created >= snapshot_ts:
            live.append(t)  # enqueued after the snapshot; its pid cannot be judged yet
        elif p is None or (t.pid_start and p.lstart != t.pid_start):
            out.reaps.append((t.id, "owner process gone"))
        else:
            if not t.pid_start:
                out.pid_starts[t.id] = p.lstart
            live.append(t)
    granted = [t for t in live if t.state == "granted"]
    granted_ids = {t.id for t in granted}
    granted_pids = {t.pid for t in granted}

    def queued(r):
        if r.env.get("BQ_TICKET", "").isdigit() and int(r.env["BQ_TICKET"]) in granted_ids:
            return True
        return any(a.pid in granted_pids for a in ancestors(r.pid, procs))

    out.runner_busy = any(p.kind == "runner_worker" for p in procs.values())
    nonagent = [r for r in roots if r.owner == "nonagent"]
    unqueued = []
    for r in roots:
        if r.owner != "agent":
            r.status = "fora da fila (não-agente)"
            continue
        if queued(r):
            r.status = "na vaga"
        else:
            r.status = "SEM VAGA"
            unqueued.append(r)
    for r in unqueued:
        out.kills.append((r.pid, "build sem vaga"))

    for p in procs.values():
        if p.owner != "agent":
            continue
        cp = int(p.env["CLAUDE_PID"]) if p.env.get("CLAUDE_PID", "").isdigit() else None
        agent_dead = cp is not None and cp not in live_claude_pids
        if agent_dead and p.kind in AGENT_EXEC_KINDS | {"gq"} and p.root in (p.pid, None) \
                and p.pid not in {k[0] for k in out.kills}:
            out.kills.append((p.pid, f"órfão: agente {cp} terminou"))
        elif p.kind == "gradle_worker" and p.ppid == 1:  # embedded_pg is ppid 1 even while in use: show only
            out.kills.append((p.pid, "worker solto (pai morreu)"))

    local_active = len(granted) + len(nonagent) + len(unqueued)
    recent_grant = last_grant_ts is not None and now - last_grant_ts < GRANT_QUIET_S
    killed_daemon = False
    for p in procs.values():
        if p.owner != "agent" or p.kind not in ("gradle_daemon", "kotlin_daemon"):
            continue
        if p.pid in busy_daemons:
            p.status = "ocupado"
            continue
        idle = now - idle_since.get(p.pid, now)
        p.status = f"ocioso {int(idle)}s"
        grace = KOTLIN_IDLE_S if p.kind == "kotlin_daemon" else IDLE_GRACE_S
        if idle < grace or recent_grant:
            continue
        out.kills.append((p.pid, "daemon ocioso"))
        killed_daemon = True

    out.used = int(out.runner_busy) + local_active
    holders = {f"t{t.id}" for t in granted} | {f"p{r.pid}@{r.lstart}" for r in nonagent + unqueued}
    if out.runner_busy:
        holders.add("runner")
    out.lowered = len(set(prev_holders) - holders)
    limit = max(SLOTS, limit - out.lowered)
    out.counts = {"granted": len(granted), "nonagent": len(nonagent), "unqueued": len(unqueued),
                  "runner": int(out.runner_busy), "local_active": local_active}
    waiting = sorted((t for t in live if t.state == "waiting"), key=lambda t: t.id)
    used = out.used
    for t in waiting:  # strict FIFO: only the head may enter, so stop at the first refusal
        if pressure >= PRESSURE_CRITICAL:
            out.blocked = "pressão de memória crítica"
            break
        if pressure >= PRESSURE_WARN and local_active > 0:
            out.blocked = "pressão de memória em alerta (só entra com zero builds locais)"
            break
        if killed_daemon:
            out.blocked = "aguardando daemon ocioso encerrar"
            break
        if used >= limit:
            est = estimates.get(t.id, ("other", DEFAULT_GB["other"]))
            why = raise_refusal(used=used, limit=limit, pressure=pressure, now=now, last_raise_ts=last_raise_ts,
                                avail_gb=avail_gb, est=est)
            if why:
                out.blocked = f"sem vaga ({used}/{limit} em uso); {why}"
                break
            out.raised = (f"limite {limit}→{used + 1}: {avail_gb:.1f} GB disponíveis − {est[1]:.1f} GB "
                          f"estimados ({est[0]}) ≥ {HEADROOM_GB:.0f} GB de folga")
            limit = used + 1
            last_raise_ts = now  # one raise per RAISE_GAP_S, so at most one per tick
        out.grants.append(t.id)
        holders.add(f"t{t.id}")
        used += 1
        local_active += 1
    out.used = used  # after this tick's grants, the same count the blocked message shows
    out.counts["local_active"] = local_active
    out.limit = limit
    out.holders = holders
    return out


# ---------------------------------------------------------------- hook analysis

SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", "\n", ";;", ";&"}
TRIGGERS = {"gradlew", "gradle", "xcodebuild", "java", "kotlinc", "kotlinc-jvm", "kotlin"}
VERSION_ONLY = {("-version",), ("--version",), ("-v",), ("version",)}
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(cmd):
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        m = _HEREDOC.search(line)
        out.append(line[:m.start()] if m else line)
        i += 1
        if m:
            while i < len(lines) and lines[i].strip() != m.group(2):
                i += 1
            i += 1
    return "\n".join(out)


def _segments(cmd):
    lex = shlex.shlex(_strip_heredocs(cmd), posix=True, punctuation_chars=";&|()\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    seg = []
    for tok in lex:
        if tok in SEPARATORS or (set(tok) <= set(";&|()\n") and not tok.startswith(">")):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(tok)
    if seg:
        yield seg


def _program(words):
    """Returns (basename, rest) of the program a segment runs, skipping env assignments and wrappers."""
    i = 0
    while i < len(words):
        w, b = words[i], os.path.basename(words[i])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
        elif b in ("nohup", "time", "exec", "command", "builtin", "rtk"):
            i += 1
            if b == "rtk" and i < len(words) and words[i] == "proxy":
                i += 1
        elif b in ("env", "caffeinate", "nice", "stdbuf", "sudo", "timeout", "gtimeout"):
            i += 1
            while i < len(words) and (words[i].startswith("-") or re.match(r"^[A-Za-z_]\w*=", words[i])):
                i += 2 if words[i] in ("-n", "-u") else 1
            if b in ("timeout", "gtimeout") and i < len(words):
                i += 1
        elif b == "perl":
            j = words.index("-e", i) if "-e" in words[i:] else None
            if j is None:
                return b, words[i + 1:]
            i = j + 2
        else:
            return b, words[i + 1:]
    return None, []


def match_call(records, session_id, created, taken):
    """The hook record of the Bash call that started a ticket: newest unclaimed `gq run` call of the session.

    shortcut: a session that launches two `gq run` calls within the window in different agents can swap their
    labels; upgrade by passing the tool_use_id through the command if that ever matters.
    """
    for r in reversed(records):
        if r.get("session_id") == session_id and r.get("id") not in taken \
                and 0 <= created - r.get("ts", 0) <= CALL_WINDOW_S:
            return r
    return None


def analyze_command(cmd, depth=0):
    """Returns (offending segments, uses_gq) for a shell command line."""
    offending, uses_gq = [], False
    try:
        segs = list(_segments(cmd))
    except ValueError:
        hit = re.search(r"(^|[\s;&|(/])(gradlew|xcodebuild|kotlinc|pod install|uv run)\b", cmd)
        return ([cmd.strip()[:120]] if hit and "gq run" not in cmd else []), "gq run" in cmd
    for words in segs:
        prog, rest = _program(words)
        if prog is None:
            continue
        if prog == "gq":
            uses_gq = uses_gq or rest[:1] == ["run"]  # gq status / enforce return at once
            continue
        if prog in ("bash", "sh", "zsh") and "-c" in rest and depth < 3:
            k = rest.index("-c")
            if k + 1 < len(rest):
                o, g = analyze_command(rest[k + 1], depth + 1)
                offending += o
                uses_gq = uses_gq or g
            continue
        trig = prog in TRIGGERS or (prog == "pod" and rest[:1] in (["install"], ["update"])) \
            or (prog == "uv" and rest[:1] == ["run"])
        if trig and tuple(rest) not in VERSION_ONLY:
            offending.append(" ".join(words)[:120])
    return offending, uses_gq
