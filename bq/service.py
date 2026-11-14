"""The user service install.sh / install.ps1 register: whether it starts with the system, and restarting it.

Starting with the system is the service manager's own switch, so a reinstall turns it back on:
launchd `enable`/`disable`, `systemctl --user enable`/`disable`, the logon trigger of the Windows task.
"""

import os
import subprocess
import sys
from pathlib import Path

from bq.settings import LABEL

TASK_PS = f"(Get-ScheduledTask -TaskName '{LABEL}' -ErrorAction Stop)"


def _run(cmd):
    """(exit code, stdout + stderr); a missing tool reads as a failure, never as an exception."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _ps(script):
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def _system():
    return "nt" if os.name == "nt" else sys.platform if sys.platform == "darwin" else "linux"


def installed():
    system = _system()
    if system == "darwin":
        return (Path.home() / f"Library/LaunchAgents/{LABEL}.plist").exists()
    if system == "linux":
        return (Path.home() / f".config/systemd/user/{LABEL}.service").exists()
    return _ps(TASK_PS + " | Out-Null")[0] == 0


def autostart():
    """True or False, or None when the service is not installed (a server started by hand)."""
    if not installed():
        return None
    system = _system()
    if system == "darwin":
        code, out = _run(["launchctl", "print-disabled", f"gui/{os.getuid()}"])
        line = next((ln for ln in out.splitlines() if f'"{LABEL}"' in ln), "")
        return not ("disabled" in line or "true" in line)  # macOS 13+ prints disabled/enabled, older true/false
    if system == "linux":
        return _run(["systemctl", "--user", "is-enabled", f"{LABEL}.service"])[1] == "enabled"
    code, out = _ps(TASK_PS + ".Triggers[0].Enabled")
    return out.lower() == "true" if code == 0 else None


def set_autostart(on):
    """Turns the start with the system on or off. Returns "" or the tool's error text."""
    system = _system()
    if system == "darwin":
        code, out = _run(["launchctl", "enable" if on else "disable", f"gui/{os.getuid()}/{LABEL}"])
    elif system == "linux":
        code, out = _run(["systemctl", "--user", "enable" if on else "disable", f"{LABEL}.service"])
    else:
        flag = "$true" if on else "$false"
        code, out = _ps(f"$t = {TASK_PS}; $t.Triggers[0].Enabled = {flag}; Set-ScheduledTask -InputObject $t | Out-Null")
    return "" if code == 0 else out or f"exit {code}"


def restart(argv):
    """Replaces this server with a fresh one that reads the saved settings. Does not return.

    POSIX: exec in place, so the pid launchd or systemd watches stays the same. Windows has no real exec: a
    detached helper starts the scheduled task again once this process is gone, or a detached copy when the
    server was started by hand.
    """
    sys.stderr.flush()
    if os.name != "nt":
        os.execv(sys.executable, [sys.executable, *argv])
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if installed():
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command",
                          f"Start-Sleep -Seconds 2; Start-ScheduledTask -TaskName '{LABEL}'"], creationflags=flags)
    else:
        subprocess.Popen([sys.executable, *argv], creationflags=flags)
    os._exit(0)
