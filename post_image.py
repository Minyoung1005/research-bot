#!/usr/bin/env python3
"""Post one or more images/files to a Slack channel thread.

Uses Slack's external-upload flow (get URL -> upload raw bytes -> complete),
which previews correctly in-thread. Reuses the bot token in .env, the same
source as notify.py. Best-effort: prints status and always exits 0 so it can
be appended to a background job without ever failing it.

    python post_image.py curve.png --channel C0XXXXXXXXX --thread 123.456
    python post_image.py a.png b.png --channel C0XXXXXXXXX --thread 123.456 --title "Baseline"
"""

import os
import sys
import argparse
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ.get("SLACK_BOT_TOKEN")


def upload(path, channel, thread=None, title=None):
    size = os.path.getsize(path)
    fname = os.path.basename(path)

    # 1) reserve an upload URL
    r = requests.get(
        "https://slack.com/api/files.getUploadURLExternal",
        headers={"Authorization": f"Bearer {TOKEN}"},
        params={"filename": fname, "length": size},
        timeout=15,
    ).json()
    if not r.get("ok"):
        print(f"[post_image] getUploadURLExternal error: {r.get('error')}")
        return False
    upload_url, file_id = r["upload_url"], r["file_id"]

    # 2) upload the raw bytes
    with open(path, "rb") as f:
        pr = requests.post(upload_url, files={"file": (fname, f)}, timeout=120)
    if pr.status_code != 200:
        print(f"[post_image] byte upload failed: HTTP {pr.status_code}")
        return False

    # 3) finalize into the channel/thread
    payload = {"files": [{"id": file_id, "title": title or fname}], "channel_id": channel}
    if thread:
        payload["thread_ts"] = thread
    c = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    ).json()
    if c.get("ok"):
        print(f"[post_image] uploaded {fname}")
        return True
    print(f"[post_image] completeUploadExternal error: {c.get('error')}")
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--thread", default=None)
    ap.add_argument("--title", default=None)
    args = ap.parse_args()

    if not TOKEN:
        print("[post_image] no SLACK_BOT_TOKEN in .env; skipping")
        sys.exit(0)

    for p in args.files:
        if os.path.exists(p):
            try:
                upload(p, args.channel, args.thread, args.title)
            except Exception as e:
                print(f"[post_image] {p} skipped: {str(e)[:120]}")
        else:
            print(f"[post_image] missing file: {p}")
    sys.exit(0)  # never hard-fail a background job
