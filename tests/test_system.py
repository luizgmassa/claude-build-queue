"""The psutil layer against the real system it runs on; CI runs it on macOS, Linux and Windows."""

import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bq import core, system  # noqa: E402

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)", "bq-test-marker"]
LISTENER = [sys.executable, "-c", "import socket, time\n"
            "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
            "print(s.getsockname()[1], flush=True); c, _ = s.accept(); time.sleep(60)"]


class System(unittest.TestCase):
    def setUp(self):
        self.children = []

    def tearDown(self):
        for c in self.children:
            c.kill()
            c.wait(timeout=10)

    def child(self, cmd, **kw):
        c = subprocess.Popen(cmd, **kw)
        self.children.append(c)
        return c

    def test_snapshot_sees_a_child_with_its_parent_and_only_the_queue_env_keys(self):
        c = self.child(SLEEPER, env={**os.environ, "CLAUDE_PID": "4242", "BQ_TICKET": "7"})
        time.sleep(1)
        procs = system.snapshot_procs()
        p = procs[c.pid]
        self.assertEqual(p.ppid, os.getpid())
        self.assertIn("bq-test-marker", p.args)
        self.assertEqual(p.env.get("CLAUDE_PID"), "4242")
        self.assertEqual(p.env.get("BQ_TICKET"), "7")
        self.assertNotIn("PATH", p.env)
        self.assertGreater(p.rss_kb, 0)
        if os.name == "nt":
            self.assertNotIn("\\", p.args)

    @unittest.skipIf(os.name == "nt", "no ps on Windows")
    def test_lstart_matches_ps_so_stored_ticket_starts_still_match(self):
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
        self.assertEqual(system.snapshot_procs()[os.getpid()].lstart, " ".join(out.split()))

    def test_etime_uses_the_ps_format(self):
        self.assertEqual(system.etime(65), "01:05")
        self.assertEqual(system.etime(3 * 3600 + 5), "03:00:05")
        self.assertEqual(system.etime(2 * 86400 + 61), "2-00:01:01")

    def test_tcp_links_see_listen_and_established_ports(self):
        srv = self.child(LISTENER, stdout=subprocess.PIPE, text=True)
        port = int(srv.stdout.readline())
        with socket.create_connection(("127.0.0.1", port)) as conn:
            mine = conn.getsockname()[1]
            time.sleep(0.5)
            procs = system.snapshot_procs()
            names = {core.exe_name(procs[pid].args.split(" ", 1)[0]) for pid in (srv.pid, os.getpid())}
            listen, est = system.tcp_links(procs, names=names)  # the real program name: macOS re-execs python
        self.assertIn(port, listen.get(srv.pid, set()))
        self.assertIn((os.getpid(), mine, port), est)
        self.assertIn((srv.pid, port, mine), est)

    def test_memory_reports_ram_swap_and_a_pressure_level(self):
        m = system.memory()
        self.assertGreater(m["total_gb"], 0)
        self.assertTrue(0 <= m["free_pct"] <= 100, m)
        self.assertIn(m["pressure"], (1, 2, 4))
        self.assertGreaterEqual(m["swap_total_gb"], 0)

    def test_stop_ends_a_process_and_ignores_one_already_gone(self):
        c = self.child(SLEEPER)
        self.assertEqual(system.stop(c.pid), "")
        c.wait(timeout=10)
        self.assertEqual(system.stop(c.pid, hard=True), "")
        self.assertEqual(system.cwd_of(c.pid), "")


if __name__ == "__main__":
    unittest.main()
