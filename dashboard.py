"""
Local web dashboard for research-bot.

Runs as its OWN process (`python dashboard.py`) — fully decoupled from bot.py so
you can restart/iterate on it without ever touching the bot. It shares state with
the bot purely through files on disk, the same way active_runs.json already works:

  reads   active_runs.json          -> which runs are live (written by bot.py)
          threads/<key>.json        -> persisted per-thread cards
          /tmp/claude_out_*.txt      -> live output tail of each run
  writes  cancel/<key>.req           -> an "End this thread" request

The bot's record_thread()/extract_*() helpers in this module are plain file I/O,
so bot.py imports and calls them directly (no running server required). The bot
runs a tiny watcher that picks up cancel/*.req and performs the actual kill.

Per-thread cards show: status (running Xm / idle / done), a progress bar from the
latest TodoWrite in the stream-json (free, real-time), the current activity + a
live tail, a persisted one-line summary, and an [End] button.
"""

import json
import os
import re
import threading
import time

from flask import Flask, Response, jsonify, request

import history  # shared SQLite conversation store (read-only here)

# ---------------------------------------------------------------------------
# Paths (shared with bot.py by living in the same directory)
# ---------------------------------------------------------------------------

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIVE_RUNS_FILE = os.path.join(BOT_DIR, "active_runs.json")
THREADS_DIR = os.path.join(BOT_DIR, "threads")
CANCEL_DIR = os.path.join(BOT_DIR, "cancel")
CONTEXT_DIR = os.path.join(BOT_DIR, "contexts")
WORK_DIR = os.environ.get("WORK_DIR", os.path.dirname(BOT_DIR))
# Fast/cheap model for the one-line card summaries (override in .env if desired).
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "haiku")
SUMMARY_INTERVAL = int(os.environ.get("DASHBOARD_SUMMARY_SEC", "60"))
SUMMARY_MAX_PER_CYCLE = 15
# Reconciliation: Slack is the source of truth for "alive". A thread shows iff its
# root message carries this reaction; we re-check periodically so missed add/remove
# events self-heal.
ALIVE_REACTION = os.environ.get("ALIVE_REACTION", "eye")
RECONCILE_INTERVAL = int(os.environ.get("DASHBOARD_RECONCILE_SEC", "45"))
RECONCILE_MAX_AGE = 3 * 86400   # don't poll threads untouched for >3 days
RECONCILE_MAX_THREADS = 50      # cap Slack calls per cycle
# Overall channel summary (bullets referencing panels by index), regenerated when the
# set of threads / their summaries change.
OVERVIEW_INTERVAL = int(os.environ.get("DASHBOARD_OVERVIEW_SEC", "90"))
# Auto-end: threads with no activity for this many hours are ended automatically
# (same path as the End button). 0 disables.
AUTO_END_HOURS = float(os.environ.get("DASHBOARD_AUTO_END_HOURS", "24"))
AUTO_END_CHECK_SEC = 600
_overview_cache = {"text": "", "sig": "", "at": 0}

os.makedirs(THREADS_DIR, exist_ok=True)
os.makedirs(CANCEL_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _load_active_runs():
    try:
        with open(ACTIVE_RUNS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _thread_key(channel_id, thread_ts):
    return f"{channel_id}:{thread_ts}"


def _thread_file(channel_id, thread_ts):
    return os.path.join(THREADS_DIR, _thread_key(channel_id, thread_ts) + ".json")


def _read_tail(path, max_bytes=262144):
    """Read the last max_bytes of a file as text (dropping a leading partial line)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "ignore")
        if size > max_bytes and "\n" in data:
            data = data.split("\n", 1)[1]
        return data
    except FileNotFoundError:
        return ""


def _clean_title(command):
    """First human-meaningful line of a command, minus the auto-appended upload note."""
    text = command.split("\n\nThe following files have been uploaded")[0]
    text = text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    text = text.strip().splitlines()[0] if text.strip() else ""
    return text[:140]


# ---------------------------------------------------------------------------
# Stream-json extraction (todos + recent text)
# ---------------------------------------------------------------------------

def extract_todos(raw):
    """Return the latest TodoWrite list as [{content, status}, ...] (newest wins)."""
    todos = []
    for line in raw.splitlines():
        line = line.strip()
        if '"TodoWrite"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "TodoWrite":
                items = block.get("input", {}).get("todos")
                if items:
                    todos = items
    return [
        {"content": t.get("content", ""),
         "active": t.get("activeForm", ""),
         "status": t.get("status", "pending")}
        for t in todos
    ]


def extract_last_text(raw, max_chars=400):
    """The most recent assistant text block (what the run last 'said')."""
    last = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or '"assistant"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    last = block["text"].strip()
    last = re.sub(r"\s+", " ", last)
    return last[:max_chars]


def _progress(todos):
    total = len(todos)
    done = sum(1 for t in todos if t["status"] == "completed")
    return done, total


def _current_activity(todos, last_text):
    for t in todos:
        if t["status"] == "in_progress":
            return t["active"] or t["content"]
    return last_text


# ---------------------------------------------------------------------------
# Persisted per-thread state (written on run finish / thread end)
# ---------------------------------------------------------------------------

def record_thread(channel_id, thread_ts, command, raw, status="idle"):
    """Persist a thread's card after a run finishes (called from bot.deliver_output).

    Writes todos/last-text/title synchronously (fast), then computes a one-line
    summary in the background so the run's response is never blocked. An already
    'ended' thread is never downgraded back to 'idle' by a late delivery.
    """
    todos = extract_todos(raw)
    last_text = extract_last_text(raw)
    path = _thread_file(channel_id, thread_ts)

    prev = {}
    try:
        with open(path) as f:
            prev = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if prev.get("status") == "ended":
        status = "ended"

    state = {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "title": _clean_title(command),
        "todos": todos,
        "last_text": last_text,
        "status": status,
        "summary": prev.get("summary", ""),
        "summary_sig": prev.get("summary_sig", ""),  # stale vs new convo -> loop regenerates
        "alive": prev.get("alive", False),
        "updated": time.time(),
    }
    _write_thread(path, state)
    # NOTE: the summary itself is (re)generated by the dashboard's summary_loop, which
    # has the project context + full conversation needed for a clear, self-contained blurb.


_SUMMARY_PREAMBLE = ("one-line summary", "one line summary", "summary", "here's the",
                     "here is the", "this is a summarization", "sure", "okay", "ok,",
                     "the task", "in short")


def clean_summary(text):
    """Reduce an LLM summary to a single clean line: drop preamble/label lines, quotes,
    and markdown. Idempotent — safe to run on already-clean text (and on display)."""
    if not text:
        return ""
    lines = [l.strip() for l in str(text).strip().splitlines()]
    lines = [l for l in lines if l]
    pick = ""
    for l in lines:
        low = l.lower()
        if l.endswith(":") and len(l) <= 40:          # bare label line ("Summary:")
            continue
        if ":" in l and any(low.startswith(p) for p in _SUMMARY_PREAMBLE):
            rest = l.split(":", 1)[1].strip()         # "Summary: <real text>" -> real text
            if rest:
                pick = rest
                break
            continue
        pick = l
        break
    pick = pick.strip().strip("`").strip('"').strip("'").strip()
    pick = re.sub(r"^[*#>\-\s]+", "", pick)            # leading markdown marks
    pick = re.sub(r"\s+", " ", pick)
    return pick[:400]


def _read_channel_context(channel_id, limit=1600):
    """The per-channel project context the bot maintains — used to disambiguate jargon."""
    try:
        with open(os.path.join(CONTEXT_DIR, channel_id + ".md")) as f:
            return f.read().strip()[:limit]
    except (FileNotFoundError, OSError):
        return ""


def _conversation_transcript(channel_id, thread_ts, msgs=10, per=400):
    try:
        conv = history.get_history(channel_id, thread_ts, limit=msgs)
    except Exception:
        conv = []
    out = []
    for m in conv:
        role = "Assistant" if m.get("role") == "assistant" else "User"
        c = re.sub(r"\s+", " ", (m.get("content") or "")).strip()[:per]
        if c:
            out.append(f"{role}: {c}")
    return "\n".join(out)


def compose_summary(channel_id, thread_ts, command="", last_text=""):
    """A clear, self-contained 1-2 sentence summary of a thread, grounded in the
    project context + the actual conversation so jargon/codenames get expanded."""
    context = _read_channel_context(channel_id)
    transcript = _conversation_transcript(channel_id, thread_ts)
    if not transcript and not last_text and not command:
        return ""
    prompt = (
        "Summarize ONE work thread for a research dashboard. The reader knows the "
        "overall project (PROJECT CONTEXT below) but has NOT followed this thread.\n"
        "Write 1-2 plain-English sentences (max 320 chars) covering the relevant 5W1H: "
        "what is being done, on/with what, why it matters, and the current status.\n"
        "CRITICAL: be self-contained. Do NOT use undefined codenames, run IDs, or "
        "'phase N' / 'track X' / 'dual-track' style labels without saying what they "
        "actually are — expand every such term into concrete words (e.g. instead of "
        "'phase 4' write what phase 4 does). Avoid acronyms a newcomer wouldn't know.\n"
        "Output ONLY the sentence(s) — no preamble, quotes, labels, or markdown.\n\n"
        f"PROJECT CONTEXT:\n{context or '(none)'}\n\n"
        f"THREAD CONVERSATION (oldest→newest):\n{transcript or command}\n\n"
        f"LATEST UPDATE:\n{last_text[:1500]}"
    )
    try:
        env = os.environ.copy()
        env["HOME"] = os.path.expanduser("~")
        import subprocess
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", SUMMARY_MODEL, "--dangerously-skip-permissions"],
            capture_output=True, text=True, cwd=WORK_DIR, timeout=90, env=env
        )
        return clean_summary(result.stdout or "")
    except Exception:
        return ""


def _write_thread(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def mark_thread(channel_id, thread_ts, status):
    """Update only the status of a persisted thread (e.g. 'ended')."""
    path = _thread_file(channel_id, thread_ts)
    try:
        with open(path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"channel_id": channel_id, "thread_ts": thread_ts,
                 "title": "", "todos": [], "last_text": "", "summary": ""}
    state["status"] = status
    state["updated"] = time.time()
    _write_thread(path, state)


def set_alive(channel_id, thread_ts, alive, title=None):
    """Flip a thread's 'alive' flag. The party-blob reaction in Slack drives this:
    present -> alive (on the board), removed -> not alive (off the board). A thread
    with alive=False is hidden even while a job is still running."""
    path = _thread_file(channel_id, thread_ts)
    try:
        with open(path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"channel_id": channel_id, "thread_ts": thread_ts,
                 "title": "", "todos": [], "last_text": "", "summary": "", "status": "idle"}
    state["alive"] = bool(alive)
    if title and not state.get("title"):
        state["title"] = _clean_title(title)
    state.setdefault("updated", time.time())   # a flag toggle isn't new activity
    _write_thread(path, state)


def is_alive(channel_id, thread_ts):
    """True if the thread currently carries the party-blob (so the bot doesn't re-add it)."""
    try:
        with open(_thread_file(channel_id, thread_ts)) as f:
            return bool(json.load(f).get("alive", False))
    except (FileNotFoundError, json.JSONDecodeError):
        return False


_bot_user_id = None


def _get_bot_user_id():
    global _bot_user_id
    if _bot_user_id is None:
        client = _get_slack_client()
        if not client:
            return None
        try:
            _bot_user_id = client.auth_test()["user_id"]
        except Exception:
            return None
    return _bot_user_id


def _root_has_reaction(channel_id, thread_ts):
    """True/False if the thread's root message carries the BOT'S own ALIVE_REACTION.
    The bot's blob is the canonical alive indicator — a user's own blob is just the
    toggle gesture and must not count (it can linger after a tap-to-archive).
    None if Slack can't be reached (so we leave the current state untouched)."""
    client = _get_slack_client()
    bot_id = _get_bot_user_id()
    if not client or not bot_id:
        return None
    try:
        resp = client.reactions_get(channel=channel_id, timestamp=thread_ts)
        msg = resp.get("message") or {}
        for r in (msg.get("reactions") or []):
            if r.get("name") == ALIVE_REACTION and bot_id in (r.get("users") or []):
                return True
        return False
    except Exception:
        return None


def reconcile_alive_once():
    """Make the dashboard match Slack: a thread is alive iff its root message still
    has the party-blob. Picks up adds/removes even when the bot missed the event."""
    targets = {}
    now = time.time()
    for fname in os.listdir(THREADS_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(THREADS_DIR, fname)) as f:
                s = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        if s.get("status") == "ended":
            continue
        if now - s.get("updated", 0) > RECONCILE_MAX_AGE:
            continue
        targets[_thread_key(s["channel_id"], s["thread_ts"])] = (s["channel_id"], s["thread_ts"], s.get("updated", 0))
    for info in _load_active_runs().values():
        ch, th = info.get("channel_id"), info.get("thread_ts")
        if ch and th:
            targets[_thread_key(ch, th)] = (ch, th, now)
    # cap Slack calls; prefer most-recently-updated threads
    ordered = sorted(targets.values(), key=lambda t: -t[2])[:RECONCILE_MAX_THREADS]
    for ch, th, _ in ordered:
        present = _root_has_reaction(ch, th)
        if present is None:
            continue
        if present != is_alive(ch, th):
            set_alive(ch, th, present)
            print(f"[reconcile] {ch}:{th} party-blob={'present' if present else 'gone'} -> alive={present}", flush=True)


def reconcile_loop():
    if not _get_slack_client():
        print("[reconcile] no SLACK_BOT_TOKEN — party-blob reconciliation DISABLED", flush=True)
        return
    print(f"[reconcile] checking party-blob ({ALIVE_REACTION}) every {RECONCILE_INTERVAL}s", flush=True)
    while True:
        try:
            reconcile_alive_once()
        except Exception as e:
            print(f"[reconcile] error: {e}", flush=True)
        time.sleep(RECONCILE_INTERVAL)


# ---------------------------------------------------------------------------
# State assembly for the UI
# ---------------------------------------------------------------------------

_channel_cache = {}
_slack_client = None


def _get_slack_client():
    """Lazily build a Slack WebClient from SLACK_BOT_TOKEN (.env), for name lookups."""
    global _slack_client
    if _slack_client is not None:
        return _slack_client or None
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        _slack_client = False
        return None
    try:
        from slack_sdk import WebClient
        _slack_client = WebClient(token=token)
    except Exception:
        _slack_client = False
    return _slack_client or None


def _channel_name(channel_id):
    if channel_id in _channel_cache:
        return _channel_cache[channel_id]
    name = channel_id
    client = _get_slack_client()
    if client:
        try:
            info = client.conversations_info(channel=channel_id)
            name = info["channel"].get("name") or channel_id
        except Exception:
            pass
    _channel_cache[channel_id] = name
    return name


def build_state():
    """Assemble the full dashboard payload: overall rollup + per-thread cards."""
    runs = _load_active_runs()

    # Group active runs by thread.
    live = {}  # thread_key -> {channel_id, thread_ts, runs:[info]}
    for sid, info in runs.items():
        ch, th = info.get("channel_id"), info.get("thread_ts")
        if not ch or not th:
            continue
        key = _thread_key(ch, th)
        live.setdefault(key, {"channel_id": ch, "thread_ts": th, "runs": []})
        live[key]["runs"].append(info)

    cards = {}
    alive_map = {}  # thread_key -> alive flag (default True if never set)

    # 1) Persisted (idle/done) threads.
    for fname in os.listdir(THREADS_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(THREADS_DIR, fname)) as f:
                s = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        key = _thread_key(s["channel_id"], s["thread_ts"])
        alive_map[key] = s.get("alive", False)   # strict: only confirmed-blob threads show
        if s.get("status") == "ended":
            continue  # discontinued threads leave the board
        if not alive_map[key]:
            continue  # no party-blob on the first message -> off the board
        cards[key] = _card_from_persisted(s)

    # 2) Live (running) threads override / add — only if the thread is alive (blobbed).
    now = time.time()
    for key, grp in live.items():
        if not alive_map.get(key, False):
            continue
        cards[key] = _card_from_live(grp, now)

    # 3) Overlay pending End requests so cards read 'ending' until the bot kills them.
    try:
        pending = {f[:-4] for f in os.listdir(CANCEL_DIR) if f.endswith(".req")}
    except FileNotFoundError:
        pending = set()
    for key in pending:
        if key in cards:
            cards[key]["status"] = "ending"

    card_list = sorted(
        cards.values(),
        key=lambda c: (0 if c["status"] == "running" else 1, -c["updated"]),
    )
    for i, c in enumerate(card_list):
        c["index"] = i + 1   # stable panel number used by the overview bullets

    running = sum(1 for c in card_list if c["status"] == "running")
    return {
        "overall": {
            "running": running,
            "idle": len(card_list) - running,
            "threads": len(card_list),
            "ts": now,
        },
        "overview": _overview_cache["text"],
        "cards": card_list,
    }


def _card_from_persisted(s):
    done, total = _progress(s.get("todos", []))
    return {
        "key": _thread_key(s["channel_id"], s["thread_ts"]),
        "channel_id": s["channel_id"],
        "channel": _channel_name(s["channel_id"]),
        "thread_ts": s["thread_ts"],
        "title": s.get("title", "") or "(thread)",
        "status": s.get("status", "idle"),
        "elapsed": "",
        "todos": s.get("todos", []),
        "done": done, "total": total,
        "activity": _current_activity(s.get("todos", []), s.get("last_text", "")),
        "tail": [],
        "summary": clean_summary(s.get("summary", "")),
        "updated": s.get("updated", 0),
    }


def _card_from_live(grp, now):
    ch, th = grp["channel_id"], grp["thread_ts"]
    info = grp["runs"][-1]  # most recent run in the thread

    out = info.get("output_file") or ""
    watch = info.get("watch_file") or ""
    raw = _read_tail(out) if out and os.path.exists(out) else _read_tail(watch)

    todos = extract_todos(raw)
    last_text = extract_last_text(raw)
    done, total = _progress(todos)

    start = info.get("event_ts")
    elapsed = ""
    try:
        secs = int(now - float(start))
        elapsed = f"{secs // 60}m" if secs >= 60 else f"{secs}s"
    except (TypeError, ValueError):
        pass

    tail_lines = []
    for line in raw.splitlines()[-400:]:
        line = line.strip()
        if not line or '"assistant"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                tail_lines.append(re.sub(r"\s+", " ", block["text"].strip()))
            elif block.get("type") == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input", {})
                hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                tail_lines.append(f"⚙ {name}: {str(hint)[:80]}")
    tail_lines = tail_lines[-6:]

    summary = ""
    try:
        with open(_thread_file(ch, th)) as f:
            summary = clean_summary(json.load(f).get("summary", ""))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return {
        "key": _thread_key(ch, th),
        "channel_id": ch,
        "channel": _channel_name(ch),
        "thread_ts": th,
        "title": _clean_title(info.get("original_command", "")) or "(thread)",
        "status": "running",
        "elapsed": elapsed,
        "running_count": len(grp["runs"]),
        "todos": todos,
        "done": done, "total": total,
        "activity": _current_activity(todos, last_text),
        "tail": tail_lines,
        "summary": summary,
        "updated": now,
    }


# ---------------------------------------------------------------------------
# Overall channel overview (bullets that reference panels by index)
# ---------------------------------------------------------------------------

def _overview_sig(cards):
    return "|".join(f"{c['index']}:{c['key']}:{c['status']}:{(c.get('summary') or '')[:40]}"
                    for c in cards)


def generate_overview(cards):
    """One LLM call -> a few bullets summarizing the whole board, citing panels as [n]."""
    if not cards:
        return ""
    lines = []
    for c in cards:
        td = f"{c['done']}/{c['total']} todos" if c.get("total") else "no todos"
        lines.append(f"[{c['index']}] #{c['channel']} \"{c['title']}\" "
                     f"(status: {c['status']}, {td}) — {c.get('summary') or c.get('activity') or ''}")
    contexts = []
    for ch in dict.fromkeys(c["channel_id"] for c in cards):   # unique, order-preserving
        ctx = _read_channel_context(ch, limit=1000)
        if ctx:
            contexts.append(f"#{_channel_name(ch)}:\n{ctx}")
    ctx_block = ("\n\n".join(contexts))[:2500]
    prompt = (
        "Below are the active threads on a research dashboard, each numbered, plus "
        "PROJECT CONTEXT for the channel(s). Write 3-6 short bullet points summarizing "
        "what's going on overall — group related work, call out what's running vs "
        "waiting, and what needs attention. Keep it self-contained: expand internal "
        "codenames / 'phase N' / 'track X' jargon into plain words. Reference threads by "
        "their number in square brackets like [1] or [2], [4]. "
        "Output ONLY the bullets, one per line starting with '- ', in English, concise.\n\n"
        f"PROJECT CONTEXT:\n{ctx_block or '(none)'}\n\n"
        "THREADS:\n" + "\n".join(lines)
    )
    try:
        env = os.environ.copy()
        env["HOME"] = os.path.expanduser("~")
        import subprocess
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", SUMMARY_MODEL, "--dangerously-skip-permissions"],
            capture_output=True, text=True, cwd=WORK_DIR, timeout=90, env=env
        )
        out = (result.stdout or "").strip()
    except Exception:
        return ""
    bullets = []
    for l in out.splitlines():
        l = l.strip()
        if l.startswith(("- ", "* ", "• ")):
            bullets.append("• " + l[2:].strip())
        elif re.match(r"^\d+[.)]\s", l):
            bullets.append("• " + re.sub(r"^\d+[.)]\s", "", l).strip())
    return "\n".join(bullets)


def overview_loop():
    """Regenerate the overview whenever the board's threads/summaries change."""
    while True:
        try:
            cards = build_state()["cards"]
            if not cards:
                _overview_cache.update(text="", sig="", at=time.time())
            else:
                sig = _overview_sig(cards)
                if sig != _overview_cache["sig"]:
                    text = generate_overview(cards)
                    if text:
                        _overview_cache.update(text=text, sig=sig, at=time.time())
        except Exception as e:
            print(f"[overview] error: {e}", flush=True)
        time.sleep(OVERVIEW_INTERVAL)


def _summary_sig(channel_id, thread_ts):
    try:
        conv = history.get_history(channel_id, thread_ts, limit=12)
    except Exception:
        conv = []
    last = (conv[-1].get("content") or "")[-80:] if conv else ""
    return f"{len(conv)}:{last}"


def summary_loop():
    """Regenerate each alive thread's summary when its conversation changes, using the
    project context + full conversation so jargon is expanded into plain language."""
    while True:
        try:
            done = 0
            for fname in os.listdir(THREADS_DIR):
                if not fname.endswith(".json") or done >= SUMMARY_MAX_PER_CYCLE:
                    continue
                path = os.path.join(THREADS_DIR, fname)
                try:
                    with open(path) as f:
                        s = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    continue
                if s.get("status") == "ended" or not s.get("alive", False):
                    continue
                ch, th = s["channel_id"], s["thread_ts"]
                sig = _summary_sig(ch, th)
                if sig == s.get("summary_sig") and s.get("summary"):
                    continue   # conversation unchanged since last summary
                text = compose_summary(ch, th, s.get("title", ""), s.get("last_text", ""))
                if not text:
                    continue
                try:
                    with open(path) as f:   # reload to avoid clobbering concurrent writes
                        s = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    continue
                s["summary"] = text
                s["summary_sig"] = sig
                _write_thread(path, s)
                done += 1
                print(f"[summary] regenerated {ch}:{th}", flush=True)
        except Exception as e:
            print(f"[summary] error: {e}", flush=True)
        time.sleep(SUMMARY_INTERVAL)


# ---------------------------------------------------------------------------
# Single-thread detail (for the click-through panel)
# ---------------------------------------------------------------------------

def _fetch_conversation(channel_id, thread_ts, limit=40):
    """The thread's conversation from the shared history DB (role/content/model)."""
    try:
        msgs = history.get_history(channel_id, thread_ts, limit=limit)
    except Exception:
        msgs = []
    out = []
    for m in msgs:
        out.append({
            "role": m.get("role", "user"),
            "content": (m.get("content") or "").strip(),
            "model": m.get("model") or "",
        })
    return out


def thread_detail(channel_id, thread_ts):
    """Full card for one thread + its conversation, for the detail modal."""
    key = _thread_key(channel_id, thread_ts)
    card = None
    for c in build_state()["cards"]:
        if c["key"] == key:
            card = c
            break
    if card is None:
        # Archived / not on the board — build a static card from the persisted file.
        try:
            with open(_thread_file(channel_id, thread_ts)) as f:
                card = _card_from_persisted(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            card = {"key": key, "channel_id": channel_id, "thread_ts": thread_ts,
                    "channel": _channel_name(channel_id), "title": "(thread)",
                    "status": "idle", "todos": [], "done": 0, "total": 0,
                    "summary": "", "activity": "", "tail": [], "elapsed": ""}
    card["conversation"] = _fetch_conversation(channel_id, thread_ts)
    return card


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Bright orange-gradient robot favicon — orange is rare in tab bars, so it's easy to
# spot. One purple eye nods to the UI accent. Swap the stops below to recolor.
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#ff9d2f"/><stop offset="1" stop-color="#ff4d2e"/>
  </linearGradient></defs>
  <rect width="100" height="100" rx="24" fill="url(#g)"/>
  <rect x="47" y="13" width="6" height="13" rx="3" fill="#fff"/>
  <circle cx="50" cy="13" r="6.5" fill="#fff"/>
  <rect x="23" y="30" width="54" height="43" rx="13" fill="#fff"/>
  <circle cx="39" cy="51" r="7.5" fill="#ff5e3a"/>
  <circle cx="61" cy="51" r="7.5" fill="#7c5cff"/>
</svg>"""


@app.route("/")
def index():
    return Response(PAGE_HTML, mimetype="text/html")


@app.route("/favicon.svg")
@app.route("/favicon.ico")
def favicon():
    return Response(FAVICON_SVG, mimetype="image/svg+xml")


@app.route("/api/state")
def api_state():
    return jsonify(build_state())


@app.route("/api/thread")
def api_thread():
    ch, th = request.args.get("channel"), request.args.get("thread")
    if not ch or not th:
        return jsonify({"error": "missing channel/thread"}), 400
    return jsonify(thread_detail(ch, th))


@app.route("/stream")
def stream():
    def gen():
        while True:
            try:
                payload = json.dumps(build_state())
            except Exception as e:
                payload = json.dumps({"error": str(e)})
            yield f"data: {payload}\n\n"
            time.sleep(2)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def request_cancel(channel_id, thread_ts):
    """Drop an End request for the bot's watcher to pick up, and optimistically
    flip the card to 'ending' so the UI reflects it immediately."""
    payload = {"channel_id": channel_id, "thread_ts": thread_ts, "ts": time.time()}
    path = os.path.join(CANCEL_DIR, _thread_key(channel_id, thread_ts) + ".req")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    mark_thread(channel_id, thread_ts, "ending")


@app.route("/api/end", methods=["POST"])
def api_end():
    data = request.get_json(force=True, silent=True) or {}
    ch, th = data.get("channel_id"), data.get("thread_ts")
    if not ch or not th:
        return jsonify({"ok": False, "error": "missing channel_id/thread_ts"}), 400
    try:
        request_cancel(ch, th)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})



# ---------------------------------------------------------------------------
# Settings GUI (/settings) — edit .env, STYLE.md, and recipes from the browser.
# WRITES ARE LOCALHOST-ONLY: the dashboard may bind 0.0.0.0 for viewing cards,
# but configuration (which includes Slack tokens) never leaves the machine.
# ---------------------------------------------------------------------------

ENV_FILE = os.path.join(BOT_DIR, ".env")
STYLE_FILE = os.path.join(BOT_DIR, "STYLE.md")
RECIPES_DIR = os.path.join(BOT_DIR, "recipes")
_SECRET_TOKENS = ("TOKEN", "KEY", "SECRET", "WEBHOOK", "PASSWORD")


def _is_secret_key(key):
    return any(t in key.upper() for t in _SECRET_TOKENS)


def _mask_value(v):
    if len(v) <= 6:
        return "\u2022" * max(len(v), 4)
    return v[:4] + "\u2026\u2022\u2022\u2022\u2022"


def _require_local():
    """403 unless the request comes from this machine (settings expose tokens)."""
    if (request.remote_addr or "") not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return Response("403 — settings are only accessible from localhost "
                        "(ssh -L 8080:localhost:8080 <machine> to tunnel in).", 403)
    return None


def _read_env_lines():
    try:
        with open(ENV_FILE) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def _env_entries():
    out = []
    for line in _read_env_lines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out.append((k.strip(), v.strip()))
    return out


def _write_env_updates(updates):
    """Replace values of existing keys in place (preserving comments/order),
    append new keys at the end. Values are forced single-line."""
    updates = {k: str(v).replace("\n", " ").strip() for k, v in updates.items()}
    lines = _read_env_lines()
    seen, out = set(), []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    os.replace(tmp, ENV_FILE)


@app.route("/settings")
def settings_page():
    guard = _require_local()
    if guard:
        return guard
    return Response(SETTINGS_HTML, mimetype="text/html")


@app.route("/api/settings")
def api_settings():
    guard = _require_local()
    if guard:
        return guard
    env = [{"key": k, "secret": _is_secret_key(k),
            "value": _mask_value(v) if _is_secret_key(k) else v}
           for k, v in _env_entries()]
    try:
        with open(STYLE_FILE) as f:
            style = f.read()
    except FileNotFoundError:
        style = ""
    recipes = []
    try:
        for fname in sorted(os.listdir(RECIPES_DIR)):
            if fname.endswith(".md"):
                with open(os.path.join(RECIPES_DIR, fname)) as f:
                    recipes.append({"name": fname, "content": f.read()})
    except FileNotFoundError:
        pass
    return jsonify({"env": env, "style": style, "recipes": recipes})


@app.route("/api/settings/env", methods=["POST"])
def api_settings_env():
    guard = _require_local()
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    updates = data.get("updates") or {}
    if not isinstance(updates, dict) or not updates:
        return jsonify({"ok": False, "error": "no updates"}), 400
    for k in updates:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            return jsonify({"ok": False, "error": f"bad key: {k}"}), 400
    _write_env_updates(updates)
    return jsonify({"ok": True, "note": "saved — restart the bot to apply .env changes"})


@app.route("/api/settings/file", methods=["POST"])
def api_settings_file():
    guard = _require_local()
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    target, content = data.get("target"), data.get("content", "")
    if not isinstance(content, str) or len(content) > 200_000:
        return jsonify({"ok": False, "error": "bad content"}), 400
    if target == "style":
        path = STYLE_FILE
    elif target == "recipe":
        name = data.get("name") or ""
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.md", name) or ".." in name:
            return jsonify({"ok": False, "error": "recipe name must be like plotting.md"}), 400
        os.makedirs(RECIPES_DIR, exist_ok=True)
        path = os.path.join(RECIPES_DIR, name)
    else:
        return jsonify({"ok": False, "error": "target must be style|recipe"}), 400
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, path)
    return jsonify({"ok": True, "note": "saved — applies from the next command, no restart needed"})


INBOX_DIR = os.path.join(BOT_DIR, "inbox")


@app.route("/api/send", methods=["POST"])
def api_send():
    """Queue a follow-up command for a thread (chat box in the card modal).
    Localhost-only: this is code execution. The bot's inbox watcher posts the
    message into the Slack thread and runs it through the normal pipeline."""
    guard = _require_local()
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    ch = data.get("channel_id")
    th = data.get("thread_ts")
    text = (data.get("text") or "").strip()
    if not ch or not th or not text:
        return jsonify({"ok": False, "error": "missing channel_id/thread_ts/text"}), 400
    if len(text) > 4000:
        return jsonify({"ok": False, "error": "message too long"}), 400
    os.makedirs(INBOX_DIR, exist_ok=True)
    path = os.path.join(INBOX_DIR, f"{_thread_key(ch, th)}_{int(time.time()*1000)}.req")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"channel_id": ch, "thread_ts": th, "text": text, "ts": time.time()}, f)
    os.replace(tmp, path)
    return jsonify({"ok": True})


def _finalize_end(ch, th):
    """End a thread that has NO running jobs directly from the dashboard: there is
    nothing for the bot to kill, so don't wait for it. Marks the card ended, drops
    any queued .req (so the bot won't re-process it later), and removes the bot's
    alive-reaction (the dashboard authenticates with the same bot token)."""
    mark_thread(ch, th, "ended")
    try:
        os.remove(os.path.join(CANCEL_DIR, _thread_key(ch, th) + ".req"))
    except FileNotFoundError:
        pass
    client = _get_slack_client()
    if client:
        try:
            client.reactions_remove(channel=ch, name=ALIVE_REACTION, timestamp=th)
            time.sleep(0.5)   # stay under Slack's rate limit when sweeping many threads
        except Exception:
            pass


def auto_end_loop():
    """End threads that have been idle for more than AUTO_END_HOURS (default 24h).
    Threads with a job currently running are never touched. Idle threads are
    finalized directly; threads stuck in 'ending' (a queued request no bot picked
    up, e.g. while the bot is down) are finalized too, so nothing hangs forever."""
    if AUTO_END_HOURS <= 0:
        print("[auto-end] disabled (DASHBOARD_AUTO_END_HOURS=0)", flush=True)
        return
    print(f"[auto-end] ending threads idle > {AUTO_END_HOURS:g}h", flush=True)
    while True:
        try:
            now = time.time()
            live = set()
            for info in _load_active_runs().values():
                ch, th = info.get("channel_id"), info.get("thread_ts")
                if ch and th:
                    live.add(_thread_key(ch, th))
            for fname in os.listdir(THREADS_DIR):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(THREADS_DIR, fname)) as f:
                        s = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    continue
                ch, th = s.get("channel_id"), s.get("thread_ts")
                if not ch or not th or _thread_key(ch, th) in live:
                    continue
                status = s.get("status")
                idle = now - s.get("updated", now)
                if status == "ending":
                    _finalize_end(ch, th)   # request no bot picked up — finish it here
                    print(f"[auto-end] {_thread_key(ch, th)} stuck ending -> ended", flush=True)
                elif status != "ended" and idle > AUTO_END_HOURS * 3600:
                    _finalize_end(ch, th)
                    print(f"[auto-end] {_thread_key(ch, th)} idle {idle/3600:.1f}h -> ended", flush=True)
        except Exception as e:
            print(f"[auto-end] error: {e}", flush=True)
        time.sleep(AUTO_END_CHECK_SEC)


def run_server(host="0.0.0.0", port=8080):
    """Run the dashboard server in the foreground (standalone process)."""
    print(f"[dashboard] http://{host}:{port}  (threads: {THREADS_DIR}, cancel: {CANCEL_DIR})",
          flush=True)
    threading.Thread(target=reconcile_loop, daemon=True).start()
    threading.Thread(target=auto_end_loop, daemon=True).start()
    threading.Thread(target=summary_loop, daemon=True).start()
    threading.Thread(target=overview_loop, daemon=True).start()
    app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)


# ---------------------------------------------------------------------------
# UI  (single page, dark, SSE-driven)
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>🤖 research-bot</title>
<style>
  :root{
    --bg:#0c0e14; --panel:#151823; --panel2:#1b1f2e; --line:#262b3d;
    --txt:#e7eaf3; --muted:#8b93a7; --faint:#5b6378;
    --green:#3fb950; --green-dim:#1f6f33; --amber:#d29922; --blue:#4493f8;
    --red:#f0506e; --accent:#7c5cff;
    --shadow:0 1px 3px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.25);
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{
    background:radial-gradient(1200px 600px at 80% -10%,#161a2b 0,var(--bg) 55%) fixed;
    color:var(--txt);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:1180px;margin:0 auto;padding:28px 22px 64px}

  header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
  .logo{
    width:38px;height:38px;border-radius:11px;flex:none;
    background:linear-gradient(135deg,var(--accent),#4493f8);
    display:grid;place-items:center;font-size:20px;box-shadow:var(--shadow);
  }
  header h1{font-size:18px;margin:0;font-weight:650;letter-spacing:.2px}
  header .sub{color:var(--muted);font-size:12.5px;margin-top:1px}
  .live{margin-left:auto;display:flex;align-items:center;gap:7px;color:var(--muted);font-size:12.5px}
  .live .dot{width:8px;height:8px;border-radius:50%;background:var(--green);
    box-shadow:0 0 0 0 rgba(63,185,80,.6);animation:pulse 2s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(63,185,80,.5)}70%{box-shadow:0 0 0 7px rgba(63,185,80,0)}100%{box-shadow:0 0 0 0 rgba(63,185,80,0)}}
  .gear{margin-left:10px;color:var(--muted);text-decoration:none;font-size:16px;
    padding:3px 8px;border-radius:8px;border:1px solid var(--line)}
  .gear:hover{color:#fff;background:#222a3d}

  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:24px}
  .stat{background:linear-gradient(180deg,var(--panel),var(--panel2));
    border:1px solid var(--line);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow)}
  .stat .n{font-size:30px;font-weight:700;line-height:1;letter-spacing:-.5px}
  .stat .l{color:var(--muted);font-size:12px;margin-top:7px;text-transform:uppercase;letter-spacing:.7px}
  .stat.run .n{color:var(--green)}

  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}

  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));
    border:1px solid var(--line);border-radius:16px;padding:17px 18px 16px;
    box-shadow:var(--shadow);position:relative;overflow:hidden;transition:border-color .2s,transform .15s}
  .card:hover{border-color:#36405c;transform:translateY(-1px)}
  .card.running{border-color:#2c5e3a}
  .card.running::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
    background:linear-gradient(var(--green),var(--green-dim))}

  .ctop{display:flex;align-items:center;gap:9px;margin-bottom:4px}
  .chip{font-size:11px;font-weight:600;padding:2.5px 9px;border-radius:999px;
    background:#222a3d;color:#9fb0d0;border:1px solid var(--line);white-space:nowrap}
  .chip.ch{color:var(--blue)}
  .pill{margin-left:auto;display:inline-flex;align-items:center;gap:6px;font-size:11.5px;
    font-weight:600;padding:3px 10px;border-radius:999px;white-space:nowrap}
  .pill.running{color:var(--green);background:rgba(63,185,80,.12);border:1px solid rgba(63,185,80,.3)}
  .pill.idle{color:var(--muted);background:#20243250;border:1px solid var(--line)}
  .pill.ending{color:var(--amber);background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.3)}
  .pill.ending .d{animation:pulse 1.6s infinite}
  .pill .d{width:7px;height:7px;border-radius:50%;background:currentColor}
  .pill.running .d{animation:pulse 1.6s infinite}

  .title{font-size:14.5px;font-weight:600;margin:6px 0 2px;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .summary{color:var(--muted);font-size:12.5px;margin:6px 0 12px;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

  .prog{display:flex;align-items:center;gap:10px;margin:10px 0 4px}
  .bar{flex:1;height:8px;border-radius:6px;background:#232838;overflow:hidden}
  .bar > i{display:block;height:100%;border-radius:6px;
    background:linear-gradient(90deg,var(--green),#5fd97a);transition:width .5s ease}
  .bar.none > i{background:linear-gradient(90deg,#3a4256,#444)}
  .pcount{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;min-width:42px;text-align:right}

  .activity{display:flex;gap:7px;align-items:flex-start;margin:11px 0 0;
    color:#c7cee0;font-size:12.5px}
  .activity .ic{color:var(--amber);flex:none}

  details.todos{margin-top:11px}
  details.todos>summary{cursor:pointer;color:var(--faint);font-size:11.5px;
    list-style:none;user-select:none;outline:none}
  details.todos>summary::-webkit-details-marker{display:none}
  details.todos>summary:hover{color:var(--muted)}
  .tlist{margin:8px 0 2px;display:flex;flex-direction:column;gap:5px}
  .titem{display:flex;gap:8px;align-items:flex-start;font-size:12.5px;color:#c2c9da}
  .titem .box{flex:none;width:15px;height:15px;border-radius:4px;border:1.5px solid #3a4256;
    margin-top:1px;display:grid;place-items:center;font-size:10px;color:#0c0e14}
  .titem.completed .box{background:var(--green);border-color:var(--green)}
  .titem.completed span{color:var(--faint);text-decoration:line-through}
  .titem.in_progress .box{border-color:var(--amber);box-shadow:0 0 0 2px rgba(210,153,34,.18)}
  .titem.in_progress span{color:#fff}

  .tail{margin-top:11px;background:#0a0c12;border:1px solid var(--line);border-radius:9px;
    padding:9px 11px;font:11.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    color:#9aa6c0;max-height:120px;overflow:auto}
  .tail div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tail .tool{color:var(--blue)}

  .cfoot{display:flex;align-items:center;margin-top:14px;gap:9px}
  .ts{color:var(--faint);font-size:11px;margin-right:auto}
  button.end{appearance:none;border:1px solid rgba(240,80,110,.35);background:rgba(240,80,110,.1);
    color:#ff8aa0;font-weight:600;font-size:12px;padding:6px 14px;border-radius:9px;cursor:pointer;
    transition:background .15s,border-color .15s}
  button.end:hover{background:rgba(240,80,110,.2);border-color:rgba(240,80,110,.6)}
  button.end:active{transform:translateY(1px)}
  button.end[disabled]{opacity:.5;cursor:default}

  .empty{text-align:center;color:var(--muted);padding:80px 0}
  .empty .big{font-size:42px;margin-bottom:10px;opacity:.5}

  /* index badge + clickable cards */
  .idx{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:7px;
    background:#2a3042;color:#cdd5e8;font-size:12px;font-weight:700;flex:none;
    font-variant-numeric:tabular-nums}
  .card.running .idx{background:linear-gradient(135deg,var(--accent),#4493f8);color:#fff}
  .card{cursor:pointer}

  /* overview */
  .overview{background:linear-gradient(180deg,var(--panel),var(--panel2));
    border:1px solid var(--line);border-radius:16px;padding:15px 18px 16px;margin-bottom:22px;
    box-shadow:var(--shadow)}
  .overview h2{margin:0 0 11px;font-size:12px;letter-spacing:.7px;text-transform:uppercase;
    color:var(--muted);font-weight:650;display:flex;align-items:center;gap:8px}
  .overview h2 .stale{color:var(--faint);font-weight:400;letter-spacing:0;text-transform:none;font-size:11px}
  .overview ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:8px}
  .overview li{font-size:13.5px;color:#d7dcea;line-height:1.5;display:flex;gap:9px}
  .overview li .b{color:var(--accent);flex:none}
  .ref{display:inline-grid;place-items:center;min-width:18px;height:18px;padding:0 5px;
    border-radius:5px;background:#2f3650;color:#9fb4ff;font-size:11px;font-weight:700;
    cursor:pointer;font-variant-numeric:tabular-nums}
  .ref:hover{background:var(--accent);color:#fff}

  /* modal */
  .modal-bg{position:fixed;inset:0;background:rgba(6,8,13,.66);backdrop-filter:blur(3px);
    display:none;align-items:flex-start;justify-content:center;z-index:50;padding:40px 16px;overflow:auto}
  .modal-bg.open{display:flex}
  .modal{background:linear-gradient(180deg,#171a26,#12141d);border:1px solid var(--line);
    border-radius:18px;width:min(780px,100%);box-shadow:0 24px 70px rgba(0,0,0,.6);
    max-height:calc(100vh - 80px);display:flex;flex-direction:column;overflow:hidden}
  .modal > header{display:flex;align-items:flex-start;gap:11px;padding:18px 20px 14px;
    border-bottom:1px solid var(--line)}
  .modal h3{margin:0;font-size:15.5px;font-weight:650;line-height:1.35}
  .modal .x{margin-left:auto;flex:none;cursor:pointer;color:var(--muted);font-size:22px;
    line-height:1;background:none;border:none;padding:2px 8px;border-radius:8px}
  .modal .x:hover{background:#222a3d;color:#fff}
  .modal .body{padding:16px 20px 22px;overflow:auto}
  .sec{margin-bottom:20px}
  .sec:last-child{margin-bottom:0}
  .sec h4{margin:0 0 9px;font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)}
  .sec .txt{font-size:13.5px;color:#dde2ef;line-height:1.55;white-space:pre-wrap}
  .conv{display:flex;flex-direction:column;gap:10px;max-height:340px;overflow:auto;padding-right:4px}
  .msg{padding:9px 12px;border-radius:11px;font-size:13px;line-height:1.5;white-space:pre-wrap;
    max-width:92%;word-wrap:break-word;overflow-wrap:anywhere}
  .msg.user{align-self:flex-end;background:#243049;color:#eaf0ff;border:1px solid #2e3a55}
  .msg.assistant{align-self:flex-start;background:#191d2a;color:#d2d8e6;border:1px solid var(--line)}
  .msg .who{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--faint);margin-bottom:4px}
  .msg .mtext{white-space:pre-wrap}
  .msg .mtext.clip{max-height:8.5em;overflow:hidden;
    -webkit-mask-image:linear-gradient(180deg,#000 68%,transparent);
            mask-image:linear-gradient(180deg,#000 68%,transparent)}
  .msg .more{margin-top:7px;background:none;border:none;color:#9fb4ff;cursor:pointer;
    font-size:11.5px;font-weight:600;padding:2px 0}
  .msg .more:hover{color:#fff}
  .sendrow{display:flex;gap:8px;margin-top:12px}
  .sendrow input{flex:1;background:#0a0c12;border:1px solid var(--line);border-radius:10px;
    color:var(--txt);padding:9px 12px;font-size:13px;outline:none}
  .sendrow input:focus{border-color:var(--accent)}
  .sendrow button{appearance:none;border:1px solid rgba(124,92,255,.45);background:rgba(124,92,255,.14);
    color:#b9a8ff;font-weight:600;font-size:12.5px;padding:0 18px;border-radius:10px;cursor:pointer}
  .sendrow button:hover{background:rgba(124,92,255,.26)}
  .sendnote{font-size:11.5px;margin-top:6px;color:var(--faint)}
  .msg.queued{opacity:.65;border-style:dashed}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">🤖</div>
    <div>
      <h1>research-bot</h1>
      <div class="sub">live thread dashboard</div>
    </div>
    <div class="live"><span class="dot"></span><span id="conn">connecting…</span><a class="gear" href="/settings" title="Settings">⚙</a></div>
  </header>

  <div class="stats">
    <div class="stat run"><div class="n" id="s-run">0</div><div class="l">Running</div></div>
    <div class="stat"><div class="n" id="s-idle">0</div><div class="l">Idle</div></div>
    <div class="stat"><div class="n" id="s-threads">0</div><div class="l">Threads</div></div>
  </div>

  <div class="overview" id="overview" style="display:none">
    <h2>📋 Overview <span class="stale" id="ov-stale"></span></h2>
    <ul id="ov-list"></ul>
  </div>

  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" style="display:none">
    <div class="big">🌙</div>No active threads. Fire off a command in Slack.
  </div>
</div>

<div class="modal-bg" id="modal-bg" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal"></div>
</div>

<script>
const esc = s => (s||"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const grid = document.getElementById('grid');
const empty = document.getElementById('empty');

function ago(ts){
  if(!ts) return "";
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if(s<60) return s+"s ago";
  if(s<3600) return Math.floor(s/60)+"m ago";
  if(s<86400) return Math.floor(s/3600)+"h ago";
  return Math.floor(s/86400)+"d ago";
}

function todoItem(t){
  const mark = t.status==='completed' ? '✓' : (t.status==='in_progress' ? '' : '');
  return `<div class="titem ${esc(t.status)}"><span class="box">${mark}</span><span>${esc(t.content)}</span></div>`;
}

function pillHTML(c){
  if(c.status==='ending') return `<span class="pill ending"><span class="d"></span>ending…</span>`;
  if(c.status==='running') return `<span class="pill running"><span class="d"></span>running${c.elapsed?' '+esc(c.elapsed):''}</span>`;
  return `<span class="pill idle"><span class="d"></span>${c.done&&c.done===c.total&&c.total?'done':'idle'}</span>`;
}

function card(c){
  const pct = c.total ? Math.round(100*c.done/c.total) : (c.status==='running'?8:0);
  const ending = c.status==='ending';
  const multi = (c.running_count>1) ? `<span class="chip">×${c.running_count} jobs</span>` : '';
  const tail = (c.tail&&c.tail.length)
    ? `<div class="tail">`+c.tail.map(l=>`<div class="${l.startsWith('⚙')?'tool':''}">${esc(l)}</div>`).join('')+`</div>` : '';
  const todos = (c.todos&&c.todos.length)
    ? `<details class="todos" onclick="event.stopPropagation()"><summary>${c.done}/${c.total} todos ▾</summary>
         <div class="tlist">${c.todos.map(todoItem).join('')}</div></details>` : '';
  const activity = c.activity
    ? `<div class="activity"><span class="ic">▸</span><span>${esc(c.activity)}</span></div>` : '';
  const summary = c.summary
    ? `<div class="summary">${esc(c.summary)}</div>` : '';

  return `<div class="card ${esc(c.status)}" onclick="openThread('${esc(c.channel_id)}','${esc(c.thread_ts)}')">
    <div class="ctop">
      <span class="idx">${c.index||''}</span>
      <span class="chip ch">#${esc(c.channel)}</span>${multi}${pillHTML(c)}
    </div>
    <div class="title">${esc(c.title)}</div>
    ${summary}
    <div class="prog">
      <div class="bar ${c.total?'':'none'}"><i style="width:${pct}%"></i></div>
      <div class="pcount">${c.total?(c.done+'/'+c.total):'—'}</div>
    </div>
    ${activity}
    ${todos}
    ${tail}
    <div class="cfoot">
      <span class="ts">${ago(c.updated)}</span>
      <button class="end" ${ending?'disabled':''} onclick="event.stopPropagation();endThread(this,'${esc(c.channel_id)}','${esc(c.thread_ts)}')">${ending?'Ending…':'End'}</button>
    </div>
  </div>`;
}

let lastCards = [];

function renderOverview(state){
  const ov = document.getElementById('overview');
  const txt = (state.overview||'').trim();
  if(!txt){ ov.style.display='none'; return; }
  ov.style.display='block';
  const lines = txt.split('\n').map(l=>l.trim()).filter(Boolean);
  document.getElementById('ov-list').innerHTML = lines.map(l=>{
    let body = esc(l.replace(/^•\s*/,''));
    body = body.replace(/\[(\d+)\]/g, (m,n)=>`<span class="ref" onclick="openByIndex(${n})">${n}</span>`);
    return `<li><span class="b">•</span><span>${body}</span></li>`;
  }).join('');
}

function render(state){
  document.getElementById('s-run').textContent = state.overall.running;
  document.getElementById('s-idle').textContent = state.overall.idle;
  document.getElementById('s-threads').textContent = state.overall.threads;
  lastCards = state.cards||[];
  renderOverview(state);
  if(!lastCards.length){ grid.innerHTML=''; empty.style.display='block'; return; }
  empty.style.display='none';
  grid.innerHTML = lastCards.map(card).join('');
}

async function endThread(btn, ch, th){
  if(!confirm('End this thread? It will be stopped and its summary saved.')) return;
  btn.disabled = true; btn.textContent = 'Ending…';
  try{
    await fetch('/api/end',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({channel_id:ch,thread_ts:th})});
  }catch(e){ btn.disabled=false; btn.textContent='End'; }
}

/* ---- detail modal ---- */
function msgHTML(m){
  const who = m.role==='assistant' ? ('bot'+(m.model?(' · '+esc(m.model)):'')) : 'you';
  const long = (m.content||'').length > 600;
  const body = long
    ? `<div class="mtext clip">${esc(m.content)}</div><button class="more" onclick="toggleMore(this)">▾ more</button>`
    : `<div class="mtext">${esc(m.content)}</div>`;
  return `<div class="msg ${m.role==='assistant'?'assistant':'user'}"><div class="who">${who}</div>${body}</div>`;
}
function toggleMore(btn){
  const clipped = btn.previousElementSibling.classList.toggle('clip');
  btn.textContent = clipped ? '▾ more' : '▴ less';
}

function detailHTML(d){
  const summary = d.summary ? `<div class="sec"><h4>Summary</h4><div class="txt">${esc(d.summary)}</div></div>` : '';
  const plan = d.activity ? `<div class="sec"><h4>Current status / plan</h4><div class="txt">${esc(d.activity)}</div></div>` : '';
  const todos = (d.todos&&d.todos.length)
    ? `<div class="sec"><h4>Todos · ${d.done}/${d.total}</h4><div class="tlist">${d.todos.map(todoItem).join('')}</div></div>` : '';
  const pend = pendingSends.map(t =>
    `<div class="msg user queued"><div class="who">you · queued</div><div class="mtext">${esc(t)}</div></div>`).join('');
  const msgs = ((d.conversation&&d.conversation.length) ? d.conversation.map(msgHTML).join('') : '') + pend;
  const conv = `<div class="sec"><h4>Chat</h4>
      <div class="conv" id="conv">${msgs || '<div class="txt" style="color:#5b6378">No messages yet — say something below.</div>'}</div>
      <div class="sendrow">
        <input id="send-input" placeholder="Message this thread — the bot posts it to Slack and runs it…"
          onkeydown="if(event.key==='Enter')sendMsg()">
        <button onclick="sendMsg()">Send</button>
      </div>
      <div class="sendnote" id="send-msg">replies land here and in the Slack thread when the run finishes</div>
    </div>`;
  const tail = (d.tail&&d.tail.length)
    ? `<div class="sec"><h4>Recent output</h4><div class="tail">${d.tail.map(l=>`<div class="${l.startsWith('⚙')?'tool':''}">${esc(l)}</div>`).join('')}</div></div>` : '';
  return `<header>
      <span class="idx">${d.index||'•'}</span>
      <div><div style="color:var(--blue);font-size:12px;font-weight:600;margin-bottom:3px">#${esc(d.channel)}</div>
        <h3>${esc(d.title)}</h3></div>
      <button class="x" onclick="closeModal()">×</button>
    </header>
    <div class="body">
      <div style="margin-bottom:16px">${pillHTML(d)}</div>
      ${summary}${plan}${todos}${conv}${tail}
    </div>`;
}

let openCh = null, openTh = null, modalTimer = null, pendingSends = [];

async function fetchDetail(){
  return await (await fetch(`/api/thread?channel=${encodeURIComponent(openCh)}&thread=${encodeURIComponent(openTh)}`)).json();
}
function paintModal(d){
  const inp = document.getElementById('send-input');
  const val = inp ? inp.value : '';
  const foc = inp && document.activeElement === inp;
  document.getElementById('modal').innerHTML = detailHTML(d);
  const ni = document.getElementById('send-input');
  if (ni){ ni.value = val; if (foc) ni.focus(); }
  const cv = document.getElementById('conv');
  if (cv) cv.scrollTop = cv.scrollHeight;
}
async function refreshModal(){
  if (!openCh) return;
  try{
    const d = await fetchDetail();
    pendingSends = pendingSends.filter(t => !(d.conversation||[]).some(m => m.role==='user' && m.content===t));
    paintModal(d);
  }catch(_){}
}
async function openThread(ch, th){
  const bg = document.getElementById('modal-bg'), m = document.getElementById('modal');
  openCh = ch; openTh = th; pendingSends = [];
  m.innerHTML = '<div class="body" style="padding:48px;text-align:center;color:#8b93a7">Loading…</div>';
  bg.classList.add('open');
  try{
    paintModal(await fetchDetail());
  }catch(e){ m.innerHTML = '<div class="body" style="padding:40px;color:#f0506e">Failed to load thread.</div>'; }
  clearInterval(modalTimer);
  modalTimer = setInterval(refreshModal, 4000);
}
async function sendMsg(){
  const inp = document.getElementById('send-input');
  const text = (inp.value||'').trim();
  if (!text || !openCh) return;
  const r = await fetch('/api/send', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({channel_id: openCh, thread_ts: openTh, text})});
  const res = await r.json();
  const note = document.getElementById('send-msg');
  if (res.ok){
    pendingSends.push(text); inp.value = '';
    note.textContent = '✓ queued — the bot posts it to the Slack thread and runs it';
    try{ paintModal(await fetchDetail()); }catch(_){}
  } else {
    note.textContent = '✗ ' + (res.error || 'failed (settings/send are localhost-only)');
  }
}
function openByIndex(n){
  const c = lastCards.find(x=>x.index==n);
  if(c) openThread(c.channel_id, c.thread_ts);
}
function closeModal(){
  document.getElementById('modal-bg').classList.remove('open');
  clearInterval(modalTimer); modalTimer = null; openCh = null; openTh = null; pendingSends = [];
}
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeModal(); });

const conn = document.getElementById('conn');
function connect(){
  const es = new EventSource('/stream');
  es.onopen = () => conn.textContent = 'live';
  es.onmessage = e => { try{ render(JSON.parse(e.data)); }catch(_){} };
  es.onerror = () => { conn.textContent = 'reconnecting…'; es.close(); setTimeout(connect, 3000); };
}
connect();
</script>
</body>
</html>
"""


SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>⚙ research-bot settings</title>
<style>
  :root{--bg:#0c0e14;--panel:#151823;--panel2:#1b1f2e;--line:#262b3d;--txt:#e7eaf3;
    --muted:#8b93a7;--faint:#5b6378;--green:#3fb950;--amber:#d29922;--blue:#4493f8;
    --red:#f0506e;--accent:#7c5cff;--shadow:0 1px 3px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.25)}
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 80% -10%,#161a2b 0,var(--bg) 55%) fixed;
    color:var(--txt);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
  .wrap{max-width:880px;margin:0 auto;padding:28px 22px 64px}
  header{display:flex;align-items:center;gap:14px;margin-bottom:18px}
  .logo{width:38px;height:38px;border-radius:11px;flex:none;background:linear-gradient(135deg,var(--accent),#4493f8);
    display:grid;place-items:center;font-size:19px;box-shadow:var(--shadow)}
  header h1{font-size:18px;margin:0;font-weight:650}
  header .sub{color:var(--muted);font-size:12.5px}
  a.back{margin-left:auto;color:var(--muted);text-decoration:none;font-size:13px;
    padding:6px 12px;border:1px solid var(--line);border-radius:9px}
  a.back:hover{color:#fff;background:#222a3d}
  .note{background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.3);color:#e3c37a;
    border-radius:12px;padding:11px 15px;font-size:12.5px;margin-bottom:20px}
  section{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);
    border-radius:16px;padding:18px;box-shadow:var(--shadow);margin-bottom:18px}
  section h2{margin:0 0 13px;font-size:12px;letter-spacing:.7px;text-transform:uppercase;color:var(--muted)}
  .field{padding:11px 0;border-bottom:1px solid #1e2333}
  .field:last-of-type{border-bottom:none}
  .fhead{display:flex;align-items:baseline;gap:9px;margin-bottom:3px}
  .fhead .flabel{font-size:13.5px;font-weight:600}
  .fhead .fkey{font:10.5px ui-monospace,Menlo,Consolas,monospace;color:var(--faint)}
  .q{display:inline-grid;place-items:center;width:15px;height:15px;border-radius:50%;flex:none;
    background:#222a3d;color:#8b93a7;font-size:10px;font-weight:700;cursor:help;position:relative;
    align-self:center}
  .q:hover{background:var(--accent);color:#fff}
  .q::after{content:attr(data-tip);position:absolute;left:0;bottom:calc(100% + 8px);
    width:320px;background:#1d2231;border:1px solid var(--line);color:#c7cee0;font-size:11.5px;
    font-weight:400;line-height:1.45;padding:8px 11px;border-radius:9px;box-shadow:var(--shadow);
    opacity:0;pointer-events:none;transition:opacity .12s;z-index:10;text-align:left}
  .q:hover::after{opacity:1}
  .fdesc{color:var(--muted);font-size:12px;margin-bottom:8px;line-height:1.45}
  input,select{background:#0a0c12;border:1px solid var(--line);border-radius:8px;color:var(--txt);
    padding:7px 10px;font:12.5px ui-monospace,Menlo,Consolas,monospace;outline:none}
  input:focus,select:focus{border-color:var(--accent)}
  input.dirty,select.dirty{border-color:var(--amber)}
  input.wide{width:100%}
  .pair{display:flex;gap:8px;margin-bottom:7px;align-items:center}
  .pair input.pk{flex:0 0 220px}
  .pair input.pv{flex:1}
  .pair .del{flex:none;cursor:pointer;color:var(--faint);border:1px solid var(--line);background:none;
    border-radius:7px;width:26px;height:26px;font-size:13px;line-height:1;margin:0;padding:0}
  .pair .del:hover{color:var(--red);border-color:rgba(240,80,110,.5)}
  button{appearance:none;border:1px solid rgba(124,92,255,.45);background:rgba(124,92,255,.14);color:#b9a8ff;
    font-weight:600;font-size:12.5px;padding:8px 16px;border-radius:9px;cursor:pointer;margin-top:10px}
  button:hover{background:rgba(124,92,255,.26)}
  button.ghost{border-color:var(--line);background:none;color:var(--muted)}
  button.ghost:hover{color:#fff;background:#222a3d}
  button.mini{padding:4px 11px;font-size:11.5px;margin-top:2px}
  textarea{width:100%;background:#0a0c12;border:1px solid var(--line);border-radius:10px;color:var(--txt);
    padding:11px 13px;font:12.5px/1.6 ui-monospace,Menlo,Consolas,monospace;outline:none;resize:vertical}
  textarea:focus{border-color:var(--accent)}
  .msg{margin-left:12px;font-size:12.5px}
  .msg.ok{color:var(--green)} .msg.err{color:var(--red)}
  .tabs{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:11px}
  .tab{font-size:12px;font-weight:600;padding:5px 12px;border-radius:999px;cursor:pointer;
    background:#222a3d;color:#9fb0d0;border:1px solid var(--line)}
  .tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
  .hint{color:var(--faint);font-size:11.5px;margin-top:9px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">⚙</div>
    <div><h1>settings</h1><div class="sub">research-bot configuration · localhost only</div></div>
    <a class="back" href="/">← dashboard</a>
  </header>

  <div class="note">Changes to <b>settings below</b> take effect after a bot restart
    (<code>tmux kill-session -t bot</code> → start again).
    <b>Writing style</b> and <b>recipes</b> apply from the next command — no restart.</div>

  <section>
    <h2>Bot settings — .env</h2>
    <div id="env-rows"></div>
    <div class="pair" style="margin-top:12px"><input id="new-key" class="pk" placeholder="NEW_KEY">
      <input id="new-val" class="pv" placeholder="value"></div>
    <button onclick="saveEnv()">Save settings</button><span class="msg" id="env-msg"></span>
    <div class="hint">🔒 secret values are masked — type a new value to replace one, leave blank to keep it.</div>
  </section>

  <section>
    <h2>Writing style — STYLE.md</h2>
    <div class="fdesc">Rules injected into every prompt: words the agent must not use, tone, formatting. Applies from the next command.</div>
    <textarea id="style-text" rows="14" spellcheck="false"></textarea>
    <button onclick="saveStyle()">Save STYLE.md</button><span class="msg" id="style-msg"></span>
  </section>

  <section>
    <h2>Recipe playbooks — recipes/</h2>
    <div class="fdesc">Task playbooks (plotting, monitoring, Slurm, …). The agent sees a one-line index and reads the full recipe when a task matches.</div>
    <div class="tabs" id="recipe-tabs"></div>
    <textarea id="recipe-text" rows="14" spellcheck="false"></textarea>
    <button onclick="saveRecipe()">Save recipe</button>
    <button class="ghost" onclick="newRecipe()">+ new recipe</button>
    <span class="msg" id="recipe-msg"></span>
  </section>
</div>

<script>
const esc = s => (s||"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* Human-readable metadata for known keys. Unknown keys render as plain text rows. */
const META = {
  SLACK_BOT_TOKEN:  {label:"Slack bot token", desc:"xoxb-… token from your Slack app's OAuth & Permissions page."},
  SLACK_APP_TOKEN:  {label:"Slack app token", desc:"xapp-… app-level token with connections:write (Socket Mode)."},
  OPENAI_API_KEY:   {label:"OpenAI API key", desc:"Only needed for the Codex runner (alternatively run `codex login` once)."},
  SLACK_WEBHOOK_URL:{label:"Slack webhook URL", desc:"Fallback destination for notify.py when no channel is given."},
  MACHINE_NAME:     {label:"Machine name", desc:"How this machine is addressed in Slack commands: /<name>."},
  WORK_DIR:         {label:"Working directory", desc:"Absolute path where the agent works by default."},
  IS_GATEWAY:       {label:"Gateway machine", type:"select", options:["true","false"],
                     desc:"The gateway is the machine that receives Slack messages and routes them — locally or over SSH to workers. Keep true unless this is a secondary bot instance that should only answer commands addressed to it."},
  CONTEXT_GIT_SYNC: {label:"Sync contexts via git", type:"select", options:["true","false"],
                     desc:"After each command the bot updates contexts/<channel>.md (the channel's memory). With sync on, that file is git-committed and pushed so your other machines pull the same memory. Turn off on single-machine setups — contexts then stay as plain local files."},
  ALLOWED_USERS:    {label:"Allowed Slack users", desc:"Comma-separated Slack member IDs allowed to command the bot. Empty = everyone in the workspace can run code!"},
  KNOWN_MACHINES:   {label:"Known machines", desc:"Override the machine list (comma-separated). Usually derived automatically from this machine + SSH workers."},
  CLAUDE_MODEL:     {label:"Claude model", type:"select", options:["","claude-fable-5","opus","sonnet","haiku"],
                     optionLabels:{"":"(claude CLI default)"}, desc:"Default model for the Claude Code runner. Override per message with --model claude/<name>."},
  CODEX_MODEL:      {label:"Codex model", type:"select", options:["gpt-5.5","gpt-5-codex","gpt-4.1","o3"],
                     desc:"Default model for the Codex runner. Override per message with --model codex/<name>."},
  ATTACHMENT_MODE:  {label:"Attachment mode", type:"select", options:["full","text","none"],
                     desc:"full: agent may read any Slack attachment · text: only text-like files, media is skipped to save tokens · none: ignore attachments. Override per message with --attachments."},
  CHANNEL_NAMES:    {label:"Channel names", type:"pairs", kPh:"name (e.g. my-project)", vPh:"channel ID (C0…)",
                     desc:"Friendly names for Slack channels, used by terminal mode (/channel, /slack)."},
  CHANNEL_DEFAULTS: {label:"Channel default machines", type:"pairs", kPh:"channel ID (C0…)", vPh:"machine name",
                     desc:"Give a channel its own default target machine (used when a message has no /machine)."},
  IDEAS_VAULT:      {label:"Idea notes folder (/idea)", desc:"Folder — an Obsidian-style vault — where the /idea command files new research ideas. Leave empty to disable /idea."},
  SUMMARY_MODEL:    {label:"Dashboard summary model", type:"select", options:["haiku","sonnet"],
                     desc:"Small model used for the one-line thread summaries on the dashboard."},
  DASHBOARD_HOST:   {label:"Dashboard bind host", desc:"0.0.0.0 = reachable on your network (cards only; this settings page stays localhost-only)."},
  DASHBOARD_PORT:   {label:"Dashboard port", desc:"Default 8080."},
  DASHBOARD_AUTO_END_HOURS:{label:"Auto-end idle threads (hours)", desc:"Threads with no activity for this long are ended automatically. 0 = never."},
};
function metaFor(key){
  if (META[key]) return META[key];
  if (key.startsWith("SSH_")) return {label:"SSH worker: "+key.slice(4), desc:"user@host[:port] — this machine becomes addressable as /"+key.slice(4)+"."};
  return {label:key, desc:""};
}

let recipes = [], cur = null;
const dirty = {}, pairs = {};

function pairRows(key){
  return pairs[key].map((p,i)=>`
    <div class="pair">
      <input class="pk" value="${esc(p[0])}" placeholder="${esc(metaFor(key).kPh||'key')}" oninput="pairs['${key}'][${i}][0]=this.value;pairDirty('${key}')">
      <input class="pv" value="${esc(p[1])}" placeholder="${esc(metaFor(key).vPh||'value')}" oninput="pairs['${key}'][${i}][1]=this.value;pairDirty('${key}')">
      <button class="del" title="remove" onclick="pairs['${key}'].splice(${i},1);pairDirty('${key}');renderPairs('${key}')">✕</button>
    </div>`).join('');
}
function renderPairs(key){ document.getElementById('pairs-'+key).innerHTML = pairRows(key); }
function pairDirty(key){
  dirty[key] = pairs[key].filter(p=>p[0].trim()&&p[1].trim()).map(p=>p[0].trim()+':'+p[1].trim()).join(',');
}
function addPair(key){ pairs[key].push(['','']); renderPairs(key); }

function controlHTML(e){
  const m = metaFor(e.key);
  if (m.type === 'pairs'){
    pairs[e.key] = (e.value||'').split(',').filter(s=>s.includes(':')).map(s=>{const i=s.indexOf(':');return [s.slice(0,i).trim(), s.slice(i+1).trim()];});
    return `<div id="pairs-${e.key}">${pairRows(e.key)}</div>
      <button class="ghost mini" onclick="addPair('${e.key}')">+ add</button>`;
  }
  if (m.type === 'select'){
    let opts = [...(m.options||[])];
    if (!opts.includes(e.value)) opts.unshift(e.value);
    const o = opts.map(v=>`<option value="${esc(v)}" ${v===e.value?'selected':''}>${esc((m.optionLabels&&m.optionLabels[v])||v||'(empty)')}</option>`).join('')
            + `<option value="__custom__">custom…</option>`;
    return `<select data-key="${esc(e.key)}" onchange="selChange(this)">${o}</select>`;
  }
  if (e.secret){
    return `<input class="wide" data-key="${esc(e.key)}" value="" placeholder="${esc(e.value)}"
      oninput="dirty[this.dataset.key]=this.value;this.classList.add('dirty')">`;
  }
  return `<input class="wide" data-key="${esc(e.key)}" value="${esc(e.value)}"
    oninput="dirty[this.dataset.key]=this.value;this.classList.add('dirty')">`;
}
function selChange(sel){
  if (sel.value === '__custom__'){
    const v = prompt('Custom value for ' + sel.dataset.key + ':');
    if (v === null){ sel.value = sel.querySelector('option[selected]')?.value ?? sel.options[0].value; return; }
    const opt = document.createElement('option'); opt.value = v; opt.textContent = v || '(empty)';
    sel.insertBefore(opt, sel.querySelector('option[value="__custom__"]')); sel.value = v;
  }
  dirty[sel.dataset.key] = sel.value; sel.classList.add('dirty');
}

async function load(){
  const d = await (await fetch('/api/settings')).json();
  document.getElementById('env-rows').innerHTML = d.env.map(e=>{
    const m = metaFor(e.key);
    return `<div class="field">
      <div class="fhead"><span class="flabel">${e.secret?'🔒 ':''}${esc(m.label)}</span>${m.desc?`<span class="q" data-tip="${esc(m.desc)}">?</span>`:''}<span class="fkey">${esc(e.key)}</span></div>
      ${controlHTML(e)}
    </div>`;
  }).join('');
  document.getElementById('style-text').value = d.style || '';
  recipes = d.recipes || [];
  if (recipes.length && cur === null) cur = recipes[0].name;
  renderTabs();
}
function renderTabs(){
  document.getElementById('recipe-tabs').innerHTML = recipes.map(r =>
    `<span class="tab ${r.name===cur?'on':''}" onclick="pick('${esc(r.name)}')">${esc(r.name)}</span>`).join('');
  const r = recipes.find(x => x.name === cur);
  document.getElementById('recipe-text').value = r ? r.content : '';
}
function pick(name){ cur = name; renderTabs(); }
function newRecipe(){
  const name = prompt('Recipe file name (e.g. deploy.md):');
  if (!name) return;
  if (!/^[A-Za-z0-9._-]+\.md$/.test(name)) { alert('Name must be like deploy.md'); return; }
  recipes.push({name, content: `# ${name.replace(/\.md$/,'')}\nOne-line description of when to use this recipe.\n\n`});
  cur = name; renderTabs();
}
function flash(id, ok, text){
  const el = document.getElementById(id);
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  el.textContent = text;
  setTimeout(() => { el.textContent = ''; }, 6000);
}
async function post(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  return await r.json();
}
async function saveEnv(){
  const updates = {...dirty};
  const nk = document.getElementById('new-key').value.trim();
  const nv = document.getElementById('new-val').value;
  if (nk) updates[nk] = nv;
  if (!Object.keys(updates).length) { flash('env-msg', false, 'nothing changed'); return; }
  const res = await post('/api/settings/env', {updates});
  flash('env-msg', res.ok, res.ok ? res.note : res.error);
  if (res.ok) { Object.keys(dirty).forEach(k => delete dirty[k]);
    document.getElementById('new-key').value=''; document.getElementById('new-val').value=''; load(); }
}
async function saveStyle(){
  const res = await post('/api/settings/file', {target:'style', content: document.getElementById('style-text').value});
  flash('style-msg', res.ok, res.ok ? res.note : res.error);
}
async function saveRecipe(){
  if (!cur) { flash('recipe-msg', false, 'no recipe selected'); return; }
  const content = document.getElementById('recipe-text').value;
  const r = recipes.find(x => x.name === cur); if (r) r.content = content;
  const res = await post('/api/settings/file', {target:'recipe', name: cur, content});
  flash('recipe-msg', res.ok, res.ok ? res.note : res.error);
}
load();
</script>
</body>
</html>
"""


# --- Standalone entry point (must follow PAGE_HTML so the routes can see it) ---

if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BOT_DIR, ".env"))
    except Exception:
        pass
    run_server(
        host=os.environ.get("DASHBOARD_HOST", "0.0.0.0"),
        port=int(os.environ.get("DASHBOARD_PORT", "8080")),
    )
