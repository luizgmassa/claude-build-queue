import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# settings load at import: read them from an empty data dir, never this machine's config.json
os.environ["BQ_DATA"] = os.environ.get("BQ_TEST_DATA") or tempfile.mkdtemp(prefix="bq-test-")
os.environ["BQ_TEST_DATA"] = os.environ["BQ_DATA"]
from bq import core, settings  # noqa: E402
from bq.hook import decide  # noqa: E402

CLIENT = "java -Xmx64m -Dorg.gradle.appname=gradlew -jar /w/gradle/wrapper/gradle-wrapper.jar check"
DAEMON = "/jdk/bin/java -cp x org.gradle.launcher.daemon.bootstrap.GradleDaemon 9.8.1"
KOTLIN = "/jdk/bin/java -cp y org.jetbrains.kotlin.daemon.KotlinCompileDaemon"


def P(pid, ppid, args, **env):
    return core.Proc(pid=pid, ppid=ppid, rss_kb=1024, cpu=0, etime="1:00", lstart="L", args=args,
                     env={k: str(v) for k, v in env.items()})


def world(*procs):
    base = [P(1, 0, "/sbin/launchd"), P(100, 1, "/Users/u/.local/bin/claude --settings /s/sessions/7.settings.json"),
            P(101, 100, "/bin/zsh -c source /u/.claude/shell-snapshots/snapshot-zsh-1.sh")]
    return {p.pid: p for p in base + list(procs)}


def tick(procs, tickets=(), *, pressure=1, busy=(), idle_since=None, now=1000.0, last_grant=None, **kw):
    roots = core.classify(procs)
    return core.plan(procs, roots, list(tickets), snapshot_ts=now - 1, now=now, pressure=pressure,
                     busy_daemons=set(busy), idle_since=idle_since or {}, last_grant_ts=last_grant,
                     live_claude_pids={p.pid for p in procs.values() if p.kind == "claude"}, **kw)


class Scheduling(unittest.TestCase):
    def waiting(self, *ids):
        return [core.Ticket(id=i, pid=200 + i, state="waiting", created=0) for i in ids]

    def gqs(self, *ids):
        return [P(200 + i, 101, f"python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100) for i in ids]

    def test_fifo_grants_head_first_up_to_two_slots(self):
        pl = tick(world(*self.gqs(3, 1, 2)), self.waiting(3, 1, 2))
        self.assertEqual(pl.grants, [1, 2])
        self.assertIn("base slots full", core.render(pl.blocked))

    def test_runner_takes_a_slot(self):
        runner = P(300, 1, "/Users/u/actions-runner/myrepo/bin/Runner.Worker spawnclient")
        pl = tick(world(runner, *self.gqs(1, 2)), self.waiting(1, 2))
        self.assertEqual(pl.grants, [1])

    def test_warn_pressure_does_not_block(self):
        granted = [core.Ticket(id=1, pid=201, state="granted", created=0)]
        pl = tick(world(*self.gqs(1, 2)), granted + self.waiting(2), pressure=2)
        self.assertEqual(pl.grants, [2])

    def test_critical_pressure_admits_nobody(self):
        self.assertEqual(tick(world(*self.gqs(1)), self.waiting(1), pressure=4).grants, [])

    def test_dead_owner_ticket_is_reaped_and_frees_the_slot(self):
        tickets = [core.Ticket(id=1, pid=999, state="granted", created=0)] + self.waiting(2)
        pl = tick(world(*self.gqs(2)), tickets)
        self.assertEqual(pl.reaps, [(1, "owner process gone")])
        self.assertEqual(pl.grants, [2])

    def test_ticket_enqueued_after_snapshot_is_not_reaped(self):
        t = core.Ticket(id=1, pid=999, state="waiting", created=5000)
        self.assertEqual(tick(world(), [t]).reaps, [])

    def test_reused_pid_is_detected_by_start_time(self):
        t = core.Ticket(id=1, pid=201, state="granted", created=0, pid_start="OTHER")
        self.assertEqual(tick(world(*self.gqs(1)), [t]).reaps, [(1, "owner process gone")])


VM = {"slots": 2, "available": True}


class NeutralScheduling(unittest.TestCase):
    """The Mac stays the default; a full Mac sends eligible waiting tickets to the build machine, within its slots."""

    def ticket(self, i, state="waiting", remote=True, dest=None, checkout=None):
        return core.Ticket(id=i, pid=200 + i, state=state, created=0, remote=remote, dest=dest, checkout=checkout)

    def run_plan(self, tickets, remote=VM, **kw):
        procs = world(*[P(200 + t.id, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100)
                        for t in tickets])
        return tick(procs, tickets, remote=remote, **kw)

    def full_mac(self):
        return [self.ticket(1, "granted", dest="mac"), self.ticket(2, "granted", dest="mac")]

    def test_a_free_mac_slot_wins_and_nothing_goes_to_the_machine(self):
        pl = self.run_plan([self.ticket(1)])
        self.assertEqual((pl.grants, pl.remote_grants), ([1], []))

    def test_a_full_mac_sends_an_eligible_ticket_to_the_machine(self):
        pl = self.run_plan(self.full_mac() + [self.ticket(3)])
        self.assertEqual((pl.grants, pl.remote_grants), ([], [3]))

    def test_a_ticket_without_the_remote_flag_never_goes(self):
        pl = self.run_plan(self.full_mac() + [self.ticket(3, remote=False)])
        self.assertEqual(pl.remote_grants, [])
        self.assertEqual(self.run_plan(self.full_mac() + [self.ticket(3)], remote=None).remote_grants, [])

    def test_the_head_that_only_runs_on_the_mac_keeps_its_place_while_a_later_one_goes(self):
        tickets = self.full_mac() + [self.ticket(3, remote=False), self.ticket(4), self.ticket(5)]
        pl = self.run_plan(tickets)
        self.assertEqual(pl.remote_grants, [4, 5])
        # the Mac frees a slot: the head, not a later ticket, takes it
        later = [self.ticket(1, "granted", dest="mac"), self.ticket(3, remote=False), self.ticket(5)]
        self.assertEqual(self.run_plan(later).grants, [3])

    def test_the_machine_never_exceeds_its_slots(self):
        tickets = self.full_mac() + [self.ticket(3), self.ticket(4), self.ticket(5)]
        pl = self.run_plan(tickets)
        self.assertEqual(pl.remote_grants, [3, 4])
        running = self.full_mac() + [self.ticket(3, "granted", dest="vm"), self.ticket(4, "granted", dest="vm"),
                                     self.ticket(5)]
        self.assertEqual(self.run_plan(running).remote_grants, [])
        done = running[:3] + [self.ticket(5)]  # one remote build ended
        self.assertEqual(self.run_plan(done).remote_grants, [5])

    def test_a_checkout_has_one_build_on_the_machine_at_a_time(self):
        wt, other = "builds/repo/wt", "builds/repo/other"
        tickets = self.full_mac() + [self.ticket(3, checkout=wt), self.ticket(4, checkout=wt),
                                     self.ticket(5, checkout=other)]
        self.assertEqual(self.run_plan(tickets).remote_grants, [3, 5])  # same tick: #4 would copy over #3
        running = self.full_mac() + [self.ticket(3, "granted", dest="vm", checkout=wt), self.ticket(4, checkout=wt)]
        self.assertEqual(self.run_plan(running).remote_grants, [])
        self.assertEqual(self.run_plan(self.full_mac() + running[3:]).remote_grants, [4])  # #3 ended
        # the build of that checkout on the Mac does not hold the machine
        local = self.full_mac()[:1] + [self.ticket(2, "granted", dest="mac", checkout=wt), self.ticket(4, checkout=wt)]
        self.assertEqual(self.run_plan(local).remote_grants, [4])

    def test_a_remote_ticket_holds_no_mac_slot_from_the_grant_on(self):
        pl = self.run_plan([self.ticket(1, "granted", dest="vm"), self.ticket(2)])
        self.assertEqual((pl.used, pl.grants), (1, [2]))  # the new ticket only
        self.assertEqual(pl.counts["vm"], 1)

    def test_the_gap_between_extras_alone_sends_nobody_to_the_machine(self):
        pl = self.run_plan(self.full_mac() + [self.ticket(3)], last_extra_ts=999.0, avail_gb=64.0)
        self.assertEqual((pl.grants, pl.remote_grants), ([], []))
        self.assertIn("next extra slot", core.render(pl.blocked))

    def test_a_gap_refusal_over_a_memory_refusal_is_a_full_mac(self):
        pl = self.run_plan(self.full_mac() + [self.ticket(3)], last_extra_ts=999.0, avail_gb=0.0)
        self.assertEqual((pl.grants, pl.remote_grants), ([], [3]))

    def test_a_daemon_pause_sends_nobody_to_the_machine(self):
        procs = world(*[P(200 + i, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100) for i in (3,)],
                      P(700, 1, DAEMON, CLAUDE_PID=100))
        pl = tick(procs, [self.ticket(3)], idle_since={700: 0}, remote=VM)
        self.assertEqual((pl.grants, pl.remote_grants), ([], []))
        self.assertIn("idle daemon", core.render(pl.blocked))

    def test_critical_pressure_counts_as_a_full_mac(self):
        pl = self.run_plan([self.ticket(1)], pressure=4)
        self.assertEqual((pl.grants, pl.remote_grants), ([], [1]))

    def test_an_unavailable_machine_is_out_of_the_choice(self):
        pl = self.run_plan(self.full_mac() + [self.ticket(3)], remote={"slots": 2, "available": False})
        self.assertEqual(pl.remote_grants, [])

    def test_a_dead_remote_gq_frees_the_machine_slot_in_the_same_tick(self):
        dead = self.ticket(3, "granted", dest="vm")
        procs = world(*[P(200 + t, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100) for t in (1, 2, 4, 5)])
        tickets = self.full_mac() + [dead, self.ticket(4), self.ticket(5)]
        pl = tick(procs, tickets, remote={"slots": 1, "available": True})
        self.assertEqual(pl.reaps, [(3, "owner process gone")])
        self.assertEqual(pl.remote_grants, [4])


class RemoteOrphan(unittest.TestCase):
    """A gq killed outright (SIGKILL, out of memory) leaves the ssh of its remote build behind; the reap ends it."""

    SSH = "/usr/bin/ssh -tt -o ServerAliveInterval=15 me@vm cd b/repo/wt && exec env BQ_TICKET={} ./gradlew check"

    def dead(self, dest):
        return core.Ticket(id=3, pid=999, state="granted", created=0, remote=True, dest=dest)

    def procs(self):
        return world(P(900, 1, self.SSH.format(3)), P(901, 1, self.SSH.format(30)),
                     P(902, 101, "/bin/zsh -c echo ssh exec env BQ_TICKET=3 ./gradlew check"))

    def test_reaping_a_remote_ticket_ends_its_ssh_and_no_other(self):
        pl = tick(self.procs(), [self.dead("vm")], remote=VM)
        self.assertEqual(pl.reaps, [(3, "owner process gone")])
        self.assertEqual(pl.kills, [(900, core.msg("kill.remote_orphan", id=3))])
        self.assertEqual(core.render(pl.kills[0][1]), "remote build of ticket #3, whose gq is gone")

    def test_reaping_a_mac_ticket_kills_nothing(self):
        self.assertEqual(tick(self.procs(), [self.dead("mac")], remote=VM).kills, [])

    def test_a_live_remote_ticket_keeps_its_ssh(self):
        live = core.Ticket(id=3, pid=203, state="granted", created=0, remote=True, dest="vm")
        procs = self.procs()
        procs[203] = P(203, 101, "python3 /u/.local/bin/gq run -- ./gradlew check", CLAUDE_PID=100)
        self.assertEqual(tick(procs, [live], remote=VM).kills, [])


class RunnerSlot(unittest.TestCase):
    RUNNER = "/Users/u/actions-runner/myrepo/bin/Runner.Worker spawnclient"

    def job_gq(self, dest):
        runner = P(300, 1, self.RUNNER)
        step = P(301, 300, "/bin/bash -e step.sh")
        gq = P(302, 301, "python3 /u/.local/bin/gq run -- ./gradlew x", GITHUB_ACTIONS="true")
        t = core.Ticket(id=1, pid=302, state="granted", created=0, remote=True, dest=dest)
        return tick(world(runner, step, gq), [t])

    def test_a_runner_job_with_a_ticket_counts_only_the_ticket(self):
        self.assertEqual(self.job_gq("mac").used, 1)
        pl = self.job_gq("vm")
        self.assertEqual(pl.used, 0)
        self.assertTrue(pl.runner_busy)  # still shown as busy

    def test_a_runner_job_whose_ticket_waits_does_not_take_a_base_slot_from_it(self):
        runner, step = P(300, 1, self.RUNNER), P(301, 300, "/bin/bash -e step.sh")
        job = P(302, 301, "python3 /u/.local/bin/gq run -- ./gradlew x", GITHUB_ACTIONS="true")
        agent = P(201, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100)
        tickets = [core.Ticket(id=1, pid=302, state="waiting", created=0),
                   core.Ticket(id=2, pid=201, state="granted", created=0, dest="mac")]
        self.assertEqual(tick(world(runner, step, job, agent), tickets).grants, [1])

    def test_a_runner_job_without_a_ticket_counts_one(self):
        runner = P(300, 1, self.RUNNER)
        self.assertEqual(tick(world(runner)).used, 1)
        other = P(302, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100)  # an agent's, not the job's
        remote_ticket = core.Ticket(id=1, pid=302, state="granted", created=0, remote=True, dest="vm")
        self.assertEqual(tick(world(runner, other), [remote_ticket]).used, 1)


class Cleanup(unittest.TestCase):
    def test_hook_process_is_not_a_build_but_bash_tool_and_orphan_runs_are(self):
        hook = P(400, 100, "uv run --quiet --script /u/.claude/plugins/x/hooks/langfuse_hook.py", CLAUDE_PID=100)
        tool = P(401, 101, "uv run scripts/t.py", CLAUDE_PID=100)
        orphan = P(402, 1, "uv run scripts/o.py", CLAUDE_PID=100)
        pl = tick(world(hook, tool, orphan))
        self.assertEqual(pl.counts["unqueued"], 2)  # the hook is not a build; the two runs are, and take slots
        self.assertEqual(pl.kills, [])
        self.assertEqual(pl.used, 2)

    def test_unqueued_agent_build_is_not_killed_but_counts_as_a_slot(self):
        procs = world(P(400, 101, CLIENT, CLAUDE_PID=100))
        pl = tick(procs)
        self.assertEqual(pl.kills, [])  # the hook is the only enforcement
        self.assertEqual(procs[400].status, core.msg("status.no_slot"))
        self.assertEqual(pl.used, 1)
        self.assertEqual(pl.outside, [400])

    def test_queued_build_is_left_alone(self):
        gq = P(201, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100)
        client = P(400, 201, CLIENT, CLAUDE_PID=100, BQ_TICKET=1)
        pl = tick(world(gq, client), [core.Ticket(id=1, pid=201, state="granted", created=0)])
        self.assertEqual(pl.kills, [])
        self.assertEqual(pl.outside, [])  # it holds a slot

    def test_runner_and_nonagent_and_protected_paths_are_never_killed(self):
        from unittest import mock
        runner = P(500, 1, CLIENT + " -Dgradle.user.home=/u/.gradle-ci", GITHUB_ACTIONS="true")
        marked = P(503, 1, CLIENT + " -Dgradle.user.home=/u/.gradle-ci", CLAUDE_PID=4242)
        human = P(501, 1, CLIENT)
        staging = P(502, 1, "/usr/bin/java -classpath /u/.staging/releases/x/lib/runtime.jar M", CLAUDE_PID=4242)
        # positive control: with the default (empty) lists, a dead agent's processes are orphans and die
        self.assertEqual({k[0] for k in tick(world(runner, marked, human, staging)).kills}, {502, 503})
        with mock.patch.object(core, "PROTECTED_PATHS", ("/.staging/",)), \
                mock.patch.object(core, "RUNNER_MARKERS", ("/actions-runner/", ".gradle-ci")):
            procs = world(runner, marked, human, staging)
            pl = tick(procs)
        self.assertEqual(pl.kills, [])
        self.assertEqual(pl.counts["nonagent"], 1)
        self.assertEqual(pl.outside, [501])  # the runner has its own card, so it is not an outside build
        self.assertEqual((procs[502].owner, procs[503].owner), ("protected", "runner"))

    def test_orphan_of_dead_agent_is_killed(self):
        pl = tick(world(P(600, 1, CLIENT, CLAUDE_PID=4242)))
        self.assertIn((600, core.msg("kill.orphan", pid=4242)), pl.kills)
        self.assertEqual(len([k for k in pl.kills if k[0] == 600]), 1)

    def test_idle_agent_daemon_is_killed_after_grace_busy_one_is_not(self):
        procs = world(P(700, 1, DAEMON, CLAUDE_PID=100), P(701, 1, DAEMON, CLAUDE_PID=100), P(702, 1, DAEMON))
        pl = tick(procs, busy={701}, idle_since={700: 900, 702: 900})
        self.assertEqual(pl.kills, [(700, core.msg("kill.idle_daemon"))])
        self.assertEqual(tick(world(P(700, 1, DAEMON, CLAUDE_PID=100)), idle_since={700: 995}).kills, [])

    def test_no_daemon_kill_right_after_a_grant(self):
        procs = world(P(700, 1, DAEMON, CLAUDE_PID=100))
        self.assertEqual(tick(procs, idle_since={700: 0}, last_grant=990).kills, [])

    def test_kotlin_daemon_dies_after_60s_idle_even_with_builds_running(self):
        gq = P(201, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100)
        procs = world(P(800, 1, KOTLIN, CLAUDE_PID=100), gq)
        busy_queue = [core.Ticket(id=1, pid=201, state="granted", created=0)]
        self.assertEqual(tick(procs, busy_queue, idle_since={800: 950}).kills, [])
        self.assertEqual(tick(procs, busy_queue, idle_since={800: 900}).kills, [(800, core.msg("kill.idle_daemon"))])
        self.assertEqual(tick(procs, busy_queue, busy={800}, idle_since={800: 0}).kills, [])

    def test_jvm_forked_by_a_daemon_with_its_flags_is_not_an_idle_daemon(self):
        # Real prefix of KGP's cinterop JVM (2026-11-19, pid 78257); the ps log cut the line, the marker is inherited.
        child = ("/Users/u/.gradle/jdks/azul_systems__inc_-21-aarch64-os_x.2/zulu21.50.19-ca-jdk21.0.11-macosx_aarch64"
                 "/Contents/Home/bin/java -Dapple.awt.application.name=GradleDaemon -Dfile.separator=/"
                 " -Didea.io.use.nio2=true -Djava.awt.headless=true -Djava.class.path=/u/kotlin-native/konan/lib/x.jar"
                 " -Dsun.java.command=org.gradle.launcher.daemon.bootstrap.GradleDaemon 9.0.0"
                 " org.jetbrains.kotlin.cli.utilities.MainKt cinterop -o /w/shared/build/FirebaseCore.klib")
        procs = world(P(700, 1, DAEMON, CLAUDE_PID=100), P(710, 700, child, CLAUDE_PID=100),
                      P(720, 1, DAEMON, CLAUDE_PID=100))
        self.assertEqual(core.kind_of(child), "gradle_daemon")
        pl = tick(procs, busy={700}, idle_since={710: 0, 720: 0})
        self.assertEqual(procs[710].kind, "java")
        self.assertEqual(pl.kills, [(720, core.msg("kill.idle_daemon"))])

    def test_test_worker_under_daemon_is_not_a_root_but_orphaned_worker_dies(self):
        procs = world(P(700, 1, DAEMON, CLAUDE_PID=100),
                      P(710, 700, "/jdk/bin/java -Dorg.gradle.internal.worker.tmpdir=/w Gradle Test Executor 1",
                        CLAUDE_PID=100),
                      P(711, 700, "/jdk/bin/java -jar something.jar", CLAUDE_PID=100),
                      P(712, 1, "/jdk/bin/java worker.org.gradle.process.internal.worker.GradleWorkerMain",
                        CLAUDE_PID=100))
        pl = tick(procs, busy={700})
        self.assertEqual(pl.kills, [(712, core.msg("kill.loose_worker"))])


class CrossPlatform(unittest.TestCase):
    def test_exe_name_drops_directory_case_and_windows_extension(self):
        for path, name in [("C:/jdk/bin/java.exe", "java"), ("./gradlew.bat", "gradlew"), ("C:\\u\\uv.EXE", "uv"),
                           ("/usr/bin/java", "java"), ("gradlew", "gradlew"), ("x.cmd", "x")]:
            self.assertEqual(core.exe_name(path), name, path)

    def test_windows_command_lines_get_the_same_kinds(self):
        for args, kind in [("C:/Users/u/.local/bin/claude.exe --resume", "claude"),
                           ("C:/py/python.exe C:/r/bq/gq.py run -- ./gradlew.bat x", "gq"),
                           ("C:/uv/uv.exe run x.py", "uv_run"), ("C:/jdk/bin/java.exe -jar x.jar", "java"),
                           ("C:/jdk/bin/java.exe -cp x org.gradle.wrapper.GradleWrapperMain check", "gradle_client"),
                           ("C:/pg/bin/postgres.exe -D C:/t/embedded-pg/x", "embedded_pg")]:
            self.assertEqual(core.kind_of(args), kind, args)

    def test_windows_gradle_wrapper_gets_its_command_type(self):
        self.assertEqual(core.command_type("./gradlew.bat :shared:check"), "gradle-check")
        self.assertEqual(core.command_type("gradlew.bat ktlintFormat"), "gradle-lint")

    def test_hook_catches_windows_spellings_and_cmd_wrapper(self):
        for cmd in ["./gradlew.bat check", "gradlew.bat build", "java.exe -jar x.jar", "cmd //c gradlew.bat build",
                    "cmd /c \"gradlew.bat test\""]:
            self.assertTrue(core.analyze_command(cmd)[0], cmd)
        self.assertEqual(core.analyze_command("gq run -- ./gradlew.bat x"), ([], True))
        self.assertEqual(core.analyze_command("cmd //c gq run -- gradlew.bat x"), ([], True))

    def test_worker_whose_parent_is_not_gradle_is_orphaned_wherever_it_was_adopted(self):
        worker = "/jdk/bin/java worker.org.gradle.process.internal.worker.GradleWorkerMain"
        procs = world(P(50, 1, "/usr/lib/systemd/systemd --user"),  # Linux subreaper
                      P(60, 101, CLIENT, CLAUDE_PID=100),
                      P(711, 50, worker, CLAUDE_PID=100), P(712, 999, worker, CLAUDE_PID=100),
                      P(713, 60, worker, CLAUDE_PID=100))
        kills = [k for k in tick(procs).kills if k[1] == core.msg("kill.loose_worker")]
        self.assertEqual(sorted(k[0] for k in kills), [711, 712])

class Hook(unittest.TestCase):
    def offending(self, cmd):
        return core.analyze_command(cmd)[0]

    def test_bare_gradle_and_friends_are_denied(self):
        for cmd in ["./gradlew check", "cd ../wt && ./gradlew -p backend test",
                    "perl -e 'alarm 3000; exec @ARGV' ./gradlew check --continue",
                    "JAVA_OPTS=x ./gradlew a 2>&1 | tail -5", "bash -c './gradlew a'",
                    "(cd iosApp && LANG=en_US.UTF-8 pod install)", "uv run scripts/test_x.py",
                    "xcodebuild test -workspace a", "java -jar x.jar", "env FOO=1 nohup ./gradlew a",
                    "x=$(./gradlew -q properties)", "rtk proxy ./gradlew check",
                    "gq run -- ./gradlew a && ./gradlew b"]:
            self.assertTrue(self.offending(cmd), cmd)

    def test_queued_and_harmless_commands_pass(self):
        for cmd in ["gq run -- ./gradlew check", "cd ../wt && gq run -- perl -e 'alarm 1; exec @ARGV' ./gradlew x",
                    "gq run -- ./gradlew x 2>&1 | tail -40", "grep gradlew AGENTS.md", "git commit -m './gradlew x'",
                    "echo java", "java -version", "./gradlew --version", "ls build/",
                    "cat > f.sh <<'EOF'\n./gradlew check\nEOF\nchmod +x f.sh"]:
            self.assertEqual(self.offending(cmd), [], cmd)

    def test_gq_must_run_in_background(self):
        self.assertIn("run_in_background", decide({"command": "gq run -- ./gradlew x"}))
        self.assertIsNone(decide({"command": "gq run -- ./gradlew x", "run_in_background": True}))
        self.assertIn("gq run --", decide({"command": "./gradlew x", "run_in_background": True}))
        self.assertIsNone(decide({"command": "gq status"}))
        self.assertIsNone(decide({"command": "gq enforce on"}))


class ExtraSlots(unittest.TestCase):
    def gqs(self, *ids):
        return [P(200 + i, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100) for i in ids]

    def run_tick(self, granted, waiting, *, avail=20.0, est=8.0, last_extra=None, pressure=1, extra=(),
                 rss=None, ceiling=None, swap=0.0, peaks=None, remaining=None):
        procs = world(*self.gqs(*granted, *waiting), *extra)
        tickets = [core.Ticket(id=i, pid=200 + i, state="granted", created=0) for i in granted] + \
                  [core.Ticket(id=i, pid=200 + i, state="waiting", created=0) for i in waiting]
        now = 1000.0
        rss = {f"t{i}": est for i in granted} if rss is None else rss  # granted runs at their peak by default
        return core.plan(procs, core.classify(procs), tickets, snapshot_ts=now - 1, now=now, pressure=pressure,
                         busy_daemons=set(), idle_since={}, last_grant_ts=None, live_claude_pids={100},
                         last_extra_ts=last_extra, avail_gb=avail,
                         estimates={i: ("gradle-check", est) for i in [*granted, *waiting]},
                         ceiling=ceiling, swap_gb=swap, rss_gb=rss, peaks=peaks or {}, remaining=remaining)

    def test_extra_entry_when_memory_covers_estimate_plus_headroom(self):
        pl = self.run_tick([1, 2], [3, 4], avail=20, est=8)
        self.assertEqual(pl.grants, [3])
        self.assertIn("ticket #3 entered above the base: 20.0 GB available − 0.0 GB reserved − 8.0 GB",
                      core.render(pl.extra))
        self.assertIn("next extra slot", core.render(pl.blocked))  # gradual: the next extra waits EXTRA_GAP_S
        self.assertEqual(pl.reserve_gb, 8.0)  # the new entry's whole estimate is still to come

    def test_no_fixed_ceiling_by_default(self):
        pl = self.run_tick([1, 2, 3, 4, 5, 6], [7], avail=40, est=4)
        self.assertEqual(pl.grants, [7])

    def test_configured_max_caps_extra_entries(self):
        pl = self.run_tick([1, 2, 3, 4], [5], avail=40, ceiling=4)
        self.assertEqual(pl.grants, [])
        self.assertIn("configured max of 4", core.render(pl.blocked))
        self.assertEqual(self.run_tick([1, 2, 3], [5], avail=40, ceiling=4).grants, [5])

    def test_running_builds_below_their_estimate_reserve_the_rest(self):
        rss = {"t1": 2.0, "t2": 8.0}  # t1 started recently: 6 GB of its 8 GB estimate still to come
        pl = self.run_tick([1, 2], [3], avail=19, est=8, rss=rss)
        self.assertEqual(pl.grants, [])
        self.assertIn("19.0 GB available − 6.0 GB reserved for running peaks − 8.0 GB", core.render(pl.blocked))
        self.assertEqual(self.run_tick([1, 2], [3], avail=19, est=8, rss={"t1": 9.0, "t2": 8.0}).grants, [3])

    def test_runner_and_builds_without_a_ticket_reserve_their_type_estimate(self):
        runner = P(300, 1, "/Users/u/actions-runner/myrepo/bin/Runner.Worker spawnclient")
        loose = P(400, 1, "/jdk/bin/java -Xmx64m -Dorg.gradle.appname=gradlew -classpath /w/gradle/wrapper/"
                          "gradle-wrapper.jar org.gradle.wrapper.GradleWrapperMain :shared:testAndroidHostTest")
        rss = {"t1": 8.0, "runner": 1.0, "p400": 2.0}
        pl = self.run_tick([1], [3], avail=40, est=8, extra=[runner, loose], rss=rss)
        self.assertEqual(pl.counts["nonagent"], 1)
        self.assertIn(f"{core.DEFAULT_GB['gradle-check'] - 1 + core.DEFAULT_GB['gradle-test'] - 2:.1f} GB reserved",
                      core.render(pl.extra))
        pl = self.run_tick([1], [3], avail=30, est=8, extra=[runner], rss={"t1": 8.0, "runner": 1.0},
                           peaks={"gradle-check": [20.0]})  # the runner's reserve follows the learned estimate
        self.assertIn("19.0 GB reserved", core.render(pl.blocked))

    def test_past_runs_of_the_same_build_lower_the_reserve_after_their_peak(self):
        curves = [[(0, 1.0), (10, 9.0), (20, 3.0)], [(0, 1.0), (10, 7.0), (30, 2.0)], [(0, 1.0), (15, 5.0)]]
        self.assertAlmostEqual(core.remaining_gb(curves, 5), 7.0)  # every peak still ahead: (9 + 7 + 5) / 3
        self.assertAlmostEqual(core.remaining_gb(curves, 20), 5 / 3)  # (3 + 2 + 0): the third run had ended
        self.assertEqual(core.remaining_gb(curves, 40), 0.0)
        self.assertIsNone(core.remaining_gb([], 5))
        rss = {"t1": 2.0, "t2": 8.0}  # t1 is past its peak: 2 GB now, its past runs reached 2.5 GB from here on
        pl = self.run_tick([1, 2], [3], avail=19, est=8, rss=rss)
        self.assertEqual(pl.grants, [])
        pl = self.run_tick([1, 2], [3], avail=19, est=8, rss=rss, remaining={"t1": 2.5})
        self.assertEqual(pl.grants, [3])
        self.assertIn("0.5 GB reserved", core.render(pl.extra))

    def test_runs_of_the_same_build_share_a_key_past_wrappers_quiet_flags_and_script_names(self):
        same = ["perl -e alarm 3000; exec @ARGV ./gradlew :shared:sourceGuardTest -q",  # gq's unquoted argv
                "./gradlew :shared:sourceGuardTest", "timeout 600 env A=1 ./gradlew --quiet :shared:sourceGuardTest"]
        self.assertEqual({core.build_key(c) for c in same}, {"gradlew :shared:sourceGuardTest"})
        self.assertEqual(core.build_key("/usr/bin/git  commit -F -"), "/usr/bin/git commit -F -")  # no build inside
        script = ("#!/bin/sh\ncd /w || exit 1\n# ./gradlew ignored\n"
                  "perl -e 'alarm 3000; exec @ARGV' ./gradlew ktlintFormat > /tmp/f.log 2>&1\necho \"EXIT=$?\"\n"
                  "./gradlew --continue :a:test \\\n  :b:test -q 2>&1 | tail -30\n")
        self.assertEqual(core.script_of("/tmp/a-1.sh x -q"), ("/tmp/a-1.sh", ["x", "-q"]))
        self.assertEqual(core.script_of("zsh /tmp/b.sh"), ("/tmp/b.sh", []))
        self.assertIsNone(core.script_of("./gradlew check"))
        key = "gradlew ktlintFormat ; gradlew --continue :a:test :b:test"
        self.assertEqual(core.build_key("/tmp/a-1.sh", script), key)
        self.assertEqual(core.build_key("perl -e alarm 3000; exec @ARGV /tmp/a-2.sh x -q", script), key + " | x")
        self.assertEqual(core.build_key("/tmp/c.sh", "git commit -F m\n"), "/tmp/c.sh")  # no build: its own path

    def test_growing_swap_stops_extra_entries_but_not_base_slots(self):
        pl = self.run_tick([1, 2], [3], avail=40, swap=core.SWAP_GROWTH_GB)
        self.assertEqual(pl.grants, [])
        self.assertIn("swap grew 0.5 GB", core.render(pl.blocked))
        self.assertEqual(self.run_tick([1], [3], avail=0, swap=5.0).grants, [3])
        self.assertEqual(self.run_tick([1, 2], [3], avail=40, swap=core.SWAP_GROWTH_GB - 0.1).grants, [3])

    def test_swap_growth_is_measured_within_the_window(self):
        now = 1000.0
        samples = [(now - core.SWAP_WINDOW_S - 5, 0.0), (now - 50, 2.0), (now - 20, 1.5), (now, 2.4)]
        self.assertAlmostEqual(core.swap_growth_gb(samples, now), 0.9)
        self.assertEqual(core.swap_growth_gb([(now, 3.0)], now), 0.0)
        self.assertEqual(core.swap_growth_gb([(now - 50, 3.0), (now, 1.0)], now), 0.0)  # shrinking swap

    def test_used_counts_this_ticks_grants(self):
        pl = self.run_tick([1, 2], [3, 4], avail=20, est=8)
        self.assertEqual(pl.used, 3)
        self.assertIn(f"({pl.used} in use, base {core.SLOTS})", core.render(pl.blocked))

    def test_no_extra_when_headroom_would_drop_below_6_gb(self):
        pl = self.run_tick([1, 2], [3], avail=13.9, est=8)
        self.assertEqual(pl.grants, [])
        self.assertIn("< 6 GB headroom", core.render(pl.blocked))

    def test_warn_pressure_still_admits_but_critical_does_not(self):
        self.assertEqual(self.run_tick([1, 2], [3], pressure=2).grants, [3])
        pl = self.run_tick([1, 2], [3], pressure=4)
        self.assertEqual(pl.grants, [])
        self.assertIn("critical", core.render(pl.blocked))

    def test_no_extra_within_gap_and_base_slots_skip_every_check(self):
        self.assertEqual(self.run_tick([1, 2], [3], last_extra=1000 - core.EXTRA_GAP_S + 10).grants, [])
        self.assertEqual(self.run_tick([1, 2], [3], last_extra=900).grants, [3])
        self.assertEqual(self.run_tick([], [3], pressure=2, avail=0, last_extra=999).grants, [3])

    def test_every_entry_above_the_base_is_checked_after_builds_end(self):
        self.assertEqual(self.run_tick([1], [3, 4], avail=0).grants, [3])  # base slot only, no stored limit

    def test_command_type_and_learned_estimate(self):
        cases = {"perl -e 'alarm 3000; exec @ARGV' ./gradlew check --continue": "gradle-check",
                 "./gradlew -p backend :runtime:test --tests *X*": "gradle-backend",
                 "./gradlew :shared:compileTestKotlinIosSimulatorArm64": "gradle-ios",
                 "./gradlew :shared:testAndroidHostTest --tests *Y*": "gradle-test",
                 "./gradlew ktlintFormat :shared:detektMetadataMain": "gradle-lint",
                 "./gradlew :androidApp:assembleDebug": "gradle",
                 "xcodebuild test -workspace a": "xcodebuild", "uv run scripts/t.py": "uv",
                 "pod install": "pod", "java -jar x.jar": "java",
                 "java -Xmx64m -classpath /w/gradle/wrapper/gradle-wrapper.jar org.gradle.wrapper.GradleWrapperMain "
                 ":shared:testAndroidHostTest": "gradle-test", CLIENT: "gradle-check"}
        for cmd, want in cases.items():
            self.assertEqual(core.command_type(cmd), want, cmd)
        self.assertEqual(core.estimate_gb("gradle-lint", {}), 3.0)
        peaks = {"gradle-lint": [9.0, 1.0, 2.0, 1.5, 1.2, 1.1]}  # 9.0 left the 5-run window
        self.assertEqual(core.estimate_gb("gradle-lint", peaks), 2.0)


class TicketMemory(unittest.TestCase):
    def test_counts_own_tree_serving_daemon_its_workers_and_kotlin_daemon_only(self):
        from unittest import mock
        from bq import server
        procs = world(P(201, 101, "python3 gq run -- ./gradlew x", CLAUDE_PID=100),
                      P(400, 201, CLIENT, CLAUDE_PID=100, BQ_TICKET=1),
                      P(700, 1, DAEMON, CLAUDE_PID=100), P(710, 700, "java GradleWorkerMain", CLAUDE_PID=100),
                      P(800, 1, KOTLIN, CLAUDE_PID=100),
                      P(900, 1, DAEMON, CLAUDE_PID=100), P(910, 900, "java GradleWorkerMain", CLAUDE_PID=100))
        for pid in procs:
            procs[pid].cpu = 10.0
        pids = server.ticket_pids(201, 1, procs, serving={700: 400, 900: 555}, kotlin_of={700: {800}, 900: {800}})
        self.assertEqual(pids, {201, 400, 700, 710, 800})  # 900 serves another client; 910 is its worker
        self.assertEqual(round(server.rss_gb(pids, procs) * 1024), 5)  # 1 MB each
        with mock.patch.object(server.system, "NCPU", 4):
            self.assertEqual(server.cpu_pct(pids, procs), 12.5)  # 5 x 10% of one core, on 4 cores
            self.assertEqual(server.load(pids, procs, total_gb=0.5)["ram_pct"], 1.0)

    def test_project_is_the_git_root_folder_and_outside_builds_are_named(self):
        from unittest import mock
        from bq import server
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp, "repo-wt")
            (wt / "backend").mkdir(parents=True)
            (wt / ".git").write_text("gitdir: elsewhere")  # a worktree's .git is a file
            Path(tmp, "plain").mkdir()
            self.assertEqual(server.project_of(str(wt / "backend")), "repo-wt")
            self.assertEqual(server.project_of(str(Path(tmp, "plain"))), "plain")  # no git: the folder itself
            self.assertEqual(server.project_of(""), "")
            procs = world(P(400, 1, CLIENT), P(410, 400, "java GradleWorkerMain"))
            with mock.patch.object(server.system, "cwd_of", return_value=str(wt / "backend")):
                loads = server.outside_and_runner([400], {"p400": {400, 410}, "runner": set()}, procs, 1, {})
        self.assertEqual(loads["outside"]["n"], 1)
        self.assertEqual(loads["outside"]["builds"],
                         [{"pid": 400, "project": "repo-wt", "ctype": core.command_type(CLIENT),
                           "cwd": str(wt / "backend"), "cmd": CLIENT, "granted": 0.0, "status": "",
                           "rss_gb": 0.0, "cpu_pct": 0.0}])

    def test_detail_dialog_tree_counts_the_daemons_serving_the_build_like_the_table(self):
        from unittest import mock
        from bq import server
        procs = world(P(600, 101, CLIENT, CLAUDE_PID=100), P(700, 1, DAEMON), P(710, 700, "java GradleWorkerMain"),
                      P(800, 1, KOTLIN))
        core.classify(procs)
        q = server.Queue()
        q.procs, q.mem, q.serving, q.kotlin_of = procs, {"total_gb": 1, "free_pct": 50}, {700: 600}, {700: {800}}
        with mock.patch.object(server.system, "cwd_of", return_value=""):
            d = q.proc_details(600)
        self.assertEqual([r["pid"] for r in d["tree"]][0], 600)
        self.assertEqual({r["pid"] for r in d["tree"]}, server.tree_pids({600}, procs, q.serving, q.kotlin_of))
        self.assertEqual(d["tree_rss_gb"], round(4 * 1024 / 1024 ** 2, 2))  # 4 processes, 1 MB each

    def test_each_runner_job_is_a_row_named_by_the_runner_with_its_gradle_step(self):
        from unittest import mock
        from bq import server
        with tempfile.TemporaryDirectory() as tmp:
            for name, agent in (("a", "mac-a"), ("b", None)):
                Path(tmp, name).mkdir()
                if agent:
                    Path(tmp, name, ".runner").write_text('\ufeff{"agentName": "%s"}' % agent, encoding="utf-8")
            step = "/bin/bash -e /r/_work/_temp/s.sh"
            procs = world(P(300, 1, f"{tmp}/a/bin.2.338.2/Runner.Worker spawnclient 163 166"),
                          P(310, 300, step), P(320, 310, CLIENT), P(330, 1, DAEMON),
                          P(400, 1, f"{tmp}/b/bin/Runner.Worker spawnclient 1 2"), P(410, 400, step))
            procs[310].started, procs[410].started = 5.0, 6.0
            jobs = {300: {300, 310, 320, 330}, 400: {400, 410}}
            core.classify(procs)
            server.root_cwd.cache_clear()  # other tests looked up the same pids
            with mock.patch.object(server.system, "cwd_of", return_value="/r/_work/Health-Hub/Health-Hub"):
                rows = server.outside_and_runner([], {"runner": {300, 310, 320, 330, 400, 410}}, procs, 1, jobs)["runners"]
        self.assertEqual([(r["pid"], r["runner_name"], r["cmd"], r["ctype"], r["project"], r["rss_gb"]) for r in rows],
                         [(300, "mac-a", CLIENT, core.command_type(CLIENT), "Health-Hub", 0.0),
                          (400, "b", step, "other", "Health-Hub", 0.0)])

    def test_emulators_and_simulators_are_rows_owned_by_who_started_or_names_them(self):
        from unittest import mock
        from bq import server
        udid = "71CCFDE2-212F-4BB4-9E30-40207E8A8A5C"
        with tempfile.TemporaryDirectory() as tmp:
            dev = Path(tmp, "Devices", udid)
            dev.mkdir(parents=True)
            Path(dev, "device.plist").write_bytes(server.plistlib.dumps(
                {"name": "iPhone 16", "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-18-5"}))
            sim = f"launchd_sim {dev.as_posix()}/data/var/run/launchd_bootstrap.plist"  # snapshot_procs uses /
            qemu = "/sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64-headless -avd health-hub-ci -no-window"
            procs = world(P(200, 101, qemu, CLAUDE_PID=100), P(210, 200, "/sdk/emulator/netsimd"),
                          P(300, 1, sim), P(310, 300, "SpringBoard"),
                          P(400, 1, "/r/bin.2.338.2/Runner.Worker spawnclient 1 2"),
                          P(410, 400, "xcrun simctl spawn 'iPhone 16 Pro' x.kexe"),
                          P(420, 400, "xcodebuild test -destination platform=iOS Simulator,name=iPhone 16,OS=18.5"),
                          P(500, 1, "/sdk/emulator/emulator @other"), P(510, 500, "qemu-system-x86_64 -avd other"),
                          P(600, 101, f"simctl spawn {udid} /bin/sleep 9"))  # macOS hides simctl's env
            core.classify(procs)
            self.assertEqual(core.claude_of(core.device_user([udid], procs, set()), procs), 100)
            self.assertEqual([p.pid for p in core.device_roots(procs)], [200, 300, 500])  # 510 runs under its launcher
            self.assertIsNone(core.device_user(["iPhone 16 Pro Max"], procs, set()))
            server.root_cwd.cache_clear()
            with mock.patch.object(server.system, "cwd_of", return_value="/w/Health-Hub"):
                rows = {r["pid"]: r for r in (server.device_row(p, procs) for p in core.device_roots(procs))}
        self.assertEqual((rows[200]["name"], rows[200]["ctype"], rows[200]["claude_pid"], rows[200]["runner_name"]),
                         ("health-hub-ci", "android-emulator", 100, None))
        self.assertEqual((rows[300]["cmd"], rows[300]["claude_pid"], rows[300]["runner_name"], rows[300]["project"]),
                         (f"iPhone 16 · iOS 18.5 · {udid}", None, "r", "Health-Hub"))
        self.assertEqual((rows[500]["name"], rows[500]["claude_pid"], rows[500]["runner_name"], rows[500]["cwd"]),
                         ("other", None, None, ""))

    def test_every_worktree_of_a_repo_shares_the_main_checkout(self):
        from bq import server
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp, "repo").resolve()
            (main / ".git/worktrees/wt").mkdir(parents=True)
            (main / ".git/worktrees/wt/commondir").write_text("../..\n")
            wt = Path(tmp, "repo-wt").resolve()
            (wt / "app").mkdir(parents=True)
            (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
            sub = main / "lib"
            sub.mkdir()
            (sub / ".git").write_text("gitdir: ../.git/modules/lib")  # a submodule: no commondir
            self.assertEqual(server.repo_of(str(wt / "app")), str(main))
            self.assertEqual(server.repo_of(str(main)), str(main))
            self.assertEqual(server.repo_of(str(sub)), str(sub))
            self.assertEqual(server.repo_of(str(Path(tmp))), str(Path(tmp)))  # no git: the folder itself

    def test_curves_keep_the_last_three_full_runs_of_each_repo_and_command(self):
        import sqlite3
        from types import MethodType, SimpleNamespace
        from bq import server
        q = SimpleNamespace(db=sqlite3.connect(":memory:"), curves={})
        q.db.execute("create table curves (repo text, cmd text, ts real, samples text, ctype text)")
        for name in ("save_curve", "history", "track", "end_outside"):
            setattr(q, name, MethodType(getattr(server.Queue, name), q))
        build = ("/r", "gradlew check", "gradle-check")
        for peak in (4.0, 6.0, 8.0, 10.0):  # the 4.0 run leaves the history
            q.save_curve({"build": build, "samples": [(2.0, 1.0), (30.0, peak), (60.0, 2.0)]})
        q.save_curve({"build": build, "samples": [(server.CURVE_LATE_S + 1, 50.0)]})  # missed its start
        self.assertEqual(sorted(max(gb for _t, gb in c) for c in q.history(build)), [6.0, 8.0, 10.0])
        self.assertEqual(q.track("p400", build, "L", 5.0, 1.0), 8.0)  # mean of the peaks still ahead
        self.assertEqual(q.track("p400", build, "L", 45.0, 1.0), 2.0)
        self.assertEqual(q.track("p402", ("/r", "gradlew build", "gradle-check"), "L", 5.0, 1.0), 8.0)  # by type
        self.assertIsNone(q.track("p401", ("/r", "other", "other"), "L", 0.0, 1.0))
        q.end_outside([400], [], {})  # p400 ran outside the queue and ended; p401 was a root in a slot
        self.assertEqual(q.curves, {})  # p402 never ran outside the queue either
        self.assertEqual(len(q.history(build)), 3)
        self.assertEqual(max(gb for _t, gb in q.history(build)[0]), 1.0)  # the newest run, its two samples

    def test_tickets_carry_the_main_page_values_and_a_rounded_peak(self):
        import sqlite3
        from types import MethodType, SimpleNamespace
        from bq.server import Queue
        db = sqlite3.connect(":memory:")
        db.execute("create table tickets (id, pid, claude_pid, ralphy, cwd, cmd, state, created, granted, ended,"
                   " end_reason, exit_code, ctype, peak_gb, subagent, dest, phase)")
        db.execute("insert into tickets (id, claude_pid, state, ctype, peak_gb) values (1, 5, 'done', 'other', 3.923477),"
                   " (2, 5, 'granted', 'other', null), (3, 5, 'waiting', 'other', null), (4, 7, 'waiting', 'other', null),"
                   " (5, 5, 'done', 'other', null)")
        q = SimpleNamespace(db=db, peak={2: 5.04}, now_gb={2: 4.96}, now_cpu={2: 12.34}, peaks=dict)
        q.ticket_rows, q.agent_label = MethodType(Queue.ticket_rows, q), MethodType(Queue.agent_label, q)
        # an agent's waiting ticket keeps its place in the whole line, not among its agent's tickets
        self.assertEqual(Queue.tickets_view(q, "claude_pid=?", (7,), {})[0]["position"], 2)
        done, granted, waiting, _, done_no_peak = Queue.tickets_view(q, "1=1", (), {})  # ordered by id
        self.assertEqual((granted["rss_gb"], granted["peak_gb"], granted["cpu_pct"]), (5.0, 5.0, 12.3))
        # a waiting ticket has no CPU before its gq runs; a done one keeps its stored peak, rounded
        self.assertEqual((done["position"], granted["position"], waiting["position"], waiting["cpu_pct"],
                          done["peak_gb"], done_no_peak["peak_gb"]), (None, None, 1, None, 3.9, None))



class SubagentCall(unittest.TestCase):
    RECS = [{"id": "a", "session_id": "S", "agent_id": None, "ts": 100.0},
            {"id": "b", "session_id": "S", "agent_id": "x1", "agent_type": "Explore", "ts": 105.0},
            {"id": "c", "session_id": "T", "agent_id": None, "ts": 106.0}]

    def test_newest_unclaimed_call_of_the_session(self):
        self.assertEqual(core.match_call(self.RECS, "S", 110.0, set())["id"], "b")
        self.assertEqual(core.match_call(self.RECS, "S", 110.0, {"b"})["id"], "a")

    def test_other_session_or_stale_call_never_matches(self):
        self.assertIsNone(core.match_call(self.RECS, "S", 110.0, {"a", "b"}))
        self.assertIsNone(core.match_call(self.RECS, "S", 105.0 + core.CALL_WINDOW_S + 1, set()))
        self.assertIsNone(core.match_call(self.RECS, None, 110.0, set()))


class Messages(unittest.TestCase):
    PAGE = Path(__file__).resolve().parent.parent / "bq/page.html"
    # Portuguese accents, plus words of the old pt-BR server texts that carry no accent
    PT = re.compile(r"[ãõçáéíóúâêôàÃÕÇÁÉÍÓÚÂÊÔ]|\b(vaga|vagas|sem|fila|ocioso|ocupado|entrou|liberou|solto|morreu|"
                    r"encerrado|folga|disponíveis|estimados|limite|teto|ninguém|permissão|matar|após|erro)\b", re.I)

    def page_i18n(self):
        html = self.PAGE.read_text(encoding="utf-8")
        return json.loads(re.search(r'<script type="application/json" id="i18n">(.*?)</script>', html, re.S).group(1))

    def test_message_is_a_key_with_params_and_renders_nested_messages_in_english(self):
        m = core.msg("blocked.no_slot", used=3, base=2, why=core.msg("refusal.gap", s=81))
        self.assertEqual(json.loads(json.dumps(m)), {"key": "blocked.no_slot", "params": {
            "used": 3, "base": 2, "why": {"key": "refusal.gap", "params": {"s": 81}}}})
        self.assertEqual(core.render(m), "base slots full (3 in use, base 2); next extra slot in 81s")
        self.assertEqual(core.render(core.msg("event.release", id=3, exit=None)), "ticket #3 released its slot (exit )")
        self.assertEqual(core.render("ticket #3 liberou a vaga (exit 0)"), "ticket #3 liberou a vaga (exit 0)")
        self.assertEqual(core.render(None), "")
        self.assertEqual(core.render(core.msg("end.unknown reason")), "end.unknown reason")

    def test_every_plan_message_has_an_english_template_and_renders(self):
        gqs = [P(200 + i, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100) for i in (1, 2, 3)]
        procs = world(*gqs, P(400, 101, CLIENT, CLAUDE_PID=100), P(600, 1, CLIENT, CLAUDE_PID=4242),
                      P(700, 1, DAEMON, CLAUDE_PID=100), P(701, 1, DAEMON, CLAUDE_PID=100),
                      P(712, 1, "/jdk/bin/java worker.org.gradle.process.internal.worker.GradleWorkerMain",
                        CLAUDE_PID=100))
        tickets = [core.Ticket(id=i, pid=200 + i, state="waiting", created=0) for i in (1, 2, 3)]
        pl = tick(procs, tickets, busy={701}, idle_since={700: 0})
        msgs = [pl.blocked] + [k[1] for k in pl.kills] + [p.status for p in procs.values() if p.status]
        self.assertGreater(len(msgs), 5)
        for m in msgs:
            self.assertIsInstance(m, dict)
            self.assertIn(m["key"], core.EN)
            self.assertFalse(self.PT.search(core.render(m)), core.render(m))

    def test_server_sources_emit_no_portuguese_text(self):
        self.assertTrue(self.PT.search('f"ticket #{tid} entrou na vaga"'))  # positive control: the scan fires
        self.assertTrue(self.PT.search("sem vaga (3/3 em uso)"))
        root = Path(__file__).resolve().parent.parent / "bq"
        for name in ("core.py", "server.py", "system.py", "settings.py", "service.py", "gq.py", "hook.py"):
            for n, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
                self.assertFalse(self.PT.search(line), f"{name}:{n}: {line.strip()}")

    def test_page_labels_every_setting_in_both_languages(self):
        i18n = self.page_i18n()
        for lang in ("en-US", "pt-BR"):
            for key, (group, _default, kind, low, _high) in settings.SPEC.items():
                for k in (f"cfg.{key}", f"cfg.{key}.help", f"cfg.group.{group}",
                          *([f"cfg.{key}.{c}" for c in low] if kind == "choice" else [])):
                    self.assertTrue(i18n[lang].get(k), f"{lang} {k}")

    def test_page_has_every_server_key_in_both_languages_with_the_same_english_text(self):
        i18n = self.page_i18n()
        self.assertEqual(set(i18n), {"en-US", "pt-BR"})
        self.assertEqual(set(i18n["en-US"]), set(i18n["pt-BR"]))
        for key, text in core.EN.items():
            self.assertEqual(i18n["en-US"].get(key), text, key)
            placeholders = set(re.findall(r"\{(\w+)\}", text))
            self.assertEqual(set(re.findall(r"\{(\w+)\}", i18n["pt-BR"][key])), placeholders, key)


    def test_page_labels_every_vm_state_and_destination_in_both_languages(self):
        from bq import server
        i18n = self.page_i18n()
        states = {*server.VM_STATES, *server.VM_STATUS.values()}
        self.assertEqual(states, set(server.VM_STATES))
        for lang in ("en-US", "pt-BR"):
            for key in [*(f"vm.{s}" for s in server.VM_STATES), "dest.mac", "dest.vm", "dest.vm_starting"]:
                self.assertTrue(i18n[lang].get(key), f"{lang} {key}")


class Toolchains(unittest.TestCase):
    QUEUED = {"npm ci": "node", "npm install": "node", "npm run build": "node", "npm test": "node",
              "npm run test:unit": "node", "pnpm build": "node", "pnpm --filter=web test": "node", "yarn": "node",
              "yarn build": "node", "bun install": "node", "bun test": "node", "npx tsc --noEmit": "node",
              "pnpm exec jest": "node", "yarn vitest run": "node", "tsc -p .": "node", "next build": "node",
              "vite build": "node", "jest": "node", "vitest run": "node", "node node_modules/.bin/tsc -b": "node",
              "cmake --build build -j8": "cc", "make -j8": "cc", "ninja -C out": "cc", "clang -c a.c": "cc",
              "g++ -O2 -o x a.cpp": "cc", "gcc-13 -c a.c": "cc", "bazel build //...": "bazel",
              "bazelisk test //x:y": "bazel", "dotnet build": "dotnet", "dotnet test x.sln": "dotnet",
              "dotnet publish -c Release": "dotnet", "dotnet restore": "dotnet", "msbuild x.sln": "msbuild",
              "swift build": "swift", "swift test --parallel": "swift", "cargo build --release": "cargo",
              "cargo +nightly test": "cargo", "go build ./...": "go", "go test ./...": "go", "mvn -q test": "maven", "cargo check": "cargo", "cargo clippy --all": "cargo",
              "nx build web": "node", "nx run web:build": "node", "nx run-many -t build test": "node",
              "nx affected --target=test": "node", "turbo run build": "node", "turbo build --filter=web": "node",
              "webpack": "node", "webpack --mode production": "node", "pnpm --filter web build": "node",
              "npm -w web run build": "node", "pnpm -C app install": "node", "yarn --cwd app test": "node",
              "./mvnw package": "maven", "sbt compile": "sbt"}
    CHEAP = ["npm --version", "npm ls", "npm run dev", "npm run lint", "npm start", "npm run build:watch",
             "npm run build -- --watch", "npx create-next-app x", "node -e 'console.log(1)'", "node server.js",
             "tsc --watch", "tsc -v", "jest --watch", "vitest watch", "next dev", "vite", "cmake -S . -B build",
             "clang --version", "clang -print-resource-dir", "make --version", "bazel query //...",
             "bazel shutdown", "dotnet --info", "dotnet run", "dotnet build-server shutdown", "msbuild /version",
             "swift package resolve", "swift --version", "cargo --version", "cargo fmt",
             "go fmt ./...", "go vet ./...", "go run .", "mvn --version", "git status", "ls", "which make",
             "brew install cmake", "nx serve web", "nx run web:serve", "turbo run dev",
             "turbo run lint", "nx graph", "webpack serve", "webpack --watch", "pnpm --filter web dev", "echo cargo build", "grep -r 'npm ci' ."]

    def test_heavy_commands_get_their_command_type_and_the_hook_queues_them(self):
        for cmd, want in self.QUEUED.items():
            self.assertEqual(core.command_type(cmd), want, cmd)
            self.assertTrue(core.analyze_command(cmd)[0], cmd)
            self.assertEqual(core.analyze_command(f"gq run -- {cmd}"), ([], True), cmd)

    def test_cheap_commands_are_not_queued(self):
        for cmd in self.CHEAP:
            self.assertEqual(core.analyze_command(cmd)[0], [], cmd)

    def test_windows_spellings_and_shell_wrappers_are_queued(self):
        for cmd in ["npm.cmd run build", "pnpm.CMD install", "dotnet.exe test", "MSBuild.exe x.sln /m",
                    "cargo.exe build", "go.exe test ./...", "mvn.cmd package", "cmd /c \"npm.cmd ci\"",
                    "cmd //c dotnet build", "bash -c 'cargo test'", "sh -c \"make -j4 && make install\"",
                    "cd web && npx vite build", "timeout 600 swift build"]:
            self.assertTrue(core.analyze_command(cmd)[0], cmd)
        self.assertEqual(core.command_type("C:/dotnet/dotnet.exe build x.sln"), "dotnet")
        self.assertEqual(core.command_type("perl -e 'alarm 3000; exec @ARGV' cargo test"), "cargo")
        self.assertEqual(core.analyze_command("bash -c 'gq run -- cargo test'"), ([], True))

    def test_process_kinds(self):
        for args, kind in [
                ("node /opt/homebrew/bin/npm ci", "node"), ("npm install", "node"),  # Linux npm retitles itself
                ("C:/nodejs/node.exe C:/nodejs/node_modules/npm/bin/npm-cli.js run build", "node"),
                ("node --max-old-space-size=4096 /w/node_modules/.bin/tsc -p .", "node"),
                ("node /w/node_modules/.bin/tsc --watch", "tsc_watch"),
                ("node /w/node_modules/.bin/next build", "node"),
                ("/usr/bin/make -j8", "cc"), ("cmake --build build", "cc"), ("/usr/bin/clang -c a.c", "compiler"),
                ("/usr/libexec/gcc/x86_64-linux-gnu/13/cc1plus -quiet a.cpp", "compiler"),
                ("C:/llvm/bin/clang++.exe -c a.cpp", "compiler"), ("swift-frontend -frontend -c a.swift", "compiler"),
                ("/usr/bin/swift-build --configuration debug", "swift"), ("cargo build", "cargo"),
                ("rustc --crate-name x src/lib.rs", "compiler"), ("go test ./...", "go"), ("dotnet build", "dotnet"),
                ("C:/dotnet/dotnet.exe test", "dotnet"), ("C:/VS/MSBuild/Bin/MSBuild.exe x.sln /m", "msbuild"),
                ("bazel build //...", "bazel"),
                ("dotnet /usr/share/dotnet/sdk/8.0/MSBuild.dll /nologo /nodemode:1 /nodeReuse:true", "msbuild_node"),
                ("C:/VS/MSBuild/Bin/MSBuild.exe /nodemode:1 /nodeReuse:true /low:false", "msbuild_node"),
                ("dotnet /usr/share/dotnet/sdk/8.0.100/Roslyn/bincore/VBCSCompiler.dll -pipename:abc", "vbcscompiler"),
                ("C:/VS/Roslyn/VBCSCompiler.exe -pipename:abc", "vbcscompiler"),
                ("dotnet exec /sdk/Sdks/Microsoft.NET.Sdk.Razor/tools/rzc.dll server -p pipe", "razor_server"),
                ("bazel(ws) -XX:+HeapDumpOnOutOfMemoryError -jar /u/.cache/bazel/A-server.jar", "bazel_server"),
                ("node /w/node_modules/eslint_d/lib/server.js", "eslint_d"),
                ("/jdk/bin/java -cp boot org.codehaus.plexus.classworlds.launcher.Launcher test", "java"),
                ("node /u/.vscode/extensions/ts/tsserver.js", "other"), ("rust-analyzer", "other"),
                ("sccache", "other"), ("clangd --background-index", "other"), ("sourcekit-lsp", "other"),
                ("node /w/node_modules/.bin/eslint .", "other"), ("node server.js", "other"),
                ("cargo check --message-format=json", "cargo"), ("dotnet /sdk/MSBuild.dll x.sln", "other")]:
            self.assertEqual(core.kind_of(args), kind, args)


class ToolchainCleanup(unittest.TestCase):
    ORPHAN = core.msg("kill.orphan", pid=4242)
    WORKER = core.msg("kill.loose_worker")

    def kills(self, *procs):
        return tick(world(*procs)).kills

    def test_build_and_daemons_of_a_dead_agent_die(self):
        procs = [P(900, 1, "cargo build", CLAUDE_PID=4242), P(901, 900, "rustc --crate-name x", CLAUDE_PID=4242),
                 P(910, 1, "dotnet /sdk/MSBuild.dll /nologo /nodemode:1 /nodeReuse:true", CLAUDE_PID=4242),
                 P(911, 910, "node /w/node_modules/.bin/tsc -p .", CLAUDE_PID=4242),
                 P(920, 1, "dotnet /sdk/Roslyn/bincore/VBCSCompiler.dll -pipename:x", CLAUDE_PID=4242),
                 P(930, 1, "dotnet exec /sdk/rzc.dll server -p x", CLAUDE_PID=4242),
                 P(940, 1, "bazel(ws) -jar /u/.cache/bazel/install/a/A-server.jar", CLAUDE_PID=4242),
                 P(941, 940, "/jdk/bin/java -jar JavaBuilder_deploy.jar --persistent_worker", CLAUDE_PID=4242),
                 P(950, 1, "node /w/node_modules/.bin/tsc --watch", CLAUDE_PID=4242),
                 P(960, 1, "node /w/node_modules/eslint_d/lib/server.js", CLAUDE_PID=4242),
                 P(970, 1, "node /opt/homebrew/bin/npm ci", CLAUDE_PID=4242),
                 P(971, 970, "node /w/node_modules/.bin/next build", CLAUDE_PID=4242)]
        # 941 is a `java`, which the existing rule already kills on its own; the bazel server's tree kill covers it too
        want = [(p, self.ORPHAN) for p in (900, 910, 920, 930, 940, 941, 950, 960, 970)]
        self.assertEqual(sorted(self.kills(*procs)), want)

    def test_the_same_processes_of_a_live_agent_or_outside_any_agent_are_spared(self):
        for env in ({"CLAUDE_PID": 100}, {}):
            procs = [P(900, 101, "cargo build", **env), P(901, 101, "make -j8", **env),
                     P(910, 1, "dotnet /sdk/MSBuild.dll /nodemode:1 /nodeReuse:true", **env),
                     P(920, 1, "C:/VS/Roslyn/VBCSCompiler.exe -pipename:x", **env),
                     P(940, 1, "bazel(ws) -jar /u/.cache/bazel/install/a/A-server.jar", **env),
                     P(941, 940, "/jdk/bin/java -jar JavaBuilder_deploy.jar --persistent_worker", **env),
                     P(950, 1, "node /w/node_modules/.bin/tsc --watch", **env),
                     P(960, 1, "node /w/node_modules/eslint_d/lib/server.js", **env)]
            self.assertEqual(self.kills(*procs), [], env)

    def test_ide_language_servers_and_caches_of_a_dead_agent_are_spared(self):
        self.assertEqual(self.kills(P(900, 1, "rust-analyzer", CLAUDE_PID=4242), P(901, 1, "sccache", CLAUDE_PID=4242),
                                    P(902, 1, "node /u/.vscode/extensions/ts/tsserver.js", CLAUDE_PID=4242),
                                    P(903, 1, "clangd --background-index", CLAUDE_PID=4242),
                                    P(904, 1, "sourcekit-lsp", CLAUDE_PID=4242),
                                    P(905, 1, "node /w/node_modules/.bin/eslint .", CLAUDE_PID=4242)), [])

    def test_nested_toolchain_under_a_queued_run_dies_with_its_gq_only(self):
        gq = P(201, 101, "python3 /u/.local/bin/gq run -- npm run build", CLAUDE_PID=4242)
        procs = [gq, P(400, 201, "node /opt/homebrew/bin/npm run build", CLAUDE_PID=4242),
                 P(401, 400, "node /w/node_modules/.bin/next build", CLAUDE_PID=4242)]
        self.assertEqual(self.kills(*procs), [(201, self.ORPHAN)])

    def test_compiler_whose_driver_died_is_killed(self):
        procs = [P(50, 1, "/usr/lib/systemd/systemd --user"),
                 P(900, 1, "/usr/bin/clang -c a.c", CLAUDE_PID=100),
                 P(901, 50, "/usr/libexec/gcc/x/13/cc1plus -quiet a.cpp", CLAUDE_PID=100),
                 P(902, 999, "swift-frontend -frontend -c a.swift", CLAUDE_PID=100),
                 P(903, 0, "C:/llvm/bin/clang++.exe -c a.cpp", CLAUDE_PID=100)]
        self.assertEqual(sorted(self.kills(*procs)), [(p, self.WORKER) for p in (900, 901, 902, 903)])

    def test_compiler_with_a_live_parent_or_outside_an_agent_is_spared(self):
        procs = [P(800, 101, "make -j8", CLAUDE_PID=100), P(801, 800, "/usr/bin/clang -c a.c", CLAUDE_PID=100),
                 P(810, 1, "sourcekit-lsp", CLAUDE_PID=100), P(811, 810, "swift-frontend -frontend -c a.swift",
                                                                 CLAUDE_PID=100),
                 P(820, 101, "python3 -m pip install x", CLAUDE_PID=100), P(821, 820, "gcc -c ext.c", CLAUDE_PID=100),
                 P(830, 1, "/Applications/Xcode.app/x/clang -c a.m"),  # Xcode's own, owner nonagent
                 P(840, 1, "/usr/bin/ld -o x a.o", CLAUDE_PID=100)]
        self.assertEqual(self.kills(*procs), [])


class MaxConfig(unittest.TestCase):
    def test_gq_config_max_keeps_other_keys_and_the_server_reads_it(self):
        import tempfile
        from unittest import mock
        from bq import gq, server
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            with mock.patch.object(gq, "CONFIG", cfg), mock.patch.object(server, "CONFIG", cfg), \
                    mock.patch.object(gq, "call", side_effect=OSError), mock.patch("builtins.print"):
                q = server.Queue.__new__(server.Queue)
                self.assertIsNone(q.ceiling())  # default: no cap
                gq.enforce("on")
                gq.config_max("5")
                self.assertEqual(json.loads(cfg.read_text()), {"enforce": True, "max": 5})
                self.assertEqual((q.ceiling(), q.enforce()), (5, True))
                with self.assertRaises(SystemExit):
                    gq.config_max("1")
                with self.assertRaises(SystemExit):
                    gq.config_max("x")
                gq.config_max("off")
                self.assertEqual(json.loads(cfg.read_text()), {"enforce": True})
                self.assertIsNone(q.ceiling())


class Settings(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from bq import settings
        self.settings = settings
        self.dir = tempfile.TemporaryDirectory()
        cfg = Path(self.dir.name) / "config.json"
        self.cfg = cfg
        for patch in (mock.patch.object(settings, "CONFIG", cfg), mock.patch.object(settings, "DATA", cfg.parent)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.dir.cleanup)

    def test_defaults_and_bad_stored_values_fall_back_one_key_at_a_time(self):
        s = self.settings
        self.assertEqual(s.load(), s.defaults())
        self.cfg.write_text(json.dumps({"headroom_gb": "lots", "extra_gap_s": 45, "max": 1, "base_slots": 3,
                                        "default_gb": {"node": 7}, "protected_paths": ["/x/"]}))
        v = s.load()
        self.assertEqual(v["headroom_gb"], 6.0)  # a string: ignored
        self.assertEqual((v["extra_gap_s"], v["base_slots"], v["max"]), (45, 3, None))  # max 1 < base 3: off
        self.assertEqual((v["default_gb"]["node"], v["default_gb"]["gradle-check"]), (7.0, 8.0))
        self.assertEqual(v["protected_paths"], ["/x/"])
        self.cfg.write_text("{not json")
        self.assertEqual(s.load(), s.defaults())

    def test_validate_reports_every_bad_value_and_cross_checks_max_against_base(self):
        s = self.settings
        clean, errors = s.validate({"base_slots": 4, "max": 3, "port": 80, "tick_s": True, "enforce": "yes",
                                    "runner_ctype": "nope", "block_pressure": 3, "protected_paths": ["ok", " "],
                                    "default_gb": {"nope": 1}, "bogus": 1})
        self.assertEqual(set(errors), {"max", "port", "tick_s", "enforce", "runner_ctype", "block_pressure",
                                       "protected_paths", "default_gb", "bogus"})
        self.assertEqual(errors["max"], core.msg("config.max_below_base", base=4))
        self.assertEqual(errors["port"], core.msg("config.range", min=1024, max=65535))
        for e in errors.values():
            self.assertIn(e["key"], core.EN)
        clean, errors = s.validate({"max": None, "base_slots": 1, "block_pressure": 2, "headroom_gb": 4})
        self.assertEqual(errors, {})
        self.assertEqual((clean["max"], clean["base_slots"], clean["headroom_gb"]), (None, 1, 4.0))

    def test_save_keeps_only_changed_and_unknown_keys(self):
        s = self.settings
        self.cfg.write_text(json.dumps({"enforce": True, "future_key": 1, "headroom_gb": 9.0}))
        clean, errors = s.validate({"headroom_gb": 6.0, "extra_gap_s": 10, "protected_paths": [" /p/ "]})
        self.assertEqual(errors, {})
        s.save(clean)
        self.assertEqual(json.loads(self.cfg.read_text()),
                         {"future_key": 1, "enforce": True, "extra_gap_s": 10, "protected_paths": ["/p/"]})


class ConfigApi(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from bq import server, settings
        self.server = server
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        cfg = Path(self.dir.name) / "config.json"
        self.cfg = cfg
        self.restarts = []
        for patch in (mock.patch.object(settings, "CONFIG", cfg), mock.patch.object(settings, "DATA", cfg.parent),
                      mock.patch.object(server, "restart_soon", lambda: self.restarts.append(1)),
                      mock.patch.object(server.service, "installed", lambda: True),
                      mock.patch.object(server.service, "autostart", lambda: True)):
            patch.start()
            self.addCleanup(patch.stop)

    def apply(self, body):
        import threading
        out = self.server.apply_config(body)
        for t in threading.enumerate():
            if t.name == "restart":
                t.join(5)
        return out

    def test_invalid_values_save_nothing_and_do_not_restart(self):
        reply, code = self.apply({"values": {"headroom_gb": -1, "extra_gap_s": 5}})
        self.assertEqual(code, 400)
        self.assertEqual(set(reply["errors"]), {"headroom_gb"})
        self.assertFalse(self.cfg.exists())
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.apply({"values": []})[1], 400)

    def test_valid_values_are_saved_then_the_server_restarts(self):
        from unittest import mock
        with mock.patch.object(self.server.service, "set_autostart", return_value="") as switch:
            reply, code = self.apply({"values": {"extra_gap_s": 5}, "autostart": True})
            switch.assert_not_called()  # unchanged
            self.assertEqual((code, reply["ok"]), (200, True))
            self.assertEqual(json.loads(self.cfg.read_text()), {"extra_gap_s": 5})
            self.assertEqual(self.restarts, [1])
            reply, code = self.apply({"values": {}, "autostart": False})
            switch.assert_called_once_with(False)
        with mock.patch.object(self.server.service, "set_autostart", return_value="denied"):
            reply, code = self.apply({"values": {}, "autostart": False})
        self.assertEqual((code, reply["errors"]["autostart"]["key"]), (400, "config.autostart"))

    def test_taken_port_is_refused(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            if port < 1024:
                self.skipTest("ephemeral port below the allowed range")
            reply, code = self.apply({"values": {"port": port}})
        self.assertEqual((code, reply["errors"]["port"]), (400, core.msg("config.port_taken", port=port)))

    def test_only_the_page_itself_may_post(self):
        from types import SimpleNamespace
        port = self.server.PORT
        ok = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}

        def allowed(**headers):
            h = {**ok, **headers}
            return self.server.Handler.same_origin(SimpleNamespace(headers={k: v for k, v in h.items() if v}))

        self.assertTrue(allowed())
        self.assertTrue(allowed(Origin=None))  # curl and gq send no Origin
        self.assertFalse(allowed(Origin="http://evil.example"))
        self.assertFalse(allowed(Host=f"evil.example:{port}"))  # DNS rebinding
        self.assertFalse(allowed(**{"Content-Type": "text/plain"}))  # a form post skips the CORS preflight

    def test_config_view_lists_every_setting_with_its_bounds(self):
        v = self.server.config_view()
        self.assertEqual([f["key"] for f in v["spec"]], list(self.server.settings.SPEC))
        port = next(f for f in v["spec"] if f["key"] == "port")
        self.assertEqual((port["min"], port["max"]), (1024, 65535))
        self.assertEqual(next(f for f in v["spec"] if f["key"] == "block_pressure")["choices"], [2, 4])
        self.assertTrue(v["autostart"])


class Autostart(unittest.TestCase):
    def test_launchd_disabled_list_is_read_in_both_formats(self):
        from unittest import mock
        from bq import service
        for out, want in [('\t"com.massa.build-queue" => disabled', False), ('"com.massa.build-queue" => true', False),
                          ('"com.massa.build-queue" => enabled', True), ('"other" => disabled', True)]:
            with mock.patch.object(service, "_system", return_value="darwin"), \
                    mock.patch.object(service, "installed", return_value=True), \
                    mock.patch.object(service, "_run", return_value=(0, out)), \
                    mock.patch.object(service.os, "getuid", return_value=501, create=True):
                self.assertEqual(service.autostart(), want, out)
        with mock.patch.object(service, "installed", return_value=False):
            self.assertIsNone(service.autostart())


if __name__ == "__main__":
    unittest.main()
