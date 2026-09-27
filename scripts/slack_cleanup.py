"""One-off: delete specified bot messages (and their threads/files) from the report channel."""
import os
import sys
import time
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

CHANNEL = os.environ.get("CHANNEL_ID", "C0B3KS5KNTC")
TARGETS = [t.strip() for t in os.environ["TARGET_TS"].split(",") if t.strip()]

# Hard guard: these must never be deleted, whatever is passed in.
PROTECTED = {
    "1790513459.658179",  # the report version being kept
    "1790506121.592199",  # Niv's comment + Quentin's reply
}

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
deleted_msgs = deleted_files = failures = 0


def delete_message(ts, label):
    global deleted_msgs, failures
    try:
        client.chat_delete(channel=CHANNEL, ts=ts)
        print(f"    deleted {label} {ts}")
        deleted_msgs += 1
    except SlackApiError as e:
        print(f"    FAILED {label} {ts}: {e.response['error']}")
        failures += 1


for parent in TARGETS:
    if parent in PROTECTED:
        print(f"SKIP protected {parent}")
        continue
    print(f"Thread {parent}")

    messages, cursor = [], None
    while True:
        try:
            r = client.conversations_replies(channel=CHANNEL, ts=parent, limit=200, cursor=cursor)
        except SlackApiError as e:
            print(f"    could not list replies: {e.response['error']}")
            messages = [{"ts": parent}]
            break
        messages.extend(r["messages"])
        cursor = r.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    for m in messages:
        for f in m.get("files") or []:
            try:
                client.files_delete(file=f["id"])
                print(f"    deleted file {f['id']} ({f.get('name')})")
                deleted_files += 1
            except SlackApiError as e:
                print(f"    file delete failed {f['id']}: {e.response['error']}")
                failures += 1

    # Replies first, newest to oldest, then the parent.
    for ts in sorted((m["ts"] for m in messages if m["ts"] != parent), reverse=True):
        if ts in PROTECTED:
            print(f"    SKIP protected reply {ts}")
            continue
        delete_message(ts, "reply")
        time.sleep(0.4)
    delete_message(parent, "parent")
    time.sleep(0.4)

print(f"\nDone: {deleted_msgs} messages, {deleted_files} files deleted, {failures} failures")
sys.exit(1 if failures else 0)
