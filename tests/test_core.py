import json
import re
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
        self.assertIn("no slot", core.render(pl.blocked))

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
        self.assertIn("20.0 GB available − 8.0 GB", core.render(pl.raised))
        self.assertIn("next extra slot", core.render(pl.blocked))  # gradual: the second raise waits RAISE_GAP_S

    def test_raise_adds_one_slot_even_above_the_limit(self):
        runner = P(300, 1, "/Users/u/actions-runner/myrepo/bin/Runner.Worker spawnclient")
        pl = self.run_tick([1, 2], [4], extra=[runner])  # 3 in use, limit 2
        self.assertEqual((pl.limit, pl.grants), (3, []))
        self.assertIn("nobody enters", core.render(pl.raised))
        pl = self.run_tick([1, 2], [4], extra=[runner], limit=3, last_raise=1000 - core.RAISE_GAP_S)
        self.assertEqual((pl.limit, pl.grants), (4, [4]))

    def test_ceiling_follows_ram(self):
        self.assertEqual([core.max_slots(gb) for gb in (8.0, 16.0, 24.0, 32.0, 48.0, 64.0)], [2, 3, 4, 6, 8, 11])

    def test_used_counts_this_ticks_grants(self):
        pl = self.run_tick([1, 2], [3, 4], avail=20, est=8)
        self.assertEqual(pl.used, 3)
        self.assertIn(f"({pl.used}/{pl.limit} in use)", core.render(pl.blocked))

    def test_no_raise_when_headroom_would_drop_below_6_gb(self):
        pl = self.run_tick([1, 2], [3], avail=13.9, est=8)
        self.assertEqual((pl.grants, pl.limit), ([], 2))
        self.assertIn("< 6 GB headroom", core.render(pl.blocked))

    def test_warn_pressure_still_raises_but_critical_does_not(self):
        self.assertEqual(self.run_tick([1, 2], [3], pressure=2).grants, [3])
        pl = self.run_tick([1, 2], [3], pressure=4)
        self.assertEqual(pl.grants, [])
        self.assertIn("critical", core.render(pl.blocked))

    def test_no_raise_within_gap_or_above_ceiling(self):
        self.assertEqual(self.run_tick([1, 2], [3], last_raise=1000 - core.RAISE_GAP_S + 10).grants, [])
        self.assertEqual(self.run_tick([1, 2], [3], last_raise=900).grants, [3])
        pl = self.run_tick([1, 2, 3, 4], [5], limit=4, avail=40)
        self.assertEqual(pl.grants, [])
        self.assertIn("ceiling of 4", core.render(pl.blocked))
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


class Messages(unittest.TestCase):
    PAGE = Path(__file__).resolve().parent.parent / "bq/page.html"
    # Portuguese accents, plus words of the old pt-BR server texts that carry no accent
    PT = re.compile(r"[ãõçáéíóúâêôàÃÕÇÁÉÍÓÚÂÊÔ]|\b(vaga|vagas|sem|fila|ocioso|ocupado|entrou|liberou|solto|morreu|"
                    r"encerrado|folga|disponíveis|estimados|limite|teto|ninguém|permissão|matar|após|erro)\b", re.I)

    def page_i18n(self):
        html = self.PAGE.read_text(encoding="utf-8")
        return json.loads(re.search(r'<script type="application/json" id="i18n">(.*?)</script>', html, re.S).group(1))

    def test_message_is_a_key_with_params_and_renders_nested_messages_in_english(self):
        m = core.msg("blocked.no_slot", used=3, limit=3, why=core.msg("refusal.gap", s=81))
        self.assertEqual(json.loads(json.dumps(m)), {"key": "blocked.no_slot", "params": {
            "used": 3, "limit": 3, "why": {"key": "refusal.gap", "params": {"s": 81}}}})
        self.assertEqual(core.render(m), "no slot (3/3 in use); next extra slot in 81s")
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
        for name in ("core.py", "server.py", "system.py"):
            for n, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
                self.assertFalse(self.PT.search(line), f"{name}:{n}: {line.strip()}")

    def test_page_has_every_server_key_in_both_languages_with_the_same_english_text(self):
        i18n = self.page_i18n()
        self.assertEqual(set(i18n), {"en-US", "pt-BR"})
        self.assertEqual(set(i18n["en-US"]), set(i18n["pt-BR"]))
        for key, text in core.EN.items():
            self.assertEqual(i18n["en-US"].get(key), text, key)
            placeholders = set(re.findall(r"\{(\w+)\}", text))
            self.assertEqual(set(re.findall(r"\{(\w+)\}", i18n["pt-BR"][key])), placeholders, key)


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


if __name__ == "__main__":
    unittest.main()
