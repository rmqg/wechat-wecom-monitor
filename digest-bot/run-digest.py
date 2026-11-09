#!/usr/bin/env python3
"""Generate the selected group's real multimodal digest using pinned local tools."""
import argparse
import re
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from image_pipeline import process_window
from model_fallback import providers as model_providers

ROOT = Path(__file__).resolve().parents[1]
STALE_TEMP_PREFIXES = ('.digest-', '.images-', '.avatar-decrypted-')
STALE_TEMP_SECONDS = 6 * 3600


def _sweep_stale_temp(work):
    """Remove stale temporary databases left behind by interrupted containers."""
    now = time.time()
    for entry in work.iterdir():
        try:
            if (entry.name.startswith(STALE_TEMP_PREFIXES) and entry.is_dir() and not entry.is_symlink()
                    and now - entry.stat().st_mtime > STALE_TEMP_SECONDS):
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


def _trigger_report_publish():
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "deployment" / "publish_reports.py")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        # The independent minute cron can retry publication without rerunning generation.
        pass



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--request-id")
    parser.add_argument("--list-groups", action="store_true")
    args = parser.parse_args()
    on_demand = args.request_id is not None
    if on_demand:
        if not re.fullmatch(r"[0-9a-f]{64}", args.request_id):
            raise SystemExit("request-id must be a lowercase SHA-256 identifier")
        if args.start is None or args.end is None or args.start >= args.end:
            raise SystemExit("request-id requires explicit start and end with start < end")
    elif args.start is not None:
        raise SystemExit('explicit start is supported only with request-id')
    if not on_demand and args.hours <= 0:
        raise SystemExit("hours must be positive")
    versions = json.loads((ROOT / "deployment/versions.json").read_text())
    image = versions["runtime_image"]
    if not image.startswith("sha256:"):
        raise SystemExit("Only a pinned local image ID is allowed")
    selection = json.loads((ROOT / "runtime/selection.json").read_text())
    end = args.end if args.end is not None else int(time.time())
    common = [
        "docker", "run", "--rm", "--pull", "never", "--read-only", "--user", "1200:1201",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "1g", "--cpus", "1",
        "--tmpfs", "/tmp:rw,nosuid,size=64m", "--env", "PYTHONPATH=/deps:/token-deps:/source/memory:/source/agent_console:/tool",
        "--env", "TZ=Asia/Shanghai", "--env", "TIKTOKEN_CACHE_DIR=/token-cache",
    ]
    mounts = [
        (ROOT / "source", "/source", True), (ROOT / "deployment", "/tool", True),
        (ROOT / "runtime/gui", "/config", True),
        (ROOT / "runtime/keyscan/all_keys.json", "/keys/all_keys.json", True),
        (ROOT / "runtime/python-deps", "/deps", True),
        (ROOT / "runtime/report-fonts", "/app/runtime/report-fonts", True),
        (ROOT / "runtime/digest", "/work", False), (ROOT / "runtime/reports", "/reports", False),
        (ROOT / "runtime/token-runtime-v1/linux-deps", "/token-deps", True),
        (ROOT / "runtime/token-runtime-v1/cache", "/token-cache", True),
    ]
    for source, target, readonly in mounts:
        common += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
    window = ["--source-db-dir", selection["source_db_dir"], "--runtime-dir", "/work"]
    start = args.start if on_demand else end - int(args.hours * 3600)
    if on_demand:
        window += ["--start", str(args.start), "--end", str(end)]
    else:
        window += ["--hours", str(args.hours), "--end", str(end)]
    if args.list_groups:
        return subprocess.run(common + ["--network", "none", image, "/tool/digest.py",
                              "--source-db-dir", selection["source_db_dir"], "--runtime-dir", "/work", "--list-groups"], timeout=120).returncode
    stage = process_window(start, end, scope="on-demand" if on_demand else "daily")
    if stage["status"] != "complete":
        print(json.dumps({**stage, "status": "images_pending"}, ensure_ascii=False), flush=True)
        return 2
    work = ROOT / "runtime/digest"
    _sweep_stale_temp(work)
    understanding = str(Path("/work") / Path(stage["understanding_path"]).relative_to(work))
    excluded = str(Path("/work") / Path(stage["exclude_path"]).relative_to(work))
    auxiliary = str(Path('/work') / Path(stage['auxiliary_path']).relative_to(work))
    notice_hashes = str(Path('/work') / Path(stage['notice_hashes_path']).relative_to(work))
    window += ["--exclude-server-ids", excluded, "--exclude-own-text-sha256", notice_hashes]
    if on_demand:
        window += ["--request-id", args.request_id]
    models = ["--mount", f"type=bind,src={ROOT / 'runtime/gemini-model.json'},dst=/models/gemini.json,readonly"]
    container = "wechat-digest-run-" + uuid.uuid4().hex[:12]
    command = common + ["--name", container, "--network", "wechat-digest_default"] + models + [
        image, "/tool/digest.py", *window, "--output-dir", "/reports",
        "--group-name", "示例群", "--group-id", "12345678901@chatroom",
        "--model-config", "/models/gemini.json", "--image-understanding", understanding,
        '--auxiliary-context', auxiliary,
    ]
    model_config = json.loads((ROOT / 'runtime/gemini-model.json').read_text(encoding='utf-8'))
    try:
        result = subprocess.run(command, timeout=900 * len(model_providers(model_config)))
    except subprocess.TimeoutExpired:
        # The timeout only kills the docker client; also stop the container.
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        print(json.dumps({"status": "failed", "stage": "digest-container-timeout"}), file=sys.stderr, flush=True)
        return 1
    if result.returncode == 0:
        _trigger_report_publish()
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
