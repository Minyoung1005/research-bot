#!/usr/bin/env python3
"""
Watch all active Claude Code sessions in real time.
Auto-creates tmux panes for each session, closes them when done.

Usage:
    python watch_claude.py          # watch all sessions
    python watch_claude.py <file>   # watch a specific output file
"""

import sys
import os
import json
import time
import re
import subprocess
import threading
import glob

DONE_MARKER  = "___CLAUDE_DONE___"
OUTPUT_GLOB  = "/tmp/claude_out_*.txt"
WATCH_SESSION = "claude-watch"
POLL_INTERVAL = 0.15
FILE_GONE_GRACE = 3.0       # seconds to wait before assuming a vanished file = done
JANITOR_INTERVAL = 5.0      # seconds between orphan-pane sweeps

# { filepath: pane_id }
watched = {}
watched_lock = threading.Lock()
welcome_pane_id = None      # the initial placeholder pane, never killed


def get_machine_from_file(filepath):
    """Try to extract machine name from output file content (first JSON line)."""
    try:
        with open(filepath, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "system" and obj.get("subtype") == "init":
                        return obj.get("cwd", "").split("/")[-1] or "unknown"
                except Exception:
                    pass
    except Exception:
        pass
    # Fall back to session id from filename
    m = re.search(r"claude_out_(claude-[\w_]+)\.txt", filepath)
    return m.group(1)[-8:] if m else os.path.basename(filepath)


def format_event(obj, label):
    t = obj.get("type", "")
    out = []

    if t == "assistant":
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") == "text":
                out.append(f"\033[37m{block['text']}\033[0m")

    elif t == "tool_use":
        name = obj.get("name", "tool")
        inp = obj.get("input", {})
        cmd = inp.get("command", inp.get("path", inp.get("prompt", str(inp))[:80]))
        out.append(f"\033[36m[{name}]\033[0m {cmd}")

    elif t == "tool_result":
        for block in obj.get("content", []):
            if block.get("type") == "text" and block["text"].strip():
                truncated = block["text"].strip()[:300]
                out.append(f"\033[90m{truncated}\033[0m")

    elif t == "system" and obj.get("subtype") == "init":
        model = obj.get("model", "")
        out.append(f"\033[33m[{label}] started • {model}\033[0m")

    return "\n".join(out)


def stream_file(filepath, pane_id, label):
    """Stream a single output file into a tmux pane."""
    seen_bytes = 0
    buf = ""
    file_seen = False
    missing_since = None

    while True:
        try:
            if not os.path.exists(filepath):
                # bot.py removes the output file on success, timeout, or error.
                # If we ever saw it, treat its disappearance as "done" after a grace period.
                if file_seen:
                    if missing_since is None:
                        missing_since = time.time()
                    elif time.time() - missing_since > FILE_GONE_GRACE:
                        send_to_pane(pane_id, f"\n\033[32m[{label}] ✅ done (file removed)\033[0m\n")
                        time.sleep(1)
                        close_pane(pane_id, filepath)
                        return
                time.sleep(0.5)
                continue

            file_seen = True
            missing_since = None

            with open(filepath, "r", errors="replace") as f:
                f.seek(seen_bytes)
                chunk = f.read()
                if chunk:
                    seen_bytes += len(chunk.encode("utf-8", errors="replace"))
                    buf += chunk
                    lines = buf.split("\n")
                    buf = lines[-1]

                    for line in lines[:-1]:
                        line = line.strip()
                        if not line:
                            continue
                        if DONE_MARKER in line:
                            send_to_pane(pane_id, f"\n\033[32m[{label}] ✅ done\033[0m\n")
                            time.sleep(2)
                            close_pane(pane_id, filepath)
                            return
                        try:
                            obj = json.loads(line)
                            text = format_event(obj, label)
                            if text:
                                send_to_pane(pane_id, text + "\n")
                        except json.JSONDecodeError:
                            # Non-JSON line (e.g. Codex CLI plain-text output) — show as-is
                            send_to_pane(pane_id, line + "\n")

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            return
        except Exception:
            time.sleep(0.5)


def send_to_pane(pane_id, text):
    """Print text directly into a tmux pane."""
    # Use tmux display-message to write to pane
    escaped = text.replace("'", "'\\''")
    subprocess.run(
        ["tmux", "send-keys", "-t", pane_id, f"printf '%b' '{escaped}'", "Enter"],
        capture_output=True
    )


def close_pane(pane_id, filepath):
    with watched_lock:
        watched.pop(filepath, None)
    subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)
    subprocess.run(["tmux", "select-layout", "-t", WATCH_SESSION, "tiled"],
                   capture_output=True)


def list_session_panes():
    """Return list of pane_ids currently in the watch session."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", WATCH_SESSION, "-F", "#{pane_id}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [p for p in result.stdout.strip().split("\n") if p]


def janitor_sweep():
    """Kill any pane in the watch session that isn't actively tracked.
    Catches orphans whose stream_file thread died, or panes from prior runs."""
    global welcome_pane_id
    with watched_lock:
        live_panes = set(watched.values())
    panes = list_session_panes()
    if not panes:
        return
    # Treat the first remaining pane as welcome if we lost the reference (e.g. after restart).
    if welcome_pane_id is None or welcome_pane_id not in panes:
        welcome_pane_id = panes[0]
    killed = 0
    for p in panes:
        if p == welcome_pane_id:
            continue
        if p in live_panes:
            continue
        subprocess.run(["tmux", "kill-pane", "-t", p], capture_output=True)
        killed += 1
    if killed:
        subprocess.run(["tmux", "select-layout", "-t", WATCH_SESSION, "tiled"],
                       capture_output=True)


def ensure_watch_session():
    """Create the watch tmux session if it doesn't exist."""
    global welcome_pane_id
    result = subprocess.run(
        ["tmux", "has-session", "-t", WATCH_SESSION], capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(["tmux", "new-session", "-d", "-s", WATCH_SESSION, "-x", "220", "-y", "50"])
        # Clear the default pane
        subprocess.run(["tmux", "send-keys", "-t", f"{WATCH_SESSION}:0.0",
                        "clear && echo 'Claude Watch — waiting for sessions...'", "Enter"])
    # Remember the first existing pane as the persistent welcome pane (never killed).
    panes = list_session_panes()
    if panes:
        welcome_pane_id = panes[0]


def add_pane_for_file(filepath):
    """Add a new tmux pane for a file and start streaming."""
    label = get_machine_from_file(filepath)

    with watched_lock:
        if filepath in watched:
            return
        # Split window to create new pane
        result = subprocess.run(
            ["tmux", "split-window", "-t", WATCH_SESSION, "-h", "-d", "bash"],
            capture_output=True, text=True
        )
        # Get the new pane id
        panes = subprocess.run(
            ["tmux", "list-panes", "-t", WATCH_SESSION, "-F", "#{pane_id}"],
            capture_output=True, text=True
        ).stdout.strip().split("\n")
        pane_id = panes[-1] if panes else f"{WATCH_SESSION}:0.0"

        # Set pane title
        subprocess.run(["tmux", "select-pane", "-t", pane_id, "-T", label], capture_output=True)

        watched[filepath] = pane_id

    # Clear pane and show header
    subprocess.run(["tmux", "send-keys", "-t", pane_id,
                    f"clear && echo '\\033[33m=== {label} ===\\033[0m'", "Enter"],
                   capture_output=True)

    # Stream in background thread
    t = threading.Thread(target=stream_file, args=(filepath, pane_id, label), daemon=True)
    t.start()

    # Re-tile panes evenly (tiled handles many panes better than even-horizontal)
    subprocess.run(["tmux", "select-layout", "-t", WATCH_SESSION, "tiled"],
                   capture_output=True)


def watch_single_file(filepath):
    """Simple single-file mode (no tmux management)."""
    label = os.path.basename(filepath)
    seen_bytes = 0
    buf = ""
    print(f"Watching {filepath} ... (Ctrl+C to stop)\n")

    while True:
        try:
            if not os.path.exists(filepath):
                time.sleep(0.5)
                continue
            with open(filepath, "r", errors="replace") as f:
                f.seek(seen_bytes)
                chunk = f.read()
                if chunk:
                    seen_bytes += len(chunk.encode("utf-8", errors="replace"))
                    buf += chunk
                    lines = buf.split("\n")
                    buf = lines[-1]
                    for line in lines[:-1]:
                        line = line.strip()
                        if not line or DONE_MARKER in line:
                            if DONE_MARKER in line:
                                print(f"\n\033[32m[done]\033[0m\n")
                            continue
                        try:
                            text = format_event(json.loads(line), label)
                            if text:
                                print(text, flush=True)
                        except json.JSONDecodeError:
                            # Non-JSON line (e.g. Codex CLI plain-text output) — print as-is
                            print(line, flush=True)
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n[stopped]")
            sys.exit(0)


def main():
    # Single file mode
    if len(sys.argv) > 1:
        watch_single_file(sys.argv[1])
        return

    # Multi-session tmux mode
    if not os.environ.get("TMUX"):
        print("Not inside tmux. Launching watch session...")
        os.execvp("tmux", ["tmux", "new-session", "-A", "-s", WATCH_SESSION,
                            "python", __file__])
        return

    ensure_watch_session()
    print(f"Claude Watch started. Monitoring {OUTPUT_GLOB}")
    print("New sessions will appear automatically as panes.\n")

    last_janitor = 0.0
    try:
        while True:
            # Discover new output files
            files = glob.glob(OUTPUT_GLOB)
            with watched_lock:
                new_files = [f for f in files if f not in watched]

            for f in new_files:
                add_pane_for_file(f)

            # Periodically reap orphan panes (stream_file thread crashed, or leftovers from prior run)
            now = time.time()
            if now - last_janitor > JANITOR_INTERVAL:
                janitor_sweep()
                last_janitor = now

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[stopped]")


if __name__ == "__main__":
    main()
