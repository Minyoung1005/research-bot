#!/usr/bin/env python3
"""
Bridge between LIVE terminal Claude Code sessions and your Slack channels.

Two jobs:
  --context : print a channel's accumulated context file, so a terminal session
              can prime itself on where that channel's work stands (/channel).
  (default) : post a conversation summary from stdin as a clean top-level
              message in the channel (/slack). Unlike notify.py (short job
              notifications), this is for substantive summaries.

Channels are addressed by friendly name (CHANNEL_NAMES in .env) or raw ID.

Usage:
    python chat_to_slack.py my-project --context
    echo "summary text" | python chat_to_slack.py my-project
    python chat_to_slack.py C0XXXXXXXXX --title "Brainstorm: eval plan" < summary.md
    python chat_to_slack.py --list        # show known channel names
"""

import sys
import os
import argparse
import socket
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
MACHINE_NAME    = os.environ.get("MACHINE_NAME", socket.gethostname())

# Friendly name -> channel ID, configured in .env as
# CHANNEL_NAMES=my-project:C0AAAAAAAAA,lab-general:C0BBBBBBBBB
CHANNELS = dict(
    pair.strip().split(":", 1)
    for pair in os.environ.get("CHANNEL_NAMES", "").split(",")
    if ":" in pair
)

CONTEXTS_DIR = os.path.join(os.path.dirname(__file__), "contexts")


def resolve_channel(name_or_id):
    """Map a friendly name to a channel ID; pass through raw IDs unchanged."""
    if name_or_id in CHANNELS:
        return CHANNELS[name_or_id]
    if name_or_id.startswith("C") and name_or_id.isupper():
        return name_or_id  # looks like a raw channel ID
    known = ", ".join(sorted(CHANNELS)) or "(none — set CHANNEL_NAMES in .env)"
    raise SystemExit(
        f"Unknown channel '{name_or_id}'. Known: {known}\n"
        f"(or pass a raw channel ID like C0XXXXXXXXX)"
    )


def print_context(channel_id):
    """Print the per-channel context file so a live session can prime on it."""
    path = os.path.join(CONTEXTS_DIR, f"{channel_id}.md")
    if not os.path.exists(path):
        raise SystemExit(f"No context file for {channel_id} at {path} "
                         f"(the bot writes it after the first command in that channel)")
    with open(path) as f:
        sys.stdout.write(f.read())


def post(channel_id, text):
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                 "Content-Type": "application/json"},
        json={"channel": channel_id, "text": text},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        print(f"Posted to {channel_id}")
    else:
        raise SystemExit(f"Slack API error: {data.get('error')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("channel", nargs="?", help="Friendly channel name or raw ID")
    parser.add_argument("--title", default="Brainstorm summary",
                        help="Bold header line above the summary")
    parser.add_argument("--list", action="store_true", help="List known channels and exit")
    parser.add_argument("--context", action="store_true",
                        help="Print the channel's context file (to prime a session) and exit")
    args = parser.parse_args()

    if args.list:
        for name, cid in sorted(CHANNELS.items()):
            print(f"{name:16s} {cid}")
        sys.exit(0)

    if not args.channel:
        parser.error("channel is required (or use --list)")

    if args.context:
        print_context(resolve_channel(args.channel))
        sys.exit(0)

    if not SLACK_BOT_TOKEN:
        raise SystemExit("SLACK_BOT_TOKEN not set in .env")

    summary = sys.stdin.read().strip()
    if not summary:
        raise SystemExit("No summary text on stdin")

    channel_id = resolve_channel(args.channel)
    text = f"📝 *{args.title}* (live session on {MACHINE_NAME})\n\n{summary}"
    post(channel_id, text)
