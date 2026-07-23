---
description: Load a Slack channel's context, then brainstorm live in this session
argument-hint: <channel-name or channel-id>
allowed-tools: Bash(python3 chat_to_slack.py:*), Bash(python chat_to_slack.py:*)
---

You are starting a LIVE brainstorming session grounded in the Slack channel `$ARGUMENTS`.

First, load the channel's accumulated context:

!`python3 chat_to_slack.py $ARGUMENTS --context`

Read the context above carefully. Then give me a SHORT (2-4 sentence) recap of
where this channel's work stands and what the open threads are, and ask what I
want to dig into. Do not restate the whole context verbatim. After this, just
chat with me normally — fast back-and-forth brainstorming.

When I'm ready to send a summary back to Slack, I'll use `/slack $ARGUMENTS`.
