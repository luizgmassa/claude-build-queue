"""Pure logic of the build queue: process classification, scheduling and the hook's command analysis.

Nothing here touches the system, so every rule is unit-testable with synthetic input.
"""

import os
import re
import shlex
from dataclasses import dataclass, field

from bq import settings

SETTINGS = settings.load()  # read once per process: the page's Save & Apply restarts the server
SLOTS = SETTINGS["base_slots"]  # base slots, runner included: these never wait on a memory check
HEADROOM_GB = SETTINGS["headroom_gb"]  # available memory left after peak reserves and the next item's estimate
EXTRA_GAP_S = SETTINGS["extra_gap_s"]  # a new build needs time to show its memory before the next extra entry
SWAP_GROWTH_GB = SETTINGS["swap_growth_gb"]  # swap growing this much within SWAP_WINDOW_S stops extra entries
SWAP_WINDOW_S = SETTINGS["swap_window_s"]
RUNNER_CTYPE = SETTINGS["runner_ctype"]  # the runner's peak reserve uses this command type's estimate
PEAK_HISTORY = SETTINGS["peak_history"]  # estimate = largest of the last N measured peaks of the same command type
DEFAULT_GB = SETTINGS["default_gb"]  # starting estimates per command type, before any measurement
IDLE_GRACE_S = SETTINGS["idle_grace_s"]
KOTLIN_IDLE_S = SETTINGS["kotlin_idle_s"]  # idle Kotlin daemon dies even mid-build: KGP falls back to in-process
GRANT_QUIET_S = SETTINGS["grant_quiet_s"]
TERM_GRACE_S = SETTINGS["term_grace_s"]
CALL_WINDOW_S = SETTINGS["call_window_s"]  # a `gq run` reaches the server within this long after the hook saw it
PROTECTED_PATHS = tuple(SETTINGS["protected_paths"])  # command-line fragments of processes never touched
RUNNER_MARKERS = tuple(SETTINGS["runner_markers"])  # command-line fragments that mark the CI runner's processes

DATA = settings.DATA
CALLS = DATA / "calls.jsonl"  # the hook's record of each `gq run` Bash call: session and subagent

REMOTE_RETRY_S = 300  # the build machine stays out of the choice this long after a failed start

PRESSURE_CRITICAL = 4  # the macOS kern.memorystatus_vm_pressure_level scale every system reports on
BLOCK_PRESSURE = SETTINGS["block_pressure"]  # 4 (critical) by default; 2 also blocks on warn

# English text of every message the server emits. A message travels as {"key", "params"}: the log renders it
# with this table, the page with its own copy per language (page.html, `i18n` block; tests keep them in step).
# Templates use only `{name}` placeholders, so Python's str.format and the page's replace read the same text.
EN = {
    "status.outside_queue": "outside the queue (non-agent)",
    "status.in_slot": "in a slot",
    "status.no_slot": "NO SLOT",
    "status.busy": "busy",
    "status.idle": "idle {s}s",
    "kill.orphan": "orphan: agent {pid} ended",
    "kill.loose_worker": "loose worker (parent died)",
    "kill.idle_daemon": "idle daemon",
    "refusal.ceiling": "configured max of {ceiling} slots",
    "refusal.gap": "next extra slot in {s}s",
    "refusal.swap": "swap grew {gb} GB in the last {s}s (limit {limit_gb} GB)",
    "refusal.headroom": "extra slot refused: {avail_gb} GB available − {reserve_gb} GB reserved for running "
                        "peaks − {est_gb} GB estimated ({ctype}) < {headroom_gb} GB headroom",
    "blocked.pressure": "critical memory pressure",
    "blocked.daemon": "waiting for an idle daemon to end",
    "blocked.no_slot": "base slots full ({used} in use, base {base}); {why}",
    "extra.granted": "ticket #{id} entered above the base: {avail_gb} GB available − {reserve_gb} GB reserved "
                     "− {est_gb} GB estimated ({ctype}) ≥ {headroom_gb} GB headroom",
    "event.enqueue": "ticket #{id} pid {pid}: {cmd}",
    "event.grant": "ticket #{id} got a slot",
    "event.grant_vm": "ticket #{id} got a slot on the build machine",
    "event.remote_running": "ticket #{id} is running on the build machine",
    "event.remote_failed": "ticket #{id}: build machine unavailable ({reason}); back to waiting",
    "event.release": "ticket #{id} released its slot (exit {exit})",
    "event.reap": "ticket #{id}: {reason}",
    "event.sigterm": "SIGTERM {pid} ({kind}): {reason}",
    "event.sigkill": "SIGKILL {pid} after {s}s: {reason}",
    "error.no_permission": "no permission to kill {pid}: {detail}",
    "error.pid_gone": "PID {pid} no longer exists",
    "end.released": "released",  # end_reason values stored in the tickets table, as "end." + value
    "end.owner process gone": "owner process gone",
    "config.type": "wrong type",
    "config.range": "must be between {min} and {max}",
    "config.max_below_base": "must be at least the base slots ({base})",
    "config.choice": "not one of the options",
    "config.list": "one non-empty entry per line, at most {n} lines of {chars} characters",
    "config.unknown": "unknown setting",
    "config.port_taken": "port {port} is in use",
    "config.autostart": "could not change the start with the system: {detail}",
}


def msg(key, **params):
    return {"key": key, "params": params}


def render(m):
    """English text of a message; a plain string (or nothing) passes through."""
    if not isinstance(m, dict):
        return "" if m is None else str(m)
    params = {k: render(v) if isinstance(v, dict) or v is None else v for k, v in m.get("params", {}).items()}
    return EN.get(m["key"], m["key"]).format(**params)


ENV_KEYS = ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "RALPHY_STATUS_FILE", "BQ_TICKET", "GITHUB_ACTIONS")

AGENT_EXEC_KINDS = {"gradle_client", "xcodebuild", "pod", "uv_run", "kotlinc", "java"}
NONAGENT_EXEC_KINDS = {"gradle_client", "xcodebuild", "pod"}
# Processes below these belong to a build already accounted for; they are never roots.
INFRA_KINDS = {"gradle_daemon", "kotlin_daemon", "gradle_worker"}
# A worker under anything else lost its parent: init, a Linux subreaper, or a reused Windows pid adopted it.
WORKER_PARENTS = INFRA_KINDS | {"gradle_client"}
# Non-JVM toolchain runs (kind = command type). The hook queues them; the server only kills them once their
# agent died, because an unqueued `make` under `pip install` or `brew install` is no reason to fail that install.
TOOLCHAIN_KINDS = {"node", "cc", "bazel", "dotnet", "msbuild", "swift", "cargo", "go", "maven", "sbt"}
# Build servers these toolchains leave behind; they die with their agent. IDE language servers are not here.
DAEMON_KINDS = {"msbuild_node", "vbcscompiler", "razor_server", "bazel_server", "tsc_watch", "eslint_d"}
INFRA_KINDS |= DAEMON_KINDS  # a bazel action or an MSBuild Exec task runs under the server, not under a gq
# A booted Android emulator (`emulator -avd X` execs into qemu-system-*) or iOS simulator device (one launchd_sim each,
# with every process of the device under it). Shown in the Running table; never queued nor killed.
DEVICE_KINDS = {"android_emulator", "ios_simulator"}
AVD = re.compile(r"(?:^|\s)(?:-avd\s+|@)(\S+)")
SIM_DEVICE = re.compile(r"(\S*/Devices/([0-9A-Fa-f-]{36}))/")
# Compiler processes only ever run under a build driver; once init adopted one, nobody reads its output.
COMPILER = re.compile(r"(cc1(plus|obj)?|clang(\+\+)?|gcc|g\+\+|cc|c\+\+)(-[\d.]+)?|swift-frontend|rustc")


@dataclass
class Proc:
    pid: int
    ppid: int
    rss_kb: int
    cpu: float
    etime: str
    lstart: str
    args: str
    started: float = 0.0  # create time, epoch seconds
    env: dict = field(default_factory=dict)
    kind: str = "other"
    owner: str = "nonagent"  # agent | runner | protected | nonagent
    root: int | None = None  # pid of the exec root this process runs under
    status: dict | str = ""  # a message once classified


def exe_name(path):
    """Program name without directory or Windows extension: `C:/jdk/bin/java.exe` and `./gradlew.bat` too."""
    return re.sub(r"\.(exe|bat|cmd)$", "", re.split(r"[/\\]", path)[-1].lower())


def kind_of(args):
    first = args.split(" ", 1)[0]
    exe = exe_name(first)
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
    if (exe == "emulator" or exe.startswith("qemu-system")) and AVD.search(args):
        return "android_emulator"
    if exe == "launchd_sim":
        return "ios_simulator"
    if exe == "claude" or "/.local/share/claude/versions/" in first:
        return "claude"
    if re.search(r"/gq(\.py)? run(\s|$)", args) or args.startswith("gq run"):
        return "gq"
    if exe == "xcodebuild":
        return "xcodebuild"
    if re.search(r"(^|[\s/])pod (install|update)\b", args):
        return "pod"
    if exe == "uv" and len(words) > 1 and words[1] == "run":
        return "uv_run"
    if exe in ("kotlinc", "kotlinc-jvm", "kotlin") or "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler" in args:
        return "kotlinc"
    exe, rest = _node_script(words[1:]) if exe in ("node", "nodejs") else (exe, words[1:])
    if re.search(r"msbuild(\.dll|\.exe)?\s(.*\s)?[-/]+nodemode:", args, re.I):
        return "msbuild_node"
    if exe == "vbcscompiler" or re.search(r"vbcscompiler\.dll\b", args, re.I):
        return "vbcscompiler"
    if re.search(r"rzc\.dll\s+server\b", args, re.I):
        return "razor_server"
    if "A-server.jar" in args:  # the bazel server runs as `bazel(<workspace>)`, a JVM with the server jar
        return "bazel_server"
    if exe == "eslint_d" or "/eslint_d/" in args:
        return "eslint_d"
    if exe == "tsc" and WATCH & set(rest):
        return "tsc_watch"
    if exe == "java":
        return "java"
    if COMPILER.fullmatch(exe):
        return "compiler"
    tool = toolchain(exe, rest)
    if tool:
        return tool
    if exe == "postgres" and "embedded-pg" in args:
        return "embedded_pg"
    return "other"


def parent_gone(p, procs):
    """Init, a Linux subreaper, or nothing (a dead Windows parent) holds the process now."""
    parent = procs.get(p.ppid)
    return p.ppid <= 1 or parent is None or exe_name(parent.args.split(" ", 1)[0]) in ("launchd", "init", "systemd")


def command_type(cmd):
    """Memory class of a queued command line, used to pick its estimate."""
    if os.name == "nt":
        cmd = cmd.replace("\\", "/")  # posix shlex would eat the separators of C:\x\gradlew.bat
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    names = [exe_name(w) for w in words]
    wrapper = next((k for k, w in enumerate(words)
                    if w.endswith(("GradleWrapperMain", "gradle-wrapper.jar")) and "gradlew" not in names), None)
    if wrapper is not None:  # the Java client `./gradlew` execs into: the tasks follow the wrapper's main class
        if words[wrapper].endswith(".jar") and words[wrapper + 1:wrapper + 2] == ["org.gradle.wrapper.GradleWrapperMain"]:
            wrapper += 1
        names[wrapper] = "gradlew"
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
    for i, n in enumerate(names):
        tool = toolchain(n, words[i + 1:])
        if tool:
            return tool
    if any(n in ("java", "kotlinc", "kotlinc-jvm", "kotlin") for n in names):
        return "java"
    return "other"


def estimate_gb(ctype, peaks):
    """Largest recent measured peak of this command type, or the starting table value."""
    recent = peaks.get(ctype, [])[-PEAK_HISTORY:]
    return max(recent) if recent else DEFAULT_GB.get(ctype, DEFAULT_GB["other"])


def remaining_gb(curves, elapsed):
    """Mean, over past runs of the same build, of the most memory each one still reached from `elapsed` seconds
    on; a run already over by then counts 0. Each curve is a list of (seconds since start, GB). None without runs."""
    if not curves:
        return None
    return sum(max((gb for t, gb in c if t >= elapsed), default=0.0) for c in curves) / len(curves)


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


def started_by_claude_itself(anc):
    """Hook and MCP processes reach their claude process without passing a Bash-tool shell."""
    for a in anc:
        if "/shell-snapshots/" in a.args:
            return False
        if a.kind == "claude":
            return True
    return False  # orphaned (ppid 1) builds still count


def classify(procs, protected_pids=()):
    """Sets kind, owner and root on every process. Returns the exec roots."""
    for p in procs.values():
        p.kind = kind_of(p.args)
    for p in procs.values():  # a JVM the daemon forks (KGP's cinterop) inherits its -D flags, the daemon marker too
        if p.kind == "gradle_daemon" and any(a.kind == "gradle_daemon" for a in ancestors(p.pid, procs)):
            p.kind = "java"
    for p in procs.values():
        anc = list(ancestors(p.pid, procs))
        if "CLAUDE_PID" not in p.env:  # Gradle starts some workers with a clean env; the parent tells the owner
            src = next((a for a in anc if "CLAUDE_PID" in a.env), None)
            if src:
                p.env = {**src.env, **p.env}
        if p.pid in protected_pids or p.kind == "claude" or any(m in p.args for m in PROTECTED_PATHS):
            p.owner = "protected"
        elif (p.env.get("GITHUB_ACTIONS") == "true" or p.kind == "runner_worker"
              or any(a.kind == "runner_worker" for a in anc)
              or any(m in p.args for m in RUNNER_MARKERS)):
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
        if p.owner == "agent" and started_by_claude_itself(anc):
            continue  # a Stop hook's `uv run` is not a build; killing it fails the hook
        if any(a.kind in AGENT_EXEC_KINDS | INFRA_KINDS or a.owner in ("runner", "protected") and a.kind != "claude"
               for a in anc):
            continue
        roots.append(p)
    for r in roots:
        r.root = r.pid
        for d in descendants(r.pid, procs):
            procs[d].root = r.pid
    return roots


def device_roots(procs):
    """The top process of each running emulator or simulator; a launcher that did not exec stands for its qemu."""
    return [p for p in procs.values() if p.kind in DEVICE_KINDS
            and not any(a.kind in DEVICE_KINDS for a in ancestors(p.pid, procs))]


def claude_of(p, procs):
    """The agent's claude pid: CLAUDE_PID, else the claude process above it. macOS hides the env of Apple's own
    tools (`simctl`), and the Bash tool's shell exports CLAUDE_PID only to its children."""
    cp = p.env.get("CLAUDE_PID", "")
    if cp.isdigit():
        return int(cp)
    return next((a.pid for a in ancestors(p.pid, procs) if a.kind == "claude"), None) if p.kind != "claude" else None


def device_user(names, procs, mine):
    """First agent or runner process outside the device (`mine`) whose command line names it by UDID or name
    (`xcodebuild -destination id=<udid>`, `simctl spawn 'iPhone 16'`); "iPhone 16" does not match "iPhone 16 Pro"."""
    names = [n for n in names if n]
    if not names:
        return None
    pat = re.compile("|".join(rf"(?<![\w-]){re.escape(n)}(?![\w-]|\s+[\w(])" for n in names), re.I)
    return next((p for _pid, p in sorted(procs.items()) if p.pid not in mine and p.owner != "protected"
                 and pat.search(p.args) and (p.owner in ("agent", "runner") or claude_of(p, procs))), None)


@dataclass
class Ticket:
    id: int
    pid: int
    state: str  # waiting | granted | done
    created: float
    granted: float | None = None
    pid_start: str | None = None
    remote: bool = False  # the client can run it on the build machine (an old client never sets this)
    dest: str | None = None  # mac | vm, written at the grant only


@dataclass
class Plan:
    grants: list = field(default_factory=list)
    remote_grants: list = field(default_factory=list)  # ticket ids sent to the build machine this tick
    kills: list = field(default_factory=list)  # (pid, reason)
    reaps: list = field(default_factory=list)  # (ticket id, reason)
    pid_starts: dict = field(default_factory=dict)  # ticket id -> lstart seen first
    blocked: dict | None = None  # message
    used: int = 0
    runner_busy: bool = False
    counts: dict = field(default_factory=dict)
    extra: dict | None = None  # message explaining this tick's entry above the base, if any
    reserve_gb: float = 0.0  # memory running builds are still expected to take before their peak
    outside: list = field(default_factory=list)  # root pids of builds running without a slot (non-agent, no gq)


def swap_growth_gb(samples, now):
    """How much swap grew within SWAP_WINDOW_S: newest sample minus the lowest one in the window."""
    recent = [gb for ts, gb in samples if now - ts <= SWAP_WINDOW_S]
    return recent[-1] - min(recent) if recent else 0.0


def peak_reserve_gb(runs):
    """Memory still to come from running builds: each (estimate, current) pair below its estimate."""
    return sum(max(0.0, est - cur) for est, cur in runs)


def extra_refusal(*, used, now, last_extra_ts, avail_gb, reserve_gb, est, ceiling, swap_gb):
    """Message saying why the head cannot enter above the base slots now, or None when it can."""
    ctype, gb = est
    if ceiling is not None and used >= ceiling:
        return msg("refusal.ceiling", ceiling=ceiling)
    if swap_gb >= SWAP_GROWTH_GB:
        return msg("refusal.swap", gb=f"{swap_gb:.1f}", s=SWAP_WINDOW_S, limit_gb=f"{SWAP_GROWTH_GB:.1f}")
    if last_extra_ts is not None and now - last_extra_ts < EXTRA_GAP_S:
        return msg("refusal.gap", s=int(EXTRA_GAP_S - (now - last_extra_ts)))
    if avail_gb - reserve_gb - gb < HEADROOM_GB:
        return msg("refusal.headroom", avail_gb=f"{avail_gb:.1f}", reserve_gb=f"{reserve_gb:.1f}",
                   est_gb=f"{gb:.1f}", ctype=ctype, headroom_gb=f"{HEADROOM_GB:.0f}")
    return None


def plan(procs, roots, tickets, *, snapshot_ts, now, pressure, busy_daemons, idle_since, last_grant_ts,
         live_claude_pids, last_extra_ts=None, avail_gb=0.0, estimates=None, ceiling=None, swap_gb=0.0,
         rss_gb=None, peaks=None, remaining=None, remote=None):
    """One scheduling tick. Pure: the caller applies the result under its lock.

    The first SLOTS runs enter freely. Each one above them needs memory for its estimate after the peak
    reserve of every running build (`rss_gb`: current GB per holder key, "t<ticket id>", "p<root pid>" or
    "runner"), swap that is not growing, EXTRA_GAP_S since the last extra entry and the configured max.
    A build with past runs of the same repo and command reserves what those runs still reached from its current
    age on (`remaining`, by holder key, see remaining_gb) instead of its type estimate.

    `remote` ({"slots": n, "available": bool}, None when off) is the build machine. When the Mac is full (the head
    refused for any reason but the gap between extras, or critical pressure), the waiting tickets whose client can run
    remotely, in id order, take the machine's free slots (`remote_grants`); the head keeps its place for the Mac.
    """
    out = Plan()
    estimates, rss_gb, peaks, remaining = estimates or {}, rss_gb or {}, peaks or {}, remaining or {}
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
    vm_granted = [t for t in granted if t.dest == "vm"]
    mac_granted = [t for t in granted if t.dest != "vm"]
    granted_ids = {t.id for t in granted}
    granted_pids = {t.pid for t in granted}

    def queued(r):
        if r.env.get("BQ_TICKET", "").isdigit() and int(r.env["BQ_TICKET"]) in granted_ids:
            return True
        return any(a.pid in granted_pids for a in ancestors(r.pid, procs))

    workers = [p for p in procs.values() if p.kind == "runner_worker"]
    out.runner_busy = bool(workers)
    ticketed = {a.pid for t in live for a in ancestors(t.pid, procs)}  # a job's own ticket counts for it
    runner_counted = any(w.pid not in ticketed for w in workers)
    nonagent = [r for r in roots if r.owner == "nonagent"]
    unqueued = []
    for r in roots:
        if r.owner != "agent":
            r.status = msg("status.outside_queue")
            continue
        if queued(r):
            r.status = msg("status.in_slot")
        else:
            r.status = msg("status.no_slot")
            unqueued.append(r)  # not killed: the hook is the enforcement; it still takes a slot

    for p in procs.values():
        if p.owner != "agent":
            continue
        cp = int(p.env["CLAUDE_PID"]) if p.env.get("CLAUDE_PID", "").isdigit() else None
        agent_dead = cp is not None and cp not in live_claude_pids
        if agent_dead and p.kind in AGENT_EXEC_KINDS | {"gq"} and p.root in (p.pid, None) \
                and p.pid not in {k[0] for k in out.kills}:
            out.kills.append((p.pid, msg("kill.orphan", pid=cp)))
        elif agent_dead and p.kind in TOOLCHAIN_KINDS | DAEMON_KINDS \
                and not any(a.kind in AGENT_EXEC_KINDS | TOOLCHAIN_KINDS | INFRA_KINDS | {"gq"}
                            for a in ancestors(p.pid, procs)):  # top of its tree; a daemon's tasks die with it
            out.kills.append((p.pid, msg("kill.orphan", pid=cp)))
        elif p.kind == "gradle_worker" and getattr(procs.get(p.ppid), "kind", None) not in WORKER_PARENTS \
                or p.kind == "compiler" and parent_gone(p, procs):
            out.kills.append((p.pid, msg("kill.loose_worker")))

    local_active = len(mac_granted) + len(nonagent) + len(unqueued)
    recent_grant = last_grant_ts is not None and now - last_grant_ts < GRANT_QUIET_S
    killed_daemon = False
    for p in procs.values():
        if p.owner != "agent" or p.kind not in ("gradle_daemon", "kotlin_daemon"):
            continue
        if p.pid in busy_daemons:
            p.status = msg("status.busy")
            continue
        idle = now - idle_since.get(p.pid, now)
        p.status = msg("status.idle", s=int(idle))
        grace = KOTLIN_IDLE_S if p.kind == "kotlin_daemon" else IDLE_GRACE_S
        if idle < grace or recent_grant:
            continue
        out.kills.append((p.pid, msg("kill.idle_daemon")))
        killed_daemon = True

    out.used = int(runner_counted) + local_active
    default = ("other", DEFAULT_GB["other"])
    runs = [(remaining.get(f"t{t.id}", estimates.get(t.id, default)[1]), rss_gb.get(f"t{t.id}", 0.0)) for t in mac_granted]
    runs += [(remaining.get(f"p{r.pid}", estimate_gb(command_type(r.args), peaks)), rss_gb.get(f"p{r.pid}", 0.0))
             for r in nonagent + unqueued]
    if runner_counted:
        runs.append((estimate_gb(RUNNER_CTYPE, peaks), rss_gb.get("runner", 0.0)))
    reserve = peak_reserve_gb(runs)
    out.outside = [r.pid for r in nonagent + unqueued]
    out.counts = {"granted": len(granted), "nonagent": len(nonagent), "unqueued": len(unqueued),
                  "runner": int(out.runner_busy), "local_active": local_active}
    waiting = sorted((t for t in live if t.state == "waiting"), key=lambda t: t.id)
    used = out.used
    full_at = None  # index of the first waiting ticket the Mac cannot take
    for i, t in enumerate(waiting):  # strict FIFO: only the head may enter, so stop at the first refusal
        if pressure >= BLOCK_PRESSURE:
            out.blocked = msg("blocked.pressure")
            full_at = i
            break
        if killed_daemon:
            out.blocked = msg("blocked.daemon")
            break
        est = estimates.get(t.id, default)
        if used >= SLOTS:
            why = extra_refusal(used=used, now=now, last_extra_ts=last_extra_ts, avail_gb=avail_gb,
                                reserve_gb=reserve, est=est, ceiling=ceiling, swap_gb=swap_gb)
            if why:
                out.blocked = msg("blocked.no_slot", used=used, base=SLOTS, why=why)
                if why["key"] == "refusal.gap":  # the gap alone is a wait; memory or swap behind it is a full Mac
                    why = extra_refusal(used=used, now=now, last_extra_ts=None, avail_gb=avail_gb,
                                        reserve_gb=reserve, est=est, ceiling=ceiling, swap_gb=swap_gb)
                full_at = i if why else None
                break
            out.extra = msg("extra.granted", id=t.id, avail_gb=f"{avail_gb:.1f}", reserve_gb=f"{reserve:.1f}",
                            est_gb=f"{est[1]:.1f}", ctype=est[0], headroom_gb=f"{HEADROOM_GB:.0f}")
            last_extra_ts = now  # one extra entry per EXTRA_GAP_S, so at most one per tick
        out.grants.append(t.id)
        reserve += est[1]  # it has not started yet: its whole estimate is still to come
        used += 1
        local_active += 1
    if full_at is not None and remote and remote["available"]:
        free = remote["slots"] - len(vm_granted)
        for t in waiting[full_at:]:
            if free <= 0:
                break
            if t.remote:
                out.remote_grants.append(t.id)
                free -= 1
    out.counts["vm"] = len(vm_granted) + len(out.remote_grants)
    out.used = used  # after this tick's grants, the same count the blocked message shows
    out.counts["local_active"] = local_active
    out.reserve_gb = reserve
    return out


# ---------------------------------------------------------------- hook analysis

SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", "\n", ";;", ";&"}
TRIGGERS = {"gradlew", "gradle", "xcodebuild", "java", "kotlinc", "kotlinc-jvm", "kotlin"}
VERSION_ONLY = {("-version",), ("--version",), ("-v",), ("version",), ("--help",), ("/version",)}
WATCH = {"--watch", "-w", "--watchAll"}
JS_PMS = {"npm", "pnpm", "yarn", "bun"}
JS_INSTALL_TEST = {"install", "i", "ci", "add", "rebuild", "test", "t", "tst"}
JS_BUILD_SCRIPT = re.compile(r"(^|[:_-])(build|test|compile|typecheck|type-check|e2e)")
JS_LONG_SCRIPT = re.compile(r"watch|dev|serve|start")  # a dev server or watcher would hold its slot forever
JS_VALUE_FLAGS = {"--filter", "-F", "--dir", "-C", "--prefix", "--workspace", "-w", "--cwd"}  # `pnpm --filter web build`
JS_TASK_RUNNERS = {"nx", "turbo"}  # `nx run web:build`, `nx run-many -t build`, `turbo run build`
# Toolchains queued for any arguments but a version or help query, and those queued for some subcommands only.
TOOL_ANY = {"make": "cc", "gmake": "cc", "ninja": "cc", "msbuild": "msbuild", "mvn": "maven", "mvnw": "maven",
            "sbt": "sbt", "swift-build": "swift", "swift-test": "swift"}
TOOL_SUBS = {"next": ("node", {"build"}), "vite": ("node", {"build"}),
             "bazel": ("bazel", {"build", "test", "coverage"}), "bazelisk": ("bazel", {"build", "test", "coverage"}),
             "dotnet": ("dotnet", {"build", "test", "publish", "restore", "pack", "msbuild"}),
             "swift": ("swift", {"build", "test"}), "cargo": ("cargo", {"build", "b", "test", "t", "bench", "check", "c", "clippy"}),
             "go": ("go", {"build", "test", "install"})}
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


def _node_script(rest):
    """Tool a `node` process runs: `node --max-old-space-size=4096 /p/npm/bin/npm-cli.js ci` -> (npm, [ci])."""
    for i, w in enumerate(rest):
        if not w.startswith("-"):
            return re.sub(r"(-cli)?\.[cm]?js$", "", exe_name(w)), rest[i + 1:]
    return "", []


def toolchain(prog, rest):
    """Command type of a heavy non-JVM toolchain run (`npm ci`, `cargo test`, `make`), or None for a cheap one."""
    if prog in ("node", "nodejs"):
        return toolchain(*_node_script(rest))
    if tuple(rest) in VERSION_ONLY:
        return None
    if prog in JS_PMS:  # drop a value flag and its value, so `pnpm --filter web build` reads as `pnpm build`
        rest = [w for i, w in enumerate(rest) if w not in JS_VALUE_FLAGS and (i == 0 or rest[i - 1] not in JS_VALUE_FLAGS)]
    # shortcut: other tools' separate flag values (`go test -run X ./...` is fine, `cargo -p x build` is not) read
    # as the subcommand, so that run is not queued; parse per-tool flags if real runs slip through.
    words = [w for w in rest if not w.startswith(("-", "+"))]  # `cargo +nightly build`
    sub = words[0] if words else ""

    def then(tool):  # `npx tsc`, `pnpm exec jest`, `yarn vitest run`
        return toolchain(exe_name(tool[0]), rest[rest.index(tool[0]) + 1:]) if tool else None

    if prog in ("npx", "pnpx", "bunx"):
        return then(words[:1])
    if prog in JS_PMS:
        script = words[1] if sub in ("run", "run-script") and len(words) > 1 else sub
        if sub in JS_INSTALL_TEST or prog == "yarn" and not words or prog == "bun" and sub == "build" \
                or JS_BUILD_SCRIPT.search(script) and not JS_LONG_SCRIPT.search(script) and not WATCH & set(rest):
            return "node"
        return then(words[1:2] if sub in ("exec", "dlx", "x") else [] if sub in ("run", "run-script") else words[:1])
    if prog in JS_TASK_RUNNERS:
        tasks = [w.split("=")[-1].split(":")[-1] for w in rest]  # `--target=build`, `web:build`
        return "node" if any(JS_BUILD_SCRIPT.search(t) for t in tasks) \
            and not any(JS_LONG_SCRIPT.search(t) for t in tasks) and not WATCH & set(rest) else None
    if prog == "webpack":
        return None if sub in ("serve", "watch") or WATCH & set(rest) else "node"
    if prog in ("tsc", "jest", "vitest"):
        return None if WATCH & set(rest) or sub in ("watch", "dev") else "node"
    if prog == "cmake":
        return "cc" if "--build" in rest else None  # configuring is cheap
    if COMPILER.fullmatch(prog):
        return "cc" if words else None  # no input file: `clang -print-resource-dir`, `gcc -dumpmachine`
    if prog in TOOL_ANY:
        return TOOL_ANY[prog]
    ctype, subs = TOOL_SUBS.get(prog, (None, ()))
    return ctype if sub in subs else None


def _program(words):
    """Returns (basename, rest) of the program a segment runs, skipping env assignments and wrappers."""
    i = 0
    while i < len(words):
        w, b = words[i], exe_name(words[i])
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
        inner = None
        if prog in ("bash", "sh", "zsh") and "-c" in rest:
            k = rest.index("-c")
            inner = rest[k + 1] if k + 1 < len(rest) else ""
        elif prog == "cmd" and rest[:1] and rest[0].lower() in ("/c", "//c"):  # Git Bash needs //c
            inner = " ".join(rest[1:])
        if inner is not None and depth < 3:
            o, g = analyze_command(inner, depth + 1)
            offending += o
            uses_gq = uses_gq or g
            continue
        if _is_build(prog, rest):
            offending.append(" ".join(words)[:120])
    return offending, uses_gq


def _is_build(prog, rest):
    trig = prog in TRIGGERS or (prog == "pod" and rest[:1] in (["install"], ["update"])) \
        or (prog == "uv" and rest[:1] == ["run"]) or toolchain(prog, rest)
    return bool(trig) and tuple(rest) not in VERSION_ONLY


# `gq` joins its argv without quotes, so `perl -e 'alarm 3000; exec @ARGV' cmd` arrives with a bare `;`
PERL_ALARM = re.compile(r"\bperl -e alarm\(?\s*\d+\)?;\s*exec\(?\s*@ARGV\)?\s+")
QUIET = {"-q", "--quiet"}
SCRIPT_EXT = (".sh", ".bash", ".zsh")
REDIRECT = re.compile(r"^\d*(>>?|<)")


def _build_lines(text):
    """Each build a shell text runs, as its program and arguments without wrappers, quiet flags or redirections."""
    try:
        segs = list(_segments(PERL_ALARM.sub("", text.replace("\\\n", " "))))  # a `\` line continuation
    except ValueError:
        return []
    out = []
    for words in segs:
        kept, skip = [], False
        for w in words:
            if skip:
                skip = False
            elif REDIRECT.match(w):
                skip = REDIRECT.sub("", w) == ""  # `> f` takes the next word; `>f` and `2>&1` do not
            else:
                kept.append(w)
        prog, rest = _program(kept)
        if prog and _is_build(prog, rest):
            out.append(" ".join([prog] + [w for w in rest if w not in QUIET]))
    return out


def script_of(cmd):
    """(path, arguments) of the shell script a queued command runs (`/tmp/x.sh a`, `zsh x.sh`), or None."""
    try:
        words = shlex.split(PERL_ALARM.sub("", cmd))
    except ValueError:
        return None
    prog, rest = _program(words)
    if prog in ("sh", "bash", "zsh") and rest and not rest[0].startswith("-"):
        return rest[0], rest[1:]
    path = words[len(words) - len(rest) - 1] if prog else ""
    return (path, rest) if path.endswith(SCRIPT_EXT) else None  # shortcut: a script without extension reads as a path


def build_key(cmd, script=None):
    """What, with the repo, groups runs of the same build for its memory curve: the builds the command runs.

    `script` is the text of the shell script the command runs (see script_of): its builds and the script's
    arguments stand in for its path, so one-off scripts running the same tasks share a key. A command running
    no build keys by its own words.
    """
    inner = _build_lines(script) if script is not None else []
    if inner:
        args = [w for w in script_of(cmd)[1] if w not in QUIET]
        return " ; ".join(inner) + (" | " + " ".join(args) if args else "")
    return " ; ".join(_build_lines(cmd)) or " ".join(PERL_ALARM.sub("", cmd).split())
