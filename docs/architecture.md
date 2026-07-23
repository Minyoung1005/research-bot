# Architecture

How a Slack message becomes an agent run and comes back. For contributors and the curious — the whole system is ~5 files of plain Python, no framework beyond Slack Bolt.

## Life of a command

1. **Event** — Slack delivers an `app_mention` (or DM) over Socket Mode. The bot checks `ALLOWED_USERS`, reacts 👀, and parses the message: `--model`/`--effort` flags out, `/machine` target out, attachments downloaded to `uploads/<ts>/`.
2. **Prompt build** (first turn in a thread) — the prompt is assembled from: the standing instruction (`LANG_INSTRUCTION`, with the channel/thread IDs baked in so `notify.py` calls land back in the right place), `STYLE.md` rules, a channel-scope guard, the per-channel context file, recent thread history, and finally your command. On later turns only the new command is sent — the resumed session already holds the history.
3. **Run** — a dedicated tmux session `claude-<timestamp>` is created (locally, or on the worker over SSH) and runs:
   ```
   claude --session-id/--resume <uuid> --output-format stream-json --verbose \
          --dangerously-skip-permissions  < prompt.txt > /tmp/claude_out_<id>.txt
   ```
   with a `___CLAUDE_DONE___` marker echoed at the end. The bot polls the output file (over SSH for workers) every 2–3 s, up to 30 min.
4. **Deliver** — the stream-json is parsed down to the assistant's text, converted from Markdown to Slack formatting, and posted in ≤2800-char chunks with automatic retry/backoff (flaky networks drop Slack API calls surprisingly often). Files the reply mentions get uploaded. The 👀 comes off.
5. **Bookkeeping** — the exchange is appended to the in-memory thread session and the SQLite history; a `claude -p` call updates the channel's context file (≤500 chars), which is committed and pushed so other machines stay in sync; the dashboard card is refreshed.

## Sessions

Per-thread, per-machine session UUIDs live in `thread_claude_sessions.json`. First turn: the bot *generates* a UUID and passes `--session-id`; later turns pass `--resume`. If Claude forks the session (it reports the actual ID in its `init` event), the stored UUID is updated. Codex sessions work the same way, except the UUID is captured from Codex's run header and resumed with `codex exec resume`. Switching runner mid-thread invalidates the other runner's session — they can't share state, so the newcomer starts fresh with the thread history in its prompt.

## Crash recovery

Every run registers itself in `active_runs.json` (removed on clean exit). On startup the bot reads leftovers, and for each: if the tmux session is still alive, re-polls its output file and delivers the result as if nothing happened; if tmux is gone, posts "this run was interrupted, please resend". This is why restarting the bot mid-run is safe.

## Cancellation

`❌` reaction or the dashboard's End button → `end_thread()`: every matching run's tmux session is killed, the session UUIDs are retired, and the run loop is woken via the done-marker so it exits without posting. The dashboard is a **separate process**; it requests cancellations by dropping `cancel/<...>.req` files that the bot's watcher thread picks up — the two processes share state only through files, so either can restart independently.

## The dashboard

`dashboard.py` (Flask) renders one card per thread from `threads/*.json`, which the bot writes after each run (status, command, todos parsed from the stream-json, last output). It periodically asks `claude -p` for one-line summaries of running sessions. It never touches Slack directly.

## History store

`history.py` — SQLite (WAL) with `sessions` and `messages` tables plus an FTS5 index maintained by triggers. `/history <query>` searches it channel-scoped. The DB lives in `data/history.db`.

## Adding a new agent CLI

Everything model-related funnels through the `MODELS` registry and `run_model()` dispatch in `bot.py`:

1. Add a registry entry: `"gemini": {"runner": "gemini", "default_model": "gemini-2.5-pro"}`.
2. Write a `run_gemini(...)` mirroring `run_codex()` — the pattern is: write prompt file → run CLI headlessly in tmux with output redirected → poll for the done marker → extract the final message → `_send_output(...)`.
3. Add a branch in `run_model()`.

The tmux/poll/deliver plumbing is agent-agnostic; a new runner is mostly "what command line do I execute and how do I pull the final text out".
