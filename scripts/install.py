#!/usr/bin/env python3
"""交互式离线配置；env 文件仅解析白名单，不执行任何 shell 内容。"""
import sys

if sys.version_info < (3, 10):
    raise SystemExit("需要 Python 3.10+；请安装新版后用 PYTHON=/path/to/python3 sh install.sh 重试。")

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("MONITOR_CONFIG", "EXPORT_ROOT", "STATE_FILE", "SEND_CMD", "SEND_TO")
FIELDS = {"EXPORT_ROOT": "export_root", "STATE_FILE": "state_file", "SEND_CMD": "send_cmd", "SEND_TO": "send_to"}


def read_source(path):
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("JSON 配置的顶层必须为对象")
        return {}, value
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator or key not in NAMES:
            raise ValueError("env 配置第 %d 行不是支持的变量赋值" % number)
        words = shlex.split(raw, comments=True, posix=True)
        if len(words) > 1:
            raise ValueError("env 配置第 %d 行含空格的值必须加引号" % number)
        values[key] = words[0] if words else ""
    return values, {}


def atomic_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".monitor-config-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def command(*parts):
    return shlex.join(str(part) for part in parts)


def main():
    parser = argparse.ArgumentParser(description="配置本地监控器；不联网，不安装依赖，不创建定时任务。")
    parser.add_argument("--non-interactive", action="store_true", help="使用配置/环境变量与安全默认值，不提问")
    parser.add_argument("--config", type=Path, help="读取 examples/install.env.example 格式的 env 文件或完整 JSON 配置")
    args = parser.parse_args()
    os.umask(0o077)
    for module in ("sqlite3", "ssl"):
        if importlib.util.find_spec(module) is None:
            raise ValueError("Python 缺少标准库 %s；请安装完整的 Python 3.10+。" % module)
    values, supplied = read_source(args.config.expanduser()) if args.config else ({}, {})
    values.update({name: os.environ[name] for name in NAMES if name in os.environ})
    default_config = Path.home() / ".config" / "wechat-wecom-monitor" / "monitor-config.json"
    default_state = Path.home() / ".local" / "state" / "wechat-wecom-monitor" / "seen.json"
    values.setdefault("MONITOR_CONFIG", str(default_config))
    if not args.non_interactive:
        if not sys.stdin.isatty():
            raise ValueError("交互安装需要终端；自动化请使用 --non-interactive。")
        print("离线配置：不登录账号、不读取消息、不下载文件；留空保留默认值。")
        answer = input("配置文件位置 [%s]：" % values["MONITOR_CONFIG"]).strip()
        if answer:
            values["MONITOR_CONFIG"] = answer
    if not values["MONITOR_CONFIG"]:
        raise ValueError("MONITOR_CONFIG 不能为空")
    destination = Path(values["MONITOR_CONFIG"]).expanduser().absolute()
    config = {"groups": [], "key_people": [], "user": {}, "rules": {},
              "export_root": "", "state_file": str(default_state), "send_cmd": "", "send_to": "", "bark_url": ""}
    if destination.exists():
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise ValueError("已有配置的顶层必须为对象")
        config.update(existing)
    config.update(supplied)
    for name, field in FIELDS.items():
        if name in values:
            config[field] = values[name]
        if not args.non_interactive:
            label = {"EXPORT_ROOT": "导出目录（留空先跑 demo）", "STATE_FILE": "监控状态文件", "SEND_CMD": "发送命令（留空不发送）", "SEND_TO": "发送目标（留空不发送）"}[name]
            answer = input("%s [%s]：" % (label, config[field])).strip()
            if answer:
                config[field] = answer
    for field in ("export_root", "state_file"):
        if config.get(field):
            config[field] = str(Path(config[field]).expanduser().absolute())
    atomic_config(destination, config)
    print("已原子写入私有 JSON 配置（权限 0600）：%s" % destination)
    print("Python %s；核心不需要第三方包。" % sys.version.split()[0])
    if importlib.util.find_spec("pip") is None:
        print("未安装 pip；核心仍可运行。需要可选依赖时，请先手动准备 pip：")
        print("  %s" % command(sys.executable, "-m", "ensurepip", "--upgrade"))
        print("  Debian/Ubuntu 若禁用 ensurepip：sudo apt-get install python3-pip python3-venv")
    for label, modules, requirements in (
        ("实时数据库", ("Crypto", "zstandard"), "requirements-live.txt"),
        ("附件正文", ("pypdf", "docx", "openpyxl"), "requirements-files.txt"),
        ("开发测试", ("pytest", "PIL"), "requirements-dev.txt"),
    ):
        missing = [module for module in modules if importlib.util.find_spec(module) is None]
        if missing:
            print("可选 %s 依赖未安装：%s" % (label, ", ".join(missing)))
            print("  手动安装（可能联网）：%s" % command(sys.executable, "-m", "pip", "install", "-r", ROOT / requirements))
    cli = ROOT / "bin" / "wechat-wecom-monitor"
    doctor = [sys.executable, str(cli), "doctor", "--config", str(destination)]
    print("运行只读环境诊断：%s" % command(*doctor), flush=True)
    result = subprocess.run(doctor, cwd=ROOT, check=False)
    print("\n下一步（下面的演示与 dry-run 不发送通知）：")
    print("  %s" % command(sys.executable, cli, "demo", "--output", Path.home() / ".local" / "share" / "wechat-wecom-monitor" / "demo"))
    export_root = config.get("export_root") or Path.home() / ".local" / "share" / "wechat-wecom-monitor" / "demo" / "wechat"
    print("  %s" % command(sys.executable, cli, "check", "--export-root", export_root, "--config", destination, "--dry-run"))
    print("阅读 README.md 与 docs/；确认分类和隐私边界后再显式启用真实发送。")
    if result.returncode:
        print("doctor 未通过，请按诊断修复后重跑上面的命令；配置已保留。", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print("安装失败：%s" % exc, file=sys.stderr)
        raise SystemExit(1)
