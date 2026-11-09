#!/usr/bin/env python3
"""Fetch collector events over SSH and classify with the shared monitor engine."""
import argparse
from pathlib import Path
import json
import os
import subprocess
import sys
from datetime import datetime

SSH_CONFIG = "~/.ssh/wecom-ssh-config"
COLLECTOR_PY = (
    "C:/Users/wecom/AppData/Local/WeComCollector/runtime/python.exe"
)
COLLECTOR_SCRIPT = (
    "C:/Users/wecom/AppData/Local/WeComCollector/collect-wecom.py"
)
HERE = os.path.dirname(os.path.abspath(__file__))
WATERMARK_FILE = os.path.join(HERE, "wecom_last_event.md")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor_engine import classify_message, load_config


def read_watermark():
    try:
        with open(WATERMARK_FILE, encoding="utf-8") as f:
            first = f.readline().strip()
            return int(first) if first else 0
    except (FileNotFoundError, ValueError):
        return 0


def write_watermark(event_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(WATERMARK_FILE, "w", encoding="utf-8") as f:
        f.write(f"{event_id}\n")
        f.write(f"last_check: {now}\n")


def fetch_events(after_id):
    cmd = [
        "ssh", "-F", SSH_CONFIG, "wecom-windows-vm",
        f"{COLLECTOR_PY} {COLLECTOR_SCRIPT} events --after {after_id}",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "ssh timeout after 120s"
    if out.returncode != 0:
        return None, (out.stderr or out.stdout or "ssh failed").strip()[-500:]
    try:
        return json.loads(out.stdout), None
    except json.JSONDecodeError as e:
        return None, f"bad JSON from events: {e}"


def classify(record, config):
    conversation = {
        "id": record.get("conversation_id", ""),
        "name": record.get("conversation_name", ""),
        "kind": "group" if record.get("conversation_type") == "group" else "private",
    }
    message = dict(record, sender_id=record.get("sender_id", ""), type=record.get("kind", "text"))
    decision = classify_message(message, conversation, config)
    return decision["action"], decision["reason"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    args = parser.parse_args()
    config = load_config(args.config)
    last_id = read_watermark()
    events, err = fetch_events(last_id)
    report = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_event_id_before": last_id,
        "error": err,
        "events": [],
        "calls": [],
        "digests": [],
        "new_event_id": last_id,
        "new_conversations": [],
    }
    if err is not None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    max_id = last_id
    seen_names = set()
    for ev in events:
        eid = ev.get("event_id", 0)
        max_id = max(max_id, eid)
        rec = ev.get("record") or {}
        name = rec.get("conversation_name") or ""
        if name and not any(g.get("name") == name for g in config.get("groups", [])):
            seen_names.add(name)
        verdict, reason = classify(rec, config)
        item = {
            "event_id": eid,
            "kind": ev.get("kind"),
            "conversation": name,
            "sender": rec.get("sender_name") or "",
            "msg_kind": rec.get("kind") or "",
            "text": (rec.get("text") or "")[:300],
            "links": [l.get("url", "") for l in (rec.get("links") or [])][:3],
            "attachments": [
                a.get("name", "") for a in (rec.get("attachments") or [])
            ][:5],
            "sent_at": rec.get("sent_at"),
            "verdict": verdict,
            "reason": reason,
        }
        report["events"].append(item)
        if verdict == "CALL":
            report["calls"].append(item)
        elif verdict == "DIGEST":
            report["digests"].append(item)
    report["new_event_id"] = max_id
    report["new_conversations"] = sorted(seen_names)
    write_watermark(max_id)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
