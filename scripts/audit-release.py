#!/usr/bin/env python3
"""发布前的隐私与敏感词检查：扫描已跟踪文件里不该出现的内容。CI 和本地都运行。"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "微信账号 ID": r"wxid_[a-z0-9]{6,}",
    "真实企微账号 ID 前缀": r"\b168885\d+\b",
    "Windows 用户 SID": r"S-1-5-21-\d+",
    "个人邮箱": r"[A-Za-z0-9._%+-]+@(gmail|qq|163|outlook|hotmail)\.com",
    "CGNAT 地址（Tailscale 等）": r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b",
    "内网地址": r"\b(192\.168|10\.\d+|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+\b",
    "私有服务与挂载路径": r"/mnt/(bot-data|usb3)[A-Za-z0-9_./-]*",
    "Bark 域名": r"bark\.rmqg",
    "内部主机名": r"\b(rmqg-ai|istoreos-root|MSC-20)\b",
    "人设标识词": r"(?i)yachiyo|八千代|月见",
}
ALLOW = {
    # QEMU user-mode networking (SLIRP) built-in gateway IP
    "内网地址": {"10.0.2.2", "10.0.26100.1742"},
}


def main() -> int:
    files = [
        name
        for name in subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split("\0")
        if name
    ]
    problems = []
    for name in files:
        if name == "scripts/audit-release.py":
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text):
                if match.group(0) in ALLOW.get(label, ()):
                    continue
                problems.append(f"{name}: {label} ({match.group(0)})")
    for name in files:
        if re.search(r"(^|/)(state|runtime|archive-mirror|vendor|wechat-decrypt)/", name):
            problems.append(f"{name}: 不该进仓库的目录")
        if name.endswith((".db", ".sqlite", ".sqlite3", ".qcow2", ".iso")):
            problems.append(f"{name}: 二进制镜像或数据库文件")
    for line in sorted(set(problems)):
        print(line)
    print(f"检查了 {len(files)} 个文件，{len(set(problems))} 处问题")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
