---
description: Post a summary of this conversation to a Slack channel (top-level message)
argument-hint: <channel-name or channel-id>
allowed-tools: Bash(python3 chat_to_slack.py:*), Bash(python chat_to_slack.py:*)
---

Summarize THIS conversation into a concise, well-structured update for the Slack
channel `$ARGUMENTS`, then post it.

Guidelines for the summary:
- Use Slack-flavored markdown (single `*bold*`, `- ` bullets, `code`).
- Lead with the key decisions / conclusions, then open questions / next steps.
- Be substantive but tight — a teammate skimming the channel should get the gist.
- Do NOT include throwaway chit-chat.

Post it by piping the summary to the helper (it reads the summary from stdin):

```bash
python3 chat_to_slack.py $ARGUMENTS --title "<short title>" <<'EOF'
<your summary markdown here>
EOF
```

After posting, confirm to me which channel it went to. If the channel name is
unknown, run `python3 chat_to_slack.py --list` and ask me.
