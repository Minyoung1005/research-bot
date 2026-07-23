import subprocess
import threading
import time
import os
import re
import json
import uuid
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
import history
import dashboard

load_dotenv()

# --- Config ---
SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN  = os.environ["SLACK_APP_TOKEN"]
MACHINE_NAME     = os.environ["MACHINE_NAME"]
WORK_DIR         = os.environ.get("WORK_DIR", os.path.expanduser("~/Projects"))
BOT_DIR          = os.path.dirname(os.path.abspath(__file__))
CONTEXT_DIR      = os.path.join(BOT_DIR, "contexts")
UPLOAD_DIR       = os.path.join(BOT_DIR, "uploads")
STYLE_FILE       = os.path.join(BOT_DIR, "STYLE.md")
RECIPES_DIR      = os.path.join(BOT_DIR, "recipes")
IS_GATEWAY       = os.environ.get("IS_GATEWAY", "true").lower() == "true"
ALLOWED_USERS    = set(os.environ.get("ALLOWED_USERS", "").split(",")) - {""}
# Auto-commit + push contexts/<channel>.md after each command so multiple machines
# share channel memory through git. Set CONTEXT_GIT_SYNC=false (recommended for
# single-machine setups) to keep contexts as plain local files — nothing is ever
# committed or pushed automatically.
CONTEXT_GIT_SYNC = os.environ.get("CONTEXT_GIT_SYNC", "true").lower() == "true"

# SSH_HOSTS: { machine_name: (host, port) } — one entry per SSH_<name>=user@host[:port]
# line in .env (SSH_AUTH_SOCK / SSH_AGENT_PID are skipped).
SSH_HOSTS = {}
for k, v in os.environ.items():
    if k.startswith("SSH_") and k not in ("SSH_AUTH_SOCK", "SSH_AGENT_PID"):
        machine = k[4:]
        if ":" in v:
            host, port = v.rsplit(":", 1)
            SSH_HOSTS[machine] = (host, port)
        else:
            SSH_HOSTS[machine] = (v, "22")

# Machines addressable as /<name> in a command. Defaults to this machine plus every
# SSH_<name> entry; workers should set KNOWN_MACHINES=name1,name2,... explicitly so
# they can recognize (and ignore) commands routed to their siblings.
KNOWN_MACHINES = [m.strip() for m in os.environ.get("KNOWN_MACHINES", "").split(",") if m.strip()] \
                 or sorted({MACHINE_NAME, *SSH_HOSTS})

# Per-channel default machine: channel_id -> machine_name, configured as
# CHANNEL_DEFAULTS=C0AAAAAAAAA:machine1,C0BBBBBBBBB:machine2 in .env.
# If a channel isn't listed, falls back to MACHINE_NAME (this machine).
CHANNEL_DEFAULTS = dict(
    pair.strip().split(":", 1)
    for pair in os.environ.get("CHANNEL_DEFAULTS", "").split(",")
    if ":" in pair
)
# Attachment handling: "full" (default) downloads every attachment for the agent;
# "text" keeps only text-like files (code, logs, configs, csv) — images, video,
# documents and archives are skipped BEFORE download, so token-hungry media never
# reaches the agent's context; "none" ignores attachments entirely.
# Per-message override: --attachments full|text|none
ATTACHMENT_MODE = os.environ.get("ATTACHMENT_MODE", "full").lower()
TEXT_ATTACHMENT_EXTS = {
    ".txt", ".md", ".rst", ".py", ".sh", ".js", ".ts", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv", ".log", ".tex", ".patch", ".diff",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".html", ".css",
}
HISTORY_KEYWORDS = ["summary", "history", "what have we done", "what did we do", "recap"]
LANG_INSTRUCTION = (
    "Default to responding in English. "
    "If the user explicitly asks you to chat in another language "
    "(e.g. 'explain in Korean', '한국어로 설명해줘', 'reply in Japanese'), "
    "honor that request — but ONLY for the conversational chat reply itself. "
    "All artifacts you produce — code, comments, docstrings, commit messages, file names, "
    "documentation, slide content, paper text, plot labels, log messages, anything written to a file — "
    "must always remain in English regardless of the chat language. "
    "Technical terms (function names, library names, CLI flags, etc.) stay in English even in Korean chat. "
    "After making any code changes, always run `git add -A && git commit -m '<brief description of changes>'` "
    "in the relevant project directory to track your work. "
    "Do not push unless explicitly asked. "
    "For long-running tasks like training, evaluation, or data collection that may take more than a few minutes: "
    "always run them in a background tmux session (e.g. `tmux new-session -d -s training '...'`), "
    "report back immediately with the session name and how to monitor progress (e.g. `tail -f training.log`), "
    f"and append `python {BOT_DIR}/notify.py \"✅ done\" --log <logfile> "
    "--channel {CHANNEL_ID} --thread {THREAD_TS}` to the command so the notification "
    "goes back to the correct Slack channel and thread. "
    "IMPORTANT: you are running non-interactively (headless) through a Slack bot — your process "
    "exits when your turn ends and you will NOT be re-invoked. Never end your turn with only tool "
    "calls, never schedule wake-ups or defer work to a future check-in; if something needs future "
    "monitoring, set up a background tmux job with notify.py as described above. ALWAYS finish your "
    "turn with a final text message summarizing status — that text is the only thing sent to Slack."
)
# Optional Obsidian-style research-ideas vault (LLM-wiki pattern) for /idea capture.
# Leave unset to disable the /idea command.
IDEAS_VAULT = os.environ.get("IDEAS_VAULT", "")
IDEA_INSTRUCTION = (
    "You are the librarian of the research ideas vault at {vault} — an Obsidian vault "
    "managed with the claude-obsidian LLM-wiki pattern. Conventions live in {vault}/WIKI.md "
    "(frontmatter schema in section 3); the catalog is {vault}/wiki/index.md.\n"
    "File the raw idea below into the wiki:\n"
    "1. Read wiki/index.md and skim the most related existing pages first.\n"
    "2. If the idea duplicates an existing page, UPDATE that page (append a section, bump "
    "`updated:`) instead of creating a new one.\n"
    "3. Otherwise create ONE new atomic page — usually wiki/concepts/<Title>.md, or "
    "wiki/questions/<Title>.md if it is an open question — with full frontmatter "
    "(type, title, created/updated = today, tags, status: seed, related).\n"
    "4. Link densely: [[wikilinks]] to related existing pages in the body and `related:` list, "
    "and add a back-reference to the new page in the 1-3 most related existing pages.\n"
    "5. Update wiki/index.md, the folder _index.md, and the Recent Changes section of wiki/hot.md.\n"
    "6. Commit inside {vault}: `git add -A && git commit -m 'idea: <short-slug>'`. Do not push.\n"
    "7. Reply with the page title, where it was filed (created vs updated), and which pages "
    "it was linked to.\n\n"
    "=== Raw idea ===\n{idea}"
)
SESSION_MAX_TURNS    = 6
THREAD_HISTORY_LIMIT = 10
TMUX_DONE_MARKER     = "___CLAUDE_DONE___"
MAX_CONCURRENT       = 5
# "On the dashboard" indicator the bot adds to a thread's ROOT message and removes
# on archive/end. 👁 (:eye:) by default — deliberately distinct from the per-message
# 👀 ack. Bot-owned: user taps on it are ignored (📁 is the user-owned toggle).
ALIVE_REACTION       = os.environ.get("ALIVE_REACTION", "eye")
# Emoji the USER adds to archive a thread (single tap). The bot then removes its own
# legacy party-blob if one is present — Slack only lets you delete your own reactions.
ARCHIVE_REACTION     = os.environ.get("ARCHIVE_REACTION", "file_folder")
CLAUDE_MODEL         = os.environ.get("CLAUDE_MODEL", "")  # empty = claude CLI's default model
CODEX_MODEL          = os.environ.get("CODEX_MODEL", "gpt-4.1")
DEFAULT_OPENAI_MODEL = os.environ.get("DEFAULT_OPENAI_MODEL", CODEX_MODEL)

# Model registry: maps provider prefix -> runner type and default model.
# Keys are matched against the leading component of the --model flag value.
# e.g. --model codex         -> runner=codex, model=gpt-4.1
#      --model codex/gpt-4o  -> runner=codex, model=gpt-4o
#      --model gpt-4.1       -> runner=codex, model=gpt-4.1  (bare OpenAI model name)
#      --model claude         -> runner=claude, model=None (use CLAUDE_MODEL env var)
#      --model claude/sonnet  -> runner=claude, model=sonnet
MODELS = {
    "codex":  {"runner": "codex",  "default_model": CODEX_MODEL},
    "gpt-4":  {"runner": "codex",  "default_model": CODEX_MODEL},
    "gpt-3":  {"runner": "codex",  "default_model": CODEX_MODEL},
    "o1":     {"runner": "codex",  "default_model": "o1"},
    "o3":     {"runner": "codex",  "default_model": "o3"},
    "claude": {"runner": "claude", "default_model": None},
}

os.makedirs(CONTEXT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
history.init_db()

app = App(token=SLACK_BOT_TOKEN)
thread_sessions = {}
active_sessions = {}  # { session_id: thread_ts } for cancellation
active_lock = threading.Lock()

# Session ids cancelled via the dashboard "End" button or the ❌ reaction. A run
# loop that finds its id here delivers nothing and exits quietly.
cancelled_sessions = set()
# Dashboard drops End requests here; a watcher thread (started in __main__) acts on them.
CANCEL_DIR = os.path.join(BOT_DIR, "cancel")
os.makedirs(CANCEL_DIR, exist_ok=True)
# Dashboard "send follow-up" requests land here; a watcher posts each into its
# Slack thread (for the record) and runs it through the normal pipeline.
INBOX_DIR = os.path.join(BOT_DIR, "inbox")
os.makedirs(INBOX_DIR, exist_ok=True)
DASHBOARD_USER = "__dashboard__"


def _consume_cancel(session_id, say, thread_ts):
    """If this run was cancelled, post a stop notice and report True so the loop bails."""
    if session_id in cancelled_sessions:
        cancelled_sessions.discard(session_id)
        try:
            say(text=f"🛑 `{MACHINE_NAME}` stopped — thread ended from dashboard.", thread_ts=thread_ts)
        except Exception:
            pass
        return True
    return False


# --- In-flight run registry (survives bot restarts, for result recovery) ---
# Each run records itself here on start and removes itself on clean exit. A hard
# kill (e.g. bot restart) skips the removal, so the record survives and the next
# bot instance can re-watch the tmux output and deliver the result it missed.
ACTIVE_RUNS_FILE = os.path.join(BOT_DIR, "active_runs.json")
active_runs_lock = threading.Lock()


def _load_active_runs():
    try:
        with open(ACTIVE_RUNS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_active_runs(data):
    tmp = ACTIVE_RUNS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, ACTIVE_RUNS_FILE)


def record_run(session_id, info):
    with active_runs_lock:
        runs = _load_active_runs()
        runs[session_id] = info
        _save_active_runs(runs)


def unrecord_run(session_id):
    with active_runs_lock:
        runs = _load_active_runs()
        if runs.pop(session_id, None) is not None:
            _save_active_runs(runs)


# --- Session ID helpers ---

def make_session_id(thread_ts):
    """Unique tmux session id per command — use timestamp to allow multiple commands in same thread."""
    safe = str(time.time()).replace(".", "_")
    return f"claude-{safe}"


# --- Per-thread Claude Code session UUID (for --resume) ---

THREAD_CLAUDE_SESSIONS_FILE = os.path.join(BOT_DIR, "thread_claude_sessions.json")
thread_claude_sessions_lock = threading.Lock()


def load_thread_claude_sessions():
    try:
        with open(THREAD_CLAUDE_SESSIONS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_thread_claude_sessions(data):
    tmp = THREAD_CLAUDE_SESSIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, THREAD_CLAUDE_SESSIONS_FILE)


thread_claude_sessions = load_thread_claude_sessions()


def get_claude_session_uuid(channel_id, thread_ts, machine):
    key = f"{channel_id}:{thread_ts}"
    return thread_claude_sessions.get(key, {}).get(machine)


def set_claude_session_uuid(channel_id, thread_ts, machine, session_uuid):
    key = f"{channel_id}:{thread_ts}"
    with thread_claude_sessions_lock:
        if key not in thread_claude_sessions:
            thread_claude_sessions[key] = {}
        thread_claude_sessions[key][machine] = session_uuid
        save_thread_claude_sessions(thread_claude_sessions)


def clear_claude_session_uuid(channel_id, thread_ts, machine=None):
    """Drop a stale session id (e.g. when --resume fails). If machine is None, drop all for that thread."""
    key = f"{channel_id}:{thread_ts}"
    with thread_claude_sessions_lock:
        if key not in thread_claude_sessions:
            return
        if machine is None:
            thread_claude_sessions.pop(key, None)
        else:
            thread_claude_sessions[key].pop(machine, None)
            if not thread_claude_sessions[key]:
                thread_claude_sessions.pop(key, None)
        save_thread_claude_sessions(thread_claude_sessions)


# Pseudo-machine key under which a thread's Codex session UUID is stored in
# thread_claude_sessions.json (alongside the real machine key for Claude), so
# Codex gets the same per-thread resume behavior as Claude without a new file.
CODEX_SESSION_KEY = "codex"


def extract_codex_session_id(raw):
    """Pull the session UUID Codex prints in its run header ('session id: <uuid>')."""
    m = re.search(r"session id:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  raw, re.IGNORECASE)
    return m.group(1) if m else None


def output_file_for(session_id):
    return f"/tmp/claude_out_{session_id}.txt"


def watch_file_for(session_id):
    return f"/tmp/claude_watch_{session_id}.txt"


# --- Access control ---

def is_allowed(user_id):
    if user_id == DASHBOARD_USER:
        return True   # dashboard sends are gated to localhost on the dashboard side
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


# --- SSH helper ---

def ssh_cmd(machine, remote_cmd):
    host, port = SSH_HOSTS.get(machine, ("", "22"))
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", port, host, remote_cmd]


# --- Context (per channel) ---

def context_file(channel_id):
    return os.path.join(CONTEXT_DIR, f"{channel_id}.md")


def git_pull():
    if not CONTEXT_GIT_SYNC:
        return
    try:
        subprocess.run(["git", "pull", "--rebase"], cwd=BOT_DIR, capture_output=True, timeout=15)
    except Exception:
        pass


def git_push(channel_id):
    if not CONTEXT_GIT_SYNC:
        return
    try:
        path = context_file(channel_id)
        subprocess.run(["git", "add", path], cwd=BOT_DIR, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "-m", f"update context: {channel_id} [{MACHINE_NAME}]"],
            cwd=BOT_DIR, capture_output=True, timeout=10
        )
        subprocess.run(["git", "push"], cwd=BOT_DIR, capture_output=True, timeout=15)
    except Exception:
        pass


def load_context(channel_id):
    git_pull()
    path = context_file(channel_id)
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    return "(no project context saved yet)"


def update_context(command, output, channel_id):
    try:
        current = load_context(channel_id)
        prompt = (
            f"{LANG_INSTRUCTION}{load_style_guide()}\n\n"
            f"Current project context:\n{current}\n\n"
            f"We just ran this command:\n{command}\n\n"
            f"Result summary:\n{output[:500]}\n\n"
            f"Update the context concisely (what was done, current status, next steps). "
            f"Keep it under 500 characters."
        )
        env = os.environ.copy()
        env["HOME"] = os.path.expanduser("~")
        result = subprocess.run(
            ["claude", "-p", prompt, "--dangerously-skip-permissions"],
            capture_output=True, text=True, cwd=WORK_DIR, timeout=60, env=env
        )
        if result.stdout:
            with open(context_file(channel_id), "w") as f:
                f.write(result.stdout.strip())
            threading.Thread(target=git_push, args=(channel_id,), daemon=True).start()
    except Exception:
        pass


# --- Session history ---

def session_key(channel_id, thread_ts):
    return f"{channel_id}:{thread_ts}"


def get_session(channel_id, thread_ts):
    return thread_sessions.get(session_key(channel_id, thread_ts), [])


def add_to_session(channel_id, thread_ts, role, content):
    key = session_key(channel_id, thread_ts)
    if key not in thread_sessions:
        thread_sessions[key] = []
    thread_sessions[key].append({"role": role, "content": content})
    while len(thread_sessions[key]) > SESSION_MAX_TURNS * 2:
        thread_sessions[key].pop(0)
        thread_sessions[key].pop(0)


def fetch_thread_history(client, channel_id, thread_ts):
    try:
        result = client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=THREAD_HISTORY_LIMIT
        )
        for m in result.get("messages", []):
            role = "assistant" if m.get("bot_id") else "user"
            text = m.get("text", "").strip()
            if text:
                add_to_session(channel_id, thread_ts, role, text)
    except Exception:
        pass


def load_style_guide():
    """Optional writing-style rules (STYLE.md at the repo root) injected into every
    prompt. Re-read on each command so edits apply immediately — no restart needed.
    A missing or empty file disables the feature."""
    try:
        with open(STYLE_FILE) as f:
            text = f.read().strip()
    except FileNotFoundError:
        return ""
    if not text:
        return ""
    return ("\n\n=== Writing style rules (apply to ALL output: replies, code, "
            "comments, commits, docs) ===\n" + text)


def load_recipes_index():
    """One-line index of recipes/*.md (title + first paragraph line) injected into
    first-turn prompts. The agent reads a full recipe file only when the task matches,
    so the standing token cost stays tiny. Drop new .md files in recipes/ to extend."""
    try:
        names = sorted(f for f in os.listdir(RECIPES_DIR) if f.endswith(".md"))
    except FileNotFoundError:
        return ""
    lines = []
    for fname in names:
        path = os.path.join(RECIPES_DIR, fname)
        title, desc = fname, ""
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("#") and title == fname:
                        title = line.lstrip("# ").strip()
                    elif not line.startswith("#"):
                        desc = line
                        break
        except Exception:
            continue
        lines.append(f"- {path} — {title}: {desc}")
    if not lines:
        return ""
    return ("\n\n=== Recipe playbooks ===\nBefore starting a task that matches one of "
            "these, READ that file first and follow its conventions:\n" + "\n".join(lines))


def build_prompt(channel_id, thread_ts, command, context):
    history = get_session(channel_id, thread_ts)
    # Inject actual channel/thread into instruction so notify.py gets correct values
    instruction = LANG_INSTRUCTION.replace("{CHANNEL_ID}", channel_id).replace("{THREAD_TS}", thread_ts)
    instruction += load_style_guide() + load_recipes_index()
    channel_scope = (
        f"You are operating exclusively in Slack channel {channel_id}. "
        f"Only reference the project context and conversation history provided below. "
        f"Do NOT reference, assume, or draw from work, experiments, or discussions in other channels."
    )
    parts = [instruction, channel_scope, f"\n=== Project Context ===\n{context}"]
    if history:
        parts.append("\n=== Conversation History ===")
        for turn in history:
            role = "User" if turn["role"] == "user" else "Assistant"
            parts.append(f"[{role}]: {turn['content']}")
    parts.append(f"\n=== Current Command ===\n{command}")
    return "\n".join(parts)


# --- File handling ---

def _safe_filename(name):
    """Strip any path components and dangerous characters from a Slack-supplied filename."""
    name = os.path.basename(name or "file")
    name = re.sub(r"[^\w.\-]", "_", name)
    return name or "file"


def download_slack_file(url, filename, event_ts):
    """Download into uploads/<event_ts>/<safe_name> so files don't pollute WORK_DIR or collide."""
    bucket = re.sub(r"[^\w.\-]", "_", str(event_ts) or "misc")
    target_dir = os.path.join(UPLOAD_DIR, bucket)
    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, _safe_filename(filename))
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=120)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        f.write(resp.content)
    return dest


def attachment_allowed(name, mimetype, mode):
    """Attachment-mode policy: "full" accepts everything, "text" only text-like
    files, "none" rejects all."""
    if mode == "none":
        return False
    if mode == "text":
        ext = os.path.splitext(name or "")[1].lower()
        return ext in TEXT_ATTACHMENT_EXTS or (mimetype or "").startswith("text/")
    return True


def extract_files_from_event(event, mode=None):
    """Download the message's attachments, honoring the attachment mode. Files the
    mode rejects are never downloaded, so they can't reach the agent's context.
    Returns (saved, skipped_names)."""
    mode = mode or ATTACHMENT_MODE
    files = event.get("files", [])
    saved, skipped = [], []
    event_ts = event.get("ts") or event.get("event_ts") or "misc"
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        name = f.get("name", f.get("id", "file"))
        if not attachment_allowed(name, f.get("mimetype", ""), mode):
            skipped.append(name)
            continue
        if url:
            try:
                local = download_slack_file(url, name, event_ts)
                saved.append((local, name))
            except Exception as e:
                saved.append((None, f"{name} (download failed: {e})"))
    return saved, skipped


# --- Distributing files to remote workers ---

def scp_cmd(machine, local_path, remote_path):
    host, port = SSH_HOSTS.get(machine, ("", "22"))
    return ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-P", port, "-p", local_path, f"{host}:{remote_path}"]


def distribute_files_to_machine(target_machine, attached_files):
    """SCP each downloaded file to the same absolute path on the remote worker.
    Returns the list unchanged so paths in the prompt stay valid."""
    if not attached_files or target_machine == MACHINE_NAME:
        return attached_files
    if target_machine not in SSH_HOSTS:
        return attached_files
    # mkdir parent dirs on remote, then scp each file
    remote_dirs = sorted({os.path.dirname(p) for p, _ in attached_files if p})
    if remote_dirs:
        mkdir_cmd = " && ".join(f"mkdir -p {d}" for d in remote_dirs)
        try:
            subprocess.run(ssh_cmd(target_machine, mkdir_cmd),
                           timeout=15, capture_output=True)
        except Exception as e:
            print(f"[scp] mkdir failed on {target_machine}: {e}", flush=True)
    for local_path, name in attached_files:
        if not local_path:
            continue
        try:
            r = subprocess.run(
                scp_cmd(target_machine, local_path, local_path),
                capture_output=True, text=True, timeout=300,
            )
            if r.returncode != 0:
                print(f"[scp] {target_machine} {local_path} -> exit={r.returncode} stderr={r.stderr.strip()[:200]}", flush=True)
            else:
                print(f"[scp] {target_machine} <- {local_path}", flush=True)
        except Exception as e:
            print(f"[scp] {target_machine} {local_path} failed: {e}", flush=True)
    return attached_files


def upload_files_to_slack(client, channel_id, thread_ts, file_paths):
    for path in file_paths:
        try:
            with open(path, "rb") as f:
                client.files_upload(
                    channels=channel_id,
                    thread_ts=thread_ts,
                    file=f,
                    filename=os.path.basename(path),
                )
        except Exception:
            pass


def find_output_files(output_text, work_dir):
    candidates = re.findall(r"[\w./\-]+\.(?:png|jpg|jpeg|gif|pdf|csv|txt|json|mp4|svg)", output_text)
    found = []
    for c in candidates:
        for path in [c, os.path.join(work_dir, c)]:
            if os.path.exists(path) and path not in found:
                found.append(path)
    return found


# --- Ack reaction ---

def ack_reaction(client, channel_id, ts, add=True):
    try:
        if add:
            client.reactions_add(channel=channel_id, timestamp=ts, name="eyes")
        else:
            client.reactions_remove(channel=channel_id, timestamp=ts, name="eyes")
    except Exception:
        pass


# --- Usage limit detection ---

USAGE_LIMIT_PHRASES = [
    "usage limit", "rate limit", "rate_limit_error",
    "too many requests", "quota exceeded", "overloaded_error",
]

def detect_usage_limit(raw):
    """Return True only if Claude's output contains a genuine usage/rate-limit
    ERROR event. We deliberately do NOT substring-scan the whole output: phrases
    like "rate limit" legitimately appear in assistant text and in file/tool
    content (e.g. simulation code with a `rate_limiter`), which previously caused
    false positives that discarded real answers. Only structured error events count."""
    # Check JSON error events
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            # stream-json error event
            err = obj.get("error", {})
            if isinstance(err, dict) and any(
                phrase in (err.get("type", "") + err.get("message", "")).lower()
                for phrase in USAGE_LIMIT_PHRASES
            ):
                return True
            # result event with is_error
            if obj.get("is_error") and any(
                phrase in str(obj.get("result", "")).lower()
                for phrase in USAGE_LIMIT_PHRASES
            ):
                return True
        except Exception:
            pass
    return False


# --- Output formatting ---

def extract_actual_session_id(raw):
    """Find the session_id Claude actually used (from the system/init event in stream-json output).
    Falls back to the session_id on any other event if init not found."""
    init_id = None
    fallback_id = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or TMUX_DONE_MARKER in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        sid = obj.get("session_id")
        if not sid:
            continue
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            init_id = sid
            break
        if fallback_id is None:
            fallback_id = sid
    return init_id or fallback_id


def parse_stream_json(raw):
    lines = []
    tools_used = []
    has_json = False
    for line in raw.splitlines():
        line = line.strip()
        if not line or TMUX_DONE_MARKER in line:
            continue
        try:
            obj = json.loads(line)
            has_json = True
            if obj.get("type") == "assistant":
                for block in obj.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        lines.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tools_used.append(block.get("name", "tool"))
            elif obj.get("type") == "tool_result":
                for block in obj.get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        lines.append(f"[tool]: {block['text']}")
        except Exception:
            if not has_json:
                lines.append(line)
    out = "\n".join(lines).strip()
    if not out and has_json:
        # Never deliver silence: the model ended its turn with tool calls only
        # (e.g. Fable 5 scheduling a wake-up that can't fire in headless mode).
        summary = ", ".join(tools_used[-6:]) if tools_used else "no visible actions"
        out = (f"⚠️ The run finished without a text reply (tool calls only: {summary}). "
               f"Please resend or rephrase your message.")
    return out


def md_to_slack(text):
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"_\1_", text)
    text = re.sub(r"```\w*\n", "```\n", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"<\2|\1>", text)
    text = re.sub(r"^[\-\*]\s+", "• ", text, flags=re.MULTILINE)
    return text


# --- Output delivery ---

def _slack_send_retry(fn, max_tries=5, **kwargs):
    """Some networks intermittently drop DNS/SSL to the Slack API; an unretried
    send silently loses the whole reply. Backoff: 3/6/12/24s."""
    for attempt in range(max_tries):
        try:
            return fn(**kwargs)
        except Exception as e:
            if attempt == max_tries - 1:
                print(f"[send] giving up after {max_tries} tries: {e}", flush=True)
                raise
            wait = 3 * (2 ** attempt)
            print(f"[send] attempt {attempt + 1} failed ({e}); retry in {wait}s", flush=True)
            time.sleep(wait)


def _send_output(output, target_machine, say, thread_ts, original_command, channel_id,
                 client=None, event_ts=None, event_channel=None):
    """Deliver pre-parsed plain text output to Slack."""
    output = md_to_slack(output)

    for prefix in [original_command, f"=== Current Command ===\n{original_command}"]:
        if output.startswith(prefix):
            output = output[len(prefix):].lstrip("\n").strip()
            break

    add_to_session_with_history(channel_id, thread_ts, "user", original_command)
    # Keep the prompt context small (1000), but persist a fuller copy for the dashboard.
    add_to_session_with_history(channel_id, thread_ts, "assistant", output[:1000],
                                history_content=output[:8000])

    CHUNK_SIZE = 2800
    chunks = [output[i:i+CHUNK_SIZE] for i in range(0, len(output), CHUNK_SIZE)]
    for i, chunk in enumerate(chunks):
        header = f"✅ `{target_machine}` done!" if i == 0 else f"📄 `{target_machine}` (cont. {i+1}/{len(chunks)})"
        _slack_send_retry(say, text=f"{header}\n{chunk}", thread_ts=thread_ts)

    if client and event_channel:
        output_files = find_output_files(output, WORK_DIR)
        if output_files:
            upload_files_to_slack(client, event_channel, thread_ts, output_files)

    threading.Thread(
        target=update_context, args=(original_command, output, channel_id), daemon=True
    ).start()

    if client and event_channel and event_ts:
        ack_reaction(client, event_channel, event_ts, add=False)


def deliver_output(output, target_machine, say, thread_ts, original_command, channel_id,
                   client=None, event_ts=None, event_channel=None):
    # Persist a per-thread dashboard card from the raw stream-json (todos live in it)
    # before we collapse it to plain text for Slack.
    try:
        dashboard.record_thread(channel_id, thread_ts, original_command, output)
    except Exception:
        pass
    output = parse_stream_json(output)
    _send_output(output, target_machine, say, thread_ts, original_command, channel_id,
                 client=client, event_ts=event_ts, event_channel=event_channel)


# --- tmux runners ---

def run_in_tmux(full_prompt, say, thread_ts, original_command, channel_id,
                client=None, event_ts=None, event_channel=None,
                claude_session_uuid=None, is_first_turn=True, model_override=None, effort_override=None):

    with active_lock:
        if len(active_sessions) >= MAX_CONCURRENT:
            print(f"[capacity] MAX_CONCURRENT={MAX_CONCURRENT} reached, rejecting: {original_command[:60]!r}", flush=True)
            say(text=f"⚠️ `{MACHINE_NAME}` is at max capacity ({MAX_CONCURRENT} concurrent tasks). Please wait.", thread_ts=thread_ts)
            return
        session_id = make_session_id(thread_ts)
        active_sessions[session_id] = thread_ts
        print(f"[start] session={session_id} active={list(active_sessions.keys())} cmd={original_command[:60]!r}", flush=True)

    output_file = output_file_for(session_id)
    watch_file  = watch_file_for(session_id)
    record_run(session_id, {
        "kind": "claude", "label": MACHINE_NAME,
        "channel_id": channel_id, "thread_ts": thread_ts,
        "original_command": original_command,
        "event_ts": event_ts, "event_channel": event_channel,
        "output_file": output_file, "watch_file": watch_file, "last_msg_file": None,
    })

    try:
        # Create dedicated tmux session
        subprocess.run(["tmux", "new-session", "-d", "-s", session_id], capture_output=True)

        prompt_file = f"/tmp/claude_prompt_{session_id}.txt"
        with open(prompt_file, "w") as f:
            f.write(full_prompt)

        try:
            os.remove(output_file)
        except FileNotFoundError:
            pass

        env_prefix = "export PATH=$PATH:$HOME/.local/bin:$HOME/.npm/bin:$HOME/anaconda3/bin:$HOME/miniconda3/bin"
        if claude_session_uuid and is_first_turn:
            session_arg = f"--session-id {claude_session_uuid}"
        elif claude_session_uuid:
            session_arg = f"--resume {claude_session_uuid}"
        else:
            session_arg = ""
        effective_claude_model = model_override or CLAUDE_MODEL
        model_arg  = f"--model {effective_claude_model}" if effective_claude_model else ""
        effort_arg = f"--effort {effort_override}" if effort_override else ""
        cmd = (
            f"{env_prefix} && "
            f"claude {session_arg} {model_arg} {effort_arg} --output-format stream-json --verbose --dangerously-skip-permissions --disallowedTools ScheduleWakeup "
            f"< {prompt_file} > {output_file} 2>&1 ; "
            f"echo {TMUX_DONE_MARKER} >> {output_file}"
        )
        result = subprocess.run(["tmux", "send-keys", "-t", session_id, cmd, "Enter"])
        if result.returncode != 0:
            say(text=f"❌ `{MACHINE_NAME}` failed to start tmux session.", thread_ts=thread_ts)
            return

        say(
            text=f"⏳ `{MACHINE_NAME}` running...\n`python {BOT_DIR}/watch_claude.py {output_file}` to watch",
            thread_ts=thread_ts
        )

        # Wait for output file to appear
        for _ in range(15):
            time.sleep(2)
            if os.path.exists(output_file):
                break
        else:
            say(text=f"❌ `{MACHINE_NAME}` claude did not start. Check `tmux attach -t {session_id}`", thread_ts=thread_ts)
            return

        timeout, interval, elapsed = 1800, 2, 0
        while elapsed < timeout:
            time.sleep(interval)
            elapsed += interval
            try:
                with open(output_file, "r") as f:
                    content = f.read()
                if TMUX_DONE_MARKER in content:
                    if _consume_cancel(session_id, say, thread_ts):
                        if client and event_channel and event_ts:
                            ack_reaction(client, event_channel, event_ts, add=False)
                        return
                    output = content.replace(TMUX_DONE_MARKER, "").strip()
                    with open(watch_file, "w") as wf:
                        wf.write(content)
                    os.remove(output_file)
                    if detect_usage_limit(output):
                        say(text=f"⚠️ `{MACHINE_NAME}` hit Claude usage limit. Please resend your message once the limit resets.", thread_ts=thread_ts)
                        if client and event_channel and event_ts:
                            ack_reaction(client, event_channel, event_ts, add=False)
                        return
                    actual_sid = extract_actual_session_id(output)
                    if actual_sid and actual_sid != claude_session_uuid:
                        print(f"[claude-session] forked: {claude_session_uuid} -> {actual_sid}", flush=True)
                        set_claude_session_uuid(channel_id, thread_ts, MACHINE_NAME, actual_sid)
                    deliver_output(output, MACHINE_NAME, say, thread_ts, original_command, channel_id,
                                   client=client, event_ts=event_ts, event_channel=event_channel)
                    return
            except FileNotFoundError:
                continue

        say(text=f"⏰ `{MACHINE_NAME}` timed out (30 min limit)", thread_ts=thread_ts)
        if client and event_channel and event_ts:
            ack_reaction(client, event_channel, event_ts, add=False)

    except Exception as e:
        say(text=f"❌ `{MACHINE_NAME}` error: {str(e)}", thread_ts=thread_ts)
        if client and event_channel and event_ts:
            ack_reaction(client, event_channel, event_ts, add=False)
    finally:
        with active_lock:
            active_sessions.pop(session_id, None)
            print(f"[done] session={session_id} remaining={list(active_sessions.keys())}", flush=True)
        unrecord_run(session_id)
        # Clean up tmux session
        subprocess.run(["tmux", "kill-session", "-t", session_id], capture_output=True)
        try:
            os.remove(prompt_file)
        except Exception:
            pass


def run_codex(full_prompt, say, thread_ts, original_command, channel_id,
              client=None, event_ts=None, event_channel=None, model_override=None,
              codex_session_uuid=None, is_first_turn=True):
    """Run the Codex CLI non-interactively in tmux (agentic: file edits, shell, git),
    mirroring run_in_tmux. Auth is handled by the codex CLI itself (codex login or
    OPENAI_API_KEY in the environment) — no API key is referenced here.

    Per-thread continuity: on the first turn we run `codex exec` and capture the
    session UUID from its header; on later turns we `codex exec resume <uuid>` so
    Codex carries the full prior conversation itself (the prompt is just the new
    message). resume inherits model/sandbox/cwd from the original session."""
    with active_lock:
        if len(active_sessions) >= MAX_CONCURRENT:
            say(text=f"⚠️ `{MACHINE_NAME}` at max capacity ({MAX_CONCURRENT}). Please wait.", thread_ts=thread_ts)
            return
        session_id = make_session_id(thread_ts)
        active_sessions[session_id] = thread_ts

    effective_model = model_override or CODEX_MODEL
    output_file   = output_file_for(session_id)
    watch_file    = watch_file_for(session_id)
    last_msg_file = f"/tmp/codex_last_{session_id}.txt"
    prompt_file   = f"/tmp/claude_prompt_{session_id}.txt"
    record_run(session_id, {
        "kind": "codex", "label": f"codex/{effective_model}",
        "channel_id": channel_id, "thread_ts": thread_ts,
        "original_command": original_command,
        "event_ts": event_ts, "event_channel": event_channel,
        "output_file": output_file, "watch_file": watch_file, "last_msg_file": last_msg_file,
    })

    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", session_id], capture_output=True)
        with open(prompt_file, "w") as f:
            f.write(full_prompt)
        for stale in (output_file, last_msg_file):
            try:
                os.remove(stale)
            except FileNotFoundError:
                pass

        env_prefix = "export PATH=$PATH:$HOME/.local/bin:$HOME/.npm/bin:$HOME/anaconda3/bin:$HOME/miniconda3/bin"
        # NOTE: --sandbox workspace-write (not --dangerously-bypass-...) because some
        # enterprise-managed Codex policies disallow DangerFullAccess and silently
        # downgrade it to read-only. workspace-write still permits file edits under
        # the workdir, /tmp, and $TMPDIR.
        if codex_session_uuid and not is_first_turn:
            # Resume: model/sandbox/cwd are inherited from the original session, and
            # `resume` rejects -m/-C/--sandbox, so we pass only the supported flags.
            codex_invocation = (
                f"codex exec resume {codex_session_uuid} --skip-git-repo-check -o {last_msg_file} -"
            )
        else:
            codex_invocation = (
                f"codex exec --sandbox workspace-write --skip-git-repo-check "
                f"-m {effective_model} -C {WORK_DIR} -o {last_msg_file} -"
            )
        cmd = (
            f"{env_prefix} && "
            f"{codex_invocation} "
            f"< {prompt_file} > {output_file} 2>&1 ; "
            f"echo {TMUX_DONE_MARKER} >> {output_file}"
        )
        result = subprocess.run(["tmux", "send-keys", "-t", session_id, cmd, "Enter"])
        if result.returncode != 0:
            say(text=f"❌ `{MACHINE_NAME}` failed to start codex tmux session.", thread_ts=thread_ts)
            return

        say(
            text=f"⏳ `{MACHINE_NAME}` (codex/{effective_model}) running...\n`python {BOT_DIR}/watch_claude.py {output_file}` to watch",
            thread_ts=thread_ts
        )

        for _ in range(15):
            time.sleep(2)
            if os.path.exists(output_file):
                break
        else:
            say(text=f"❌ `{MACHINE_NAME}` codex did not start. Check `tmux attach -t {session_id}`", thread_ts=thread_ts)
            return

        timeout, interval, elapsed = 1800, 2, 0
        while elapsed < timeout:
            time.sleep(interval)
            elapsed += interval
            try:
                with open(output_file, "r") as f:
                    content = f.read()
            except FileNotFoundError:
                continue
            if TMUX_DONE_MARKER not in content:
                continue
            if _consume_cancel(session_id, say, thread_ts):
                if client and event_channel and event_ts:
                    ack_reaction(client, event_channel, event_ts, add=False)
                return
            with open(watch_file, "w") as wf:
                wf.write(content)
            # Prefer the clean final-message file; fall back to raw stdout.
            output = ""
            try:
                with open(last_msg_file, "r") as f:
                    output = f.read().strip()
            except FileNotFoundError:
                pass
            if not output:
                output = content.replace(TMUX_DONE_MARKER, "").strip()
            # Capture the Codex session UUID so the next turn in this thread can resume it.
            new_sid = extract_codex_session_id(content)
            if new_sid:
                set_claude_session_uuid(channel_id, thread_ts, CODEX_SESSION_KEY, new_sid)
            os.remove(output_file)
            _send_output(output, f"codex/{effective_model}", say, thread_ts, original_command, channel_id,
                         client=client, event_ts=event_ts, event_channel=event_channel)
            return

        say(text=f"⏰ `{MACHINE_NAME}` (codex) timed out (30 min limit)", thread_ts=thread_ts)
        if client and event_channel and event_ts:
            ack_reaction(client, event_channel, event_ts, add=False)

    except Exception as e:
        say(text=f"❌ Codex error: {e}", thread_ts=thread_ts)
        if client and event_channel and event_ts:
            ack_reaction(client, event_channel, event_ts, add=False)
    finally:
        with active_lock:
            active_sessions.pop(session_id, None)
        unrecord_run(session_id)
        subprocess.run(["tmux", "kill-session", "-t", session_id], capture_output=True)
        for tmp in (prompt_file, last_msg_file):
            try:
                os.remove(tmp)
            except Exception:
                pass


def parse_effort_flag(text):
    """Extract --effort <value> from raw command text.

    Returns (effort_str, cleaned_text) where effort_str is e.g. "max", "high",
    or None if no flag was found.
    """
    m = re.search(r"--effort\s+(\S+)", text)
    if m:
        effort_str = m.group(1)
        cleaned = (text[:m.start()] + text[m.end():]).strip()
        return (effort_str, cleaned)
    return (None, text)


def parse_attachments_flag(text):
    """Extract --attachments full|text|none from raw command text — a per-message
    override of ATTACHMENT_MODE."""
    m = re.search(r"--attachments\s+(full|text|none)\b", text)
    if m:
        cleaned = (text[:m.start()] + text[m.end():]).strip()
        return (m.group(1), cleaned)
    return (None, text)


def parse_model_flag(text):
    """Extract --model <value> or --codex from raw command text.

    Returns (model_str, cleaned_text) where:
      - model_str is the raw provider/model string (e.g. "codex/gpt-4.1",
        "claude/sonnet", "gpt-4.1") or None if no flag was found.
      - cleaned_text has the flag and its value removed.

    Backwards-compat: --codex is treated as --model codex/gpt-4.1.
    """
    # --codex alias
    if "--codex" in text:
        cleaned = text.replace("--codex", "").strip()
        return (f"codex/{CODEX_MODEL}", cleaned)

    # --model <value>  (value = non-whitespace token)
    m = re.search(r"--model\s+(\S+)", text)
    if m:
        model_str = m.group(1)
        cleaned = text[:m.start()] + text[m.end():]
        cleaned = cleaned.strip()
        return (model_str, cleaned)

    return (None, text)


def resolve_model(model_str):
    """Given a model string from parse_model_flag, return (runner, effective_model).

    runner          : "codex" | "claude"
    effective_model : model name to pass to the runner, or None to use env default.

    Logic:
      "codex"            -> ("codex",  CODEX_MODEL)
      "codex/gpt-4o"     -> ("codex",  "gpt-4o")
      "gpt-4.1"          -> ("codex",  "gpt-4.1")   # bare OpenAI model name
      "claude"           -> ("claude", None)          # use CLAUDE_MODEL env var
      "claude/sonnet"    -> ("claude", "sonnet")
      unknown            -> ("claude", model_str)     # fallback: treat as claude model
    """
    if model_str is None:
        return ("claude", None)

    parts = model_str.split("/", 1)
    prefix = parts[0].lower()
    suffix = parts[1] if len(parts) > 1 else None

    if prefix in MODELS:
        entry = MODELS[prefix]
        runner = entry["runner"]
        if suffix:
            effective_model = suffix
        else:
            effective_model = entry["default_model"]  # may be None for claude
        return (runner, effective_model)

    # Bare OpenAI model name (e.g. "gpt-4.1", "o1-mini") — check known patterns
    for key in MODELS:
        if prefix.startswith(key):
            entry = MODELS[key]
            return (entry["runner"], model_str)

    # Unknown prefix — fall back to claude runner, pass as model override
    return ("claude", model_str)


def run_model(model_str, full_prompt, say, thread_ts, original_command, channel_id,
              client=None, event_ts=None, event_channel=None,
              session_uuid=None, is_first_turn=True, effort_override=None):
    """Dispatch to the correct runner based on model_str.

    model_str: raw string from parse_model_flag (e.g. "codex/gpt-4.1",
               "claude/sonnet", None).  None means default Claude Code CLI.
    session_uuid / is_first_turn: the per-thread session to resume (Claude UUID
               for the claude runner, Codex session UUID for the codex runner).
    """
    runner, effective_model = resolve_model(model_str)

    if runner == "codex":
        run_codex(
            full_prompt, say, thread_ts, original_command, channel_id,
            client=client, event_ts=event_ts, event_channel=event_channel,
            model_override=effective_model,
            codex_session_uuid=session_uuid, is_first_turn=is_first_turn,
        )
    else:
        # Claude Code CLI runner; effective_model overrides CLAUDE_MODEL when set.
        run_in_tmux(
            full_prompt, say, thread_ts, original_command, channel_id,
            client=client, event_ts=event_ts, event_channel=event_channel,
            claude_session_uuid=session_uuid, is_first_turn=is_first_turn,
            model_override=effective_model, effort_override=effort_override,
        )


def add_to_session_with_history(channel_id, thread_ts, role, content, model='', history_content=None):
    """Append to in-memory thread_sessions and persist to history DB.

    in-memory `content` stays small to keep Claude prompts bounded; `history_content`
    (if given) is the fuller text persisted to the DB, so the dashboard can show whole
    messages behind a 'more' toggle.
    """
    add_to_session(channel_id, thread_ts, role, content)
    try:
        history.add_message(
            session_key(channel_id, thread_ts), role,
            history_content if history_content is not None else content,
            model=model, channel_id=channel_id, thread_ts=thread_ts,
        )
    except Exception as e:
        print(f"[history] add_message failed: {e}", flush=True)


def run_on_remote_tmux(target_machine, full_prompt, say, thread_ts, original_command, channel_id,
                       claude_session_uuid=None, is_first_turn=True, attached_files=None, model_override=None, effort_override=None):
    if target_machine not in SSH_HOSTS:
        say(
            text=f"❌ No SSH config for `{target_machine}`. Add `SSH_{target_machine}=user@host:port` to the gateway's .env",
            thread_ts=thread_ts
        )
        return
    session_id  = make_session_id(thread_ts)
    prompt_file = f"/tmp/claude_prompt_{session_id}.txt"
    output_file = f"/tmp/claude_out_{session_id}.txt"
    cleaned_up  = False
    try:
        # Push any user-uploaded files (images/videos/docs) to the same absolute path on the worker
        # so the path baked into the prompt actually resolves there.
        if attached_files:
            distribute_files_to_machine(target_machine, attached_files)

        escaped = full_prompt.replace("'", "'\\''")
        subprocess.run(
            ssh_cmd(target_machine, f"printf '%s' '{escaped}' > {prompt_file} && rm -f {output_file}"),
            timeout=15
        )
        subprocess.run(
            ssh_cmd(target_machine, f"tmux new-session -d -s {session_id} 2>/dev/null || true"),
            timeout=10
        )

        env_prefix = "export PATH=$PATH:$HOME/.local/bin:$HOME/.npm/bin:$HOME/anaconda3/bin:$HOME/miniconda3/bin"
        if claude_session_uuid and is_first_turn:
            session_arg = f"--session-id {claude_session_uuid}"
        elif claude_session_uuid:
            session_arg = f"--resume {claude_session_uuid}"
        else:
            session_arg = ""
        effective_claude_model = model_override or CLAUDE_MODEL
        model_arg  = f"--model {effective_claude_model}" if effective_claude_model else ""
        effort_arg = f"--effort {effort_override}" if effort_override else ""
        cmd = (
            f"{env_prefix} && "
            f"claude {session_arg} {model_arg} {effort_arg} --output-format stream-json --verbose --dangerously-skip-permissions --disallowedTools ScheduleWakeup "
            f"< {prompt_file} > {output_file} 2>&1 ; "
            f"echo {TMUX_DONE_MARKER} >> {output_file}"
        )
        subprocess.run(
            ssh_cmd(target_machine, f"tmux send-keys -t {session_id} '{cmd}' Enter"),
            timeout=10
        )

        say(
            text=f"⏳ `{target_machine}` running... (SSH in and run `python watch_claude.py {output_file}` from the bot directory to watch)",
            thread_ts=thread_ts
        )

        timeout, interval, elapsed = 1800, 3, 0
        while elapsed < timeout:
            time.sleep(interval)
            elapsed += interval
            result = subprocess.run(
                ssh_cmd(target_machine, f"cat {output_file} 2>/dev/null"),
                capture_output=True, text=True, timeout=10
            )
            content = result.stdout
            if TMUX_DONE_MARKER in content:
                output = content.replace(TMUX_DONE_MARKER, "").strip()
                subprocess.run(
                    ssh_cmd(target_machine, f"cp {output_file} /tmp/claude_watch_{session_id}.txt && rm -f {output_file} && tmux kill-session -t {session_id} 2>/dev/null"),
                    timeout=5
                )
                cleaned_up = True
                if detect_usage_limit(output):
                    say(text=f"⚠️ `{target_machine}` hit Claude usage limit. Please resend your message once the limit resets.", thread_ts=thread_ts)
                    return
                actual_sid = extract_actual_session_id(output)
                if actual_sid and actual_sid != claude_session_uuid:
                    print(f"[claude-session] forked on {target_machine}: {claude_session_uuid} -> {actual_sid}", flush=True)
                    set_claude_session_uuid(channel_id, thread_ts, target_machine, actual_sid)
                deliver_output(output, target_machine, say, thread_ts, original_command, channel_id)
                return

        say(text=f"⏰ `{target_machine}` timed out (30 min limit)", thread_ts=thread_ts)

    except Exception as e:
        say(text=f"❌ `{target_machine}` SSH error: {str(e)}", thread_ts=thread_ts)
    finally:
        if not cleaned_up:
            try:
                subprocess.run(
                    ssh_cmd(target_machine, f"tmux kill-session -t {session_id} 2>/dev/null; rm -f {prompt_file} {output_file}"),
                    timeout=10
                )
            except Exception:
                pass


def run_with_history(command, say, thread_ts, client, channel_id, event_ts):
    say(text="⏳ Reading channel history...", thread_ts=thread_ts)
    try:
        result = client.conversations_history(channel=channel_id, limit=100)
        messages = result["messages"][::-1]
        lines = [
            f"[{m.get('user', 'bot')}]: {m.get('text', '')}"
            for m in messages if m.get("text")
        ]
        history_text = "\n".join(lines)
    except Exception as e:
        history_text = f"(failed to read channel history: {type(e).__name__}: {e})"

    full_prompt = (
        f"{LANG_INSTRUCTION}{load_style_guide()}{load_recipes_index()}\n\n"
        f"Below is the recent Slack channel conversation:\n\n{history_text}\n\n"
        f"=== Command ===\n{command}\n\n"
        f"After responding, update context.md with a project summary "
        f"(what was done, current status, next steps, under 500 characters)."
    )
    run_in_tmux(full_prompt, say, thread_ts, command, channel_id,
                client=client, event_ts=event_ts, event_channel=channel_id)


# --- Routing ---

def parse_and_run(text, say, thread_ts, channel_id, user_id,
                  client=None, event_ts=None, event=None):

    if not is_allowed(user_id):
        say(text="Sorry, you don't have access to this bot.", thread_ts=thread_ts)
        return

    # /history search: <query>  — full-text search over persisted history
    _history_search_match = re.match(r"^/history\s+(?:search:\s*)?(.+)", text.strip(), re.IGNORECASE)
    if _history_search_match or text.strip().lower().startswith("search:"):
        query = _history_search_match.group(1) if _history_search_match else text.strip()[len("search:"):].strip()
        try:
            results = history.search(query, channel_id=channel_id)
        except Exception as e:
            say(text=f"❌ History search error: {e}", thread_ts=thread_ts)
            return
        if not results:
            say(text=f"No history results for: `{query}`", thread_ts=thread_ts)
        else:
            lines = [f"*History results for* `{query}`:"]
            for r in results:
                snippet = r.get("snippet") or r.get("content", "")[:120]
                ts_label = r.get("thread_ts", "")
                lines.append(f"• [{r.get('role','?')}] {snippet}  _(thread: {ts_label})_")
            say(text="\n".join(lines), thread_ts=thread_ts)
        return

    # /idea <text> — capture a research idea into the Obsidian ideas vault
    _idea_match = re.match(r"^/idea\s+(.+)", text.strip(), re.IGNORECASE | re.DOTALL)
    if _idea_match:
        if not IDEAS_VAULT:
            say(text="⚠️ `/idea` is not configured — set `IDEAS_VAULT=/path/to/your/vault` in .env.",
                thread_ts=thread_ts)
            return
        idea_text = _idea_match.group(1).strip()
        if client and event_ts:
            ack_reaction(client, channel_id, event_ts, add=True)
        instruction = LANG_INSTRUCTION.replace("{CHANNEL_ID}", channel_id).replace("{THREAD_TS}", str(thread_ts))
        instruction += load_style_guide()
        full_prompt = instruction + "\n\n" + IDEA_INSTRUCTION.format(vault=IDEAS_VAULT, idea=idea_text)
        t = threading.Thread(
            target=run_in_tmux,
            args=(full_prompt, say, thread_ts, f"/idea {idea_text}", channel_id),
            kwargs={"client": client, "event_ts": event_ts, "event_channel": channel_id},
        )
        t.daemon = True
        t.start()
        return

    broadcast = "/all" in text
    explicit_target = None
    for m in KNOWN_MACHINES:
        if re.search(rf"/{m}\b", text):
            explicit_target = m
            break
    target = explicit_target or CHANNEL_DEFAULTS.get(channel_id, MACHINE_NAME)

    print(f"[route] target={target} broadcast={broadcast} is_gateway={IS_GATEWAY} machine={MACHINE_NAME}", flush=True)

    if not IS_GATEWAY and target != MACHINE_NAME and not broadcast:
        return

    # Extract --model / --codex and --effort flags before stripping machine tokens
    model_str, text = parse_model_flag(text)
    effort_str, text = parse_effort_flag(text)
    att_mode, text = parse_attachments_flag(text)
    att_mode = att_mode or ATTACHMENT_MODE
    runner, effective_model = resolve_model(model_str)   # "codex" or "claude"

    attached_files, skipped_files = extract_files_from_event(event, att_mode) if event else ([], [])
    if skipped_files:
        names = ", ".join(f"`{n}`" for n in skipped_files[:10])
        say(text=f"📎 Skipped {len(skipped_files)} attachment(s) (attachment mode `{att_mode}`): {names}",
            thread_ts=thread_ts)

    command = text
    for m in KNOWN_MACHINES:
        command = command.replace(f"/{m}", "")
    command = command.replace("/all", "").strip()

    if attached_files:
        file_notes = []
        for local_path, name in attached_files:
            if local_path:
                file_notes.append(f"- `{local_path}` ({name})")
            else:
                file_notes.append(f"- {name}")
        command += (
            "\n\nThe following files have been uploaded and saved locally. "
            "Please read and use their contents when responding:\n"
            + "\n".join(file_notes)
        )

    if not command.strip():
        say(text="Please enter a command! e.g. `@bot summarize the latest experiment results`", thread_ts=thread_ts)
        return

    if att_mode != "full":
        command += (
            f"\n\n[Attachment policy: {att_mode}] Do not read, open, or transcribe images, "
            "videos, archives, or other binary media for this task unless the user explicitly "
            "insists — they consume a large number of tokens. Work from text and reply in text."
        )

    if client and event_ts:
        ack_reaction(client, channel_id, event_ts, add=True)

    # Mark the thread "alive" on the dashboard (once). No Slack reaction — the 👀
    # ack is the only reaction the bot adds; the 👁 indicator is only ever removed.
    if client and not dashboard.is_alive(channel_id, thread_ts):
        dashboard.set_alive(channel_id, thread_ts, True, title=command)

    if session_key(channel_id, thread_ts) not in thread_sessions and client:
        fetch_thread_history(client, channel_id, thread_ts)

    context = load_context(channel_id)
    full_prompt = build_prompt(channel_id, thread_ts, command, context)
    is_history_request = any(kw in command.lower() for kw in HISTORY_KEYWORDS)
    targets = KNOWN_MACHINES if broadcast else [target]

    for t_machine in targets:
        if runner == "codex":
            # Switching to Codex invalidates the thread's Claude session (they can't
            # share state) — the next Claude turn restarts with full build_prompt context.
            clear_claude_session_uuid(channel_id, thread_ts, t_machine)
            existing_codex = get_claude_session_uuid(channel_id, thread_ts, CODEX_SESSION_KEY)
            if existing_codex:
                session_uuid = existing_codex
                is_first_turn = False
                prompt_for_runner = command          # codex session already has the history
            else:
                session_uuid = None                  # captured from codex output after the run
                is_first_turn = True
                prompt_for_runner = full_prompt
            print(f"[codex-session] thread={thread_ts} uuid={session_uuid} first_turn={is_first_turn}", flush=True)
        else:
            # Switching to Claude invalidates the thread's Codex session, symmetrically.
            clear_claude_session_uuid(channel_id, thread_ts, CODEX_SESSION_KEY)
            existing_uuid = get_claude_session_uuid(channel_id, thread_ts, t_machine)
            if existing_uuid:
                session_uuid = existing_uuid
                is_first_turn = False
                prompt_for_runner = command          # session already has history
            else:
                session_uuid = str(uuid.uuid4())
                is_first_turn = True
                prompt_for_runner = full_prompt
                set_claude_session_uuid(channel_id, thread_ts, t_machine, session_uuid)
            print(f"[claude-session] thread={thread_ts} machine={t_machine} uuid={session_uuid} first_turn={is_first_turn}", flush=True)

        if t_machine == MACHINE_NAME:
            if is_history_request and client and runner != "codex":
                t = threading.Thread(
                    target=run_with_history,
                    args=(command, say, thread_ts, client, channel_id, event_ts)
                )
            else:
                t = threading.Thread(
                    target=run_model,
                    args=(model_str, prompt_for_runner, say, thread_ts, command, channel_id),
                    kwargs={
                        "client": client, "event_ts": event_ts, "event_channel": channel_id,
                        "session_uuid": session_uuid, "is_first_turn": is_first_turn,
                        "effort_override": None if runner == "codex" else effort_str,
                    }
                )
        else:
            t = threading.Thread(
                target=run_on_remote_tmux,
                args=(t_machine, prompt_for_runner, say, thread_ts, command, channel_id),
                kwargs={
                    "claude_session_uuid": None if runner == "codex" else session_uuid,
                    "is_first_turn": is_first_turn,
                    "attached_files": attached_files,
                    "model_override": None if runner == "codex" else effective_model,
                    "effort_override": None if runner == "codex" else effort_str,
                }
            )
        t.daemon = True
        t.start()


# --- Slack event handlers ---

@app.event("app_mention")
def handle_mention(event, say, client):
    text = event.get("text", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel_id = event.get("channel")
    user_id = event.get("user", "")
    event_ts = event.get("ts")
    print(f"[mention] channel={channel_id} user={user_id} text={text!r}", flush=True)
    text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    parse_and_run(text, say, thread_ts, channel_id, user_id,
                  client=client, event_ts=event_ts, event=event)


@app.event("message")
def handle_dm(event, say, client):
    if event.get("channel_type") != "im":
        return
    if event.get("subtype"):
        return
    text = event.get("text", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel_id = event.get("channel")
    user_id = event.get("user", "")
    event_ts = event.get("ts")
    parse_and_run(text, say, thread_ts, channel_id, user_id,
                  client=client, event_ts=event_ts, event=event)


@app.event("reaction_added")
def handle_reaction(event, say, client):
    reaction = event.get("reaction", "")
    user_id = event.get("user", "")
    item = event.get("item", {})
    channel_id = item.get("channel")
    ts = item.get("ts")

    if not is_allowed(user_id) or user_id == get_bot_user_id():
        return

    # Party-blob is the BOT's indicator only — user taps on it are IGNORED. Using it as
    # a gesture is a trap: tapping adds the user's own copy, which the bot cannot remove.
    if reaction == ALIVE_REACTION:
        return

    # 📁 is the USER-owned archive toggle: adding it archives the thread (bot removes
    # its blob indicator; running jobs keep going — ❌ / End actually stops them).
    if reaction == ARCHIVE_REACTION and channel_id and ts:
        archive_thread(channel_id, item.get("thread_ts") or ts)
        return

    if reaction == "+1" and channel_id and ts:
        try:
            result = client.conversations_replies(channel=channel_id, ts=ts, limit=5)
            for m in reversed(result.get("messages", [])):
                if not m.get("bot_id") and m.get("text"):
                    parse_and_run(m["text"], say, ts, channel_id, user_id,
                                  client=client, event_ts=ts)
                    break
        except Exception:
            pass

    if reaction == "x" and channel_id and ts:
        # End every run in this thread. `ts` is the reacted message; for runs whose
        # thread_ts differs (a reply was reacted to), also match the message's own thread.
        thread_ts = item.get("thread_ts") or ts
        end_thread(channel_id, thread_ts)
        if thread_ts != ts:
            end_thread(channel_id, ts)
        say(text="⛔ Cancelled.", thread_ts=thread_ts)


@app.event("reaction_removed")
def handle_reaction_removed(event, say, client):
    """Removing your own 📁 revives the thread — the archive toggle is fully user-owned
    (add 📁 = archive, remove 📁 = revive), so nothing ever lingers that the wrong party
    owns. Party-blob removal events are ignored (that's the bot's indicator)."""
    reaction = event.get("reaction", "")
    user_id = event.get("user", "")
    item = event.get("item", {})
    channel_id = item.get("channel")
    ts = item.get("ts")
    if not is_allowed(user_id) or user_id == get_bot_user_id():
        return
    if reaction == ARCHIVE_REACTION and channel_id and ts:
        revive_thread(channel_id, item.get("thread_ts") or ts)


# --- Recovery of runs orphaned by a bot restart ---

def _recover_say(channel):
    """A say()-compatible shim that posts via the Web API (no request context needed)."""
    def _say(text, thread_ts=None):
        try:
            app.client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
        except Exception as e:
            print(f"[recover] post failed: {e}", flush=True)
    return _say


def _resume_run(session_id, info):
    """Re-watch an orphaned run's output and deliver its result (or report it was
    interrupted). Runs in a daemon thread started at bot launch."""
    output_file   = info["output_file"]
    channel_id    = info["channel_id"]
    thread_ts     = info["thread_ts"]
    event_channel = info.get("event_channel") or channel_id
    event_ts      = info.get("event_ts")
    label         = info.get("label", MACHINE_NAME)
    say = _recover_say(event_channel)

    timeout, interval, elapsed = 1800, 3, 0
    while elapsed < timeout:
        try:
            with open(output_file) as f:
                content = f.read()
        except FileNotFoundError:
            content = ""

        if TMUX_DONE_MARKER in content:
            if _consume_cancel(session_id, say, thread_ts):
                unrecord_run(session_id)
                return
            try:
                with open(info["watch_file"], "w") as wf:
                    wf.write(content)
            except Exception:
                pass
            if info.get("kind") == "codex":
                output = ""
                try:
                    with open(info["last_msg_file"]) as f:
                        output = f.read().strip()
                except Exception:
                    pass
                if not output:
                    output = content.replace(TMUX_DONE_MARKER, "").strip()
                _send_output(output, label, say, thread_ts, info["original_command"], channel_id,
                             client=app.client, event_ts=event_ts, event_channel=event_channel)
            else:
                output = content.replace(TMUX_DONE_MARKER, "").strip()
                deliver_output(output, label, say, thread_ts, info["original_command"], channel_id,
                               client=app.client, event_ts=event_ts, event_channel=event_channel)
            try:
                os.remove(output_file)
            except Exception:
                pass
            unrecord_run(session_id)
            print(f"[recover] delivered orphaned run {session_id} ({label})", flush=True)
            return

        # No result yet — is the tmux job still alive, or did the restart kill it mid-run?
        alive = subprocess.run(["tmux", "has-session", "-t", session_id],
                               capture_output=True).returncode == 0
        if not alive:
            say(text=f"⚠️ `{label}` — a run in this thread was interrupted by a bot restart "
                     f"and didn't finish. Please resend it.", thread_ts=thread_ts)
            unrecord_run(session_id)
            print(f"[recover] run {session_id} was interrupted (no result, tmux gone)", flush=True)
            return

        time.sleep(interval)
        elapsed += interval

    say(text=f"⏰ `{label}` — recovered run didn't finish in time. Please resend it.", thread_ts=thread_ts)
    unrecord_run(session_id)


def recover_orphaned_runs():
    """On startup, re-attach to any runs the previous bot instance left in flight."""
    runs = _load_active_runs()
    if not runs:
        return
    print(f"[recover] {len(runs)} orphaned run(s) from previous instance: {list(runs.keys())}", flush=True)
    for session_id, info in runs.items():
        threading.Thread(target=_resume_run, args=(session_id, info), daemon=True).start()


# --- Alive/archive helpers ---

_bot_user_id = None


def get_bot_user_id():
    """Bot's own Slack user id (cached) — used to ignore reaction events we caused."""
    global _bot_user_id
    if _bot_user_id is None:
        try:
            _bot_user_id = app.client.auth_test()["user_id"]
        except Exception:
            return None
    return _bot_user_id


def remove_alive_blob(channel_id, thread_ts):
    """Remove the bot's own party-blob from the thread root so Slack visually
    reflects archived/ended state (users can't remove the bot's reaction)."""
    try:
        app.client.reactions_remove(channel=channel_id, name=ALIVE_REACTION, timestamp=thread_ts)
        return True
    except Exception as e:
        if "no_reaction" not in str(e):
            print(f"[alive] reactions_remove({ALIVE_REACTION}) failed: {e}", flush=True)
        return False


def archive_thread(channel_id, thread_ts):
    """Archive a thread: drop it from the dashboard and clear the bot's party-blob.
    Running jobs keep going — this is the non-destructive counterpart to end_thread."""
    dashboard.set_alive(channel_id, thread_ts, False)
    remove_alive_blob(channel_id, thread_ts)
    print(f"[alive] archived thread {channel_id}:{thread_ts}", flush=True)


def revive_thread(channel_id, thread_ts):
    """Bring a thread back onto the dashboard. An explicitly revived 'ended' thread
    is flipped back to idle so its card shows. No Slack reaction is added."""
    dashboard.set_alive(channel_id, thread_ts, True)
    try:
        with open(dashboard._thread_file(channel_id, thread_ts)) as f:
            if json.load(f).get("status") == "ended":
                dashboard.mark_thread(channel_id, thread_ts, "idle")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    print(f"[alive] revived thread {channel_id}:{thread_ts}", flush=True)


# --- End-thread (dashboard "End" button + ❌ reaction) ---

def end_thread(channel_id, thread_ts):
    """Stop every running session in a thread, retire its resume UUIDs, and persist
    a final 'ended' card with a one-line summary. Used by the cancel watcher (which
    the dashboard triggers via a request file) and by the ❌ reaction."""
    runs = _load_active_runs()
    killed = []
    for sid, info in list(runs.items()):
        if info.get("channel_id") != channel_id or info.get("thread_ts") != thread_ts:
            continue
        cancelled_sessions.add(sid)            # so the run loop exits without delivering
        subprocess.run(["tmux", "kill-session", "-t", sid], capture_output=True)
        with active_lock:
            active_sessions.pop(sid, None)
        # Read whatever output exists for a closing summary, then poke the marker so the
        # still-spinning run loop wakes, sees it cancelled, and bails promptly.
        out_path = info.get("output_file")
        if out_path and os.path.exists(out_path):
            try:
                with open(out_path) as f:
                    raw = f.read()
                dashboard.record_thread(channel_id, thread_ts,
                                        info.get("original_command", ""), raw, status="ended")
                if TMUX_DONE_MARKER not in raw:
                    with open(out_path, "a") as f:
                        f.write(f"\n{TMUX_DONE_MARKER}\n")
            except Exception:
                pass
        killed.append(sid)
    clear_claude_session_uuid(channel_id, thread_ts)
    dashboard.mark_thread(channel_id, thread_ts, "ended")
    remove_alive_blob(channel_id, thread_ts)  # ended threads lose the alive indicator too
    print(f"[end] thread {channel_id}:{thread_ts} killed={killed}", flush=True)


def cancel_watcher():
    """Poll CANCEL_DIR for End requests the dashboard dropped, and act on them.
    This is the only bridge the bot needs to the (separate) dashboard process."""
    while True:
        try:
            for fname in os.listdir(CANCEL_DIR):
                if not fname.endswith(".req"):
                    continue
                path = os.path.join(CANCEL_DIR, fname)
                try:
                    with open(path) as f:
                        req = json.load(f)
                except Exception:
                    req = {}
                os.remove(path)
                ch, th = req.get("channel_id"), req.get("thread_ts")
                if ch and th:
                    end_thread(ch, th)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[cancel-watcher] {e}", flush=True)
        time.sleep(1.5)


def inbox_watcher():
    """Poll INBOX_DIR for follow-up commands sent from the dashboard. Each is
    posted into its Slack thread as a visible message (so the thread stays the
    single source of truth) and then run through parse_and_run as usual."""
    while True:
        try:
            for fname in sorted(os.listdir(INBOX_DIR)):
                if not fname.endswith(".req"):
                    continue
                path = os.path.join(INBOX_DIR, fname)
                try:
                    with open(path) as f:
                        req = json.load(f)
                except Exception:
                    req = {}
                os.remove(path)
                ch = req.get("channel_id")
                th = req.get("thread_ts")
                text = (req.get("text") or "").strip()
                if not ch or not th or not text:
                    continue
                try:
                    app.client.chat_postMessage(channel=ch, thread_ts=th,
                                                text=f"⌨️ _from dashboard:_ {text}")
                except Exception as e:
                    print(f"[inbox] post failed: {e}", flush=True)
                parse_and_run(text, _recover_say(ch), th, ch, DASHBOARD_USER, client=app.client)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[inbox-watcher] {e}", flush=True)
        time.sleep(1.5)


if __name__ == "__main__":
    print(f"Bot started! Machine: {MACHINE_NAME} | Gateway: {IS_GATEWAY} | Work dir: {WORK_DIR}")
    print(f"Context dir: {CONTEXT_DIR}")
    print(f"SSH hosts: {SSH_HOSTS}")
    print(f"Allowed users: {ALLOWED_USERS if ALLOWED_USERS else 'everyone'}")
    recover_orphaned_runs()
    threading.Thread(target=cancel_watcher, daemon=True).start()
    print(f"[cancel-watcher] watching {CANCEL_DIR}", flush=True)
    threading.Thread(target=inbox_watcher, daemon=True).start()
    print(f"[inbox-watcher] watching {INBOX_DIR}", flush=True)
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
