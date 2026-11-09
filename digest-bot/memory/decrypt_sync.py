#!/usr/bin/env python3
"""Refresh decrypted WeChat DB copies from the live read-only source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import struct
import sys
from pathlib import Path

from Crypto.Cipher import AES


PAGE_SZ = 4096
SALT_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b"SQLite format 3\x00"
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24


def decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
        return SQLITE_HDR + decrypted + b"\x00" * RESERVE_SZ
    encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
    decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
    return decrypted + b"\x00" * RESERVE_SZ


def full_decrypt(db_path: Path, out_path: Path, enc_key: bytes) -> int:
    file_size = db_path.stat().st_size
    total_pages = file_size // PAGE_SZ
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with db_path.open("rb") as fin, out_path.open("wb") as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if page:
                    page = page + b"\x00" * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(decrypt_page(enc_key, page, pgno))
    return total_pages


def patch_wal(wal_path: Path, out_path: Path, enc_key: bytes) -> int:
    if not wal_path.exists() or wal_path.stat().st_size <= WAL_HEADER_SZ:
        return 0
    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
    wal_size = wal_path.stat().st_size
    patched = 0
    with wal_path.open("rb") as wf, out_path.open("r+b") as df:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        if len(wal_hdr) < WAL_HEADER_SZ:
            return 0
        wal_salt1 = struct.unpack(">I", wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack(">I", wal_hdr[20:24])[0]
        while wf.tell() + frame_size <= wal_size:
            frame_header = wf.read(WAL_FRAME_HEADER_SZ)
            if len(frame_header) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack(">I", frame_header[0:4])[0]
            frame_salt1 = struct.unpack(">I", frame_header[8:12])[0]
            frame_salt2 = struct.unpack(">I", frame_header[12:16])[0]
            encrypted_page = wf.read(PAGE_SZ)
            if len(encrypted_page) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1_000_000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(decrypt_page(enc_key, encrypted_page, pgno))
            patched += 1
    return patched


def strip_key_metadata(keys: dict) -> dict:
    return {key: value for key, value in keys.items() if not key.startswith("_")}


def key_variants(rel_path: str) -> list[str]:
    normalized = rel_path.replace("\\", "/")
    variants = [rel_path, normalized, normalized.replace("/", "\\")]
    return list(dict.fromkeys(variants))


def load_keys(keys_file: Path) -> dict:
    with keys_file.open(encoding="utf-8") as f:
        return strip_key_metadata(json.load(f))


def key_for_rel(keys: dict, rel_path: str) -> dict | None:
    for candidate in key_variants(rel_path):
        if candidate in keys:
            return keys[candidate]
    return None


def should_skip_source_db(rel_path: str) -> bool:
    """Skip WeChat auxiliary indexes that are not required for message ingest."""
    name = Path(rel_path).name.lower()
    return name.endswith("_fts.db")


def file_state(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "mtime_ns": 0, "size": 0}
    stat = path.stat()
    return {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def source_signature(db_path: Path) -> str:
    payload = {
        "db": file_state(db_path),
        "wal": file_state(Path(str(db_path) + "-wal")),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    try:
        with state_file.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(state_file)


def verify_sqlite(path: Path) -> bool:
    try:
        conn = sqlite3.connect(path)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        conn.close()
        return True
    except sqlite3.Error:
        return False


def refresh_decrypted(
    source_db_dir: Path,
    decrypted_dir: Path,
    keys_file: Path,
    state_file: Path,
    force: bool = False,
) -> dict:
    keys = load_keys(keys_file)
    state = load_state(state_file)
    result = {"updated": [], "skipped": [], "missing_key": [], "failed": []}

    for db_path in sorted(source_db_dir.rglob("*.db")):
        rel_path = db_path.relative_to(source_db_dir).as_posix()
        if should_skip_source_db(rel_path):
            result["skipped"].append(rel_path)
            state.pop(rel_path, None)
            out_path = decrypted_dir / rel_path
            for candidate in (out_path, Path(str(out_path) + "-wal"), Path(str(out_path) + "-shm")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
            continue
        key_info = key_for_rel(keys, rel_path)
        if not key_info:
            result["missing_key"].append(rel_path)
            continue
        signature = source_signature(db_path)
        out_path = decrypted_dir / rel_path
        if not force and state.get(rel_path, {}).get("signature") == signature and out_path.exists():
            result["skipped"].append(rel_path)
            continue

        try:
            enc_key = bytes.fromhex(key_info["enc_key"])
            full_decrypt(db_path, out_path, enc_key)
            patched = patch_wal(Path(str(db_path) + "-wal"), out_path, enc_key)
            if not verify_sqlite(out_path):
                raise RuntimeError("sqlite verification failed")
            for suffix in ("-wal", "-shm"):
                residual = Path(str(out_path) + suffix)
                if residual.exists():
                    residual.unlink()
            state[rel_path] = {"signature": signature, "patched_wal_pages": patched}
            result["updated"].append({"db": rel_path, "patched_wal_pages": patched})
        except Exception as exc:
            result["failed"].append({"db": rel_path, "error": str(exc)})

    save_state(state_file, state)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh decrypted DB copies from live WeChat DBs")
    parser.add_argument("--source-db-dir", default="config/xwechat_files/PLEASE_SET_WECHAT_ACCOUNT_DIR/db_storage")
    parser.add_argument("--decrypted-dir", default="runtime/wechat-decrypt/decrypted")
    parser.add_argument("--keys-file", default="runtime/wechat-decrypt/keys/all_keys.json")
    parser.add_argument("--state-file", default="runtime/wechat-decrypt/sync_state.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    root = Path.cwd()
    source_db_dir = Path(args.source_db_dir)
    decrypted_dir = Path(args.decrypted_dir)
    keys_file = Path(args.keys_file)
    state_file = Path(args.state_file)
    if not source_db_dir.is_absolute():
        source_db_dir = root / source_db_dir
    if not decrypted_dir.is_absolute():
        decrypted_dir = root / decrypted_dir
    if not keys_file.is_absolute():
        keys_file = root / keys_file
    if not state_file.is_absolute():
        state_file = root / state_file

    result = refresh_decrypted(source_db_dir, decrypted_dir, keys_file, state_file, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
