import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# settings load at import: read them from an empty data dir, never this machine's config.json
os.environ["BQ_DATA"] = os.environ.get("BQ_TEST_DATA") or tempfile.mkdtemp(prefix="bq-test-")
os.environ["BQ_TEST_DATA"] = os.environ["BQ_DATA"]
from bq import core, gq, remote, server, settings  # noqa: E402

HOST = "me@build-vm"
POSIX_ONLY = unittest.skipIf(remote.fcntl is None, "the remote path does not exist on Windows")
CONFIG = {"host": HOST, "root": "~/remote-builds", "ssh_options": ["-i", "~/.ssh/key"]}
GRADLEW = "./gradlew " + " ".join(remote.NO_DAEMONS)  # what every remote build starts with
TICKET = 5
EXEC = f"exec env BQ_TICKET={TICKET}"


class Child:
    def __init__(self, code, on_wait=None):
        self.code, self.on_wait, self.terminated = code, on_wait, False

    def wait(self):
        if self.on_wait:
            self.on_wait()
        return self.code

    def terminate(self):
        self.terminated = True


class FakeShell:
    """Answers every process the remote path starts; `fail` maps a step to (exit code, stderr)."""

    def __init__(self, top, common, fail=(), child=None):
        self.top, self.common, self.fail = top, common, dict(fail)
        self.child = child or Child(0)
        self.calls, self.popened, self.clock = [], [], 0.0
        self.budget = "false"
        self.status = "RUNNING"
        self.statuses = []  # answered by the next describes, in order, before `status`

    @staticmethod
    def step(argv):
        if argv[0] == "ssh":
            return "probe" if argv[-1] == "true" else "mkdir" if argv[-1].startswith("mkdir") else "clear"
        if argv[0] == "rsync":
            return "rsync-out" if "--delete" in argv else "rsync-back"
        if argv[0] == "gcloud":
            return "describe" if "describe" in argv else "start"
        return {"git": "git"}.get(argv[0], argv[0])

    def run(self, argv, cwd=None, timeout=None):
        name = self.step(argv)
        self.calls.append((name, argv))
        if name in self.fail:
            code, err = self.fail[name]
            return code, "", err
        if name == "git":
            return 0, f"{self.top}\n{self.common}\n", ""
        if name == "describe":
            return 0, json.dumps({"status": self.statuses.pop(0) if self.statuses else self.status,
                                    "metadata": {"items": [{"key": "budget-exceeded", "value": self.budget}]}}), ""
        return 0, "", ""

    def popen(self, argv):
        self.popened.append(argv)
        if "popen" in self.fail:
            raise OSError(self.fail["popen"])
        return self.child

    def sleep(self, seconds):
        self.clock += seconds

    def now(self):
        return self.clock

    def names(self):
        return [n for n, _a in self.calls]

    def argv(self, name):
        return next(a for n, a in self.calls if n == name)


class RemoteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bq-remote-")).resolve()
        self.top = self.tmp / "worktrees" / "wt"
        (self.top / "backend").mkdir(parents=True)
        self.cwd = os.getcwd()
        os.chdir(self.top)
        self.lines = []
        self.saved = {s: signal.getsignal(s) for s in gq.SIGNALS}
        self.real_shell, self.real_locks = remote.SHELL, remote.LOCKS
        remote.LOCKS = self.tmp / "locks"
        self.use_shell()

    def tearDown(self):
        os.chdir(self.cwd)
        remote.SHELL, remote.LOCKS = self.real_shell, self.real_locks
        for s, h in self.saved.items():
            signal.signal(s, h)
        settings.CONFIG.unlink(missing_ok=True)

    def use_shell(self, **kw):
        self.shell = remote.SHELL = FakeShell(self.top, self.tmp / "evolvai" / ".git", **kw)
        return self.shell

    def configure(self, value):
        settings.DATA.mkdir(parents=True, exist_ok=True)
        settings.CONFIG.write_text(json.dumps({"remote": value}))

    def run_remote(self, cmd):
        """plan, start and execute in a row, the path gq takes once the ticket is granted for the machine."""
        j = remote.plan(cmd, self.lines.append)
        if j is None or remote.start(j, self.lines.append):
            return None
        return remote.execute(j, self.lines.append, gq.SIGNALS, TICKET)


class Config(RemoteBase):
    @POSIX_ONLY
    def test_a_budget_key_without_the_start_fields_is_refused(self):
        self.configure({**CONFIG, "cap_metadata_key": "budget-exceeded"})
        self.assertIsNone(remote.config(self.lines.append))
        self.assertEqual(self.lines, ["remote config ignored (cap_metadata_key)"])

    @POSIX_ONLY
    def test_slots_default_to_2_and_must_be_a_positive_integer_and_the_old_key_is_ignored(self):
        self.configure(CONFIG)
        cfg = remote.config(self.lines.append)
        self.assertEqual((cfg["slots"], cfg["cap_metadata_key"]), (2, None))
        for bad in (0, -1, True, "2", 1.5):
            self.configure({**CONFIG, "slots": bad})
            self.assertIsNone(remote.config(self.lines.append))
        self.assertEqual(self.lines[-1], "remote config ignored (slots)")
        self.configure({**CONFIG, "cap_metadata_key": " "})
        self.assertIsNone(remote.config(self.lines.append))
        self.configure({**CONFIG, "instance": "i", "zone": "z", "project": "p", "slots": 3,
                        "cap_metadata_key": "budget-exceeded", "overflow_after_s": "x"})
        cfg = remote.config(self.lines.append)
        self.assertEqual((cfg["slots"], cfg["cap_metadata_key"]), (3, "budget-exceeded"))
        self.assertNotIn("overflow_after_s", cfg)

    @POSIX_ONLY
    def test_fetch_back_is_an_optional_list_of_patterns(self):
        self.configure(CONFIG)
        self.assertEqual(remote.config(self.lines.append)["fetch_back"], [])
        self.configure({**CONFIG, "fetch_back": ["build/generated/openapi/***"]})
        self.assertEqual(remote.config(self.lines.append)["fetch_back"], ["build/generated/openapi/***"])
        for bad in ("build/x", [""], [3]):
            self.configure({**CONFIG, "fetch_back": bad})
            self.assertIsNone(remote.config(self.lines.append))
        self.assertEqual(self.lines[-1], "remote config ignored (fetch_back)")

    def test_without_the_key_nothing_runs(self):
        self.assertIsNone(self.run_remote(["./gradlew", "check"]))
        self.assertEqual((self.shell.calls, self.shell.popened, self.lines), ([], [], []))

    @POSIX_ONLY
    def test_invalid_fields_are_ignored_with_a_line(self):
        cases = [({"root": "r"}, "host"), ({"host": HOST}, "root"), ({"host": " ", "root": "r"}, "host"),
                 ({**CONFIG, "ssh_options": "-i key"}, "ssh_options"), ({**CONFIG, "local_tasks": [""]}, "local_tasks"),
                 ({**CONFIG, "zone": 3}, "zone"), ("host", "remote")]
        for value, field in cases:
            self.lines.clear()
            self.configure(value)
            self.assertIsNone(self.run_remote(["./gradlew", "check"]))
            self.assertEqual(self.lines, [f"remote config ignored ({field})"])
        self.assertEqual(self.shell.calls, [])

    def test_windows_ignores_the_key(self):
        self.configure(CONFIG)
        real, remote.fcntl = remote.fcntl, None
        try:
            self.assertIsNone(self.run_remote(["./gradlew", "check"]))
        finally:
            remote.fcntl = real
        self.assertEqual(self.lines, ["remote config ignored (windows)"])


@POSIX_ONLY
class Eligibility(RemoteBase):
    def test_option_values_are_not_tasks(self):
        tasks, dirs = remote.parse(["check", "-x", ":shared:iosSimulatorArm64Test", "--tests", "*Ios*", "-p",
                                    "backend", "--console=plain", "--project-dir=x", "-Pa=b", "--continue", "test"])
        self.assertEqual((tasks, dirs), (["check", "test"], ["backend", "x"]))

    def test_local_tasks_and_other_programs_stay_local(self):
        self.configure(CONFIG)
        for cmd in (["./gradlew", "ktlintFormat"], ["./gradlew", ":shared:linkDebugFrameworkIosArm64"],
                    ["./gradlew", ":shared:generateDummyFramework"], ["./gradlew", ":androidApp:installDebug"],
                    ["./gradlew", "connectedAndroidTest"], ["./gradlew", "dependencyCheckAnalyze"],
                    ["./gradlew", "-p", "backend", ":persistence:applyMigrations"],
                    ["./gradlew", ":androidApp:assembleDebug"], ["./gradlew", ":androidApp:bundleRelease"],
                    ["xcodebuild", "-scheme", "x"], ["pod", "install"],
                    ["uv", "run", "x.py"], ["node", "x.mjs"], ["/tmp/x.sh", ":shared:check"],
                    ["perl", "-e", "alarm 3; exec @ARGV", "/tmp/x.sh", "check"]):
            self.assertIsNone(self.run_remote(cmd), cmd)
        self.assertEqual((self.shell.popened, self.lines), ([], []))
        self.assertNotIn("probe", self.shell.names())

    def test_custom_local_tasks_replace_the_default(self):
        self.configure({**CONFIG, "local_tasks": ["detekt"]})
        self.assertIsNone(self.run_remote(["./gradlew", ":shared:DETEKT"]))
        self.assertEqual(self.run_remote(["./gradlew", "ktlintFormat"]), 0)

    def test_project_dir_outside_the_checkout_stays_local(self):
        self.configure(CONFIG)
        self.assertIsNone(self.run_remote(["./gradlew", "-p", "../outro", "check"]))
        self.assertIsNone(self.run_remote(["./gradlew", "--project-dir=/elsewhere", "check"]))
        self.assertEqual(self.shell.popened, [])

    def test_outside_git_stays_local(self):
        self.configure(CONFIG)
        self.use_shell(fail={"git": (128, "fatal: not a git repository")})
        self.assertIsNone(self.run_remote(["./gradlew", "check"]))
        self.assertEqual((self.shell.names(), self.lines), (["git"], []))


@POSIX_ONLY
class RemoteRun(RemoteBase):
    def test_eligible_command_syncs_runs_and_copies_back(self):
        self.configure(CONFIG)
        self.shell.child = Child(3)
        self.assertEqual(self.run_remote(["./gradlew", ":shared:testAndroidHostTest"]), 3)
        self.assertEqual(self.shell.names(), ["git", "probe", "mkdir", "rsync-out", "clear", "rsync-back"])
        dest = "remote-builds/evolvai/wt"
        self.assertEqual(self.shell.argv("mkdir"), ["ssh", *remote.FAIL_FAST, "-i", "~/.ssh/key", HOST,
                                                    f"mkdir -p {dest}"])
        self.assertEqual(self.shell.argv("rsync-out"),
                         ["rsync", "-a", "--timeout=60", "-e", "ssh -i '~/.ssh/key'", "--delete", "--exclude=/.git",
                          "--filter=P build/", "--filter=P .gradle/", "--filter=P .kotlin/",
                          "--filter=:- .gitignore", f"{self.top}/", f"{HOST}:{dest}/"])
        back = self.shell.argv("rsync-back")
        self.assertEqual(back[-2:], [f"{HOST}:{dest}/", f"{self.top}/"])
        for pattern in ("build/reports/***", "build/test-results/***", "build/outputs/***"):
            self.assertIn(f"--include={pattern}", back)
        self.assertLess(back.index("--include=*/"), back.index("--exclude=*"))
        self.assertEqual(self.shell.popened, [["ssh", "-tt", *remote.KEEPALIVE, "-i", "~/.ssh/key", HOST,
                                               f"cd {dest} && {EXEC} {GRADLEW} :shared:testAndroidHostTest"]])
        self.assertEqual(self.lines, [f"remote {HOST}: {GRADLEW} :shared:testAndroidHostTest"])

    def test_fetch_back_patterns_join_the_copy_back(self):
        self.configure({**CONFIG, "fetch_back": ["build/generated/openapi/***"]})
        self.assertEqual(self.run_remote(["./gradlew", ":shared:testAndroidHostTest"]), 0)
        back = self.shell.argv("rsync-back")
        for pattern in (*remote.BACK, "build/generated/openapi/***"):
            self.assertIn(f"--include={pattern}", back)
        self.assertLess(back.index("--include=build/generated/openapi/***"), back.index("--exclude=*"))
        self.assertIn("-path '*/build/generated/openapi'", self.shell.argv("clear")[-1])

    def test_probe_retries_until_the_vm_answers(self):
        self.configure(CONFIG)
        answers = iter([255, 255, 0])
        real_run = self.shell.run
        self.shell.run = lambda argv, cwd=None, timeout=None: ((next(answers), "", "") if argv[-1] == "true"
                                                               else real_run(argv, cwd))
        self.assertEqual(self.run_remote(["./gradlew", "check"]), 0)
        self.assertEqual(self.shell.clock, 2 * remote.PROBE_PAUSE_S)
        self.assertEqual(len(self.shell.popened), 1)

    def test_signal_before_ssh_exists_still_stops_it(self):
        self.configure(CONFIG)
        child = Child(0)
        real_popen = self.shell.popen

        def popen(argv):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return real_popen(argv)

        self.shell.child, self.shell.popen = child, popen
        self.assertEqual(self.run_remote(["./gradlew", "check"]), 128 + signal.SIGTERM)
        self.assertTrue(child.terminated)

    def test_signal_during_copy_back_uses_the_default_action(self):
        self.configure(CONFIG)
        real_run, seen = self.shell.run, []

        def run(argv, cwd=None, timeout=None):
            if FakeShell.step(argv) == "rsync-back":
                seen.append(signal.getsignal(signal.SIGTERM))
            return real_run(argv, cwd)

        self.shell.run = run
        self.run_remote(["./gradlew", "check"])
        self.assertEqual(seen, [signal.SIG_DFL])

    def test_alarm_wrapper_is_passed_whole_and_quoted(self):
        self.configure(CONFIG)
        cmd = ["perl", "-e", "alarm 3000; exec @ARGV", "./gradlew", "check", "-x", ":shared:iosSimulatorArm64Test"]
        self.assertEqual(self.run_remote(cmd), 0)
        remote_cmd = self.shell.popened[0][-1]
        self.assertEqual(remote_cmd, f"cd remote-builds/evolvai/wt && {EXEC} "
                                     f"perl -e 'alarm 3000; exec @ARGV' {GRADLEW} check -x :shared:iosSimulatorArm64Test")
        self.assertEqual(shlex.split(remote_cmd)[6:], cmd[:4] + remote.NO_DAEMONS + cmd[4:])

    def test_every_remote_build_runs_without_a_gradle_or_kotlin_daemon(self):
        self.configure(CONFIG)
        no_daemon, in_process = remote.NO_DAEMONS
        self.assertEqual((no_daemon, in_process), ("--no-daemon", "-Pkotlin.compiler.execution.strategy=in-process"))
        for cmd, want in [(["./gradlew", "check"], ["./gradlew", no_daemon, in_process, "check"]),
                          (["perl", "-e", "alarm 3; exec @ARGV", "./gradlew", "-p", "backend", "test"],
                           ["perl", "-e", "alarm 3; exec @ARGV", "./gradlew", no_daemon, in_process, "-p", "backend",
                            "test"]),
                          (["./gradlew", "check", no_daemon], ["./gradlew", in_process, "check", no_daemon])]:
            self.use_shell()
            self.assertEqual(self.run_remote(cmd), 0)
            self.assertEqual(shlex.split(self.shell.popened[0][-1])[6:], want)

    def test_project_dir_inside_runs_from_the_same_subdirectory(self):
        self.configure({**CONFIG, "root": "/srv/builds"})
        os.chdir(self.top / "backend")
        self.assertEqual(self.run_remote(["./gradlew", "-p", ".", "check"]), 0)
        self.assertEqual(self.shell.popened[0][-1], "cd /srv/builds/evolvai/wt/backend && "
                                                    f"{EXEC} {GRADLEW} -p . check")
        os.chdir(self.top)
        self.assertEqual(self.run_remote(["./gradlew", "-p", "backend", "check"]), 0)
        self.assertIn(f"{GRADLEW} -p backend check", self.shell.popened[1][-1])

    def test_start_runs_only_with_instance_zone_and_project(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "us-central1-a"})
        self.run_remote(["./gradlew", "check"])
        self.assertNotIn("start", self.shell.names())
        self.configure({**CONFIG, "instance": "vm", "zone": "us-central1-a", "project": "p"})
        self.use_shell()
        self.run_remote(["./gradlew", "check"])
        self.assertEqual(self.shell.calls[1], ("start", ["gcloud", "compute", "instances", "start", "vm",
                                                          "--zone=us-central1-a", "--project=p", "--quiet"]))

    def test_connection_lost_exits_255_without_running_locally(self):
        self.configure(CONFIG)
        self.shell.child = Child(255)
        self.assertEqual(self.run_remote(["./gradlew", "check"]), 255)
        self.assertIn("remote connection lost", self.lines)
        self.assertEqual(self.shell.names()[-1], "rsync-back")

    def test_failed_copy_back_keeps_the_remote_code(self):
        self.configure(CONFIG)
        self.use_shell(fail={"rsync-back": (23, "rsync error: some files could not be transferred")})
        self.assertEqual(self.run_remote(["./gradlew", "check"]), 0)
        self.assertEqual(self.lines[-1], "remote reports not copied back "
                                         "(rsync: rsync error: some files could not be transferred)")

    def test_signal_terminates_ssh_and_exits_128_plus_signal(self):
        self.configure(CONFIG)
        for signum in (signal.SIGTERM, getattr(signal, "SIGALRM", signal.SIGTERM)):
            child = Child(255, on_wait=lambda s=signum: signal.getsignal(s)(s, None))
            self.use_shell(child=child)
            self.assertEqual(self.run_remote(["./gradlew", "check"]), 128 + signum)
            self.assertTrue(child.terminated)
            self.assertNotIn("rsync-back", self.shell.names())


@POSIX_ONLY
class Fallback(RemoteBase):
    def fallback(self, reason, **kw):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p"})
        self.use_shell(**kw)
        self.assertIsNone(self.run_remote(["./gradlew", "check"]))
        self.assertEqual(self.lines, [f"remote unavailable ({reason})"])
        self.assertEqual(self.shell.popened, [])

    def test_vm_down_waits_180_seconds_then_falls_back(self):
        self.fallback("ssh: ssh: connect to host build-vm port 22: Operation timed out",
                      fail={"probe": (255, "ssh: connect to host build-vm port 22: Operation timed out\n")})
        self.assertGreaterEqual(self.shell.clock, remote.PROBE_LIMIT_S)
        self.assertGreater(self.shell.names().count("probe"), 30)

    def test_start_failure_falls_back(self):
        self.fallback("start: ERROR: (gcloud.compute.instances.start) permission denied",
                      fail={"start": (1, "ERROR: (gcloud.compute.instances.start) permission denied")})
        self.assertNotIn("probe", self.shell.names())

    def test_a_capped_vm_is_unavailable_before_it_is_started(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p", "cap_metadata_key": "budget-exceeded"})
        self.use_shell()
        self.shell.budget = "true"
        self.assertIsNone(self.run_remote(["./gradlew", "check"]))
        self.assertEqual(self.lines, ["remote unavailable (budget exceeded)"])
        self.assertEqual(self.shell.names(), ["git", "describe"])
        self.assertIn("--format=json", self.shell.argv("describe"))

    def test_null_metadata_items_do_not_crash_the_budget_check(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p", "cap_metadata_key": "budget-exceeded"})
        for body in ({"metadata": {"items": None}}, {"metadata": None}):
            self.use_shell()
            real = self.shell.run
            self.shell.run = lambda argv, cwd=None, timeout=None, b={**body, "status": "RUNNING"}, r=real: \
                (0, json.dumps(b), "") if "describe" in argv else r(argv, cwd)
            self.assertEqual(self.run_remote(["./gradlew", "check"]), 0)

    def test_an_uncapped_vm_starts_after_the_budget_check(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p", "cap_metadata_key": "budget-exceeded"})
        self.use_shell()
        self.assertEqual(self.run_remote(["./gradlew", "check"]), 0)
        self.assertEqual(self.shell.names()[:3], ["git", "describe", "start"])

    def test_no_budget_key_describes_the_vm_only_after_the_start(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p"})
        self.use_shell()
        self.shell.budget = "true"  # without the key the metadata is never read as a cap
        self.assertEqual(self.run_remote(["./gradlew", "check"]), 0)
        self.assertEqual(self.shell.names()[:4], ["git", "start", "describe", "probe"])

    def test_a_start_racing_the_idle_shutdown_waits_and_starts_again(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p"})
        self.use_shell()
        self.shell.statuses = ["STOPPING", "STOPPING", "TERMINATED"]
        self.assertEqual(self.run_remote(["./gradlew", "check"]), 0)
        self.assertEqual(self.shell.names()[:8], ["git", "start", "describe", "describe", "describe", "start",
                                                  "describe", "probe"])
        self.assertEqual(self.shell.clock, 3 * remote.PROBE_PAUSE_S)

    def test_a_machine_that_never_comes_up_falls_back_without_the_probe(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p"})
        self.use_shell()
        self.shell.status = "TERMINATED"
        self.assertIsNone(self.run_remote(["./gradlew", "check"]))
        self.assertEqual(self.lines, ["remote unavailable (start: the machine is still TERMINATED)"])
        self.assertGreaterEqual(self.shell.clock, remote.PROBE_LIMIT_S)
        self.assertNotIn("probe", self.shell.names())

    def test_a_failed_describe_after_the_start_falls_back(self):
        self.fallback("start: boom", fail={"describe": (1, "boom")})
        self.assertEqual(self.shell.names(), ["git", "start", "describe"])

    def test_rsync_failure_falls_back(self):
        self.fallback("rsync: exit 12", fail={"rsync-out": (12, "")})

    def test_ssh_that_cannot_start_falls_back(self):
        self.configure(CONFIG)
        self.use_shell(fail={"popen": "No such file or directory: 'ssh'"})
        self.assertIsNone(self.run_remote(["./gradlew", "check"]))
        self.assertEqual(self.lines[-1], "remote unavailable (ssh: No such file or directory: 'ssh')")


@POSIX_ONLY
class GcloudTimeout(RemoteBase):
    """The real Shell against a `gcloud` that never answers."""

    def start(self, **extra):
        self.use_shell()  # git answers the plan; gcloud is the silent one, run for real
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p", **extra})
        j = remote.plan(["./gradlew", "check"], self.lines.append)
        remote.SHELL = remote.Shell()
        return remote.start(j, self.lines.append)

    def test_a_silent_gcloud_is_killed_and_the_machine_is_unavailable(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gcloud").write_text("#!/bin/sh\nexec sleep 30\n")
        (bin_dir / "gcloud").chmod(0o755)
        began = time.monotonic()
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}), \
                mock.patch.object(remote, "GCLOUD_LIMIT_S", 0.2):
            self.assertEqual(self.start(), "start: timed out after 0.2s")
            self.assertEqual(self.start(cap_metadata_key="budget-exceeded"), "budget: timed out after 0.2s")
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(self.lines, ["remote unavailable (start: timed out after 0.2s)",
                                      "remote unavailable (budget: timed out after 0.2s)"])

    def test_only_gcloud_runs_under_the_limit(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p", "cap_metadata_key": "budget-exceeded"})
        real, limited = self.shell.run, {}

        def run(argv, cwd=None, timeout=None):
            if timeout is not None:
                limited[FakeShell.step(argv)] = timeout
            return real(argv, cwd)

        self.shell.run = run
        self.assertEqual(self.run_remote(["./gradlew", "check"]), 0)
        self.assertEqual(limited, {"describe": 120, "start": 120})


class Queue:
    """The local queue server as gq sees it. `waits` are the answers of /api/wait, as a state or (state, dest)."""

    def __init__(self, waits):
        self.waits, self.released, self.paths, self.remote_posts, self.enqueued = list(waits), [], [], [], []

    def call_retrying(self, path, body=None):
        self.paths.append(path)
        if path == "/api/enqueue":
            self.enqueued.append(body)
            return {"id": 5}
        if path == "/api/remote":
            self.remote_posts.append(body)
            return {"ok": True}
        state, dest = self.waits.pop(0) if isinstance(self.waits[0], tuple) else (self.waits.pop(0), None)
        return {"state": state, "dest": dest, "position": 1, "waiting": 1}

    def call(self, path, body=None, timeout=None):
        self.released.append(body)
        return {"ok": True}


@POSIX_ONLY
class GqRun(RemoteBase):
    def gq_run(self, queue, local_code=0):
        self.configure(CONFIG)
        local = mock.Mock()
        local.wait.return_value = local_code
        with mock.patch.object(gq, "call_retrying", queue.call_retrying), mock.patch.object(gq, "call", queue.call), \
                mock.patch.object(gq.subprocess, "Popen", return_value=local) as popen, \
                mock.patch.object(gq, "say", self.lines.append), mock.patch.dict(os.environ, {"BQ_TICKET": ""}):
            with self.assertRaises(SystemExit) as exited:
                gq.run(["./gradlew", "check"])
        return exited.exception.code, popen

    def test_the_enqueue_says_whether_the_command_may_run_remotely(self):
        queue = Queue([("granted", "mac")])
        self.gq_run(queue)
        self.assertIs(queue.enqueued[0]["remote"], True)
        self.assertEqual(queue.enqueued[0]["checkout"], "remote-builds/evolvai/wt")
        queue = Queue([("granted", "mac")])
        with mock.patch.object(gq, "call_retrying", queue.call_retrying), mock.patch.object(gq, "call", queue.call), \
                mock.patch.object(gq.subprocess, "Popen", return_value=mock.Mock(wait=lambda: 0)), \
                mock.patch.dict(os.environ, {"BQ_TICKET": ""}):
            with self.assertRaises(SystemExit):
                gq.run(["./gradlew", "ktlintFormat"])
        self.assertIs(queue.enqueued[0]["remote"], False)
        self.assertIsNone(queue.enqueued[0]["checkout"])

    def test_a_mac_grant_never_touches_the_build_machine(self):
        queue = Queue([("granted", "mac")])
        code, popen = self.gq_run(queue)
        self.assertEqual(code, 0)
        popen.assert_called_once()
        self.assertEqual((self.shell.calls[1:], self.shell.popened, queue.remote_posts), ([], [], []))

    def test_a_long_wait_alone_never_sends_the_build_to_the_machine(self):
        code, popen = self.gq_run(Queue(["waiting", "waiting", ("granted", "mac")]))
        self.assertEqual(code, 0)
        popen.assert_called_once()
        self.assertEqual(self.shell.popened, [])

    def test_a_vm_grant_starts_runs_releases_with_the_remote_code_and_exits_with_it(self):
        self.shell.child = Child(7)
        queue = Queue([("granted", "vm")])
        code, popen = self.gq_run(queue)
        self.assertEqual(code, 7)
        popen.assert_not_called()
        self.assertEqual(queue.remote_posts, [{"id": 5, "phase": "running"}])
        self.assertEqual(queue.released, [{"id": 5, "exit_code": 7}])
        self.assertEqual(len(self.shell.popened), 1)

    def test_the_server_finds_the_ssh_of_a_remote_build_by_its_ticket(self):
        self.gq_run(Queue([("granted", "vm")]))  # the queue gave it ticket 5
        ssh = core.Proc(pid=77, ppid=1, rss_kb=1, cpu=0, etime="", lstart="L", args=" ".join(self.shell.popened[0]))
        self.assertEqual(core.remote_ssh(5, {77: ssh}), [77])
        self.assertEqual(core.remote_ssh(50, {77: ssh}), [])

    def test_a_failed_start_hands_the_ticket_back_and_runs_nowhere_until_the_next_grant(self):
        self.use_shell(fail={"rsync-out": (12, "")})
        queue = Queue([("granted", "vm"), "waiting", ("granted", "mac")])
        code, popen = self.gq_run(queue)
        self.assertEqual(code, 0)
        self.assertEqual(queue.remote_posts, [{"id": 5, "phase": "failed", "reason": "rsync: exit 12"}])
        self.assertIn("remote unavailable (rsync: exit 12)", self.lines)
        self.assertEqual(self.shell.popened, [])
        popen.assert_called_once()  # the later Mac grant
        self.assertEqual(queue.released, [{"id": 5, "exit_code": 0}])

    def test_ssh_that_cannot_start_hands_the_ticket_back(self):
        self.use_shell(fail={"popen": "no ssh"})
        queue = Queue([("granted", "vm"), ("granted", "mac")])
        self.gq_run(queue)
        self.assertEqual([p["phase"] for p in queue.remote_posts], ["running", "failed"])
        self.assertEqual(queue.remote_posts[1]["reason"], "ssh")

    def test_a_lost_connection_exits_255_and_is_not_repeated(self):
        self.shell.child = Child(255)
        queue = Queue([("granted", "vm")])
        code, popen = self.gq_run(queue)
        self.assertEqual(code, 255)
        popen.assert_not_called()
        self.assertEqual(len(self.shell.popened), 1)

    def test_a_cancelled_remote_build_terminates_ssh_and_releases_the_ticket(self):
        child = Child(255, on_wait=lambda: signal.getsignal(signal.SIGALRM)(signal.SIGALRM, None))
        self.shell.child = child
        queue = Queue([("granted", "vm")])
        code, _popen = self.gq_run(queue)
        self.assertEqual(code, 128 + signal.SIGALRM)
        self.assertTrue(child.terminated)
        self.assertEqual(queue.released, [{"id": 5, "exit_code": 128 + signal.SIGALRM}])

    def test_nested_run_inside_a_granted_slot_never_goes_remote(self):
        self.configure(CONFIG)
        with mock.patch.object(gq, "call", return_value={"state": "granted"}), \
                mock.patch.object(gq.os, "execvp", side_effect=SystemExit(0)) as execvp, \
                mock.patch.dict(os.environ, {"BQ_TICKET": "42"}):
            with self.assertRaises(SystemExit):
                gq.run(["./gradlew", "check"])
        execvp.assert_called_once_with("./gradlew", ["./gradlew", "check"])
        self.assertEqual((self.shell.calls, self.shell.popened), ([], []))


@POSIX_ONLY
class ServerFlow(RemoteBase):
    """The server's side of a grant for the build machine: write it, take it back on a failed start, wait 300 s."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(server, "DATA", self.tmp / "queue-data")  # a database of its own per test
        patch.start()
        self.addCleanup(patch.stop)
        self.q = server.Queue()
        self.addCleanup(self.q.db.close)
        self.configure({**CONFIG, "slots": 1})
        self.procs = {p.pid: p for p in [core.Proc(pid=1, ppid=0, rss_kb=1, cpu=0, etime="", lstart="L",
                                                   args="/sbin/launchd")]}

    def enqueue(self, i, remote_ok=True, **body):
        pid = 9000 + i
        self.procs[pid] = core.Proc(pid=pid, ppid=1, rss_kb=1, cpu=0, etime="", lstart="L",
                                    args="python3 /u/.local/bin/gq run -- ./gradlew x")
        return self.q.enqueue({"pid": pid, "cwd": "/w", "cmd": "./gradlew check", "remote": remote_ok, **body})["id"]

    def plan(self, now):
        pl = core.plan(self.procs, [], self.q.active_tickets(), snapshot_ts=now, now=now, pressure=4,
                       busy_daemons=set(), idle_since={}, last_grant_ts=None, live_claude_pids=set(),
                       remote=self.q.remote_view(now))
        self.q.grant(pl, now)
        return pl

    def row(self, tid):
        return self.q.db.execute("select state, dest, phase from tickets where id=?", (tid,)).fetchone()

    def test_failed_start_returns_the_ticket_in_place_and_the_machine_waits_300_s(self):
        first, second = self.enqueue(1), self.enqueue(2)
        now = time.time() + 10
        self.assertEqual(self.plan(now).remote_grants, [first])
        self.assertEqual(self.row(first), ("granted", "vm", "starting"))
        self.assertEqual(self.q.status(first)["dest"], "vm")
        self.assertEqual(self.q.wait(first, 0)["dest"], "vm")
        self.assertEqual(self.row(second), ("waiting", None, None))  # one slot: the second waits
        self.q.remote_phase(first, "running", None)
        self.assertEqual(self.row(first), ("granted", "vm", "running"))
        self.q.release(first, 0)  # the slot comes back with the release
        self.assertEqual(self.plan(now).remote_grants, [second])
        self.q.remote_phase(second, "failed", "rsync: exit 12")
        self.assertEqual(self.row(second), ("waiting", None, None))
        self.assertEqual(self.q.status(second)["position"], 1)  # same id, same place
        self.assertEqual(self.plan(now + 1).remote_grants, [])  # the machine is out of the choice
        down = self.q.remote_down_until
        self.assertAlmostEqual(down, time.time() + core.REMOTE_RETRY_S, delta=5)
        self.assertEqual(self.plan(down - 1).remote_grants, [])
        self.assertEqual(self.plan(down + 1).remote_grants, [second])
        self.assertIn("event.remote_failed", [e["msg"]["key"] for e in self.q.events])

    def test_a_reaped_remote_gq_frees_the_machine_slot_in_the_same_tick(self):
        first, second = self.enqueue(1), self.enqueue(2)
        now = time.time() + 10
        self.plan(now)
        del self.procs[9001]  # the gq of the remote build died
        pl = self.plan(now + 1)
        self.assertEqual(pl.reaps, [(first, "owner process gone")])
        self.assertEqual(pl.remote_grants, [second])

    def test_two_tickets_of_one_checkout_take_the_machine_one_after_the_other(self):
        self.configure({**CONFIG, "slots": 2})
        first, second = (self.enqueue(i, checkout="remote-builds/evolvai/wt") for i in (1, 2))
        plain = self.enqueue(3, checkout=7)  # not a path: stored as none, like an old client
        self.assertEqual([t.checkout for t in self.q.active_tickets()], ["remote-builds/evolvai/wt"] * 2 + [None])
        now = time.time() + 10
        self.assertEqual(self.plan(now).remote_grants, [first, plain])
        self.assertEqual(self.row(second), ("waiting", None, None))
        self.q.release(first, 0)
        self.assertEqual(self.plan(now).remote_grants, [second])

    def test_a_client_that_does_not_say_remote_never_gets_the_machine(self):
        old = self.enqueue(1, remote_ok=False)
        self.assertEqual(self.plan(time.time() + 10).remote_grants, [])
        self.assertEqual(self.row(old), ("waiting", None, None))

    def test_a_report_for_a_ticket_not_on_the_machine_changes_nothing(self):
        tid = self.enqueue(1)
        self.q.remote_phase(tid, "failed", "x")
        self.assertEqual(self.q.remote_down_until, 0.0)
        self.assertNotIn("event.remote_failed", [e["msg"]["key"] for e in self.q.events])

    def test_tick_hands_a_full_macs_eligible_ticket_to_the_machine(self):
        tid = self.enqueue(1)
        mem = {"pressure": 4, "free_pct": 50, "total_gb": 0, "app_gb": 0, "wired_gb": 0, "compressed_gb": 0,
               "cached_gb": 0, "free_gb": 0, "swap_used_gb": 0, "swap_total_gb": 0}
        with mock.patch.object(server.system, "snapshot_procs", return_value=self.procs), \
                mock.patch.object(server.system, "memory", return_value=mem), \
                mock.patch.object(server.system, "tcp_links", return_value=({}, [])), \
                mock.patch.object(server.system, "cpu_total", return_value=0.0):
            self.q.tick()
        self.assertEqual(self.row(tid), ("granted", "vm", "starting"))

    def test_an_unknown_phase_is_a_bad_request(self):
        with self.assertRaises(ValueError):
            self.q.remote_phase(1, "later", None)


@POSIX_ONLY
@unittest.skipIf(shutil.which("rsync") is None, "no rsync on this machine")
class RealRsync(RemoteBase):
    """The argv the remote path builds, run by the real rsync between two local directories."""

    def local(self, name, src, dst):
        argv = self.shell.argv(name)
        i = argv.index("-e")
        return argv[:i] + argv[i + 2:-2] + [src, dst]

    def test_copy_keeps_ignored_files_here_and_the_remote_build_state(self):
        (self.top / ".gitignore").write_text("build/\nlocal.properties\n.gradle/\n")
        (self.top / "local.properties").write_text("sdk.dir=/here")
        (self.top / "backend" / "A.kt").write_text("a")
        (self.top / "build" / "reports" / "kover").mkdir(parents=True)
        (self.top / "build" / "reports" / "kover" / "report.xml").write_text("old")
        # an earlier local build: same size and second as the remote one would pass rsync's quick check
        os.utime(self.top / "build/reports/kover/report.xml", (time.time() - 3600,) * 2)
        remote_dir = self.tmp / "vm"
        for d in ("build/reports/kover", "backend/build/test-results/t", ".gradle/c"):
            (remote_dir / d).mkdir(parents=True)
        (remote_dir / ".gitignore").write_text("build/\nlocal.properties\n.gradle/\n")
        (remote_dir / "build/reports/kover/report.xml").write_text("new")
        (remote_dir / "backend/build/test-results/t/TEST-x.xml").write_text("t")
        (remote_dir / ".gradle/c/cache").write_text("c")
        (remote_dir / "backend/Stale.kt").write_text("s")
        self.configure(CONFIG)
        self.run_remote(["./gradlew", "check"])
        out = subprocess.run(self.local("rsync-out", f"{self.top}/", f"{remote_dir}/"), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue((remote_dir / "backend/A.kt").exists())
        self.assertFalse((remote_dir / "backend/Stale.kt").exists())
        self.assertFalse((remote_dir / "local.properties").exists())
        for kept in ("build/reports/kover/report.xml", "backend/build/test-results/t/TEST-x.xml", ".gradle/c/cache"):
            self.assertTrue((remote_dir / kept).exists(), kept)
        back = subprocess.run(self.local("rsync-back", f"{remote_dir}/", f"{self.top}/"), capture_output=True,
                              text=True)
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertEqual((self.top / "build/reports/kover/report.xml").read_text(), "new")
        self.assertTrue((self.top / "backend/build/test-results/t/TEST-x.xml").exists())
        self.assertFalse((self.top / ".gradle").exists())
        self.assertEqual((self.top / "local.properties").read_text(), "sdk.dir=/here")

    def test_clear_removes_what_comes_back_and_keeps_the_rest(self):
        remote_dir = self.tmp / "vm"
        stale = ("build/reports/old", "backend/build/test-results/t", "build/outputs/apk",
                 "contracts-kotlin/build/generated/openapi/c9")
        kept = ("build/classes/k", "backend/build/generated/ksp/k", ".gradle/c", "src/build/reportsx")
        for d in stale + kept:
            (remote_dir / d).mkdir(parents=True)
            (remote_dir / d / "f").write_text("x")
        self.configure({**CONFIG, "fetch_back": ["build/generated/openapi/***"]})
        self.run_remote(["./gradlew", "check"])
        command = self.shell.argv("clear")[-1].split(" && ", 1)[1]
        out = subprocess.run(["sh", "-c", command], cwd=remote_dir, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        for d in stale:
            self.assertFalse((remote_dir / d).exists(), d)
        for d in kept:
            self.assertTrue((remote_dir / d / "f").exists(), d)


NOW = 1000.0
VM = {"dest": "vm", "state": "granted"}


class VmState(unittest.TestCase):
    def state(self, tickets=(), down=0.0, reason="", polled=None, polling=True):
        return server.vm_state(list(tickets), NOW, down, reason, polled, polling)

    def test_budget_beats_everything_else(self):
        self.assertEqual(self.state([{**VM, "phase": "running"}], NOW + 5, "start: x", (NOW, "RUNNING", True)),
                         ("no_budget", ""))
        self.assertEqual(self.state(down=NOW + 5, reason="budget exceeded"), ("no_budget", ""))
        self.assertEqual(self.state(down=NOW - 5, reason="budget exceeded", polled=(NOW, "RUNNING", False)),
                         ("on", ""))  # the failure is over: it no longer counts

    def test_a_recent_failure_is_unavailable_with_its_reason(self):
        self.assertEqual(self.state(down=NOW + 5, reason="start: boom"), ("unavailable", "start: boom"))

    def test_tickets_beat_the_poll(self):
        self.assertEqual(self.state([{**VM, "phase": "starting"}], polled=(NOW, "TERMINATED", False))[0], "starting")
        self.assertEqual(self.state([{**VM, "phase": "running"}], polled=(NOW, "TERMINATED", False))[0], "on")

    def test_a_ticket_that_is_not_on_the_machine_does_not_count(self):
        self.assertEqual(self.state([{"dest": "mac", "state": "granted", "phase": None}],
                                    polled=(NOW, "TERMINATED", False))[0], "off")

    def test_an_idle_machine_follows_gcloud(self):
        for status, want in [("RUNNING", "on"), ("STAGING", "starting"), ("PROVISIONING", "starting"),
                             ("TERMINATED", "off"), ("STOPPING", "off")]:
            self.assertEqual(self.state(polled=(NOW, status, False))[0], want, status)

    def test_a_poll_older_than_three_periods_is_unknown_and_loses_its_budget_flag(self):
        old = NOW - 3 * server.VM_POLL_S - 1
        self.assertEqual(self.state(polled=(old, "RUNNING", False))[0], "unknown")
        self.assertEqual(self.state(polled=(old, "RUNNING", True))[0], "unknown")
        self.assertEqual(self.state(polled=(NOW - 3 * server.VM_POLL_S, "RUNNING", False))[0], "on")

    def test_a_poll_means_nothing_without_an_instance(self):
        self.assertEqual(self.state(polled=(NOW, "RUNNING", True), polling=False)[0], "on")

    def test_no_valid_poll_is_unknown_and_no_instance_is_always_on(self):
        self.assertEqual(self.state()[0], "unknown")
        self.assertEqual(self.state(polled=(NOW, None, False))[0], "unknown")
        self.assertEqual(self.state(polling=False)[0], "on")


class StatusLines(unittest.TestCase):
    MEM = {"pressure": 1, "free_pct": 40, "swap_used_gb": 0, "swap_total_gb": 0}

    def view(self, vm, *tickets, blocked=""):
        return {"used": 3, "base_slots": 3, "max_slots": None, "avail_gb": 12.5, "reserve_gb": 4.0,
                "swap_growth_gb": 0.0, "swap_window_s": 60, "counts": {}, "enforce": True, "memory": self.MEM,
                "blocked": blocked, "vm": vm,
                "tickets": [{"agent": "ralphy #1", "cmd": "./gradlew check", "agent_alive": True, **t}
                            for t in tickets]}

    # the pre-change print of `gq status` (git show 42602e4:bq/gq.py), with these numbers
    HEAD = ("slots 3 in use (base 3, max off, avail 12.5 GB, reserved 4.0 GB, swap +0.0 GB/60s)  runner=idle  "
            "enforce=on  pressure=1  free=40%  swap=0/0 GB")

    def test_without_the_vm_block_the_text_is_what_it_always_was(self):
        lines = gq.status_lines(self.view(None, {"id": 7, "state": "granted", "dest": "mac"},
                                          {"id": 8, "state": "waiting"}, {"id": 9, "state": "done"},
                                          blocked="base slots full"))
        self.assertEqual(lines, [self.HEAD, "blocked: base slots full",
                                 "#7 granted  ralphy #1  ./gradlew check", "#8 waiting  ralphy #1  ./gradlew check"])

    def test_with_the_vm_every_ticket_shows_its_destination_and_the_vm_line(self):
        vm = {"used": 1, "slots": 2, "state": "unavailable", "reason": "start: boom"}
        lines = gq.status_lines(self.view(vm, {"id": 1, "state": "granted", "dest": "mac"},
                                          {"id": 2, "state": "granted", "dest": "vm", "phase": "starting"},
                                          {"id": 3, "state": "granted", "dest": "vm", "phase": "running"},
                                          {"id": 4, "state": "waiting"}, {"id": 5, "state": "done", "dest": "mac"},
                                          blocked="base slots full"))
        self.assertEqual(lines, [
            self.HEAD, "vm 1/2 in use  state=unavailable (start: boom)", "blocked: base slots full",
            "#1 granted  mac            ralphy #1  ./gradlew check",
            "#2 granted  vm (starting)  ralphy #1  ./gradlew check",
            "#3 granted  vm             ralphy #1  ./gradlew check",
            "#4 waiting                 ralphy #1  ./gradlew check"])


@POSIX_ONLY
class VmView(RemoteBase):
    """What /api/state tells of the machine, through the real tick."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(server, "DATA", self.tmp / "queue-data")
        patch.start()
        self.addCleanup(patch.stop)
        self.q = server.Queue()
        self.addCleanup(self.q.db.close)
        self.configure({**CONFIG, "slots": 1})
        self.procs = {1: core.Proc(pid=1, ppid=0, rss_kb=1, cpu=0, etime="", lstart="L", args="/sbin/launchd")}

    def enqueue(self, i):
        pid = 9000 + i
        self.procs[pid] = core.Proc(pid=pid, ppid=1, rss_kb=1, cpu=0, etime="", lstart="L",
                                    args="python3 /u/.local/bin/gq run -- ./gradlew x")
        return self.q.enqueue({"pid": pid, "cwd": "/w", "cmd": "./gradlew check", "remote": True})["id"]

    def tick(self):
        mem = {"pressure": 4, "free_pct": 50, "total_gb": 0, "app_gb": 0, "wired_gb": 0, "compressed_gb": 0,
               "cached_gb": 0, "free_gb": 0, "swap_used_gb": 0, "swap_total_gb": 0}
        with mock.patch.object(server.system, "snapshot_procs", return_value=self.procs), \
                mock.patch.object(server.system, "memory", return_value=mem), \
                mock.patch.object(server.system, "tcp_links", return_value=({}, [])), \
                mock.patch.object(server.system, "cpu_total", return_value=0.0):
            self.q.tick()
        return self.q.view

    def test_no_remote_key_means_no_vm_block(self):
        settings.CONFIG.unlink()
        self.assertIsNone(self.tick()["vm"])

    def test_a_remote_ticket_stays_listed_and_counted_until_the_release(self):
        tid = self.enqueue(1)
        v = self.tick()
        self.assertEqual(v["vm"], {"used": 1, "slots": 1, "state": "starting", "reason": ""})
        self.q.remote_phase(tid, "running", None)
        v = self.tick()
        self.assertEqual((v["vm"]["used"], v["vm"]["state"]), (1, "on"))
        self.assertEqual([(t["id"], t["dest"], t["phase"]) for t in v["tickets"]], [(tid, "vm", "running")])
        self.q.release(tid, 0)
        self.assertEqual(self.tick()["vm"]["used"], 0)

    def test_a_failed_start_shows_the_reason(self):
        tid = self.enqueue(1)
        self.tick()
        self.q.remote_phase(tid, "failed", "start: boom")
        v = self.tick()
        self.assertEqual((v["vm"]["state"], v["vm"]["reason"]), ("unavailable", "start: boom"))

    def test_the_last_poll_decides_an_idle_machine(self):
        self.q.vm_polled = (time.time(), "TERMINATED", False)
        self.assertEqual(self.tick()["vm"]["state"], "on")  # no instance configured: nothing to poll, always on
        self.configure({**CONFIG, "slots": 1, "instance": "vm", "zone": "z", "project": "p"})
        self.assertEqual(self.tick()["vm"]["state"], "off")
        self.q.vm_polled = (time.time(), "RUNNING", True)
        self.assertEqual(self.tick()["vm"]["state"], "no_budget")

    def test_the_polled_status_drives_an_idle_machine_and_a_vm_release_asks_for_a_fresh_poll(self):
        self.configure({**CONFIG, "slots": 1, "instance": "vm", "zone": "z", "project": "p"})
        with mock.patch.object(remote, "find_gcloud", return_value="gcloud"):
            self.q.poll_vm(None)
            self.assertEqual(self.tick()["vm"]["state"], "on")
            self.shell.status = "TERMINATED"
            self.q.poll_vm(None)
            self.assertEqual(self.tick()["vm"]["state"], "off")
        tid = self.enqueue(1)
        self.tick()
        self.assertFalse(self.q.vm_wake.is_set())
        self.q.release(tid, 0)
        self.assertTrue(self.q.vm_wake.is_set())
        self.q.vm_wake.clear()
        mac = self.enqueue(2)
        self.q.db.execute("update tickets set state='granted', dest='mac' where id=?", (mac,))
        self.q.release(mac, 0)
        self.assertFalse(self.q.vm_wake.is_set())  # a Mac release says nothing of the machine

    def test_a_failing_poll_is_unknown_logged_once_and_the_tick_goes_on(self):
        self.configure({**CONFIG, "slots": 1, "instance": "vm", "zone": "z", "project": "p"})
        self.use_shell(fail={"describe": (1, "boom")})
        with mock.patch.object(remote, "find_gcloud", return_value="gcloud"), \
                mock.patch.object(server, "log") as logged:
            said = self.q.poll_vm(None)
            self.q.poll_vm(said)
        self.assertEqual(logged.call_count, 1)
        self.assertEqual(self.tick()["vm"]["state"], "unknown")
        self.use_shell()
        with mock.patch.object(remote, "find_gcloud", return_value="gcloud"):
            self.assertIsNone(self.q.poll_vm(said))  # a good answer clears it: the next failure is logged again


class InstanceState(RemoteBase):
    @POSIX_ONLY
    def test_status_and_cap_come_from_one_describe(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p", "cap_metadata_key": "budget-exceeded"})
        cfg = remote.config(self.lines.append)
        self.shell.budget = "true"
        self.assertEqual(remote.instance_state(cfg), ("RUNNING", True))
        self.assertEqual(self.shell.names(), ["describe"])

    @POSIX_ONLY
    def test_find_gcloud_searches_the_homebrew_dirs_and_never_the_cwd(self):
        seen = []
        with mock.patch.dict(os.environ, {"PATH": f"/bin{os.pathsep}{os.pathsep}/usr/bin{os.pathsep}"}), \
                mock.patch.object(remote.shutil, "which", side_effect=lambda n, path: seen.append(path) or "/x/gcloud"):
            self.assertEqual(remote.find_gcloud(), "/x/gcloud")
        self.assertEqual(seen[0].split(os.pathsep), ["/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin"])
        with mock.patch.dict(os.environ, {"PATH": ""}), mock.patch.object(remote.shutil, "which", return_value=None):
            self.assertEqual(remote.find_gcloud(), "gcloud")  # nothing found: the bare name, as the client uses

    @POSIX_ONLY
    def test_a_failed_describe_is_unavailable(self):
        self.configure({**CONFIG, "instance": "vm", "zone": "z", "project": "p"})
        self.use_shell(fail={"describe": (1, "boom")})
        with self.assertRaises(remote.Unavailable):
            remote.instance_state(remote.config(self.lines.append))


if __name__ == "__main__":
    unittest.main()
