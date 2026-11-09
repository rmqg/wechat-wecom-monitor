#!/usr/bin/env python3
"""Deterministic local classification and chat-export/v1 incremental delivery."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
import urllib.request

DEFAULT_CONFIG = {
    "groups": [], "key_people": [], "user": {"ids": [], "aliases": []},
    "rules": {"urgent_keywords": ["紧急", "急事", "立即", "urgent"],
              "notification_keywords": ["通知", "截止", "作业", "签到", "会议", "安排"],
              "private": "DIGEST", "mention": "CALL"},
}
ACTIONS = {"CALL", "DIGEST", "IGNORE"}


def load_config(path=None):
    config = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("配置必须是 JSON 对象")
        config.update(value)
        config["rules"] = {**DEFAULT_CONFIG["rules"], **value.get("rules", {})}
    if not isinstance(config.get("groups"), list) or not isinstance(config.get("key_people"), list):
        raise ValueError("groups 和 key_people 必须是数组")
    for group in config["groups"]:
        if not isinstance(group, dict) or group.get("tier", "P2") not in {"P0", "P1", "P2", "IGNORE"}:
            raise ValueError("群 tier 必须为 P0/P1/P2/IGNORE")
    for key in ("private", "mention"):
        if config["rules"].get(key) not in ACTIONS:
            raise ValueError(f"rules.{key} 必须为 CALL/DIGEST/IGNORE")
    return config


def _terms(value):
    if isinstance(value, str):
        return [s.strip() for s in re.split(r"[,，、;；\n|]+", value) if s.strip()]
    if isinstance(value, list):
        return [str(s) for s in value if str(s)]
    return []


def classify_message(message, conversation=None, config=None):
    """Classify a contract message; return an auditable action and reason, no model needed."""
    config = config or DEFAULT_CONFIG
    conversation = conversation or {}
    rules = {**DEFAULT_CONFIG["rules"], **config.get("rules", {})}
    user = config.get("user", {})
    own_ids = _terms(user.get("ids", [])) + [str(user[k]) for k in ("main_id", "bot_id", "main_wxid", "bot_wxid") if user.get(k)]
    sender_id = str(message.get("sender_id", ""))
    sender_name = str(message.get("sender_name", message.get("sender", "")))
    text = str(message.get("text", message.get("content", "")) or "")
    lower = text.casefold()
    group = next((g for g in config.get("groups", []) if
                  (g.get("id") and str(g["id"]) == str(conversation.get("id"))) or
                  (g.get("name") and g["name"] == conversation.get("name"))), {})
    tier = group.get("tier", "P2")
    def result(action, reason):
        return {"action": action, "reason": reason, "tier": tier,
                "platform": conversation.get("platform", message.get("platform", "other"))}
    if message.get("is_self") or (sender_id and sender_id in own_ids):
        return result("IGNORE", "自己发送的消息")
    if message.get("type") in {"system", "revoke", "sticker"}:
        return result("IGNORE", "系统、撤回或表情消息")
    if conversation.get("kind", "group") == "group" and tier == "IGNORE":
        return result("IGNORE", "群明确配置为 IGNORE")
    urgent = next((word for word in _terms(rules.get("urgent_keywords")) if word.casefold() in lower), None)
    if urgent:
        return result("CALL", f"紧急关键词：{urgent}")
    mentions = _terms(message.get("mentions", []))
    aliases = _terms(user.get("aliases", [])) + _terms(group.get("user_card", []))
    if any(uid in mentions for uid in own_ids) or "@all" in mentions or any(
            re.search(r"@" + re.escape(alias) + r"(?=$|[\s，,。.!！?？:：])", text) for alias in aliases):
        return result(rules["mention"], "直接 @ 用户或 @所有人")
    for person in config.get("key_people", []):
        ids = [str(person.get(k, "")) for k in ("id", "wxid")]
        names = [str(person.get(k, "")) for k in ("name", "名字")]
        if (sender_id and sender_id in ids) or (sender_name and sender_name in names):
            return result(person.get("action", "CALL") if person.get("action", "CALL") in ACTIONS else "CALL", "关键联系人")
    if conversation.get("kind") == "private":
        return result(rules["private"], "私聊规则")
    authority = any(v in {sender_id, sender_name} for v in _terms(group.get("authorities")))
    call_when = next((v for v in _terms(group.get("call_when")) if v.casefold() in lower), None)
    notice = next((v for v in _terms(rules.get("notification_keywords")) if v.casefold() in lower), None)
    if tier == "P0" and (authority or call_when):
        return result("CALL", "P0 权威发言" if authority else f"P0 call_when：{call_when}")
    if (tier in {"P0", "P1"} and (authority or call_when or notice)) or (tier == "P2" and notice):
        return result("DIGEST", "重要群权威发言" if authority else f"通知或群规则：{call_when or notice}")
    return result("IGNORE", "未命中重要规则的普通消息")


classify = classify_message


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _child(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("导出文件不存在或路径越过 export-root")
    return path


def read_export(export_root):
    root = Path(export_root).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "chat-export/v1":
        raise ValueError("manifest.schema 必须为 chat-export/v1")
    source = manifest.get("source", {})
    namespace = _digest([source.get("platform"), source.get("account_id")])
    records = []
    for entry in manifest.get("conversations", []):
        cid = entry.get("id", "")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", cid):
            raise ValueError("非法 conversation_id")
        conversation = {**entry, "platform": source.get("platform", "other")}
        members = {}
        if entry.get("members_file"):
            member_data = json.loads(_child(root, entry["members_file"]).read_text(encoding="utf-8"))
            members = {str(m["id"]): m.get("name") or m.get("display_name", "") for m in member_data.get("members", [])}
        latest = {}
        with _child(root, entry["file"]).open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    break  # An exporter may still be appending its final line.
                message = json.loads(line)
                if not isinstance(message.get("id"), str) or type(message.get("ts")) is not int:
                    raise ValueError("消息需要 string id 和 integer ts")
                if not isinstance(message.get("sender_id"), str) or not isinstance(message.get("type"), str):
                    raise ValueError("消息缺 sender_id/type")
                latest[message["id"]] = message
        for message in sorted(latest.values(), key=lambda m: (m["ts"], m["id"])):
            fingerprint = _digest(message)
            message = dict(message)
            if not message.get("sender_name"):
                message["sender_name"] = members.get(message["sender_id"], "")
            records.append((namespace + ":" + cid + ":" + message["id"], fingerprint, conversation, message))
    return manifest, records


def send_chat(command, target, text, request_id, dry_run=False):
    args = shlex.split(command) if isinstance(command, str) else list(command)
    if not args or not target:
        raise ValueError("发送需要 send_cmd 和 send_to")
    args += ["--to", target, "--text", text, "--request-id", request_id, "--timeout", "20", "--json"]
    if dry_run:
        args.append("--dry-run")
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"schema": "chat-send/v1", "status": "unknown", "request_id": request_id,
                "detail": "发送进程未能确认结果；必须人工确认，禁止自动重发"}
    try:
        result = json.loads(proc.stdout)
        expected = {"sent": 0, "failed": 1, "unknown": 2, "invalid": 3, "dry_run": 0}
        valid = (result.get("schema") == "chat-send/v1" and result.get("request_id") == request_id
                 and result.get("status") in expected and proc.returncode == expected[result["status"]]
                 and ((result["status"] == "dry_run") == dry_run or result["status"] in {"failed", "unknown", "invalid"}))
        if valid:
            return result
    except (ValueError, AttributeError):
        pass
    return {"schema": "chat-send/v1", "status": "unknown", "request_id": request_id,
            "detail": "响应格式或退出码不符合契约；禁止自动重发"}


def _send_bark(url, text, request_id, dry_run=False):
    if dry_run:
        return {"status": "dry_run", "request_id": request_id, "transport": "bark"}
    payload = json.dumps({"title": "消息监控", "body": text}).encode()
    try:
        request = urllib.request.Request(url, payload, {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.load(response)
        status = "sent" if value.get("code") == 200 else "unknown"
    except Exception:
        status = "unknown"
    return {"status": status, "request_id": request_id, "transport": "bark"}


@contextlib.contextmanager
def _state_lock(path, dry_run):
    if dry_run:
        yield
        return
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def check_export(export_root, config=None, state_file=None, send_cmd=None, send_to=None, bark_url=None, dry_run=False):
    config = config or load_config()
    state_path = Path(state_file or config.get("state_file") or Path.home() / ".local/state/wechat-wecom-monitor/watermark.json")
    send_cmd = send_cmd or config.get("send_cmd")
    send_to = send_to or config.get("send_to")
    bark_url = bark_url or config.get("bark_url")
    if send_cmd and not send_to:
        raise ValueError("配置 send_cmd 时必须提供 send_to")
    if bark_url and not bark_url.startswith("https://"):
        raise ValueError("bark_url 必须使用 HTTPS")
    with _state_lock(state_path, dry_run):
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"schema": "monitor-state/v1", "seen": {}, "deliveries": {}, "last_ts": {}}
        if state.get("schema") != "monitor-state/v1":
            raise ValueError("水位文件 schema 不匹配")
        manifest, records = read_export(export_root)
        report = {"schema": "monitor-report/v1", "source": manifest.get("source", {}), "dry_run": dry_run,
                  "counts": {"CALL": 0, "DIGEST": 0, "IGNORE": 0}, "messages": [], "deliveries": []}
        for key, fingerprint, conversation, message in records:
            if state["seen"].get(key) == fingerprint:
                continue
            decision = classify_message(message, conversation, config)
            report["counts"][decision["action"]] += 1
            report["messages"].append({"conversation_id": conversation["id"], "id": message["id"], "ts": message["ts"], **decision})
            state["seen"][key] = fingerprint
            cursor_key = key.rsplit(":", 1)[0]
            state["last_ts"][cursor_key] = max(message["ts"], state["last_ts"].get(cursor_key, 0))
            if decision["action"] == "IGNORE":
                continue
            text = f'[{decision["action"]}] {conversation.get("name", conversation["id"])} / {message.get("sender_name") or message["sender_id"]}: {message.get("text", "")}\n原因：{decision["reason"]}'
            for transport, destination in (("chat-send", send_to if send_cmd else None), ("bark", bark_url)):
                if not destination:
                    continue
                request_id = _digest([key, fingerprint, transport, destination])
                if request_id not in state["deliveries"]:
                    state["deliveries"][request_id] = {"status": "pending", "transport": transport, "destination": destination, "text": text}
        if not dry_run:
            atomic_json(state_path, state)
        for request_id, delivery in state["deliveries"].items():
            if delivery["status"] not in {"pending", "failed"}:
                if delivery["status"] == "unknown":
                    report["deliveries"].append({"request_id": request_id, "status": "unknown", "automatic_retry": False})
                continue
            if delivery["transport"] == "chat-send" and (not send_cmd or delivery["destination"] != send_to):
                continue
            if delivery["transport"] == "bark" and delivery["destination"] != bark_url:
                continue
            if not dry_run:
                delivery["status"] = "unknown"  # Persist BEFORE submission; crashes must fail closed.
                atomic_json(state_path, state)
            result = (send_chat(send_cmd, delivery["destination"], delivery["text"], request_id, dry_run)
                      if delivery["transport"] == "chat-send" else
                      _send_bark(delivery["destination"], delivery["text"], request_id, dry_run))
            report["deliveries"].append(result)
            if not dry_run:
                delivery["status"] = result["status"]
                delivery["updated_at"] = int(time.time())
                if result["status"] in {"sent", "invalid", "unknown"}:
                    delivery.pop("text", None)
                atomic_json(state_path, state)
        return report
