# Setup

End-to-end walkthrough from nothing to a working bot. Takes about 10 minutes.

## Prerequisites

On every machine that will execute commands:

- **Python 3.9+**
- **tmux** (`apt install tmux` / `brew install tmux`)
- **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)** — installed and logged in (`claude` must work in a fresh shell). The bot looks for it in `PATH` plus `~/.local/bin`, `~/.npm/bin`, and conda locations.
- *(optional)* **[Codex CLI](https://github.com/openai/codex)** — only if you want `--model codex`; authenticated via `codex login` or `OPENAI_API_KEY`.

Only the **gateway** machine (the one connected to Slack) needs Python and the bot itself. Remote workers just need tmux + the agent CLI ([details](multi-machine.md)).

## 1. Create the Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**.
2. Pick your workspace, choose the **YAML** tab, paste the manifest below (also in [`slack-app-manifest.yaml`](../slack-app-manifest.yaml)), click **Next** → **Create**.

<details>
<summary>📋 <b>slack-app-manifest.yaml</b> — click to expand, then copy-paste</summary>

```yaml
display_information:
  name: research-bot
  description: Drive Claude Code on your own machines from Slack
  background_color: "#1a1d21"
features:
  bot_user:
    display_name: research-bot
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - channels:history
      - groups:history
      - im:history
      - im:read
      - im:write
      - mpim:history
      - chat:write
      - files:read
      - files:write
      - reactions:read
      - reactions:write
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
      - reaction_added
      - reaction_removed
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

</details>

3. **App-Level Token:** *Basic Information → App-Level Tokens → Generate Token and Scopes* → add scope `connections:write` → generate. Copy the `xapp-...` token — this is `SLACK_APP_TOKEN`.
4. **Install:** *Install App → Install to Workspace* → allow.
5. **Bot token:** *OAuth & Permissions* → copy the *Bot User OAuth Token* (`xoxb-...`) — this is `SLACK_BOT_TOKEN`.

The manifest enables **Socket Mode**, which means the bot dials out to Slack over a websocket — you need no public URL, no inbound ports, no webserver.

> **Workspace won't let you install apps?** Custom apps created from a manifest are usually allowed even where marketplace apps aren't. If your workspace blocks even that, create a free Slack workspace for yourself or your lab — it takes two minutes.

## 2. Install the bot

```bash
git clone https://github.com/Minyoung1005/research-bot.git
cd research-bot
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` — three values are required:

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
MACHINE_NAME=desktop          # how this machine is addressed: /desktop
```

Strongly recommended:

```bash
ALLOWED_USERS=U0XXXXXXXXX   # your Slack user ID — otherwise ANYONE in the workspace can run code
```

(Find your user ID in Slack: your profile → ⋮ → *Copy member ID*.)

See the [configuration reference](../README.md#configuration-reference) for everything else.

## 3. First run

```bash
python bot.py
```

You should see `Bot started! Machine: desktop | Gateway: True | ...`. In Slack, invite the bot to a channel (`/invite @research-bot`) and mention it:

```
@research-bot hello! what directory are you running in?
```

The bot reacts with 👀, posts a `⏳ running...` note, and replies with `✅ done!` when the agent finishes. DMs work without a mention.

## 4. Run it persistently

The simple way — tmux:

```bash
tmux new-session -d -s bot "cd ~/research-bot && python bot.py"
tmux new-session -d -s dashboard "cd ~/research-bot && python dashboard.py"   # optional
```

Or a systemd user service (`~/.config/systemd/user/research-bot.service`):

```ini
[Unit]
Description=research-bot Slack bot
After=network-online.target

[Service]
WorkingDirectory=%h/research-bot
ExecStart=/usr/bin/python3 %h/research-bot/bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now research-bot
```

Restarts are safe either way: in-flight runs are journaled in `active_runs.json`, and a new bot instance re-attaches to their tmux output and still delivers the results ([how](architecture.md#crash-recovery)).

## 5. Updating

```bash
git pull && pip install -r requirements.txt   # then restart the bot
```

Your `.env`, `data/`, `contexts/`, and `STYLE.md` edits are yours; only code changes come in.
