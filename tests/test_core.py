import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bq import core  # noqa: E402
from bq.hook import decide  # noqa: E402

CLIENT = "java -Xmx64m -Dorg.gradle.appname=gradlew -jar /w/gradle/wrapper/gradle-wrapper.jar check"
DAEMON = "/jdk/bin/java -cp x org.gradle.launcher.daemon.bootstrap.GradleDaemon 9.8.1"
KOTLIN = "/jdk/bin/java -cp y org.jetbrains.kotlin.daemon.KotlinCompileDaemon"


def P(pid, ppid, args, **env):
    return core.Proc(pid=pid, ppid=ppid, rss_kb=1024, cpu=0, etime="1:00", lstart="L", args=args,
                     env={k: str(v) for k, v in env.items()})


def world(*procs):
    base = [P(1, 0, "/sbin/launchd"), P(100, 1, "/Users/u/.local/bin/claude --settings /s/sessions/7.settings.json"),
            P(101, 100, "/bin/zsh -c source snapshot")]
    return {p.pid: p for p in base + list(procs)}


def tick(procs, tickets=(), *, pressure=1, busy=(), idle_since=None, now=1000.0, last_grant=None):
    roots = core.classify(procs)
    return core.plan(procs, roots, list(tickets), snapshot_ts=now - 1, now=now, pressure=pressure,
                     busy_daemons=set(busy), idle_since=idle_since or {}, last_grant_ts=last_grant,
                     live_claude_pids={p.pid for p in procs.values() if p.kind == "claude"})


class Scheduling(unittest.TestCase):
    def waiting(self, *ids):
        return [core.Ticket(id=i, pid=200 + i, state="waiting", created=0) for i in ids]

    def gqs(self, *ids):
        return [P(200 + i, 101, f"python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100) for i in ids]

    def test_fifo_grants_head_first_up_to_two_slots(self):
        pl = tick(world(*self.gqs(3, 1, 2)), self.waiting(3, 1, 2))
        self.assertEqual(pl.grants, [1, 2])
        self.assertIn("sem vaga", pl.blocked)

    def test_runner_takes_a_slot(self):
        runner = P(300, 1, "/Users/u/actions-runner/myrepo/bin/Runner.Worker spawnclient")
        pl = tick(world(runner, *self.gqs(1, 2)), self.waiting(1, 2))
        self.assertEqual(pl.grants, [1])

    def test_warn_pressure_admits_only_with_zero_local_builds(self):
        self.assertEqual(tick(world(*self.gqs(1, 2)), self.waiting(1, 2), pressure=2).grants, [1])
        granted = [core.Ticket(id=1, pid=201, state="granted", created=0)]
        pl = tick(world(*self.gqs(1, 2)), granted + self.waiting(2), pressure=2)
        self.assertEqual(pl.grants, [])
        self.assertIn("alerta", pl.blocked)

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


class Cleanup(unittest.TestCase):
    def test_unqueued_agent_build_is_killed_and_counts_as_a_slot(self):
        pl = tick(world(P(400, 101, CLIENT, CLAUDE_PID=100)))
        self.assertEqual(pl.kills, [(400, "build sem vaga")])
        self.assertEqual(pl.used, 1)

    def test_queued_build_is_left_alone(self):
        gq = P(201, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100)
        client = P(400, 201, CLIENT, CLAUDE_PID=100, BQ_TICKET=1)
        pl = tick(world(gq, client), [core.Ticket(id=1, pid=201, state="granted", created=0)])
        self.assertEqual(pl.kills, [])

    def test_runner_and_nonagent_and_staging_are_never_killed(self):
        runner = P(500, 1, CLIENT + " -Dgradle.user.home=/u/.gradle-health-hub-ci", GITHUB_ACTIONS="true")
        human = P(501, 1, CLIENT)
        staging = P(502, 1, "/usr/bin/java -classpath /u/.health-hub-local/releases/x/lib/runtime.jar M",
                    CLAUDE_PID=4242)
        pl = tick(world(runner, human, staging))
        self.assertEqual(pl.kills, [])
        self.assertEqual(pl.counts["nonagent"], 1)

    def test_orphan_of_dead_agent_is_killed(self):
        pl = tick(world(P(600, 1, CLIENT, CLAUDE_PID=4242)))
        self.assertIn((600, "build sem vaga"), pl.kills)
        self.assertEqual(len([k for k in pl.kills if k[0] == 600]), 1)

    def test_idle_agent_daemon_is_killed_after_grace_busy_one_is_not(self):
        procs = world(P(700, 1, DAEMON, CLAUDE_PID=100), P(701, 1, DAEMON, CLAUDE_PID=100), P(702, 1, DAEMON))
        pl = tick(procs, busy={701}, idle_since={700: 900, 702: 900})
        self.assertEqual(pl.kills, [(700, "daemon ocioso")])
        self.assertEqual(tick(world(P(700, 1, DAEMON, CLAUDE_PID=100)), idle_since={700: 995}).kills, [])

    def test_no_daemon_kill_right_after_a_grant(self):
        procs = world(P(700, 1, DAEMON, CLAUDE_PID=100))
        self.assertEqual(tick(procs, idle_since={700: 0}, last_grant=990).kills, [])

    def test_kotlin_daemon_waits_for_zero_local_builds(self):
        gq = P(201, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100)
        procs = world(P(800, 1, KOTLIN, CLAUDE_PID=100), gq)
        busy_queue = [core.Ticket(id=1, pid=201, state="granted", created=0)]
        self.assertEqual(tick(procs, busy_queue, idle_since={800: 0}).kills, [])
        self.assertEqual(tick(world(P(800, 1, KOTLIN, CLAUDE_PID=100)), idle_since={800: 0}).kills,
                         [(800, "daemon ocioso")])

    def test_test_worker_under_daemon_is_not_a_root_but_orphaned_worker_dies(self):
        procs = world(P(700, 1, DAEMON, CLAUDE_PID=100),
                      P(710, 700, "/jdk/bin/java -Dorg.gradle.internal.worker.tmpdir=/w Gradle Test Executor 1",
                        CLAUDE_PID=100),
                      P(711, 700, "/jdk/bin/java -jar something.jar", CLAUDE_PID=100),
                      P(712, 1, "/jdk/bin/java worker.org.gradle.process.internal.worker.GradleWorkerMain",
                        CLAUDE_PID=100))
        pl = tick(procs, busy={700})
        self.assertEqual(pl.kills, [(712, "worker solto (pai morreu)")])


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


if __name__ == "__main__":
    unittest.main()
