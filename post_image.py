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
import time
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

    # 2) upload the RAW bytes (NOT requests multipart files={...}: multipart makes
    #    Slack store an empty filetype/mimetype with no thumbnail -> invisible)
    with open(path, "rb") as f:
        pr = requests.post(upload_url, data=f.read(),
                           headers={"Content-Type": "application/octet-stream"}, timeout=120)
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
        return file_id
    print(f"[post_image] completeUploadExternal error: {c.get('error')}")
    return None


def post_image_blocks(file_ids_titles, channel, thread=None):
    """On Enterprise Grid, bot files uploaded via the external-upload flow are
    shared to the channel but do NOT render inline for other members. Re-post
    them as `image` blocks referencing each file by id (slack_file) — those DO
    render for everyone. (files.sharedPublicURL needs a user token: bot tokens
    get not_allowed_token_type.)"""
    blocks = []
    for fid, title in file_ids_titles:
        blocks.append({"type": "image", "slack_file": {"id": fid},
                       "alt_text": title, "title": {"type": "plain_text", "text": title[:2000]}})
    if not blocks:
        return
    payload = {"channel": channel, "blocks": blocks, "text": "figures"}
    if thread:
        payload["thread_ts"] = thread
    # Right after completeUploadExternal, Slack is still processing the file
    # (empty mimetype/filetype); an image block referencing it by id fails
    # `invalid_blocks` until processing finishes. Retry with backoff.
    c = {}
    for attempt in range(6):
        c = requests.post("https://slack.com/api/chat.postMessage",
                          headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                          json=payload, timeout=15).json()
        if c.get("ok") or c.get("error") != "invalid_blocks":
            break
        time.sleep(2)
    print("[post_image] inline image blocks: " + ("ok" if c.get("ok") else f"error {c.get('error')}"))


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

    img_ids = []  # (file_id, title) for inline image-block re-post
    IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    for p in args.files:
        if os.path.exists(p):
            try:
                fid = upload(p, args.channel, args.thread, args.title)
                if fid and p.lower().endswith(IMG_EXT):
                    img_ids.append((fid, args.title or os.path.basename(p)))
            except Exception as e:
                print(f"[post_image] {p} skipped: {str(e)[:120]}")
        else:
            print(f"[post_image] missing file: {p}")

    # Enterprise-Grid render fix: bot external-upload files don't preview inline
    # for other members; re-post images as image blocks referencing them by id.
    if img_ids:
        try:
            post_image_blocks(img_ids, args.channel, args.thread)
        except Exception as e:
            print(f"[post_image] image-block re-post skipped: {str(e)[:120]}")
    sys.exit(0)  # never hard-fail a background job
