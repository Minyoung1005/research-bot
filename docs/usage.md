# Usage

## Commands

Mention the bot in a channel, or DM it (no mention needed in DMs).

| Message | What happens |
|---------|--------------|
| `@research-bot <command>` | run on this channel's default machine |
| `@research-bot /server <command>` | run on machine `server` ([multi-machine](multi-machine.md)) |
| `@research-bot /all <command>` | broadcast to every machine |
| `... --model codex` | run with the Codex CLI |
| `... --model codex/gpt-4o` | Codex with a specific model |
| `... --model claude/sonnet` | Claude Code with a specific model |
| `... --effort high` | reasoning-effort level for Claude (`low`–`max`) |
| `... --attachments text` | attachment mode for this message (`full`/`text`/`none`) |
| `@research-bot /history <query>` | full-text search over persisted history |
| `@research-bot summary` (or `recap`, `history`) | answer from the channel's recent messages |
| `@research-bot /idea <text>` | file an idea into your vault (needs `IDEAS_VAULT`) |

The `--model` and `--effort` flags can appear anywhere in the message; they're stripped before the command reaches the agent.

## Reactions

| Reaction | On | Effect |
|----------|----|--------|
| 👀 | your message | added by the bot: acknowledged, running |
| 👁 | thread root | added by the bot: this thread is on the dashboard (auto-managed) |
| ❌ | any message in a thread | kill every running session in that thread |
| 👍 | a user message | re-run that command |
| 📁 | thread root | archive the thread on the dashboard; remove 📁 to revive |

## Sessions: how memory works

**One Slack thread = one agent session.** The first command in a thread starts a fresh Claude Code session (seeded with the channel context and recent thread history); every later command in that thread *resumes* it, so the agent remembers everything it did — files it edited, results it computed, what you told it.

Details worth knowing:

- Sessions are **per machine**: `/server` and `/desktop` in the same thread hold separate sessions.
- **Switching agents resets the sibling**: going Claude → Codex (or back) mid-thread starts that agent fresh — they can't share internal state. The new agent still gets the thread history in its first prompt.
- Session mappings live in `thread_claude_sessions.json` and survive bot restarts.
- Up to `MAX_CONCURRENT` (default 5) commands can run at once per machine — including several in the same thread.
- Each command has a **30-minute limit**. Longer work should be launched as a background job (below).

## Long-running jobs

Ask for anything that takes longer than a few minutes (training, evals, data collection) and the agent is instructed to:

1. Launch it in a detached tmux session on the machine,
2. reply immediately with the session name and how to monitor it,
3. chain a `notify.py` call so **the thread gets pinged when the job finishes**, including a log tail.

You can also use the notifier yourself from any script:

```bash
python notify.py "Training done ✅" --log train.log --channel C0XXXXXXXXX --thread 1234567890.123456
```

## Files

- **Slack → machine:** attach files to your message; they're saved under `uploads/<ts>/` and copied to the target machine at the same path, and the agent is told where they are.
- **Machine → Slack:** if the agent's reply mentions produced files (`.png`, `.pdf`, `.csv`, `.mp4`, ...), the bot uploads them to the thread automatically.

### Attachment modes (token control)

Having the agent read media — images, video, PDFs, archives — costs far more tokens than text. The attachment mode caps that:

| Mode | Effect |
|------|--------|
| `full` *(default)* | every attachment is downloaded and offered to the agent |
| `text` | only text-like files pass (code, logs, configs, csv, ...); media is skipped **before download** and you get a 📎 notice listing what was skipped; the agent is also told not to open media already on disk |
| `none` | attachments are ignored entirely |

Set the default with `ATTACHMENT_MODE=text` in `.env`, and override per message with `--attachments full` when you really do want the agent to look at a screenshot.

## History search

Every exchange is persisted to a SQLite database with an FTS5 full-text index (survives restarts, unlike the in-prompt thread memory):

```
@research-bot /history dataloader deadlock
```

returns matching snippets from the current channel with their thread timestamps. Standard FTS5 query syntax works (`"exact phrase"`, `term1 AND term2`).

## Watching a run live

```bash
python watch_claude.py                     # all active sessions, auto-split tmux panes
python watch_claude.py /tmp/claude_out_claude-XXXX.txt   # one specific run
```

Every run also happens inside a real tmux session (`tmux ls`, then `tmux attach -t claude-...`) if you want the raw view.

## Terminal mode: `/channel` — fast, temporary sessions

The Slack round-trip is great from your phone, but when you're **at the machine**, talking to Claude Code directly is faster — and sometimes you want a throwaway conversation that *doesn't* fill the channel with messages. The bundled `/channel` command gives you both, without losing the channel's accumulated context:

```bash
cd research-bot
claude
> /channel my-project
```

`/channel <name>` loads that channel's context file — the same memory the Slack bot maintains after every command — into your live terminal session and recaps where the work stands. Then you brainstorm at full interactive speed. Nothing is posted to Slack and nothing is written to the bot's history: the session is temporary by design.

When the conversation produced something worth keeping:

```
> /slack my-project
```

posts a tidy summary (decisions, open questions, next steps) as a top-level message in the channel, so the team — and the bot's channel context on its next update — picks up where you left off. If it was truly throwaway, just exit and nothing ever happened.

Setup: map friendly names to channel IDs in `.env`, e.g. `CHANNEL_NAMES=my-project:C0AAAAAAAAA,lab-general:C0BBBBBBBBB` (`python chat_to_slack.py --list` shows them). The commands live in `.claude/commands/`, so they're available whenever you run `claude` from the repo directory; to use them from anywhere, copy them to `~/.claude/commands/` and make the `chat_to_slack.py` paths absolute.

## Customizing

- **Dashboard chat** — click any card to open it as a chat: conversation history with a message box at the bottom. Sends are posted into the Slack thread by the bot and run normally, and the reply lands in both places. Localhost-only (it's code execution) — tunnel with `ssh -L` from elsewhere.
- **Settings GUI** — with the dashboard running, open `http://localhost:8080/settings` (⚙ in the dashboard header) to edit everything below from the browser: `.env` values (secret values are masked; `.env` changes need a bot restart), `STYLE.md`, and recipes (both live-applied). The page refuses non-localhost requests — from another machine, tunnel with `ssh -L 8080:localhost:8080 <machine>`.
- **[`STYLE.md`](../STYLE.md)** — writing rules injected into every prompt: banned-word table, tone rules. Re-read on every command, so edits apply immediately.
- **[`recipes/`](../recipes/)** — task playbooks. Each `.md` file's title + first line go into a one-line index in the prompt; the agent reads the full file before doing a matching task. Starters cover plotting, monitoring, and Slurm — add your own (`deploy.md`, `dataset_checks.md`, your cluster's quirks). Like STYLE.md, changes apply on the next command.
- **`LANG_INSTRUCTION`** (top of `bot.py`) — the standing instruction every agent run receives: language policy, commit-after-changes, the long-job protocol. Edit to taste.
- **`MODELS`** registry (top of `bot.py`) — maps `--model` prefixes to runners. [Add a new agent CLI](architecture.md#adding-a-new-agent-cli) in ~20 lines.
