"""Remote builds: `gq run` overflows an eligible Gradle command to a build machine over ssh.

Off unless config.json has a `remote` key. An eligible command waits in the local queue first; once it has waited
`overflow_after_s` (60 s by default), gq starts the machine and copies the checkout there with rsync while the
ticket keeps its place. If a local slot came free meanwhile the build runs here; otherwise gq drops the ticket, runs
`gq run` on that machine (its own queue holds the slot), copies the reports back and exits with the remote code.
Anything that keeps it from starting leaves the command in the local queue; a build that started remotely never runs
locally. Standard library only, like gq.
"""

import os
import posixpath
import shlex
import signal
import subprocess
import time
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path

from bq import settings

try:
    import fcntl
except ImportError:  # Windows: no flock and no native rsync, so the remote path does not exist there
    fcntl = None

# Gradle options whose next argument is a value, not a task (`check -x :shared:iosSimulatorArm64Test`)
VALUE_OPTIONS = {"-x", "--exclude-task", "--tests", "-p", "--project-dir", "-I", "--init-script", "-c",
                 "--settings-file", "--console"}
# the last four read keys, a database or a signing file that exist only on this machine (~/.config, the environment)
LOCAL_TASKS = ["ktlintFormat", "ios", "generateDummyFramework", "installDebug", "connected", "dependencyCheck",
               "applyMigrations", "assemble", "bundle"]
PROBE_LIMIT_S = 180
PROBE_PAUSE_S = 5
SSH_LOST = 255
# unanchored: `**/build/reports/***` would miss the root project's build/reports (the Kover XML)
BACK = ["build/reports/***", "build/test-results/***", "build/outputs/***"]
# openrsync (macOS) does not let `:- .gitignore` shield the receiver from --delete, so the build state that has
# to survive between runs is protected explicitly
PROTECTED = ["build/", ".gradle/", ".kotlin/"]
FAIL_FAST = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
# a silent network drop or a vanished VM ends the run as a lost connection after about a minute
KEEPALIVE = ("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4")
LOCKS = settings.DATA / "remote-locks"
# top: local checkout; dest: its copy on the build machine; cwd: where the command runs there
Job = namedtuple("Job", "cfg cmd top dest cwd")


class Unavailable(Exception):
    """The remote path cannot start; gq runs the command locally instead."""


class Shell:
    """Every process the remote path starts goes through here, so the tests swap it for a fake."""

    def run(self, argv, cwd=None):
        try:
            p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        except OSError as e:
            return 127, "", str(e)
        return p.returncode, p.stdout, p.stderr

    def popen(self, argv):
        return subprocess.Popen(argv)

    def sleep(self, seconds):
        time.sleep(seconds)

    def now(self):
        return time.monotonic()


SHELL = Shell()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _text_list(value):
    return isinstance(value, list) and all(map(_text, value))


def _seconds(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


START = ("instance", "zone", "project")
REQUIRED = {"host": _text, "root": _text}
OPTIONAL = {**{k: _text for k in START}, "ssh_options": _text_list, "local_tasks": _text_list,
            "overflow_after_s": _seconds}
OVERFLOW_AFTER_S = 60


def _invalid_field(r):
    if not isinstance(r, dict):
        return "remote"
    checks = {**REQUIRED, **{k: ok for k, ok in OPTIONAL.items() if k in r}}
    return next((k for k, ok in checks.items() if not ok(r.get(k))), None)


def config(say):
    """The validated `remote` settings, or None when the key is absent, invalid, or this is Windows."""
    raw = settings.read_raw()
    if "remote" not in raw:
        return None
    field = "windows" if fcntl is None else _invalid_field(raw["remote"])
    if field:
        say(f"remote config ignored ({field})")
        return None
    r = raw["remote"]
    start = [r.get(k) for k in START]
    return {"host": r["host"], "root": r["root"], "ssh_options": r.get("ssh_options", []),
            "local_tasks": r.get("local_tasks", LOCAL_TASKS), "start": start if all(start) else None,
            "overflow_after_s": r.get("overflow_after_s", OVERFLOW_AFTER_S)}


def gradle_args(cmd):
    """The Gradle arguments of `./gradlew ...` or `perl -e '<code>' ./gradlew ...`; None for anything else."""
    if cmd[:1] == ["./gradlew"]:
        return cmd[1:]
    perl_wrapped = cmd[:2] == ["perl", "-e"] and cmd[3:4] == ["./gradlew"]
    return cmd[4:] if perl_wrapped else None


def option_value(a, it):
    """(name, value) of `-p x` (taking x from it) or `--project-dir=x`; None for any other argument."""
    if a in VALUE_OPTIONS:
        return a, next(it, "")
    name, eq, value = a.partition("=")
    return (name, value) if eq and name in VALUE_OPTIONS else None


def parse(args):
    """(tasks, project dirs) of a Gradle command line: positional arguments, minus the option values."""
    tasks, dirs, it = [], [], iter(args)
    for a in it:
        opt = option_value(a, it)
        if opt is None and not a.startswith("-"):
            tasks.append(a)
        elif opt and opt[0] in ("-p", "--project-dir"):
            dirs.append(opt[1])
    return tasks, dirs


def checkout(cwd):
    """(top of the checkout, name of the main repository) for cwd, or None outside git."""
    code, out, _err = SHELL.run(["git", "rev-parse", "--show-toplevel", "--git-common-dir"], cwd=cwd)
    lines = out.splitlines()
    if code != 0 or len(lines) != 2:
        return None
    return Path(lines[0]).resolve(), (Path(cwd) / lines[1]).resolve().parent.name


def remote_root(root):
    """Paths without a leading slash are relative to the remote home, for ssh and rsync alike; `~` is not
    expanded inside a quoted argument, so `~/x` becomes `x`."""
    return "." if root == "~" else root[2:] if root.startswith("~/") else root


def has_local_task(tasks, local_tasks):
    local = [t.lower() for t in local_tasks]
    return any(lt in t.lower() for t in tasks for lt in local)


def job(cmd, cfg, cwd):
    """The remote run of an eligible command, None otherwise."""
    args = gradle_args(cmd)
    if args is None:
        return None
    tasks, dirs = parse(args)
    found = None if has_local_task(tasks, cfg["local_tasks"]) else checkout(cwd)
    if found is None:
        return None
    top, repo = found
    here = Path(cwd).resolve()
    if not all((here / d).resolve().is_relative_to(top) for d in dirs):
        return None
    dest = posixpath.join(remote_root(cfg["root"]), repo, top.name)
    rel = here.relative_to(top).as_posix()
    return Job(cfg, cmd, top, dest, dest if rel == "." else posixpath.join(dest, rel))


def ssh(cfg, command, *extra):
    return ["ssh", *extra, *cfg["ssh_options"], cfg["host"], command]


def rsync(cfg, *args):
    return ["rsync", "-a", "--timeout=60", "-e", shlex.join(["ssh", *cfg["ssh_options"]]), *args]


def _reason(step, code, err):
    lines = [x for x in err.strip().splitlines() if x.strip()]
    return f"{step}: {lines[-1] if lines else f'exit {code}'}"


def step(name, argv):
    code, _out, err = SHELL.run(argv)
    if code != 0:
        raise Unavailable(_reason(name, code, err))


@contextmanager
def lock(dest):
    """One rsync at a time per checkout: two sessions in the same worktree must not interleave copies."""
    LOCKS.mkdir(parents=True, exist_ok=True)
    with open(LOCKS / (dest.replace("/", "_") + ".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def probe(cfg):
    started = SHELL.now()
    while True:
        code, _out, err = SHELL.run(ssh(cfg, "true", *FAIL_FAST))
        if code == 0:
            return
        if SHELL.now() - started >= PROBE_LIMIT_S:
            raise Unavailable(_reason("ssh", code, err))
        SHELL.sleep(PROBE_PAUSE_S)


def prepare(j):
    cfg, top, dest = j.cfg, j.top, j.dest
    if cfg["start"]:
        instance, zone, project = cfg["start"]
        step("start", ["gcloud", "compute", "instances", "start", instance, f"--zone={zone}",
                       f"--project={project}", "--quiet"])
    probe(cfg)
    step("rsync", ssh(cfg, "mkdir -p " + shlex.quote(dest), *FAIL_FAST))
    protect = [f"--filter=P {p}" for p in PROTECTED]
    with lock(dest):
        # ignored files (local.properties included) stay here
        step("rsync", rsync(cfg, "--delete", "--exclude=/.git", *protect, "--filter=:- .gitignore", f"{top}/",
                            f"{cfg['host']}:{dest}/"))


def fetch_back(j, say):
    includes = [f"--include={p}" for p in BACK]
    with lock(j.dest):
        code, _out, err = SHELL.run(rsync(j.cfg, "-m", "--include=*/", *includes, "--exclude=*",
                                          f"{j.cfg['host']}:{j.dest}/", f"{j.top}/"))
    if code != 0:
        say(f"remote reports not copied back ({_reason('rsync', code, err)})")


def _default(signals):
    for s in signals:
        signal.signal(s, signal.SIG_DFL)


def execute(j, say, signals, started=lambda: None):
    """The remote exit code, or None when ssh cannot start; `started` runs once the remote run exists."""
    cfg, line = j.cfg, shlex.join(j.cmd)
    caught, child = [], None

    def stop(signum, _frame):
        caught.append(signum)
        if child is not None:
            child.terminate()  # the pseudo-TTY makes sshd send SIGHUP to the remote gq, which frees its ticket

    for s in signals:
        signal.signal(s, stop)
    say(f"remote {cfg['host']}: {line}")
    try:
        child = SHELL.popen(ssh(cfg, f"cd {shlex.quote(j.cwd)} && exec gq run -- {line}", "-tt", *KEEPALIVE))
    except OSError as e:
        _default(signals)
        say(f"remote unavailable (ssh: {e}); running locally")
        return None
    started()
    if caught:  # a signal arrived before the child existed
        child.terminate()
    code = child.wait()
    if caught:
        return 128 + caught[0]
    code = code if code >= 0 else 128 - code
    if code == SSH_LOST:
        say("remote connection lost")
    _default(signals)  # a signal during the copy back must still stop gq
    fetch_back(j, say)
    return code


def plan(cmd, say):
    """The remote run of cmd when the `remote` key is on and cmd is eligible, None otherwise."""
    cfg = config(say)
    return None if cfg is None else job(cmd, cfg, os.getcwd())


def start(j, say):
    """Starts the machine and copies the checkout; False, with a line, when the remote path cannot start."""
    try:
        prepare(j)
    except Unavailable as e:
        say(f"remote unavailable ({e}); running locally")
        return False
    return True
