"""Environment, dependency, cryptography, and configuration self-check (``doctor``)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile


def run_doctor(config_path: Path | None = None, *, mode: str = "auto") -> tuple[dict, int]:
    """Run diagnostic checks and return ``(report_dict, exit_code)``.

    Every failing check includes a Chinese human-readable description in ``detail``
    and an actionable copy-pasteable command in ``remediation`` (no raw stack traces).
    """
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str, fix: str = "", required: bool = True) -> None:
        item = {
            "name": name,
            "ok": bool(ok),
            "required": bool(required),
            "detail": detail,
            "remediation": "" if ok else fix,
        }
        checks.append(item)

    # 1. Python runtime version
    py_ok = sys.version_info >= (3, 9)
    record(
        "python_version",
        py_ok,
        f"Python {sys.version.split()[0]} ({sys.executable})" if py_ok else f"当前 Python 版本过低：{sys.version.split()[0]}（需要 >= 3.9，推荐 3.10+）",
        "sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip",
    )

    # 2. SQLite3 + WAL mode support
    try:
        with tempfile.TemporaryDirectory(prefix="wecom-doc-sql-") as tmp:
            db = sqlite3.connect(Path(tmp) / "t.db")
            try:
                wal = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                integ = db.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                db.close()
        sql_ok = str(wal).lower() == "wal" and integ == "ok"
        record(
            "sqlite3_wal",
            sql_ok,
            f"SQLite {sqlite3.sqlite_version} (journal_mode={wal})",
            "sudo apt-get update && sudo apt-get install -y python3 libsqlite3-0",
        )
    except Exception:
        record(
            "sqlite3_wal",
            False,
            "SQLite3 标准库缺失或无法在临时目录开启 WAL 日志模式",
            "sudo apt-get update && sudo apt-get install -y python3 libsqlite3-0 && chmod 1777 /tmp",
        )

    # 3. PyCryptodome (AES-128-CBC) & wxSQLite3 self-test vector
    try:
        import wxwork_crypto as crypto

        key = bytes(range(16))
        with tempfile.TemporaryDirectory(prefix="wecom-doc-enc-") as tmp:
            p = Path(tmp) / "plain.db"
            c = sqlite3.connect(p)
            c.execute("PRAGMA page_size=4096")
            c.execute("CREATE TABLE t(x TEXT)")
            c.execute("INSERT INTO t VALUES('ok')")
            c.commit()
            c.close()
            page1 = p.read_bytes()[:4096]
        enc_page1 = crypto.encrypt_wxsqlite3_aes128_page(key, page1, 1)
        verified = crypto.verify_wxsqlite3_aes128_key(key, enc_page1)
        dec_page1 = crypto.decrypt_wxsqlite3_aes128_page(key, enc_page1, 1)
        crypto_ok = verified and (dec_page1 == page1)
        record(
            "wxsqlite3_aes128_cipher",
            crypto_ok,
            "utelle/SQLite3MultipleCiphers wxSQLite3 AES-128-CBC 加解密与第 1 页校验自测通过",
            "./install.sh --non-interactive || sudo apt-get install -y python3-pycryptodome",
        )
    except Exception:
        record(
            "wxsqlite3_aes128_cipher",
            False,
            "缺少 pycryptodome (Crypto.Cipher.AES) 依赖或 wxSQLite3 AES-128-CBC 自检未通过",
            "./install.sh --non-interactive || sudo apt-get install -y python3-pycryptodome",
        )

    # 4. Pillow (image cache variant verification)
    try:
        from PIL import Image

        img = Image.new("RGB", (8, 8), (10, 20, 30))
        record(
            "pillow_image_codec",
            img.size == (8, 8),
            "Pillow 图像解码正常",
            "./install.sh --non-interactive || sudo apt-get install -y python3-pil",
        )
    except Exception:
        record(
            "pillow_image_codec",
            False,
            "缺少 Pillow (PIL.Image) 图像解码库",
            "./install.sh --non-interactive || sudo apt-get install -y python3-pil",
        )

    # 5. Configuration check (if supplied and exists)
    if config_path is not None and Path(config_path).is_file():
        try:
            cfg = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
            has_data_dir = bool(cfg.get("data_dir"))
            record(
                "config_file",
                has_data_dir,
                f"配置文件语法合法: {config_path}" if has_data_dir else f"配置文件缺少必填字段 data_dir: {config_path}",
                f"cp config.example.json {config_path} && chmod 600 {config_path}",
            )
        except Exception:
            record(
                "config_file",
                False,
                f"配置文件 JSON 格式无效，无法解析: {config_path}",
                f"cp config.example.json {config_path} && chmod 600 {config_path}",
            )
    else:
        record(
            "config_file",
            True,
            "未指定现有 config.json（离线模式；可运行 ./install.sh 自动生成配置）",
            "cp config.example.json collector/config.json && chmod 600 collector/config.json",
            required=False,
        )

    # 6. Host / Guest specific checks when explicitly requested
    effective_mode = mode
    if effective_mode == "auto":
        effective_mode = "guest" if os.name == "nt" else "offline"

    if effective_mode == "host":
        kvm_ok = Path("/dev/kvm").exists() and os.access("/dev/kvm", os.R_OK | os.W_OK)
        record(
            "host_kvm_device",
            kvm_ok,
            "/dev/kvm 存在且当前用户可读写" if kvm_ok else "/dev/kvm 不存在或当前用户无读写权限",
            "sudo modprobe kvm_intel || sudo modprobe kvm_amd; sudo usermod -aG kvm $(whoami)",
        )
        qemu_bin = shutil.which("qemu-system-x86_64")
        docker_bin = shutil.which("docker")
        runtime_ok = bool(qemu_bin or docker_bin)
        runtime_detail = f"qemu-system-x86_64: {qemu_bin}" if qemu_bin else (f"docker: {docker_bin}" if docker_bin else "未找到 qemu-system-x86_64 或 docker")
        record(
            "host_vm_runtime",
            runtime_ok,
            runtime_detail,
            "sudo apt-get update && sudo apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils seabios ipxe-qemu genisoimage novnc python3-websockify",
        )
    elif effective_mode == "guest" and os.name == "nt":
        from wxwork_keys import get_wxwork_pids

        pids = get_wxwork_pids()
        record(
            "guest_wxwork_process",
            len(pids) > 0,
            f"检测到 {len(pids)} 个运行中的 WXWork.exe 进程" if pids else "未检测到运行中的企业微信客户端 (WXWork.exe)",
            "Start-Process 'C:\\Program Files (x86)\\WXWork\\WXWork.exe'",
            required=False,
        )

    all_required_ok = all(item["ok"] for item in checks if item["required"])
    failed_fixes = [
        {"check": item["name"], "detail": item["detail"], "remediation": item["remediation"]}
        for item in checks
        if not item["ok"]
    ]
    report = {
        "healthy": all_required_ok,
        "mode": effective_mode,
        "platform": sys.platform,
        "checks": checks,
    }
    if failed_fixes:
        report["failed_checks"] = failed_fixes
    return report, (0 if all_required_ok else 1)
