#!/usr/bin/env python3
"""gq: run a Gradle/JVM command only while holding a machine-wide build slot.

  gq run -- ./gradlew :shared:check     wait in FIFO order, run, release the slot on exit
  gq status                             show slots and queue
  gq enforce on|off                     turn the automatic cleanup kills on or off
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = os.environ.get("BQ_URL", "http://127.0.0.1:8787")
CONFIG = Path(os.environ.get("BQ_DATA", Path.home() / ".local/share/build-queue")) / "config.json"
UNREACHABLE_LIMIT_S = 60
EX_TEMPFAIL = 75
SIGNALS = [getattr(signal, n) for n in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK") if hasattr(signal, n)]


def say(msg):
    print(f"[gq] {msg}", file=sys.stderr, flush=True)


def call(path, body=None, timeout=40):
    req = urllib.request.Request(URL + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def call_retrying(path, body=None, timeout=40):
    first_failure = None
    while True:
        try:
            return call(path, body, timeout)
        except (urllib.error.URLError, OSError, ValueError) as e:
            first_failure = first_failure or time.time()
            if time.time() - first_failure > UNREACHABLE_LIMIT_S:
                say(f"build-queue server unreachable at {URL} for {UNREACHABLE_LIMIT_S}s ({e}); "
                    "report this to the user, do not run the build outside the queue")
                sys.exit(EX_TEMPFAIL)
            time.sleep(3)


def agent_identity():
    ralphy = re.search(r"/sessions/(\d+)\.agent-status", os.environ.get("RALPHY_STATUS_FILE", ""))
    cp = os.environ.get("CLAUDE_PID", "")
    return {"claude_pid": int(cp) if cp.isdigit() else None, "ralphy": ralphy.group(1) if ralphy else None,
            "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID")}


def windows_program(cmd):
    """CreateProcess runs neither the `gradlew` shell script nor a `.bat` it has to find on PATH."""
    name = cmd[0]
    if not os.path.splitext(name)[1]:
        name = next((name + e for e in (".bat", ".cmd", ".exe") if os.path.isfile(name + e)), name)
    return [shutil.which(name) or name] + cmd[1:]


def run(cmd):
    if os.name == "nt":
        cmd = windows_program(cmd)
    parent = os.environ.get("BQ_TICKET", "")
    if parent.isdigit():
        try:
            granted = call(f"/api/ticket?id={parent}", timeout=5).get("state") == "granted"
        except (urllib.error.URLError, OSError, ValueError):
            granted = False
        if granted:  # nested call inside a granted run: same slot, no new ticket
            if os.name == "nt":  # Windows exec spawns and exits at once, so the caller would stop waiting
                sys.exit(subprocess.call(cmd))
            try:
                os.execvp(cmd[0], cmd)
            except OSError as e:
                say(f"cannot start {cmd[0]}: {e}")
                sys.exit(127)
    tid = call_retrying("/api/enqueue", {"pid": os.getpid(), "cwd": os.getcwd(), "cmd": " ".join(cmd),
                                         **agent_identity()})["id"]
    released = False

    def release(code):
        nonlocal released
        if not released:
            released = True
            try:
                call("/api/release", {"id": tid, "exit_code": code}, timeout=5)
            except (urllib.error.URLError, OSError, ValueError):
                pass  # the server reaps the ticket once this pid is gone

    def abort(signum, _frame):
        release(128 + signum)
        sys.exit(128 + signum)

    for s in SIGNALS:
        signal.signal(s, abort)

    last, started = None, time.time()
    while True:
        st = call_retrying(f"/api/wait?id={tid}&timeout=25")
        if st["state"] == "granted":
            break
        if st["state"] != "waiting":
            say(f"ticket #{tid} ended before getting a slot: {st.get('reason')}")
            sys.exit(EX_TEMPFAIL)
        msg = f"ticket #{tid}: position {st['position']} of {st['waiting']} waiting; {st.get('blocked') or 'next'}"
        if msg != last:
            say(msg + f" ({int(time.time() - started)}s)")
            last = msg
    say(f"ticket #{tid}: slot granted after {int(time.time() - started)}s; running")

    try:
        child = subprocess.Popen(cmd, env={**os.environ, "BQ_TICKET": str(tid)})
    except OSError as e:
        say(f"cannot start {cmd[0]}: {e}")
        release(127)
        sys.exit(127)

    def forward(signum, _frame):
        if os.name == "nt" and signum != signal.SIGTERM:
            return  # Ctrl+C and Ctrl+Break already reach the child through the shared console
        try:
            child.send_signal(signum)
        except (ProcessLookupError, OSError):
            pass

    for s in SIGNALS:
        signal.signal(s, forward)
    code = child.wait()
    code = code if code >= 0 else 128 - code
    release(code)
    sys.exit(code)


def status():
    for _ in range(20):  # the view is empty until the server's first tick
        v = call("/api/state", timeout=5)
        if v:
            break
        time.sleep(0.5)
    else:
        sys.exit("build-queue server is up but has not finished its first scan yet")
    m = v["memory"]
    print(f"slots {v['used']}/{v['slots']} (base {v.get('base_slots')}, max {v.get('max_slots')}, "
          f"avail {v.get('avail_gb')} GB)  runner={'busy' if v['counts'].get('runner') else 'idle'}  "
          f"enforce={'on' if v['enforce'] else 'off'}  pressure={m['pressure']}  free={m['free_pct']}%  "
          f"swap={m['swap_used_gb']}/{m['swap_total_gb']} GB")
    if v["blocked"]:
        print(f"blocked: {v['blocked']}")
    for t in v["tickets"]:
        if t["state"] != "done":
            print(f"#{t['id']} {t['state']:8} {t['agent']}  {t['cmd'][:90]}")


def enforce(flag):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"enforce": flag == "on"}, indent=1))
    try:
        call("/api/wake", timeout=5)
    except (urllib.error.URLError, OSError, ValueError):
        pass
    print(f"enforce {flag}")


def main(argv):
    if argv[:1] == ["run"]:
        cmd = argv[1:]
        if cmd[:1] == ["--"]:
            cmd = cmd[1:]
        if cmd:
            return run(cmd)
    elif argv[:1] == ["status"]:
        return status()
    elif argv[:1] == ["enforce"] and argv[1:2] in (["on"], ["off"]):
        return enforce(argv[1])
    print(__doc__, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
