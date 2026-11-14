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
            P(101, 100, "/bin/zsh -c source /u/.claude/shell-snapshots/snapshot-zsh-1.sh")]
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
    def test_hook_process_is_not_a_build_but_bash_tool_and_orphan_runs_are(self):
        hook = P(400, 100, "uv run --quiet --script /u/.claude/plugins/x/hooks/langfuse_hook.py", CLAUDE_PID=100)
        tool = P(401, 101, "uv run scripts/t.py", CLAUDE_PID=100)
        orphan = P(402, 1, "uv run scripts/o.py", CLAUDE_PID=100)
        pl = tick(world(hook, tool, orphan))
        self.assertEqual(sorted(k[0] for k in pl.kills), [401, 402])
        self.assertEqual(pl.used, 2)

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

    def test_kotlin_daemon_dies_after_60s_idle_even_with_builds_running(self):
        gq = P(201, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100)
        procs = world(P(800, 1, KOTLIN, CLAUDE_PID=100), gq)
        busy_queue = [core.Ticket(id=1, pid=201, state="granted", created=0)]
        self.assertEqual(tick(procs, busy_queue, idle_since={800: 950}).kills, [])
        self.assertEqual(tick(procs, busy_queue, idle_since={800: 900}).kills, [(800, "daemon ocioso")])
        self.assertEqual(tick(procs, busy_queue, busy={800}, idle_since={800: 0}).kills, [])

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
        self.assertIsNone(decide({"command": "gq status"}))
        self.assertIsNone(decide({"command": "gq enforce on"}))


class DynamicLimit(unittest.TestCase):
    def gqs(self, *ids):
        return [P(200 + i, 101, "python3 /u/.local/bin/gq run -- ./gradlew x", CLAUDE_PID=100) for i in ids]

    def run_tick(self, granted, waiting, *, limit=2, avail=20.0, est=8.0, last_raise=None, prev=(), pressure=1,
                 extra=()):
        procs = world(*self.gqs(*granted, *waiting), *extra)
        tickets = [core.Ticket(id=i, pid=200 + i, state="granted", created=0) for i in granted] + \
                  [core.Ticket(id=i, pid=200 + i, state="waiting", created=0) for i in waiting]
        now = 1000.0
        return core.plan(procs, core.classify(procs), tickets, snapshot_ts=now - 1, now=now, pressure=pressure,
                         busy_daemons=set(), idle_since={}, last_grant_ts=None, live_claude_pids={100},
                         limit=limit, prev_holders=set(prev), last_raise_ts=last_raise, avail_gb=avail,
                         estimates={i: ("gradle-check", est) for i in waiting}, ceiling=core.max_slots(24.0))

    def test_raises_one_slot_when_memory_covers_estimate_plus_headroom(self):
        pl = self.run_tick([1, 2], [3, 4], avail=20, est=8)
        self.assertEqual((pl.grants, pl.limit), ([3], 3))
        self.assertIn("20.0 GB disponíveis − 8.0 GB", pl.raised)
        self.assertIn("próxima vaga extra", pl.blocked)  # gradual: the second raise waits RAISE_GAP_S

    def test_raise_adds_one_slot_even_above_the_limit(self):
        runner = P(300, 1, "/Users/u/actions-runner/myrepo/bin/Runner.Worker spawnclient")
        pl = self.run_tick([1, 2], [4], extra=[runner])  # 3 in use, limit 2
        self.assertEqual((pl.limit, pl.grants), (3, []))
        self.assertIn("ninguém entra", pl.raised)
        pl = self.run_tick([1, 2], [4], extra=[runner], limit=3, last_raise=1000 - core.RAISE_GAP_S)
        self.assertEqual((pl.limit, pl.grants), (4, [4]))

    def test_ceiling_follows_ram(self):
        self.assertEqual([core.max_slots(gb) for gb in (8.0, 16.0, 24.0, 32.0, 48.0, 64.0)], [2, 3, 4, 6, 8, 11])

    def test_used_counts_this_ticks_grants(self):
        pl = self.run_tick([1, 2], [3, 4], avail=20, est=8)
        self.assertEqual(pl.used, 3)
        self.assertIn(f"({pl.used}/{pl.limit} em uso)", pl.blocked)

    def test_no_raise_when_headroom_would_drop_below_6_gb(self):
        pl = self.run_tick([1, 2], [3], avail=13.9, est=8)
        self.assertEqual((pl.grants, pl.limit), ([], 2))
        self.assertIn("< 6 GB de folga", pl.blocked)

    def test_no_raise_within_gap_or_above_ceiling_or_without_normal_pressure(self):
        self.assertEqual(self.run_tick([1, 2], [3], last_raise=950).grants, [])
        self.assertEqual(self.run_tick([1, 2], [3], last_raise=900).grants, [3])
        pl = self.run_tick([1, 2, 3, 4], [5], limit=4, avail=40)
        self.assertEqual(pl.grants, [])
        self.assertIn("teto de 4", pl.blocked)
        self.assertEqual(self.run_tick([], [3], pressure=2, avail=0, limit=2).grants, [3])  # base slot, warn

    def test_limit_drops_one_per_finished_holder_and_rises_again_only_by_analysis(self):
        pl = self.run_tick([1, 2], [4], limit=3, prev={"t1", "t2", "t3"}, avail=10)
        self.assertEqual((pl.lowered, pl.limit, pl.grants), (1, 2, []))
        pl = self.run_tick([1, 2], [4], limit=3, prev={"t1", "t2", "t3"}, avail=20)
        self.assertEqual((pl.limit, pl.grants), (3, [4]))
        self.assertEqual(self.run_tick([1], [], limit=3, prev={"t1", "t2", "t3"}).limit, 2)  # floor is SLOTS

    def test_command_type_and_learned_estimate(self):
        cases = {"perl -e 'alarm 3000; exec @ARGV' ./gradlew check --continue": "gradle-check",
                 "./gradlew -p backend :runtime:test --tests *X*": "gradle-backend",
                 "./gradlew :shared:compileTestKotlinIosSimulatorArm64": "gradle-ios",
                 "./gradlew :shared:testAndroidHostTest --tests *Y*": "gradle-test",
                 "./gradlew ktlintFormat :shared:detektMetadataMain": "gradle-lint",
                 "./gradlew :androidApp:assembleDebug": "gradle",
                 "xcodebuild test -workspace a": "xcodebuild", "uv run scripts/t.py": "uv",
                 "pod install": "pod", "java -jar x.jar": "java"}
        for cmd, want in cases.items():
            self.assertEqual(core.command_type(cmd), want, cmd)
        self.assertEqual(core.estimate_gb("gradle-lint", {}), 3.0)
        peaks = {"gradle-lint": [9.0, 1.0, 2.0, 1.5, 1.2, 1.1]}  # 9.0 left the 5-run window
        self.assertEqual(core.estimate_gb("gradle-lint", peaks), 2.0)


class TicketMemory(unittest.TestCase):
    def test_counts_own_tree_serving_daemon_its_workers_and_kotlin_daemon_only(self):
        from bq.server import ticket_rss_gb
        procs = world(P(201, 101, "python3 gq run -- ./gradlew x", CLAUDE_PID=100),
                      P(400, 201, CLIENT, CLAUDE_PID=100, BQ_TICKET=1),
                      P(700, 1, DAEMON, CLAUDE_PID=100), P(710, 700, "java GradleWorkerMain", CLAUDE_PID=100),
                      P(800, 1, KOTLIN, CLAUDE_PID=100),
                      P(900, 1, DAEMON, CLAUDE_PID=100), P(910, 900, "java GradleWorkerMain", CLAUDE_PID=100))
        gb = ticket_rss_gb(201, 1, procs, serving={700: 400, 900: 555}, kotlin_of={700: {800}, 900: {800}})
        self.assertEqual(round(gb * 1024), 5)  # 201, 400, 700, 710, 800 at 1 MB each; 900 and 910 excluded

    def test_done_ticket_peak_is_rounded_and_missing_peak_stays_empty(self):
        import sqlite3
        from types import SimpleNamespace
        from bq.server import Queue
        db = sqlite3.connect(":memory:")
        db.execute("create table tickets (id, pid, claude_pid, ralphy, cwd, cmd, state, created, granted, ended,"
                   " end_reason, exit_code, ctype, peak_gb, subagent)")
        db.execute("insert into tickets (id, state, peak_gb) values (1, 'done', 3.923477172851), (2, 'done', null)")
        rows = Queue.ticket_rows(SimpleNamespace(db=db), "1=1", ())
        self.assertEqual([r["peak_gb"] for r in rows], [3.9, None])



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


if __name__ == "__main__":
    unittest.main()
