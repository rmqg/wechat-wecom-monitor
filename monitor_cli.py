#!/usr/bin/env python3
"""Command-line interface; the offline route requires only the Python standard library."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from monitor_engine import atomic_json, check_export, load_config, read_export, send_chat

ROOT = Path(__file__).resolve().parent


def doctor(config_path=None):
    checks = []
    def add(name, status, detail, fix):
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})
    add("python", "ok" if sys.version_info >= (3, 10) else "failed", sys.version.split()[0], "python3 --version  # 安装 Python 3.10 或更新版本")
    config = load_config()
    if config_path:
        path = Path(config_path)
        try:
            config = load_config(path)
            mode = path.stat().st_mode & 0o777
            add("config", "ok" if mode == 0o600 else "failed", "配置可解析；权限应为 600", f"chmod 600 {shlex.quote(str(path))}")
        except (OSError, ValueError, TypeError) as exc:
            add("config", "failed", str(exc), "./install.sh --non-interactive --config examples/install.env.example")
    else:
        add("config", "warning", "未指定真实配置；离线演示可直接运行", "./install.sh --non-interactive --config examples/install.env.example")
    add("stdlib", "ok", "离线采集、导出、分类、推送模拟不依赖第三方库", "python3 --version")
    local = config.get("local_db", {})
    for module, package in (("Crypto", "pycryptodome"), ("zstandard", "zstandard"), ("pypdf", "pypdf"), ("docx", "python-docx"), ("openpyxl", "openpyxl")):
        present = importlib.util.find_spec(module) is not None
        required = bool(local) and module in {"Crypto", "zstandard"}
        add(module, "ok" if present else ("failed" if required else "warning"),
            "本地库依赖" if required else "可选功能依赖（离线演示不需要）", f"python3 -m pip install {package}")
    if local:
        accounts = local.get("accounts", [])
        if not accounts:
            add("local_db", "failed", "local_db.accounts 为空", "python3 -m json.tool " + shlex.quote(str(config_path or "monitor-config.json")))
        for index, account in enumerate(accounts):
            for key in ("base_dir", "key_file"):
                path = Path(account.get(key, ""))
                valid = bool(account.get(key)) and (path.is_dir() if key == "base_dir" else path.is_file())
                fix = f"test {'-d' if key == 'base_dir' else '-f'} {shlex.quote(str(path))}  # 修正 local_db.accounts 中的路径"
                add(f"local_db.{index}.{key}", "ok" if valid else "failed", "只读源路径检查", fix)
    export_root = config.get("export_root")
    if export_root:
        try:
            read_export(export_root)
            add("export", "ok", "chat-export/v1 导出目录可读取", "./bin/wechat-wecom-monitor check --config " + shlex.quote(str(config_path)))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            add("export", "failed", str(exc), f"test -f {shlex.quote(str(Path(export_root) / 'manifest.json'))}  # 先运行上游导出工具")
    elif not local:
        add("source", "warning", "尚未配置导出源或本地库；不影响离线演示", "./bin/wechat-wecom-monitor demo")
    command = config.get("send_cmd")
    if command:
        try:
            args = shlex.split(command)
            executable = bool(args) and shutil.which(args[0])
            if not executable:
                add("sender", "failed", "找不到发送命令", "command -v " + shlex.quote(args[0] if args else "wechat-send"))
            elif not config.get("send_to"):
                add("sender", "failed", "缺少 send_to", "python3 -m json.tool " + shlex.quote(str(config_path)))
            else:
                result = send_chat(command, config["send_to"], "监控连通性诊断（不提交）", "monitor-doctor-probe", dry_run=True)
                add("sender", "ok" if result["status"] == "dry_run" else "failed", "chat-send/v1 --dry-run: " + result["status"], shlex.join(args + ["--to", config["send_to"], "--text", "诊断", "--dry-run", "--json"]))
        except ValueError as exc:
            add("sender", "failed", str(exc), "command -v wechat-send")
    else:
        add("sender", "warning", "未配置推送；check 仍会完成分类", "command -v wechat-send  # 将命令及 send_to 写入配置")
    if config.get("bark_url") and not config["bark_url"].startswith("https://"):
        add("bark", "failed", "推送地址要求 HTTPS", "python3 -m json.tool " + shlex.quote(str(config_path)))
    return {"schema": "monitor-doctor/v1", "ok": not any(c["status"] == "failed" for c in checks), "checks": checks}


def _export_rows(root, platform, rows, account_id="acct_synthetic"):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    conversations = []
    for cid, name, kind, messages in rows:
        path = root / "conversations" / (cid + ".jsonl")
        path.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for message in messages:
                stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        member_path = root / "members" / (cid + ".json")
        atomic_json(member_path, {"conversation_id": cid, "updated_at": int(time.time()),
                                  "members": [{"id": m["sender_id"], "name": m.get("sender_name", "示例成员")} for m in messages]})
        conversations.append({"id": cid, "name": name, "kind": kind, "file": "conversations/" + path.name,
                              "members_file": "members/" + member_path.name,
                              "last_ts": max((m["ts"] for m in messages), default=0), "message_count": len(messages)})
    atomic_json(root / "manifest.json", {"schema": "chat-export/v1", "producer": {"name": "wechat-wecom-monitor", "version": "0.1.0"},
                                       "source": {"platform": platform, "account_id": account_id},
                                       "generated_at": "2026-01-01T00:00:00+00:00", "timezone": "UTC", "conversations": conversations})


def demo(output=None):
    started = time.monotonic()
    temporary = tempfile.TemporaryDirectory(prefix="monitor-demo-") if output is None else None
    root = Path(output or temporary.name).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("demo --output 需要空目录，避免覆盖已有数据")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = load_config(ROOT / "docs/monitor-config-TEMPLATE.json")
    reports = []
    sender = shlex.join([sys.executable, str(ROOT / "bin/wechat-wecom-monitor"), "demo-send"])
    try:
        # Synthetic SQLite rows exercise collection before producing contract directories.
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE synthetic(platform TEXT, cid TEXT, name TEXT, kind TEXT, id TEXT, text TEXT, sender TEXT)")
            for platform in ("wechat", "wecom"):
                db.executemany("INSERT INTO synthetic VALUES(?,?,?,?,?,?,?)", [
                    (platform, "group_family", "家庭示例群", "group", "1", "回家吃饭", "acct_parent_example"),
                    (platform, "group_project", "项目示例群", "group", "2", "通知：会议安排", "acct_member_example"),
                    (platform, "group_interest", "兴趣示例群", "group", "3", "天气真好", "acct_member_example"),
                    (platform, "private_example", "示例私聊", "private", "4", "周末好", "acct_friend_example"),
                ])
                rows = []
                for cid, name, kind in db.execute("SELECT DISTINCT cid,name,kind FROM synthetic WHERE platform=?", (platform,)):
                    messages = [{"id": mid, "ts": 1767225600 + int(mid), "sender_id": sender_id, "type": "text", "text": text}
                                for mid, text, sender_id in db.execute("SELECT id,text,sender FROM synthetic WHERE platform=? AND cid=?", (platform, cid))]
                    rows.append((cid, name, kind, messages))
                export = root / platform
                _export_rows(export, platform, rows)
                report = check_export(export, config, root / (platform + "-watermark.json"), sender, "synthetic_target")
                reports.append(report)
                repeat = check_export(export, config, root / (platform + "-watermark.json"), sender, "synthetic_target")
                if any(repeat["counts"].values()):
                    raise RuntimeError("合成增量消费失败")
        return {"schema": "monitor-demo/v1", "synthetic": True, "network_used": False, "reports": reports,
                "elapsed_seconds": round(time.monotonic() - started, 3), "output": str(root) if output else None}
    finally:
        if temporary:
            temporary.cleanup()


def check_local(config, state_file=None, send_cmd=None, send_to=None, bark_url=None, dry_run=False):
    spec = importlib.util.spec_from_file_location("monitor_local_wechat", ROOT / "wechat/wechat_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix="monitor-local-") as tmp:
        report = module.collect_local(config, since=0, seen_file=None)
        if report.get("errors"):
            raise ValueError("本地采集错误：" + json.dumps(report["errors"], ensure_ascii=False))
        rows = []
        import hashlib
        for section, kind in (("groups", "group"), ("private", "private")):
            for cid, slot in report.get(section, {}).items():
                safe_id = "local_" + hashlib.sha256(cid.encode()).hexdigest()[:24]
                messages = []
                for row in slot.get("messages", []):
                    message = {"id": str(row.get("server_id") or f'{row["local_id"]}:{row["create_time"]}'),
                               "ts": int(row["create_time"]), "sender_id": row.get("sender_wxid", ""),
                               "sender_name": row.get("sender_name", ""),
                               "type": {1: "text", 3: "image", 34: "voice", 43: "video", 47: "sticker", 49: "file", 10000: "system"}.get(row.get("type"), "other"),
                               "text": row.get("text", "")}
                    messages.append(message)
                rows.append((safe_id, slot.get("name") or cid, kind, messages))
                # IDs are made safe for the contract; preserve ID-based group rules.
                for group in config.get("groups", []):
                    if group.get("id") == cid:
                        group["id"] = safe_id
        export = Path(tmp) / "export"
        account_id = hashlib.sha256(json.dumps(config.get("local_db", {}).get("accounts", []), sort_keys=True).encode()).hexdigest()
        _export_rows(export, "wechat", rows, account_id)
        result = check_export(export, config, state_file, send_cmd, send_to, bark_url, dry_run)
        result["collector"] = {"accounts": report.get("accounts", {}), "new_watermark": report.get("new_watermark")}
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="微信/企微本地独立消息监控（离线演示无需账号）")
    parser.add_argument("--version", action="version", version="0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)
    demo_parser = sub.add_parser("demo", help="离线合成数据端到端演示")
    demo_parser.add_argument("--output")
    doctor_parser = sub.add_parser("doctor", help="检查环境并提供修复命令")
    doctor_parser.add_argument("--config")
    check_parser = sub.add_parser("check", help="增量读取导出目录或只读本地库")
    check_parser.add_argument("--export-root")
    check_parser.add_argument("--local-db", action="store_true")
    check_parser.add_argument("--config")
    check_parser.add_argument("--state-file")
    check_parser.add_argument("--send-cmd")
    check_parser.add_argument("--send-to")
    check_parser.add_argument("--bark-url")
    check_parser.add_argument("--dry-run", action="store_true")
    mock = sub.add_parser("demo-send", help=argparse.SUPPRESS)
    mock.add_argument("--to", required=True)
    mock.add_argument("--text", required=True)
    mock.add_argument("--request-id", required=True)
    mock.add_argument("--timeout")
    mock.add_argument("--json", action="store_true")
    mock.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            result = demo(args.output)
        elif args.command == "doctor":
            result = doctor(args.config)
        elif args.command == "demo-send":
            result = {"schema": "chat-send/v1", "status": "dry_run" if args.dry_run else "sent", "request_id": args.request_id,
                      "to": args.to, "conversation_id": "synthetic_target", "submitted_at": int(time.time()), "extra": {"synthetic": True}}
        else:
            config = load_config(args.config)
            export_root = args.export_root or config.get("export_root")
            options = dict(config=config, state_file=args.state_file, send_cmd=args.send_cmd, send_to=args.send_to, bark_url=args.bark_url, dry_run=args.dry_run)
            if args.local_db or (not export_root and config.get("local_db")):
                result = check_local(**options)
            elif export_root:
                result = check_export(export_root, **options)
            else:
                parser.error("check 需要 --export-root 或配置 local_db")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "doctor" and not result["ok"] else 0
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(json.dumps({"schema": "monitor-error/v1", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
