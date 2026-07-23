# Troubleshooting

Symptom → fix, roughly in the order people hit them.

## The bot doesn't respond at all

- Is `bot.py` running and did it print `Bot started!`? If it crashed on startup, the console says which env var is missing.
- Wrong token pairing: `SLACK_BOT_TOKEN` must be the `xoxb-...` from *OAuth & Permissions*, `SLACK_APP_TOKEN` the `xapp-...` app-level token with `connections:write`.
- Is the bot **invited** to the channel? (`/invite @research-bot`). DMs need no invite.
- App not reinstalled after a scope change: *Install App → Reinstall to Workspace*.
- The console logs every event (`[mention] channel=... text=...`). No log line = Slack isn't delivering the event (check the app's *Event Subscriptions* match the manifest).

## "Sorry, you don't have access to this bot."

Your Slack user ID isn't in `ALLOWED_USERS`. Add it (profile → ⋮ → *Copy member ID*) and restart the bot.

## "claude did not start. Check `tmux attach -t claude-...`"

The `claude` CLI isn't reachable in a non-interactive shell. The runner extends `PATH` with `~/.local/bin`, `~/.npm/bin`, and conda bins; if yours lives elsewhere, symlink it (`ln -s $(which claude) ~/.local/bin/claude`). Attach to the named tmux session to see the actual error — commonly a login/credentials prompt.

## Reply says usage limit hit

Claude's API/subscription limit. The bot detects structured rate-limit errors and tells you rather than posting a broken reply — resend after the limit resets.

## `⏰ timed out (30 min limit)`

Single commands are capped at 30 minutes. For long work, ask for it as a background job ("run this in tmux and notify me") — that's the designed path and has no time limit.

## `⚠️ at max capacity (5 concurrent tasks)`

Five runs are already active on that machine. Wait, or ❌ threads you no longer need. Raise `MAX_CONCURRENT` in `bot.py` if your machine can take it.

## Remote machine problems

- `❌ No SSH config for ...` — add `SSH_<name>=user@host[:port]` to the **gateway's** `.env` and restart.
- SSH errors / hangs — the bot uses `BatchMode=yes`: password prompts fail by design. Test with `ssh -o BatchMode=yes user@host 'echo ok'`; set up key auth if it fails.
- Runs start but never finish — check `tmux` and `claude` exist on the worker for non-interactive shells (`ssh user@host 'claude --version'`).

## Attachments / result files don't appear

The manifest includes `files:read` and `files:write`; if you created the app manually, add them and reinstall. Result files are only auto-uploaded when the agent's reply mentions their path.

## Dashboard shows nothing

Run `dashboard.py` from the repo directory on the same machine as the bot — they share state through `threads/`, `active_runs.json`, and `cancel/` in the repo. Cards appear after the first command completes.

## A run was lost when the bot restarted

It usually isn't: journaled runs are re-attached and delivered after restart. If the restart killed the tmux session itself, the bot posts "interrupted — please resend" in the affected thread. If you saw neither, check `active_runs.json` for stale entries and the console `[recover]` lines.

## Replies arrive with weird formatting

Slack's own quirk-set (no real Markdown). The bot converts headings/bold/links/bullets; deeply nested Markdown or tables inside replies will still look plain. Complex artifacts are better written to a file and shared.

Still stuck? [Open an issue](https://github.com/Minyoung1005/research-bot/issues) with the console output around the failure.
