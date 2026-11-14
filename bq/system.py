"""What the server reads from and does to the operating system: processes, TCP links, memory, kills.

psutil covers macOS, Linux and Windows. The memory pressure signal is the one thing each system names
differently, so it has one small reader per system.
"""

import ctypes
import functools
import os
import re
import subprocess
import sys
import time

import psutil

from bq import core

PRESSURE_NORMAL = 1
PSI_CRITICAL_PCT = core.SETTINGS["psi_critical_pct"]  # Linux: share of the last 10 s every runnable task waited
LOW_MEMORY_PCT = core.SETTINGS["low_memory_pct"]  # no kernel signal: available memory below this share is critical
ATTRS = ["pid", "ppid", "name", "cmdline", "environ", "memory_info", "cpu_percent", "create_time", "status"]


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, file=sys.stderr, flush=True)


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError) as e:
        log(f"command failed: {cmd[0]}: {e}")
        return ""


def lstart(ts):
    """Start time in the `ps -o lstart` format, so pid_start values stored before psutil still match."""
    t = time.localtime(int(ts))
    return time.strftime(f"%a %b {t.tm_mday} %H:%M:%S %Y", t)


def etime(seconds):
    """Elapsed time in the `ps -o etime` format the page parses: [[dd-]hh:]mm:ss."""
    d, s = divmod(max(0, int(seconds)), 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    out = f"{h:02d}:{m:02d}:{s:02d}" if h or d else f"{m:02d}:{s:02d}"
    return f"{d}-{out}" if d else out


def snapshot_procs():
    now, procs, starts = time.time(), {}, {}
    for p in psutil.process_iter(ATTRS, ad_value=None):
        i = p.info
        if i["status"] == psutil.STATUS_ZOMBIE or i["create_time"] is None:  # a zombie is dead already
            continue
        args = " ".join(i["cmdline"] or []) or i["name"] or ""
        if os.name == "nt":
            args = args.replace("\\", "/")  # one separator for every path rule in core
        env = {k: v for k, v in (i["environ"] or {}).items() if k in core.ENV_KEYS}
        mem = i["memory_info"]
        procs[i["pid"]] = core.Proc(pid=i["pid"], ppid=i["ppid"] or 0, rss_kb=mem.rss // 1024 if mem else 0,
                                    cpu=round(i["cpu_percent"] or 0.0, 1), etime=etime(now - i["create_time"]),
                                    lstart=lstart(i["create_time"]), args=args, env=env)
        starts[i["pid"]] = i["create_time"]
    for p in procs.values():  # Windows keeps a dead parent's pid, which a later process may reuse
        if starts.get(p.ppid, 0) > starts[p.pid]:
            p.ppid = 0
    return procs


def tcp_links(procs, names=("java",)):
    """Returns (listen ports by pid, established (pid, local port, remote port) triples) for java processes."""
    listen, est = {}, []
    for pid, proc in procs.items():
        if core.exe_name(proc.args.split(" ", 1)[0]) not in names:
            continue
        try:
            conns = psutil.Process(pid).net_connections(kind="tcp")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for c in conns:
            if c.status == psutil.CONN_LISTEN:
                listen.setdefault(pid, set()).add(c.laddr.port)
            elif c.status == psutil.CONN_ESTABLISHED and c.raddr:
                est.append((pid, c.laddr.port, c.raddr.port))
    return listen, est


def memory():
    vm, sw, gb = psutil.virtual_memory(), psutil.swap_memory(), 1024 ** 3
    out = {"total_gb": round(vm.total / gb, 1), "swap_used_gb": round(sw.used / gb, 1),
           "swap_total_gb": round(sw.total / gb, 1)}
    if sys.platform == "darwin":
        out.update(macos_memory())
        return out
    free_pct = int(100 * vm.available / vm.total) if vm.total else 0
    out.update(free_pct=free_pct, pressure=pressure(free_pct), app_gb=round(vm.used / gb, 1),
               wired_gb=None, compressed_gb=None, free_gb=round(vm.free / gb, 1),
               cached_gb=round((getattr(vm, "cached", 0) + getattr(vm, "buffers", 0)) / gb, 1))
    return out


def macos_memory():
    names = ["kern.memorystatus_vm_pressure_level", "kern.memorystatus_level", "hw.pagesize"]
    vals = {}
    for line in run(["sysctl"] + names).splitlines():
        k, _, v = line.partition(":")
        vals[k.strip()] = v.strip()
    page = int(vals.get("hw.pagesize", "16384") or 16384)
    vm = {}
    for line in run(["vm_stat"]).splitlines():
        m = re.match(r'"?([^":]+)"?:\s+(\d+)', line)
        if m:
            vm[m.group(1).strip()] = int(m.group(2)) * page
    gb = 1024 ** 3
    return {
        "app_gb": round((vm.get("Anonymous pages", 0) - vm.get("Pages purgeable", 0)) / gb, 1),
        "wired_gb": round(vm.get("Pages wired down", 0) / gb, 1),
        "compressed_gb": round(vm.get("Pages occupied by compressor", 0) / gb, 1),
        "cached_gb": round(vm.get("File-backed pages", 0) / gb, 1),
        "free_gb": round(vm.get("Pages free", 0) / gb, 1),
        "free_pct": int(vals.get("kern.memorystatus_level", "0") or 0),
        "pressure": int(vals.get("kern.memorystatus_vm_pressure_level", "1") or 1),
    }


@functools.cache
def low_memory_handle():
    """Windows: the kernel's own low-memory event, signaled when available memory is nearly gone."""
    k = ctypes.windll.kernel32
    k.CreateMemoryResourceNotification.restype = ctypes.c_void_p
    k.QueryMemoryResourceNotification.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    return k.CreateMemoryResourceNotification(0)  # 0 = LowMemoryResourceNotification


def pressure(free_pct):
    """1 = normal, 4 = critical, on the macOS scale (warn is never reported: it does not block)."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/pressure/memory") as f:
                full = float(re.search(r"^full avg10=([\d.]+)", f.read(), re.M).group(1))
            return core.PRESSURE_CRITICAL if full >= PSI_CRITICAL_PCT else PRESSURE_NORMAL
        except (OSError, AttributeError, ValueError):
            pass  # kernel without PSI: fall back to the available share
    elif os.name == "nt" and low_memory_handle():
        low = ctypes.c_int()
        if ctypes.windll.kernel32.QueryMemoryResourceNotification(low_memory_handle(), ctypes.byref(low)):
            return core.PRESSURE_CRITICAL if low.value else PRESSURE_NORMAL
    return core.PRESSURE_CRITICAL if free_pct < LOW_MEMORY_PCT else PRESSURE_NORMAL


def cwd_of(pid):
    try:
        return psutil.Process(pid).cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return ""


def stop(pid, hard=False):
    """SIGTERM, or SIGKILL when hard. Windows has no graceful stop: both end the process at once.

    Returns an error message (see core.EN), or "" (also when the process is already gone)."""
    try:
        p = psutil.Process(pid)
        p.kill() if hard else p.terminate()
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied as e:
        return core.msg("error.no_permission", pid=pid, detail=str(e))
    return ""
