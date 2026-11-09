"""Offline synthetic encrypted WeCom fixture generator and end-to-end demo runner.

Generates a complete synthetic WeCom account directory (encrypted with wxSQLite3
AES-128-CBC using a public test key embedded in a simulated process memory dump),
runs key extraction, WAL snapshot recovery, message decoding, archive ingestion,
and ``chat-export/v1`` export without any real account or network access.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import time

from PIL import Image

from archive_store import ArchiveStore
from chat_export import export_chat_v1
from media import MediaIndex
from messages import FileIndex, load_records
import wxwork_crypto as crypto
from wxwork_keys import extract_candidate_keys_from_buffer, scan_keys
from wxwork_snapshot import recover_snapshot

# Public synthetic test key (never used for real accounts)
DEMO_RAW_KEY = bytes.fromhex("3a7f1c9e4b2d80561290abef3456789c")
DEMO_ACCOUNT_ID = "1000000000000001"
PAGE_SIZE = crypto.PAGE_SZ
FRAME_SIZE = 24 + PAGE_SIZE


def _vint(value: int) -> bytes:
    encoded = bytearray()
    while value > 127:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _pb_bytes(field_no: int, value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _vint(field_no * 8 + 2) + _vint(len(value)) + value


def _pb_int(field_no: int, value: int) -> bytes:
    return _vint(field_no * 8) + _vint(value)


def _text_payload(value: str) -> bytes:
    return _pb_bytes(1, _pb_int(1, 0) + _pb_bytes(2, _pb_bytes(1, value)))


def _wal_checksum(data: bytes, state: tuple[int, int], byteorder: str) -> tuple[int, int]:
    s0, s1 = state
    words = struct.unpack(byteorder + str(len(data) // 4) + "I", data)
    for a, b in zip(words[::2], words[1::2]):
        s0 = (s0 + a + s1) & 0xFFFFFFFF
        s1 = (s1 + b + s0) & 0xFFFFFFFF
    return s0, s1


def _encrypt_sqlite_file(db_path: Path, key: bytes) -> None:
    raw = db_path.read_bytes()
    encrypted = b"".join(
        crypto.encrypt_wxsqlite3_aes128_page(key, raw[offset : offset + PAGE_SIZE], offset // PAGE_SIZE + 1)
        for offset in range(0, len(raw), PAGE_SIZE)
    )
    db_path.write_bytes(encrypted)

    wal_path = Path(str(db_path) + "-wal")
    if wal_path.is_file() and wal_path.stat().st_size >= 32:
        raw_wal = wal_path.read_bytes()
        header = raw_wal[:32]
        magic = struct.unpack_from(">I", header, 0)[0]
        byteorder = "<" if magic == 0x377F0682 else ">"
        state = struct.unpack_from(">II", header, 24)
        out_wal = bytearray(header)
        complete_end = 32 + ((len(raw_wal) - 32) // FRAME_SIZE) * FRAME_SIZE
        for offset in range(32, complete_end, FRAME_SIZE):
            frame_header = raw_wal[offset : offset + 24]
            page_no = struct.unpack_from(">I", frame_header, 0)[0]
            enc_page = crypto.encrypt_wxsqlite3_aes128_page(
                key, raw_wal[offset + 24 : offset + FRAME_SIZE], page_no
            )
            state = _wal_checksum(frame_header[:8] + enc_page, state, byteorder)
            out_wal.extend(frame_header[:16])
            out_wal.extend(struct.pack(">II", *state))
            out_wal.extend(enc_page)
        out_wal.extend(raw_wal[complete_end:])
        wal_path.write_bytes(bytes(out_wal))


def build_synthetic_account(account_root: Path, key: bytes = DEMO_RAW_KEY) -> dict:
    """Create a synthetic encrypted WeCom account directory under ``account_root``."""
    data_dir = account_root / "Data"
    cache_mapping_dir = account_root / "CacheMapping"
    cache_img_dir = account_root / "Cache" / "Image"
    cache_file_dir = account_root / "Cache" / "File"
    for directory in (data_dir, cache_mapping_dir, cache_img_dir, cache_file_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # Synthetic PNG image attachment
    img = Image.new("RGB", (64, 48), (36, 112, 188))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    img_md5 = hashlib.md5(img_bytes).hexdigest()
    (cache_img_dir / "demo_chart.png").write_bytes(img_bytes)

    # Synthetic PDF/text file attachment
    doc_bytes = b"Synthetic WeCom project specification v1.0\n"
    doc_md5 = hashlib.md5(doc_bytes).hexdigest()
    (cache_file_dir / "spec-v1.txt").write_bytes(doc_bytes)

    # 1. user.db
    user_db = data_dir / "user.db"
    conn = sqlite3.connect(user_db)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE user_table(id TEXT PRIMARY KEY, name TEXT, real_name TEXT)")
    conn.executemany(
        "INSERT INTO user_table VALUES(?, ?, ?)",
        [
            (DEMO_ACCOUNT_ID, "林晓", "林晓"),
            ("1000000000000002", "陈默", "陈默"),
            ("1000000000000003", "苏晴", "苏晴"),
        ],
    )
    conn.commit()
    conn.close()
    _encrypt_sqlite_file(user_db, key)

    # 2. session.db
    session_db = data_dir / "session.db"
    conn = sqlite3.connect(session_db)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE conversation_table(id TEXT PRIMARY KEY, name TEXT, roomname_remark TEXT)")
    conn.executemany(
        "INSERT INTO conversation_table VALUES(?, ?, ?)",
        [
            ("R:900001", "架构研发组", ""),
            (f"S:1000000000000002_{DEMO_ACCOUNT_ID}", "", ""),
            ("FILEASSIST", "File Transfer", ""),
        ],
    )
    conn.commit()
    conn.close()
    _encrypt_sqlite_file(session_db, key)

    # 3. file.db
    file_db = data_dir / "file.db"
    conn = sqlite3.connect(file_db)
    conn.execute("PRAGMA page_size=4096")
    conn.execute(
        "CREATE TABLE file_table4(message_id INTEGER, file_index INTEGER, name TEXT, size INTEGER, md5 TEXT, server_id TEXT)"
    )
    conn.execute(
        "INSERT INTO file_table4 VALUES(4, 0, 'spec-v1.txt', ?, ?, 'srv-file-4')",
        (len(doc_bytes), doc_md5),
    )
    conn.commit()
    conn.close()
    _encrypt_sqlite_file(file_db, key)

    # 4. message_lookup.db (unencrypted plain SQLite to exercise mixed plain+encrypted handling)
    lookup_db = data_dir / "message_lookup.db"
    conn = sqlite3.connect(lookup_db)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE lookup_meta(k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO lookup_meta VALUES('schema', '1')")
    conn.commit()
    conn.close()

    # 5. CacheMapping/cache_map.db
    cache_db = cache_mapping_dir / "cache_map.db"
    conn = sqlite3.connect(cache_db)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE mapping(type INTEGER, key TEXT, file_name TEXT, file_md5 TEXT)")
    conn.executemany(
        "INSERT INTO mapping VALUES(?, ?, ?, ?)",
        [
            (1, "demo-img-1", "Image/demo_chart.png", img_md5),
            (2, "demo-file-1", "File/spec-v1.txt", doc_md5),
        ],
    )
    conn.commit()
    conn.close()
    _encrypt_sqlite_file(cache_db, key)

    # 6. message.db + active WAL transaction
    msg_db = data_dir / "message.db"
    conn = sqlite3.connect(msg_db)
    conn.execute("PRAGMA page_size=4096")
    conn.execute(
        """CREATE TABLE message_table(
            message_id INTEGER PRIMARY KEY, server_id INTEGER, sequence INTEGER,
            sender_id INTEGER, conversation_id TEXT, content_type INTEGER,
            send_time INTEGER, flag INTEGER, content BLOB, devinfo INTEGER,
            from_app_id TEXT, msg_from_devinfo INTEGER, extra_content BLOB,
            local_extra_content BLOB, client_id TEXT
        )"""
    )
    # Row 1: group text with mention and URL
    mention = _pb_bytes(1, _pb_int(1, 5) + _pb_bytes(2, _pb_int(1, 0) + _pb_bytes(2, "所有人")))
    text_seg = _text_payload(" 请查看构建说明 https://example.org/docs/build?v=1，谢谢！")
    conn.execute(
        "INSERT INTO message_table VALUES(1, 50001, 1, 1000000000000002, 'R:900001', 2, 1794000100, 0, ?, 0, '', 0, ?, ?, 'c1')",
        (mention + text_seg, b"", b""),
    )
    # Row 2: direct chat text
    conn.execute(
        "INSERT INTO message_table VALUES(2, 50002, 1, 1000000000000002, ?, 2, 1794000200, 0, ?, 0, '', 0, ?, ?, 'c2')",
        (f"S:1000000000000002_{DEMO_ACCOUNT_ID}", _text_payload("下午三点同步接口方案"), b"", b""),
    )
    # Row 3: image message
    img_payload = (
        _pb_bytes(1, "demo-img-1")
        + _pb_bytes(2, "demo_chart.png")
        + _pb_int(4, len(img_bytes))
        + _pb_int(5, 64)
        + _pb_int(6, 48)
        + _pb_bytes(10, img_md5)
    )
    conn.execute(
        "INSERT INTO message_table VALUES(3, 50003, 2, 1000000000000003, 'R:900001', 14, 1794000300, 0, ?, 0, '', 0, ?, ?, 'c3')",
        (img_payload, b"", b""),
    )
    conn.commit()
    conn.close()

    # Open in WAL mode and append Row 4 (file card) and Row 5 (merged forward chat) into WAL
    wal_conn = sqlite3.connect(msg_db)
    wal_conn.execute("PRAGMA journal_mode=WAL")
    wal_conn.execute("PRAGMA wal_autocheckpoint=0")
    wal_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    wal_conn.execute(
        "INSERT INTO message_table VALUES(4, 50004, 3, 1000000000000002, 'R:900001', 36, 1794000400, 0, ?, 0, '', 0, ?, ?, 'c4')",
        (b"", b"", b""),
    )
    merged_entry_1 = (
        _pb_int(2, 1794000350)
        + _pb_int(5, 2)
        + _pb_bytes(11, "陈默")
        + _pb_bytes(101, _text_payload("第一条转发记录"))
    )
    merged_entry_2 = (
        _pb_int(2, 1794000360)
        + _pb_int(5, 2)
        + _pb_bytes(11, "苏晴")
        + _pb_bytes(101, _text_payload("第二条转发记录"))
    )
    merged_payload = _pb_bytes(1, merged_entry_1) + _pb_bytes(1, merged_entry_2) + _pb_bytes(2, "架构研发组") + _pb_int(5, 2)
    wal_conn.execute(
        "INSERT INTO message_table VALUES(5, 50005, 1, ?, 'FILEASSIST', 4, 1794000500, 0, ?, 0, '', 0, ?, ?, 'c5')",
        ( int(DEMO_ACCOUNT_ID), merged_payload, b"", b""),
    )
    wal_conn.commit()
    _encrypt_sqlite_file(msg_db, key)
    wal_conn.close()

    # Simulated process memory buffer containing the wxSQLite3 Codec struct
    memory_blob = (
        b"\x00" * 64
        + b"\x01\x00\x00\x00\x01\x00\x00\x00"
        + key
        + b"\x01\x00\x00\x00"
        + key
        + b"\x00" * 64
    )
    return {
        "data_dir": data_dir,
        "memory_blob": memory_blob,
    }


def run_demo(output_dir: Path) -> dict:
    """Run an offline end-to-end collection & ``chat-export/v1`` export into ``output_dir``."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    with tempfile.TemporaryDirectory(prefix="wecom-demo-src-") as temp_src:
        account_root = Path(temp_src) / DEMO_ACCOUNT_ID
        fixture = build_synthetic_account(account_root, DEMO_RAW_KEY)
        data_dir = fixture["data_dir"]

        # 1. Extract candidate key from simulated process memory buffer
        primary, secondary = extract_candidate_keys_from_buffer(fixture["memory_blob"])
        candidates = primary + secondary

        # 2. Verify keys against page 1 of each synthetic database
        state_dir = output_dir / "state"
        keys_file = state_dir / "database-keys.private.json"
        key_summary = scan_keys(data_dir, keys_file, extra_candidates=candidates)

        # 3. Recover consistent read-only SQLite snapshots (including encrypted WAL frames)
        keys_data = json.loads(keys_file.read_text(encoding="utf-8"))
        snapshots_dir = state_dir / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        recovery = {}
        for name in ("message.db", "file.db", "message_lookup.db", "session.db", "user.db"):
            db_path = data_dir / name
            first = db_path.read_bytes()[:PAGE_SIZE]
            key = bytes(16) if crypto.is_plain_sqlite_page(first) else bytes.fromhex(keys_data[name]["enc_key"])
            recovery[name] = recover_snapshot(db_path, snapshots_dir / name, key)

        for path in (account_root / "CacheMapping").glob("*.db"):
            first = path.read_bytes()[:PAGE_SIZE]
            key = bytes(16) if crypto.is_plain_sqlite_page(first) else bytes.fromhex(keys_data[path.name]["enc_key"])
            recovery["cache-" + path.name] = recover_snapshot(path, snapshots_dir / ("cache-" + path.name), key)

        # 4. Decode messages, resolve media in Cache, ingest into ArchiveStore, and export chat-export/v1
        file_index = FileIndex(snapshots_dir / "file.db")
        media_dir = output_dir / "archive" / "media"
        media_index = MediaIndex(snapshots_dir, account_root, media_dir)
        records, errors, _ = load_records(
            snapshots_dir, f"wecom-windows:{DEMO_ACCOUNT_ID}", media_index, file_index
        )
        archive_file = output_dir / "archive" / "messages.sqlite3"
        with ArchiveStore(archive_file) as archive:
            ingest_outcome = archive.ingest(records, observed_at=time.time(), baseline=False)
            stored_records = archive.records()

        shutil.rmtree(snapshots_dir, ignore_errors=True)

        chat_export_dir = output_dir / "chat-export"
        export_summary = export_chat_v1(
            stored_records,
            chat_export_dir,
            account_id=DEMO_ACCOUNT_ID,
        )

    return {
        "ok": len(errors) == 0 and export_summary["messages"] == 5,
        "mode": "offline-demo",
        "duration_seconds": round(time.time() - started, 3),
        "output_dir": str(output_dir),
        "chat_export_dir": str(output_dir / "chat-export"),
        "keys_verified": key_summary["verified_databases"],
        "plain_databases": key_summary["plain_databases"],
        "messages_decoded": len(stored_records),
        "decode_errors": len(errors),
        "ingest": ingest_outcome,
        "chat_export": export_summary,
    }
