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
from bq import gq, remote, settings  # noqa: E402

HOST = "me@build-vm"
POSIX_ONLY = unittest.skipIf(remote.fcntl is None, "the remote path does not exist on Windows")
CONFIG = {"host": HOST, "root": "~/remote-builds", "ssh_options": ["-i", "~/.ssh/key"]}


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

    @staticmethod
    def step(argv):
        if argv[0] == "ssh":
            return "probe" if argv[-1] == "true" else "mkdir"
        if argv[0] == "rsync":
            return "rsync-out" if "--delete" in argv else "rsync-back"
        return {"git": "git", "gcloud": "start"}.get(argv[0], argv[0])

    def run(self, argv, cwd=None):
        name = self.step(argv)
        self.calls.append((name, argv))
        if name in self.fail:
            code, err = self.fail[name]
            return code, "", err
        if name == "git":
            return 0, f"{self.top}\n{self.common}\n", ""
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
        """plan, start and execute in a row, the path gq takes once a ticket has waited long enough."""
        j = remote.plan(cmd, self.lines.append)
        if j is None or not remote.start(j, self.lines.append):
            return None
        return remote.execute(j, self.lines.append, gq.SIGNALS)


class Config(RemoteBase):
    @POSIX_ONLY
    def test_overflow_limit_defaults_to_60_and_rejects_negative_or_boolean(self):
        self.configure(CONFIG)
        self.assertEqual(remote.config(self.lines.append)["overflow_after_s"], 60)
        for bad in (-1, True, "60"):
            self.configure({**CONFIG, "overflow_after_s": bad})
            self.assertIsNone(remote.config(self.lines.append))
        self.assertEqual(self.lines[-1], "remote config ignored (overflow_after_s)")

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
        self.assertEqual(self.shell.names(), ["git", "probe", "mkdir", "rsync-out", "rsync-back"])
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
                                               f"cd {dest} && exec gq run -- ./gradlew :shared:testAndroidHostTest"]])
        self.assertEqual(self.lines, [f"remote {HOST}: ./gradlew :shared:testAndroidHostTest"])

    def test_probe_retries_until_the_vm_answers(self):
        self.configure(CONFIG)
        answers = iter([255, 255, 0])
        real_run = self.shell.run
        self.shell.run = lambda argv, cwd=None: ((next(answers), "", "") if argv[-1] == "true"
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

        def run(argv, cwd=None):
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
        self.assertEqual(remote_cmd, "cd remote-builds/evolvai/wt && exec gq run -- "
                                     "perl -e 'alarm 3000; exec @ARGV' ./gradlew check -x :shared:iosSimulatorArm64Test")
        self.assertEqual(shlex.split(remote_cmd)[7:], cmd)

    def test_project_dir_inside_runs_from_the_same_subdirectory(self):
        self.configure({**CONFIG, "root": "/srv/builds"})
        os.chdir(self.top / "backend")
        self.assertEqual(self.run_remote(["./gradlew", "-p", ".", "check"]), 0)
        self.assertEqual(self.shell.popened[0][-1], "cd /srv/builds/evolvai/wt/backend && exec gq run -- "
                                                    "./gradlew -p . check")
        os.chdir(self.top)
        self.assertEqual(self.run_remote(["./gradlew", "-p", "backend", "check"]), 0)
        self.assertIn("./gradlew -p backend check", self.shell.popened[1][-1])

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
        self.assertEqual(self.lines, [f"remote unavailable ({reason}); running locally"])
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

    def test_rsync_failure_falls_back(self):
        self.fallback("rsync: exit 12", fail={"rsync-out": (12, "")})

    def test_ssh_that_cannot_start_falls_back(self):
        self.configure(CONFIG)
        self.use_shell(fail={"popen": "No such file or directory: 'ssh'"})
        self.assertIsNone(self.run_remote(["./gradlew", "check"]))
        self.assertEqual(self.lines[-1], "remote unavailable (ssh: No such file or directory: 'ssh'); running locally")


class Queue:
    """The local queue server as gq sees it: answers to enqueue, wait and ticket, and the releases it got."""

    def __init__(self, waits, ticket="waiting"):
        self.waits, self.ticket, self.released, self.paths = list(waits), ticket, [], []

    def call_retrying(self, path, body=None):
        self.paths.append(path)
        if path == "/api/enqueue":
            return {"id": 5}
        if path.startswith("/api/ticket"):
            return {"state": self.ticket}
        state = self.waits.pop(0)
        return {"state": state, "position": 1, "waiting": 1}

    def call(self, path, body=None, timeout=None):
        self.released.append(body)
        return {"ok": True}


@POSIX_ONLY
class GqRun(RemoteBase):
    def gq_run(self, queue, overflow_after_s=0, local_code=0):
        self.configure({**CONFIG, "overflow_after_s": overflow_after_s})
        local = mock.Mock()
        local.wait.return_value = local_code
        with mock.patch.object(gq, "call_retrying", queue.call_retrying), mock.patch.object(gq, "call", queue.call), \
                mock.patch.object(gq.subprocess, "Popen", return_value=local) as popen, \
                mock.patch.object(gq, "say", self.lines.append), mock.patch.dict(os.environ, {"BQ_TICKET": ""}):
            with self.assertRaises(SystemExit) as exited:
                gq.run(["./gradlew", "check"])
        return exited.exception.code, popen

    def test_a_free_local_slot_never_touches_the_build_machine(self):
        code, popen = self.gq_run(Queue(["granted"]))
        self.assertEqual(code, 0)
        popen.assert_called_once()
        self.assertEqual((self.shell.calls[1:], self.shell.popened), ([], []))  # only the eligibility git call

    def test_a_long_wait_overflows_releases_the_ticket_and_exits_with_the_remote_code(self):
        self.shell.child = Child(7)
        queue = Queue(["waiting"])
        code, popen = self.gq_run(queue)
        self.assertEqual(code, 7)
        popen.assert_not_called()
        self.assertEqual(queue.released, [{"id": 5, "exit_code": None}])
        self.assertEqual(len(self.shell.popened), 1)
        self.assertIn("ticket #5: waited 0s; trying the build machine", self.lines)

    def test_a_wait_shorter_than_the_limit_stays_local(self):
        code, popen = self.gq_run(Queue(["waiting", "granted"]), overflow_after_s=3600)
        self.assertEqual(code, 0)
        popen.assert_called_once()
        self.assertNotIn("rsync-out", self.shell.names())

    def test_a_slot_granted_while_the_machine_starts_runs_here(self):
        code, popen = self.gq_run(Queue(["waiting"], ticket="granted"), local_code=3)
        self.assertEqual(code, 3)
        popen.assert_called_once()
        self.assertIn("rsync-out", self.shell.names())
        self.assertEqual(self.shell.popened, [])

    def test_an_unavailable_machine_keeps_the_ticket_waiting_and_is_tried_once(self):
        self.use_shell(fail={"rsync-out": (12, "")})
        queue = Queue(["waiting", "waiting", "granted"])
        code, popen = self.gq_run(queue)
        self.assertEqual(code, 0)
        popen.assert_called_once()
        self.assertEqual(self.shell.names().count("rsync-out"), 1)
        self.assertEqual(queue.released, [{"id": 5, "exit_code": 0}])

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


if __name__ == "__main__":
    unittest.main()
