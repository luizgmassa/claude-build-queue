"""Remote builds: the server of the local queue decides, at the grant, whether an eligible Gradle command runs here
or on a build machine, and `gq run` carries out what it was given.

Off unless config.json has a `remote` key. `job` tells the server on entry that the command may run there. Once the
ticket is granted for the machine, gq starts it (when `instance`, `zone` and `project` are set), copies the checkout
with rsync, runs the command through `ssh` (no `gq` on that side: the ticket here holds the machine's slot), copies the
reports back (plus any `fetch_back` patterns of the config) and exits with the remote code. Anything that keeps it
from starting hands the ticket back to the queue; a build that started remotely never runs locally. Standard library only, like gq.
"""

import json
import os
import posixpath
import shlex
import shutil
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
# Compute Engine statuses of a machine on its way up: anything else after a start means the start did not take
UP = {"PROVISIONING", "STAGING", "RUNNING"}
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
    """The remote path cannot start; the ticket goes back to the queue."""


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


def _slots(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


START = ("instance", "zone", "project")
REQUIRED = {"host": _text, "root": _text}
OPTIONAL = {**{k: _text for k in START}, "ssh_options": _text_list, "local_tasks": _text_list,
            "slots": _slots, "cap_metadata_key": _text, "fetch_back": _text_list}
SLOTS = 2


def _invalid_field(r):
    if not isinstance(r, dict):
        return "remote"
    checks = {**REQUIRED, **{k: ok for k, ok in OPTIONAL.items() if k in r}}
    bad = next((k for k, ok in checks.items() if not ok(r.get(k))), None)
    # the budget check runs with the start: a key without it would never be read
    return bad or ("cap_metadata_key" if r.get("cap_metadata_key") and not all(r.get(k) for k in START) else None)


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
            "slots": r.get("slots", SLOTS), "cap_metadata_key": r.get("cap_metadata_key"),
            "fetch_back": r.get("fetch_back", [])}


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


def find_gcloud():
    """launchd's PATH lacks Homebrew, where gcloud lives on the Mac: look there too. The client keeps the bare name."""
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]  # an empty entry would mean the cwd
    path = os.pathsep.join([*dirs, "/opt/homebrew/bin", "/usr/local/bin"])
    return shutil.which("gcloud", path=path) or "gcloud"


def instance_state(cfg, gcloud="gcloud", name="budget"):
    """(status, capped) of the build machine from one `describe`: its Compute Engine status and whether the metadata
    `cap_metadata_key` is `true`. Raises Unavailable, its reason led by `name`, when the answer cannot be had."""
    instance, zone, project = cfg["start"]
    code, out, err = SHELL.run([gcloud, "compute", "instances", "describe", instance, f"--zone={zone}",
                                f"--project={project}", "--format=json"])
    if code != 0:
        raise Unavailable(_reason(name, code, err))
    key = cfg["cap_metadata_key"]
    try:
        info = json.loads(out)
        items = (info.get("metadata") or {}).get("items") or []
        capped = bool(key) and any(i.get("key") == key and str(i.get("value")).lower() == "true" for i in items)
        return info.get("status"), capped
    except (ValueError, AttributeError, TypeError) as e:
        raise Unavailable(f"{name}: unreadable describe output ({e})")


def check_budget(cfg):
    """Refuses a machine whose metadata `cap_metadata_key` is `true`: the budget function sets it to stop spending."""
    if cfg["cap_metadata_key"] and instance_state(cfg)[1]:
        raise Unavailable("budget exceeded")


def start_machine(cfg):
    """Starts the machine and returns once Compute Engine reports it on its way up. A start that lands while the idle
    shutdown is still stopping the machine ends DONE and leaves it TERMINATED, so a machine found STOPPING is waited
    out and started again, within PROBE_LIMIT_S."""
    instance, zone, project = cfg["start"]
    started, status = SHELL.now(), None
    while True:
        if status != "STOPPING":  # a start sent to a stopping machine is the one that does not take
            step("start", ["gcloud", "compute", "instances", "start", instance, f"--zone={zone}",
                           f"--project={project}", "--quiet"])
        status = instance_state(cfg, name="start")[0]
        if status in UP:
            return
        if SHELL.now() - started >= PROBE_LIMIT_S:
            raise Unavailable(f"start: the machine is still {status}")
        SHELL.sleep(PROBE_PAUSE_S)


def prepare(j):
    cfg, top, dest = j.cfg, j.top, j.dest
    if cfg["start"]:
        check_budget(cfg)
        start_machine(cfg)
    probe(cfg)
    step("rsync", ssh(cfg, "mkdir -p " + shlex.quote(dest), *FAIL_FAST))
    protect = [f"--filter=P {p}" for p in PROTECTED]
    with lock(dest):
        # ignored files (local.properties included) stay here
        step("rsync", rsync(cfg, "--delete", "--exclude=/.git", *protect, "--filter=:- .gitignore", f"{top}/",
                            f"{cfg['host']}:{dest}/"))
        step("clear", ssh(cfg, f"cd {shlex.quote(dest)} && {clear_command(BACK + cfg['fetch_back'])}", *FAIL_FAST))


def clear_command(patterns):
    """Removes, on the build machine, what the copy back would bring: `build/` survives between runs, and a report of a
    deleted test or a dropped generated directory must not reach this machine. The build cache restores the rest."""
    paths = ["." + p if p.startswith("/") else "*/" + p for p in (p.removesuffix("/***") for p in patterns)]
    tests = [a for path in paths for a in ("-o", "-path", path)][1:]
    return shlex.join(["find", ".", "(", *tests, ")", "-prune", "-exec", "rm", "-rf", "{}", "+"])


def fetch_back(j, say):
    includes = [f"--include={p}" for p in BACK + j.cfg["fetch_back"]]
    with lock(j.dest):
        code, _out, err = SHELL.run(rsync(j.cfg, "-m", "--include=*/", *includes, "--exclude=*",
                                          f"{j.cfg['host']}:{j.dest}/", f"{j.top}/"))
    if code != 0:
        say(f"remote reports not copied back ({_reason('rsync', code, err)})")


def _default(signals):
    for s in signals:
        signal.signal(s, signal.SIG_DFL)


def execute(j, say, signals):
    """The remote exit code, or None when ssh cannot start."""
    cfg, line = j.cfg, shlex.join(j.cmd)
    caught, child = [], None

    def stop(signum, _frame):
        caught.append(signum)
        if child is not None:
            child.terminate()  # the pseudo-TTY makes sshd send SIGHUP to the remote build, which dies with it

    for s in signals:
        signal.signal(s, stop)
    say(f"remote {cfg['host']}: {line}")
    try:
        child = SHELL.popen(ssh(cfg, f"cd {shlex.quote(j.cwd)} && exec {line}", "-tt", *KEEPALIVE))
    except OSError as e:
        _default(signals)
        say(f"remote unavailable (ssh: {e})")
        return None
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
    """Starts the machine and copies the checkout; None, or the reason (also said) when the remote path cannot start."""
    try:
        prepare(j)
    except Unavailable as e:
        say(f"remote unavailable ({e})")
        return str(e)
    return None
