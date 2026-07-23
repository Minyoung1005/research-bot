# Multi-machine setup

Control every machine you own from one Slack workspace.

## Architecture

One machine is the **gateway**: it runs `bot.py`, holds the Slack connection, and routes commands. Other machines are **workers**: the gateway SSHes in, starts a tmux session there, runs the agent CLI, and polls the output file over SSH until done.

```
Slack ⇄ gateway (bot.py) ── ssh ──▸ worker A (tmux + claude)
                        └── ssh ──▸ worker B (tmux + claude)
```

Workers do **not** run the bot. They need:

- `tmux` and the `claude` CLI (logged in) reachable from a non-interactive shell,
- SSH key access from the gateway (`BatchMode=yes` — password prompts will fail),
- a clone of this repo only if you want `watch_claude.py` there (optional).

## Configuration

On the **gateway**, add one line per worker to `.env`:

```bash
SSH_server=you@server.example.com        # default port 22
SSH_cluster=you@10.0.0.7:43284           # custom port after the colon
```

The env var name after `SSH_` becomes the machine's Slack name: `/server`, `/cluster`. The machine list is derived automatically from `MACHINE_NAME` + all `SSH_*` entries.

Test access first:

```bash
ssh -o BatchMode=yes you@server.example.com 'tmux -V && claude --version'
```

If that prints versions without prompting, routing will work.

## Routing rules

1. `/machinename` anywhere in the message wins.
2. Otherwise the channel's entry in `CHANNEL_DEFAULTS` (e.g. `CHANNEL_DEFAULTS=C0AAAAAAAAA:server`) — handy for giving each project channel its own machine.
3. Otherwise the gateway itself (`MACHINE_NAME`).
4. `/all` broadcasts to every known machine; each posts its own reply.

## What carries over to workers

- **Sessions**: per-thread session UUIDs are tracked per machine, so `/server` follow-ups resume the right session on the right box.
- **Files**: attachments you drop in Slack are `scp`'d to the worker at the same absolute path before the run starts.
- **Model/effort flags**: `--model claude/...` and `--effort` apply on workers too (Codex runs are gateway-local).

## Multiple bot instances

You can also run `bot.py` on several machines with the *same* Slack app (each machine gets its own `.env` with its own `MACHINE_NAME`). Set `IS_GATEWAY=true` on exactly one; the others set `IS_GATEWAY=false` and will ignore commands not addressed to them. In that mode, set `KNOWN_MACHINES=desktop,server,cluster` explicitly on every machine so each can recognize commands routed to its siblings. Note that Slack app-level tokens allow only one Socket Mode connection each — create one app token per machine.

The single-gateway + SSH-workers setup is simpler and is what we recommend; run multiple instances only if machines can't SSH to each other.
