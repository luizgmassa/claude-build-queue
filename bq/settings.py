"""Every setting of the build queue: default, bounds and validation. Values live in config.json.

Standard library only, because gq and the hook import it too. Only the keys that differ from their default are
stored, so a later default change still reaches a machine that never touched that key. `enforce` and `max` are
read on every scheduler tick; every other key applies when the server (re)starts.
"""

import json
import os
from pathlib import Path

DATA = Path(os.environ.get("BQ_DATA", Path.home() / ".local/share/build-queue"))
CONFIG = DATA / "config.json"
LABEL = "com.massa.build-queue"  # the service name install.sh and install.ps1 register

# Starting estimates (GB of the whole build: client, daemon, workers, Kotlin daemon) before any measurement.
DEFAULT_GB = {"gradle-check": 8.0, "gradle-backend": 6.0, "gradle-ios": 6.0, "gradle-test": 5.0,
              "gradle-lint": 3.0, "gradle": 4.0, "xcodebuild": 6.0, "java": 2.0, "pod": 1.0, "uv": 1.0,
              "node": 3.0, "cc": 4.0, "bazel": 6.0, "dotnet": 3.0, "msbuild": 4.0, "swift": 4.0, "cargo": 4.0,
              "go": 2.0, "maven": 3.0, "sbt": 4.0, "other": 4.0}
CTYPES = tuple(DEFAULT_GB)

# key -> (group, default, kind, low, high). The page builds its advanced form from this table, in this order.
SPEC = {
    "base_slots": ("queue", 2, "int", 1, 16),  # runner included: these never wait on a memory check
    "max": ("queue", None, "int_or_off", 1, 64),  # off = no ceiling; at least base_slots
    "headroom_gb": ("queue", 6.0, "float", 0.0, 512.0),  # left after peak reserves and the next estimate
    "extra_gap_s": ("queue", 30, "int", 0, 3600),  # a new build needs time to show its memory
    "swap_growth_gb": ("queue", 0.5, "float", 0.05, 64.0),  # growth within swap_window_s that stops extras
    "swap_window_s": ("queue", 60, "int", 10, 3600),
    "peak_history": ("queue", 5, "int", 1, 100),  # estimate = largest of the last N peaks of the type
    "runner_ctype": ("queue", "gradle-check", "ctype", None, None),  # the runner's reserve uses this estimate
    "call_window_s": ("queue", 60, "int", 5, 3600),  # a `gq run` reaches the server this soon after the hook
    "block_pressure": ("memory", 4, "choice", (2, 4), None),  # macOS kernel level that blocks: 2 warn, 4 critical
    "psi_critical_pct": ("memory", 10.0, "float", 0.1, 100.0),  # Linux PSI full avg10
    "low_memory_pct": ("memory", 5, "int", 1, 50),  # no kernel signal: available share of RAM below this
    "enforce": ("cleanup", False, "bool", None, None),
    "idle_grace_s": ("cleanup", 20, "int", 1, 3600),
    "kotlin_idle_s": ("cleanup", 60, "int", 1, 3600),
    "grant_quiet_s": ("cleanup", 20, "int", 0, 3600),
    "term_grace_s": ("cleanup", 10, "int", 1, 600),
    "protected_paths": ("cleanup", [], "list", None, None),  # a process whose command line has one: never touched
    "runner_markers": ("cleanup", ["/actions-runner/"], "list", None, None),  # ...counts as the CI runner
    "default_gb": ("estimates", DEFAULT_GB, "gb_table", 0.1, 512.0),
    "port": ("server", 8787, "int", 1024, 65535),
    "tick_s": ("server", 2.0, "float", 0.5, 30.0),
    "unreachable_limit_s": ("server", 60, "int", 10, 3600),  # gq gives up after the server is gone this long
}
LIST_MAX = 50
ITEM_MAX = 200


def _err(key, **params):
    return {"key": key, "params": params}


def _is_number(value, kind):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return kind != "int" or isinstance(value, int)


def _number(value, kind, low, high):
    if not _is_number(value, kind):
        return None, _err("config.type")
    if not low <= value <= high:
        return None, _err("config.range", min=low, max=high)
    return (float(value) if kind == "float" else value), None


def _check_number(value, spec, _base):
    return _number(value, spec[2], spec[3], spec[4])


def _check_bool(value, _spec, _base):
    return (value, None) if isinstance(value, bool) else (None, _err("config.type"))


def _check_int_or_off(value, spec, base):
    if value is None:
        return None, None
    n, err = _number(value, "int", spec[3], spec[4])
    if err:
        return None, err
    if base is not None and n < base:
        return None, _err("config.max_below_base", base=base)
    return n, None


def _check_ctype(value, _spec, _base):
    return (value, None) if value in CTYPES else (None, _err("config.choice"))


def _check_choice(value, spec, _base):
    ok = value in spec[3] and not isinstance(value, bool)
    return (value, None) if ok else (None, _err("config.choice"))


def _list_item_ok(item):
    return isinstance(item, str) and 0 < len(item.strip()) <= ITEM_MAX


def _check_list(value, _spec, _base):
    if not isinstance(value, list) or len(value) > LIST_MAX or not all(map(_list_item_ok, value)):
        return None, _err("config.list", n=LIST_MAX, chars=ITEM_MAX)
    return [v.strip() for v in value], None


def _check_gb_table(value, spec, _base):
    if not isinstance(value, dict) or not set(value) <= set(CTYPES):
        return None, _err("config.type")
    out = dict(spec[1])
    for ctype, gb in value.items():
        out[ctype], err = _number(gb, "float", spec[3], spec[4])
        if err:
            return None, err
    return out, None


CHECKS = {"bool": _check_bool, "int": _check_number, "float": _check_number, "int_or_off": _check_int_or_off,
          "ctype": _check_ctype, "choice": _check_choice, "list": _check_list, "gb_table": _check_gb_table}


def check(key, value, base_slots=None):
    """(clean value, None) or (None, error message) for one setting."""
    spec = SPEC[key]
    return CHECKS[spec[2]](value, spec, base_slots)


def defaults():
    return {k: (dict(s[1]) if isinstance(s[1], dict) else list(s[1]) if isinstance(s[1], list) else s[1])
            for k, s in SPEC.items()}


def read_raw():
    try:
        raw = json.loads(CONFIG.read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def load():
    """Every setting: the stored value where it is valid, the default otherwise (a bad file never stops the server)."""
    raw, out = read_raw(), defaults()
    base, err = check("base_slots", raw.get("base_slots", out["base_slots"]))
    out["base_slots"] = out["base_slots"] if err else base
    for key in SPEC:
        if key in raw and key != "base_slots":
            value, err = check(key, raw[key], out["base_slots"])
            if not err:
                out[key] = value
    return out


def validate(values):
    """(complete clean settings, {key: error message}) for a full or partial set of values over the stored ones."""
    merged = {**load(), **{k: v for k, v in values.items() if k in SPEC}}
    clean, errors = {}, {}
    for key in ("base_slots", *[k for k in SPEC if k != "base_slots"]):
        value, err = check(key, merged[key], clean.get("base_slots"))
        if err:
            errors[key] = err
        else:
            clean[key] = value
    unknown = set(values) - set(SPEC)
    for key in unknown:
        errors[key] = _err("config.unknown")
    return clean, errors


def save(clean):
    """Writes the values that differ from their default, keeping keys this version does not know."""
    base = defaults()
    raw = {k: v for k, v in read_raw().items() if k not in SPEC}
    raw.update({k: v for k, v in clean.items() if v != base[k]})
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=1))
    os.replace(tmp, CONFIG)  # a reader never sees half a file
