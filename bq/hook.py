#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny Bash calls that run Gradle/JVM work outside `gq run`."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bq.core import analyze_command  # noqa: E402

PAGE = "http://127.0.0.1:8787"


def decide(tool_input):
    cmd = tool_input.get("command") or ""
    offending, uses_gq = analyze_command(cmd)
    if offending:
        return ("Gradle/JVM/xcodebuild/pod install/uv run must go through the machine-wide build queue "
                "(max 2 concurrent runs, FIFO). Prefix each such command with `gq run --`, e.g. "
                "`cd ../my-worktree && gq run -- ./gradlew :shared:check`, and call Bash with "
                "run_in_background: true. gq waits for a slot, runs, and frees the slot on exit. "
                f"Offending: {offending}. Queue: {PAGE}")
    if uses_gq and not tool_input.get("run_in_background"):
        return ("`gq run` may wait minutes for a free slot; call Bash with run_in_background: true "
                "and read the output when the task-completion notification arrives.")
    return None


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") != "Bash":
            return
        reason = decide(data.get("tool_input") or {})
    except Exception:  # a hook bug must never block unrelated work; the server's cleanup still applies
        return
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
