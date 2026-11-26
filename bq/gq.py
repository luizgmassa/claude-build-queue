#!/usr/bin/env python3
"""gq: run a Gradle/JVM command only while holding a machine-wide build slot.

  gq run -- ./gradlew :shared:check     wait in FIFO order, run, release the slot on exit
  gq status                             show slots and queue
  gq enforce on|off                     turn the automatic cleanup kills on or off
  gq config max N|off                   cap the slots at N (at least 2), or no cap (default)
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ~/.local/bin/gq is a symlink to this file
from bq import remote, settings  # noqa: E402  (standard library only, like gq itself)

SETTINGS = settings.load()
URL = os.environ.get("BQ_URL", f"http://127.0.0.1:{SETTINGS['port']}")
CONFIG = settings.CONFIG
UNREACHABLE_LIMIT_S = SETTINGS["unreachable_limit_s"]
EX_TEMPFAIL = 75
BASE_SLOTS = SETTINGS["base_slots"]
# SIGALRM: a timeout wrapper outside gq (perl -e 'alarm N; exec @ARGV' gq run -- ...) would kill gq by default
# and leave the build running without a slot; it stops the child with SIGTERM instead
SIGNALS = [getattr(signal, n) for n in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK", "SIGALRM") if hasattr(signal, n)]


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


def run_nested(cmd):
    """A call inside a granted run takes the same slot, with no new ticket; returns only when the parent is not granted."""
    parent = os.environ.get("BQ_TICKET", "")
    if not parent.isdigit():
        return
    try:
        granted = call(f"/api/ticket?id={parent}", timeout=5).get("state") == "granted"
    except (urllib.error.URLError, OSError, ValueError):
        granted = False
    if not granted:
        return
    if os.name == "nt":  # Windows exec spawns and exits at once, so the caller would stop waiting
        sys.exit(subprocess.call(cmd))
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        say(f"cannot start {cmd[0]}: {e}")
        sys.exit(127)


def run_on_vm(j, tid, release, arm):
    """Carries out a grant for the build machine: exits with the remote code once the build ran there; returns when it
    could not start, after handing the ticket back to the queue (the server keeps its place)."""
    reason = remote.start(j, say)
    if reason is None:
        call_retrying("/api/remote", {"id": tid, "phase": "running"})
        code = remote.execute(j, say, SIGNALS)
        if code is not None:
            release(code)
            sys.exit(code)
        reason = "ssh"
    arm()  # execute left the default actions in place when ssh did not start
    call_retrying("/api/remote", {"id": tid, "phase": "failed", "reason": reason})


def run(cmd):
    if os.name == "nt":
        cmd = windows_program(cmd)
    run_nested(cmd)
    job = remote.plan(cmd, say)
    tid = call_retrying("/api/enqueue", {"pid": os.getpid(), "cwd": os.getcwd(), "cmd": " ".join(cmd),
                                         "remote": job is not None, **agent_identity()})["id"]
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

    def arm():
        for s in SIGNALS:
            signal.signal(s, abort)

    arm()
    last, started = None, time.time()
    while True:
        st = call_retrying(f"/api/wait?id={tid}&timeout=25")
        if st["state"] == "granted" and st.get("dest") == "vm" and job is not None:
            say(f"ticket #{tid}: slot granted on the build machine after {int(time.time() - started)}s")
            run_on_vm(job, tid, release, arm)
            continue  # back to waiting, in the same place
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
            child.send_signal(signal.SIGTERM if signum == getattr(signal, "SIGALRM", None) else signum)
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
    print(f"slots {v['used']} in use (base {v.get('base_slots')}, max {v.get('max_slots') or 'off'}, "
          f"avail {v.get('avail_gb')} GB, reserved {v.get('reserve_gb')} GB, "
          f"swap +{v.get('swap_growth_gb')} GB/{v.get('swap_window_s')}s)  runner={'busy' if v['counts'].get('runner') else 'idle'}  "
          f"enforce={'on' if v['enforce'] else 'off'}  pressure={m['pressure']}  free={m['free_pct']}%  "
          f"swap={m['swap_used_gb']}/{m['swap_total_gb']} GB")
    if v["blocked"]:
        print(f"blocked: {v['blocked']}")
    for t in v["tickets"]:
        if t["state"] != "done":
            ended = " (ended)" if t.get("agent_alive") is False else ""
            print(f"#{t['id']} {t['state']:8} {t['agent']}{ended}  {t['cmd'][:90]}")


def set_config(key, value):
    """Changes one key of config.json, keeping the others, and wakes the server to apply it."""
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    if value is None:
        cfg.pop(key, None)
    else:
        cfg[key] = value
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1))
    try:
        call("/api/wake", timeout=5)
    except (urllib.error.URLError, OSError, ValueError):
        pass


def enforce(flag):
    set_config("enforce", flag == "on")
    print(f"enforce {flag}")


def config_max(value):
    if value == "off":
        set_config("max", None)
        return print("max off")
    _n, err = settings.check("max", int(value) if value.isdigit() else value, BASE_SLOTS)
    if err:
        high = settings.SPEC["max"][4]
        sys.exit(f"max must be off or a whole number from {BASE_SLOTS} to {high}")
    set_config("max", int(value))
    print(f"max {int(value)}")


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
    elif argv[:2] == ["config", "max"] and len(argv) == 3:
        return config_max(argv[2])
    print(__doc__, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
