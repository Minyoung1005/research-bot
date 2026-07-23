#!/usr/bin/env python3
"""
Send a Slack notification when a long-running job finishes.
Sends to the correct channel/thread if provided, otherwise uses webhook.

Usage:
    python notify.py "Training complete!"
    python notify.py "Training done" --log training.log
    python notify.py "Training done" --channel C0XXXXXXXXX --thread 1234567890.123456
    python notify.py "Training done" --log training.log --channel C0XXXXXXXXX --thread 1234567890.123456

Claude Code should always pass --channel and --thread when available so the
notification goes back to the right Slack thread.
"""

import sys
import os
import argparse
import socket
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
WEBHOOK_URL     = os.environ.get("SLACK_WEBHOOK_URL")
MACHINE_NAME    = os.environ.get("MACHINE_NAME", socket.gethostname())


def send_via_api(message, channel_id, thread_ts=None, log_tail=None):
    """Send to specific channel/thread via Slack API."""
    text = f"🖥️ *{MACHINE_NAME}*: {message}"
    if log_tail:
        text += f"\n```{log_tail[-2000:]}```"

    payload = {"channel": channel_id, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=10
    )
    data = resp.json()
    if data.get("ok"):
        print(f"Slack notified (channel {channel_id}): {message}")
    else:
        print(f"Slack API error: {data.get('error')}")
        send_via_webhook(message, log_tail)  # fallback


def send_via_webhook(message, log_tail=None):
    """Fallback: send via incoming webhook."""
    if not WEBHOOK_URL:
        print("Error: no SLACK_WEBHOOK_URL and API send failed")
        return
    text = f"🖥️ *{MACHINE_NAME}*: {message}"
    if log_tail:
        text += f"\n```{log_tail[-2000:]}```"
    resp = requests.post(WEBHOOK_URL, json={"text": text}, timeout=10)
    if resp.status_code == 200:
        print(f"Slack notified (webhook): {message}")
    else:
        print(f"Webhook error: {resp.status_code}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("message", help="Message to send")
    parser.add_argument("--log", help="Log file to tail and include", default=None)
    parser.add_argument("--channel", help="Slack channel ID", default=None)
    parser.add_argument("--thread", help="Slack thread timestamp", default=None)
    args = parser.parse_args()

    log_tail = None
    if args.log and os.path.exists(args.log):
        with open(args.log, "r") as f:
            log_tail = f.read()

    if args.channel and SLACK_BOT_TOKEN:
        send_via_api(args.message, args.channel, args.thread, log_tail)
    else:
        send_via_webhook(args.message, log_tail)
