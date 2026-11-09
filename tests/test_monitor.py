"""Synthetic-only contract, classification, persistence and operator acceptance tests."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor_engine as engine
import monitor_cli as cli


@pytest.fixture
def config():
    return engine.load_config(ROOT / "docs/monitor-config-TEMPLATE.json")


def message(**extra):
    return {"id": "m1", "ts": 10, "sender_id": "acct_member_example", "type": "text", "text": "普通闲聊", **extra}


@pytest.mark.parametrize("platform", ["wechat", "wecom"])
@pytest.mark.parametrize("cid,kind,extra,action", [
    ("group_family", "group", {"sender_id": "acct_parent_example"}, "CALL"),
    ("group_family", "group", {"text": "今晚回家"}, "CALL"),
    ("group_project", "group", {"text": "任务截止"}, "DIGEST"),
    ("group_interest", "group", {"text": "会议通知"}, "DIGEST"),
    ("group_interest", "group", {}, "IGNORE"),
    ("group_interest", "group", {"text": "紧急处理"}, "CALL"),
    ("group_interest", "group", {"text": "@示例用户 请看看"}, "CALL"),
    ("group_interest", "group", {"mentions": ["acct_main_example"]}, "CALL"),
    ("group_interest", "group", {"mentions": ["@all"]}, "CALL"),
    ("group_interest", "group", {"sender_id": "acct_contact_example"}, "CALL"),
    ("private_example", "private", {}, "DIGEST"),
    ("group_chatter", "group", {"text": "紧急"}, "IGNORE"),
    ("group_family", "group", {"text": "紧急", "is_self": True}, "IGNORE"),
    ("group_family", "group", {"text": "紧急", "sender_id": "acct_main_example"}, "IGNORE"),
    ("group_family", "group", {"type": "system"}, "IGNORE"),
])
def test_classification(platform, cid, kind, extra, action, config):
    decision = engine.classify_message(message(**extra), {"id": cid, "kind": kind, "platform": platform}, config)
    assert decision["action"] == action
    assert decision["reason"]
    assert decision["platform"] == platform


def export(tmp_path, messages=None, platform="wechat"):
    root = tmp_path / "export"
    cli._export_rows(root, platform, [("group_interest", "兴趣示例群", "group", messages or [message(text="会议通知")])])
    return root


def test_incremental_ties_late_updates_and_partial(tmp_path, config):
    root = export(tmp_path, [message(), message(id="m2")])
    state = tmp_path / "watermark.json"
    assert engine.check_export(root, config, state)["counts"]["IGNORE"] == 2
    assert not any(engine.check_export(root, config, state)["counts"].values())
    path = root / "conversations/group_interest.jsonl"
    with path.open("a") as stream:
        stream.write(json.dumps(message(id="m3", ts=1)) + "\n")
        stream.write(json.dumps(message(id="m1", text="紧急更新")) + "\n")
        stream.write('{"id":')
    report = engine.check_export(root, config, state)
    assert report["counts"] == {"CALL": 1, "DIGEST": 0, "IGNORE": 1}
    assert state.stat().st_mode & 0o777 == 0o600
    saved = json.loads(state.read_text())
    assert max(saved["last_ts"].values()) == 10


def test_members_resolve_authority(tmp_path, config):
    root = export(tmp_path, [message(sender_id="acct_unknown", text="闲聊")])
    config["groups"] = [{"id": "group_interest", "tier": "P0", "authorities": ["老师"]}]
    engine.atomic_json(root / "members/group_interest.json", {"members": [{"id": "acct_unknown", "name": "老师"}]})
    assert engine.check_export(root, config, tmp_path / "state.json")["counts"]["CALL"] == 1


def test_dry_run_does_not_consume(tmp_path, config):
    root = export(tmp_path)
    state = tmp_path / "state.json"
    result = engine.check_export(root, config, state, dry_run=True)
    assert result["counts"]["DIGEST"] == 1
    assert not state.exists()
    assert engine.check_export(root, config, state)["counts"]["DIGEST"] == 1


@pytest.mark.parametrize("status,code,retried", [("sent", 0, False), ("failed", 1, True), ("unknown", 2, False), ("invalid", 3, False)])
def test_sender_status_persistence(tmp_path, config, monkeypatch, status, code, retried):
    root = export(tmp_path)
    state = tmp_path / "state.json"
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        request_id = args[args.index("--request-id") + 1]
        # Before submission, the durable record must already be unknown.
        saved = json.loads(state.read_text())
        assert saved["deliveries"][request_id]["status"] == "unknown"
        return subprocess.CompletedProcess(args, code, json.dumps({"schema": "chat-send/v1", "status": status, "request_id": request_id}), "")
    monkeypatch.setattr(engine.subprocess, "run", run)
    first = engine.check_export(root, config, state, "fake-sender", "target")
    assert first["deliveries"][0]["status"] == status
    engine.check_export(root, config, state, "fake-sender", "target")
    assert len(calls) == (2 if retried else 1)
    if retried:
        assert calls[0] == calls[1]


@pytest.mark.parametrize("stdout,code", [("not JSON", 0), ('{"schema":"wrong"}', 0), ('{}', 2)])
def test_malformed_sender_fails_closed(monkeypatch, stdout, code):
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], code, stdout, ""))
    assert engine.send_chat("fake", "target", "text", "request")["status"] == "unknown"


def test_timeout_never_retried(tmp_path, config, monkeypatch):
    root = export(tmp_path)
    calls = []
    def timeout(*a, **k):
        calls.append(1)
        raise subprocess.TimeoutExpired("sender", 25)
    monkeypatch.setattr(engine.subprocess, "run", timeout)
    state = tmp_path / "state.json"
    engine.check_export(root, config, state, "fake", "target")
    engine.check_export(root, config, state, "fake", "target")
    assert len(calls) == 1


def test_bark_and_dryrun(tmp_path, config, monkeypatch):
    root = export(tmp_path)
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self): return b'{"code":200}'
    def request(req, **kw):
        calls.append(req)
        return Response()
    monkeypatch.setattr(engine.urllib.request, "urlopen", request)
    state = tmp_path / "state.json"
    assert engine.check_export(root, config, state, bark_url="https://push.example.invalid/key", dry_run=True)["deliveries"][0]["status"] == "dry_run"
    assert calls == []
    assert engine.check_export(root, config, state, bark_url="https://push.example.invalid/key")["deliveries"][0]["status"] == "sent"
    assert len(calls) == 1


def test_path_traversal_rejected(tmp_path):
    root = export(tmp_path)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["conversations"][0]["file"] = "../outside.jsonl"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError): engine.read_export(root)


def test_source_namespace_separates_platforms(tmp_path, config):
    state = tmp_path / "state.json"
    root = export(tmp_path)
    engine.check_export(root, config, state)
    root = export(tmp_path, platform="wecom")
    assert engine.check_export(root, config, state)["counts"]["DIGEST"] == 1


def test_configuration_validation(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"groups":[{"tier":"BAD"}]}')
    with pytest.raises(ValueError): engine.load_config(path)


def test_doctor_offline_and_permission_failure(tmp_path):
    result = cli.doctor()
    assert result["ok"]
    assert all(item.get("fix") for item in result["checks"])
    path = tmp_path / "config.json"
    path.write_text("{}")
    path.chmod(0o644)
    assert not cli.doctor(path)["ok"]
    path.chmod(0o600)
    assert cli.doctor(path)["ok"]


def test_demo_end_to_end_under_five_seconds(tmp_path):
    result = cli.demo(tmp_path / "demo")
    assert result["elapsed_seconds"] < 5
    assert result["network_used"] is False
    assert {r["source"]["platform"] for r in result["reports"]} == {"wechat", "wecom"}
    for report in result["reports"]:
        assert report["counts"] == {"CALL": 1, "DIGEST": 2, "IGNORE": 1}
        assert len(report["deliveries"]) == 3
        assert all(d["status"] == "sent" and d["extra"]["synthetic"] for d in report["deliveries"])


def test_cli_check_json(tmp_path):
    root = export(tmp_path)
    result = subprocess.run([str(ROOT / "bin/wechat-wecom-monitor"), "check", "--export-root", str(root), "--state-file", str(tmp_path / "state.json"), "--dry-run"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema"] == "monitor-report/v1"


def test_install_config_unattended(tmp_path):
    config = tmp_path / "installed.json"
    env_file = tmp_path / "install.env"
    env_file.write_text(f'MONITOR_CONFIG={config}\nSTATE_FILE={tmp_path / "state.json"}\n')
    environment = {k: v for k, v in os.environ.items() if k not in ("MONITOR_CONFIG", "STATE_FILE", "EXPORT_ROOT", "SEND_CMD", "SEND_TO")}
    environment.update({"PYTHON": sys.executable, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    proc = subprocess.run(["sh", str(ROOT / "install.sh"), "--non-interactive", "--config", str(env_file)], cwd=ROOT, env=environment, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert config.is_file()
    assert config.stat().st_mode & 0o777 == 0o600
    assert cli.doctor(config)["ok"]


def test_local_classifier_entrypoint(tmp_path):
    spec = importlib.util.spec_from_file_location("local_collector_test", ROOT / "wechat/wechat_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.collect_local)
    assert "--classify" in subprocess.run([sys.executable, str(ROOT / "wechat/wechat_check.py"), "--help"], capture_output=True, text=True).stdout


def test_local_collection_runs_shared_classification(config, monkeypatch):
    spec = importlib.util.spec_from_file_location("synthetic_local_collector", ROOT / "wechat/wechat_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class SyntheticReader:
        def __init__(self, *args): pass
        def open(self): return True, "synthetic"
        def chatroom_names(self): return ["group_family"]
        def private_contacts(self): return [("acct_friend_example", "table")]
        def read_group(self, cid, since, is_private=False):
            if not is_private and cid != "group_family":
                return None, "no table"
            return [{"server_id": "synthetic-1", "local_id": 1, "create_time": 10,
                     "sender_wxid": "acct_friend_example", "type": 1,
                     "text": "周末好" if is_private else "今晚回家", "file": None}], None
    monkeypatch.setattr(module, "AccountReader", SyntheticReader)
    config["local_db"] = {"accounts": [{"base_dir": "synthetic", "key_file": "synthetic", "label": "synthetic", "self_id": "acct_main_example"}]}
    report = module.collect_local(config, classify=True, seen_file=None)
    assert not report["errors"]
    assert report["groups"]["group_family"]["messages"][0]["classification"]["action"] == "CALL"
    assert report["private"]["acct_friend_example"]["messages"][0]["classification"]["action"] == "DIGEST"
    assert "seen_file" not in report
