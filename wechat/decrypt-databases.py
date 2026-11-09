#!/usr/bin/env python3
import sys, os, json, sqlite3
from pathlib import Path

# Add PyCryptodome path
sys.path.insert(0, os.environ.get("WECHAT_PYTHON_DEPS", "/path/to/python-deps"))
from Crypto.Cipher import AES

PAGE_SZ = 4096
SALT_SZ = 16
IV_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b"SQLite format 3\x00"

KEY_FILE = Path("/path/to/wechat-private-web/keys/all_keys.json")
DB_DIR = Path("/path/to/wechat-main/acct_main_example/db_storage")
OUT_DIR = Path(os.environ.get("WECHAT_DECRYPT_OUT", "./decrypted_db"))

OUT_DIR.mkdir(parents=True, exist_ok=True)

with open(KEY_FILE, "r", encoding="utf-8") as f:
    key_data = json.load(f)

# Normalize key map: rel_path -> enc_key_bytes
keys = {}
for k, v in key_data.items():
    if isinstance(v, dict) and "enc_key" in v:
        keys[k.replace("\\", "/")] = bytes.fromhex(v["enc_key"])

print(f"Loaded {len(keys)} database keys from {KEY_FILE}")

success = 0
failed = 0

for root, _, files in os.walk(DB_DIR):
    for f in sorted(files):
        if not f.endswith(".db") or f.endswith("-wal") or f.endswith("-shm"):
            continue
        p = Path(root) / f
        rel = p.relative_to(DB_DIR).as_posix()
        
        enc_key = keys.get(rel)
        if not enc_key:
            print(f"[SKIP] No key for {rel}")
            continue
        
        out_path = OUT_DIR / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_size = p.stat().st_size
        total_pages = file_size // PAGE_SZ
        
        print(f"Decrypting {rel} ({file_size / 1024 / 1024:.2f} MB, {total_pages} pages)...", end="", flush=True)
        
        try:
            with open(p, "rb") as fin, open(out_path, "wb") as fout:
                for pgno in range(1, total_pages + 1):
                    page_data = fin.read(PAGE_SZ)
                    if len(page_data) < PAGE_SZ:
                        break
                    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
                    cipher = AES.new(enc_key, AES.MODE_CBC, iv=iv)
                    if pgno == 1:
                        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
                        decrypted = cipher.decrypt(encrypted)
                        out_page = SQLITE_HDR + decrypted + (b"\x00" * RESERVE_SZ)
                    else:
                        encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
                        decrypted = cipher.decrypt(encrypted)
                        out_page = decrypted + (b"\x00" * RESERVE_SZ)
                    fout.write(out_page)
            
            # Verify SQLite
            conn = sqlite3.connect(str(out_path))
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM sqlite_master;")
            table_count = cur.fetchone()[0]
            conn.close()
            print(f" OK! ({table_count} tables)")
            success += 1
        except Exception as e:
            print(f" FAILED: {e}")
            failed += 1

print(f"\nFinished! Decrypted: {success}, Failed: {failed}")
