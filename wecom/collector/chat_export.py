"""Export normalized WeCom archive records to the shared ``chat-export/v1`` format."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHANGHAI_TZ = timezone(timedelta(hours=8))

_MIME_BY_FORMAT = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bitmap": "image/bmp",
}

_EXT_BY_FORMAT = {
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "bitmap": ".bmp",
}


def stable_conversation_id(raw_id: str) -> str:
    """Map a platform conversation ID to a deterministic ``[A-Za-z0-9_.-]+`` ID."""
    raw = str(raw_id or "unknown")
    # Common WeCom prefixes: R:<room>, S:<peer>_<self>, O:<official>, M:<app>
    candidate = raw.replace(":", "_")
    if _SAFE_ID_RE.fullmatch(candidate):
        return candidate
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate).strip("_") or "conv"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned}_{digest}"


def _atomic_write_text(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _contract_conversation_kind(record: dict) -> str:
    conv_type = str(record.get("conversation_type") or "")
    if conv_type == "group" or record.get("is_group"):
        return "group"
    if conv_type in ("direct", "self"):
        return "private"
    if conv_type == "official":
        return "channel"
    return "other"


def _contract_message_type(record: dict) -> str:
    kind = str(record.get("kind") or "other")
    if kind in ("text", "image", "file", "link", "voice", "video", "sticker", "quote", "forward", "system", "revoke", "other"):
        return kind
    if kind == "merged":
        return "forward"
    if kind == "mixed":
        return "text"
    if kind == "app":
        if record.get("links"):
            return "link"
        return "text" if record.get("text") else "other"
    if kind == "call":
        return "text" if record.get("text") else "other"
    return "other"


def _extract_mentions(record: dict) -> list[str]:
    text = str(record.get("text") or "")
    mentions: list[str] = []
    if "@所有人" in text or "@All" in text or "@all" in text:
        mentions.append("@all")
    return mentions


def _export_media_item(attachment: dict, export_root: Path) -> dict:
    raw_kind = str(attachment.get("kind") or "file")
    kind = raw_kind if raw_kind in ("image", "file", "voice", "video", "sticker") else "file"
    raw_status = str(attachment.get("status") or "pending_download")
    is_original = bool(attachment.get("original", False))
    variant = str(attachment.get("variant") or "")
    is_thumbnail = (raw_status == "ready" and not is_original and variant == "client_cache")

    if raw_status == "ready":
        status = "ready"
    elif raw_status == "missing":
        status = "missing"
    else:
        status = "pending"

    sha256 = str(attachment.get("sha256") or "")
    name = str(attachment.get("name") or "")
    size = int(attachment.get("size") or 0)
    image_fmt = str(attachment.get("image_format") or "").lower()
    src_path = Path(str(attachment["path"])) if attachment.get("path") else None

    ext = ""
    if src_path and src_path.suffix:
        ext = src_path.suffix.lower()
    elif image_fmt in _EXT_BY_FORMAT:
        ext = _EXT_BY_FORMAT[image_fmt]
    elif name and "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
            ext = ".bin"
    else:
        ext = ".bin"

    mime = _MIME_BY_FORMAT.get(image_fmt) or mimetypes.guess_type(name or f"file{ext}")[0] or "application/octet-stream"

    rel_path = ""
    if status == "ready" and sha256 and len(sha256) == 64:
        rel_path = f"media/{sha256[:2]}/{sha256}{ext}"
        dest = export_root / rel_path
        if src_path and src_path.is_file() and not dest.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest)
            size = dest.stat().st_size

    source_quality = "original" if is_original else "client_full"
    item: dict = {
        "kind": kind,
        "path": rel_path,
        "sha256": sha256,
        "mime": mime,
        "bytes": size,
        "name": name,
        "status": status,
        "is_thumbnail": is_thumbnail,
        "source_quality": source_quality,
    }
    return item


def _convert_record_to_contract(record: dict, export_root: Path, account_id: str) -> dict:
    msg_id = str(record.get("server_id") or record.get("message_id") or record.get("id") or "")
    ts = int(record.get("sent_at") or 0)
    msg_type = _contract_message_type(record)
    raw_sender = str(record.get("sender_id") or "")
    sender_id = "system" if msg_type == "system" or not raw_sender else raw_sender
    sender_name = str(record.get("sender_name") or "")
    is_self = bool(raw_sender and account_id and raw_sender == account_id)

    entry: dict = {
        "id": msg_id,
        "ts": ts,
        "sender_id": sender_id,
        "type": msg_type,
    }
    if sender_name:
        entry["sender_name"] = sender_name
    if account_id:
        entry["is_self"] = is_self

    text = str(record.get("text") or "")
    if text and not (record.get("unsupported") and not text.strip()):
        entry["text"] = text

    if record.get("forward"):
        entry["forward"] = record["forward"]
    elif record.get("kind") == "merged" and text:
        forward_items = []
        for line in text.splitlines()[1:]:
            if ": " in line:
                f_sender, f_body = line.split(": ", 1)
            else:
                f_sender, f_body = "", line
            if f_body.startswith("[图片]"):
                f_type = "image"
            elif f_body.startswith("[文件]"):
                f_type = "file"
            else:
                f_type = "text"
            forward_items.append({"sender_name": f_sender, "ts": ts, "type": f_type, "text": f_body})
        if forward_items:
            entry["forward"] = forward_items

    links = record.get("links") or []
    if links and isinstance(links[0], dict) and links[0].get("url"):
        first_link = links[0]
        entry["link"] = {
            "url": str(first_link.get("url") or ""),
            "title": str(first_link.get("title") or ""),
            "description": str(first_link.get("description") or ""),
        }

    attachments = record.get("attachments") or []
    if attachments:
        entry["media"] = [_export_media_item(att, export_root) for att in attachments]

    mentions = _extract_mentions(record)
    if mentions:
        entry["mentions"] = mentions

    if record.get("content_type") is not None:
        entry["raw_type"] = str(record["content_type"])

    extra: dict = {}
    for key in (
        "content_type_name",
        "unsupported",
        "conversation_type",
        "peer_id",
        "peer_name",
        "member_id",
        "member_name",
        "member_ids",
        "member_names",
    ):
        if key in record and record[key] not in (None, "", [], False):
            extra[key] = record[key]
    if len(links) > 1:
        extra["links"] = links
    if extra:
        entry["extra"] = extra

    return entry


def export_chat_v1(
    records: list[dict],
    export_root: Path,
    *,
    account_id: str = "",
    producer_name: str = "wecom-export",
    producer_version: str = "0.1.0",
    timezone_name: str = "Asia/Shanghai",
) -> dict:
    """Write or incrementally update a ``chat-export/v1`` directory from normalized records."""
    export_root = Path(export_root)
    export_root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        export_root.chmod(0o700)

    conversations_dir = export_root / "conversations"
    members_dir = export_root / "members"
    state_dir = export_root / "state"
    for directory in (conversations_dir, members_dir, state_dir):
        directory.mkdir(parents=True, exist_ok=True)

    state_file = state_dir / "chat-export-v1-state.json"
    prior_state: dict = {}
    if state_file.is_file():
        try:
            prior_state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior_state = {}
    emitted_digests: dict[str, str] = dict(prior_state.get("emitted", {}))

    by_conv: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        raw_conv = str(record.get("conversation_id") or "unknown")
        by_conv[raw_conv].append(record)

    manifest_conversations: list[dict] = []
    appended_lines = 0

    for raw_conv in sorted(by_conv.keys()):
        conv_records = sorted(
            by_conv[raw_conv],
            key=lambda item: (int(item.get("sent_at") or 0), int(item.get("message_id") or 0), str(item.get("id") or "")),
        )
        conv_id = stable_conversation_id(raw_conv)
        conv_file_rel = f"conversations/{conv_id}.jsonl"
        conv_file = export_root / conv_file_rel
        members_file_rel = f"members/{conv_id}.json"
        members_file = export_root / members_file_rel

        new_lines: list[str] = []
        members_by_id: dict[str, dict[str, str]] = {}
        last_ts = 0
        conv_name = ""
        conv_kind = "other"

        for record in conv_records:
            if not conv_name and record.get("conversation_name"):
                conv_name = str(record["conversation_name"])
            conv_kind = _contract_conversation_kind(record)
            ts = int(record.get("sent_at") or 0)
            if ts > last_ts:
                last_ts = ts

            sender_id = str(record.get("sender_id") or "")
            sender_name = str(record.get("sender_name") or "")
            if sender_id and sender_id != "system":
                members_by_id[sender_id] = {
                    "id": sender_id,
                    "name": sender_name or sender_id,
                    "display_name": sender_name or sender_id,
                }

            contract_item = _convert_record_to_contract(record, export_root, account_id)
            line_json = json.dumps(contract_item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            line_key = f"{conv_id}|{contract_item['id']}"
            line_hash = hashlib.sha256(line_json.encode("utf-8")).hexdigest()

            if not conv_file.is_file() or emitted_digests.get(line_key) != line_hash:
                new_lines.append(line_json + "\n")
                emitted_digests[line_key] = line_hash

        if new_lines:
            with conv_file.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("".join(new_lines))
                stream.flush()
                os.fsync(stream.fileno())
            appended_lines += len(new_lines)
        elif not conv_file.is_file():
            conv_file.write_text("", encoding="utf-8")

        members_payload = {
            "conversation_id": conv_id,
            "updated_at": last_ts,
            "members": [members_by_id[mid] for mid in sorted(members_by_id.keys())],
        }
        _atomic_write_text(members_file, json.dumps(members_payload, ensure_ascii=False, indent=2) + "\n")

        manifest_conversations.append(
            {
                "id": conv_id,
                "kind": conv_kind,
                "name": conv_name or conv_id,
                "file": conv_file_rel,
                "members_file": members_file_rel,
                "last_ts": last_ts,
                "message_count": len(conv_records),
            }
        )

    opaque_account = (
        hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16] if account_id else "anonymous"
    )
    manifest = {
        "schema": "chat-export/v1",
        "producer": {"name": producer_name, "version": producer_version},
        "source": {"platform": "wecom", "account_id": opaque_account},
        "generated_at": datetime.now(_SHANGHAI_TZ).isoformat(timespec="seconds"),
        "timezone": timezone_name,
        "conversations": manifest_conversations,
    }
    _atomic_write_text(export_root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _atomic_write_text(
        state_file,
        json.dumps({"emitted": emitted_digests, "updated_at": datetime.now(_SHANGHAI_TZ).isoformat()}, ensure_ascii=False)
        + "\n",
    )
    return {
        "schema": "chat-export/v1",
        "conversations": len(manifest_conversations),
        "messages": len(records),
        "appended_lines": appended_lines,
    }
