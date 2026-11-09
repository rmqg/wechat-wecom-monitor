"""Clean-room WeCom database key discovery via Win32 ReadProcessMemory.

Design:
1. Enumerate running ``WXWork.exe`` processes via ``CreateToolhelp32Snapshot``.
2. Query committed private readable memory regions with ``VirtualQueryEx`` and
   stream them in bounded chunks via ``ReadProcessMemory``.
3. Extract 16-byte raw candidate keys from in-memory wxSQLite3 cipher structures:
   - Primary pass: wxSQLite3 ``Codec`` structure (``m_isEncrypted == 1``,
     ``m_hasReadKey == 1``, 16-byte ``m_readKey``, ``m_hasWriteKey == 1``,
     16-byte ``m_writeKey`` equal to ``m_readKey``, or read-only
     ``m_hasWriteKey == 0``).
   - Fallback pass (only if any database remains unresolved):
     SQLite3MultipleCiphers ``AES128Cipher`` structure (``m_legacy`` in 0..1,
     ``m_legacyPageSize`` in {0, 4096}, ``m_keyLength == 16``, 16-byte ``m_key``).
4. Validate every candidate key against page 1 of each encrypted SQLite database
   in ``data_dir`` and ``CacheMapping`` using ``verify_wxsqlite3_aes128_key``
   (decrypting page 1 and verifying both the 100-byte SQLite header and the root
   B-tree page structure).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import tempfile
import time

import wxwork_crypto as crypto

_CODEC_RW_PREFIX = b"\x01\x00\x00\x00\x01\x00\x00\x00"
_ONE_LE = b"\x01\x00\x00\x00"
_ZERO_LE = b"\x00\x00\x00\x00"
_SIXTEEN_LE = b"\x10\x00\x00\x00"
_ZERO_KEY = b"\x00" * 16
_CHUNK_BYTES = 4 * 1024 * 1024
_MAX_USER_ADDRESS_32 = 0x7FFF0000


if os.name == "nt":
    import ctypes
    from ctypes import wintypes as wt

    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_VM_READ = 0x0010
    _MEM_COMMIT = 0x1000
    _MEM_PRIVATE = 0x20000
    _PAGE_GUARD = 0x100
    _READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD),
            ("szExeFile", wt.WCHAR * 260),
        ]

    class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_size_t),
            ("AllocationBase", ctypes.c_size_t),
            ("AllocationProtect", wt.DWORD),
            ("PartitionId", wt.WORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wt.DWORD),
            ("Protect", wt.DWORD),
            ("Type", wt.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    _kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wt.BOOL
    _kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wt.BOOL
    _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.VirtualQueryEx.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(_MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    ]
    _kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    _kernel32.ReadProcessMemory.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.ReadProcessMemory.restype = wt.BOOL
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]
    _kernel32.CloseHandle.restype = wt.BOOL


def get_wxwork_pids(process_name: str = "WXWork.exe") -> list[int]:
    """Return all running process IDs matching ``process_name`` (case-insensitive)."""
    if os.name != "nt":
        return []
    target = process_name.lower()
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == ctypes.c_void_p(-1).value or not snapshot:
        return []
    pids: list[int] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.lower() == target:
                    pids.append(int(entry.th32ProcessID))
                if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        _kernel32.CloseHandle(snapshot)
    return pids


def extract_candidate_keys_from_buffer(
    data: bytes, *, include_secondary: bool = False
) -> tuple[list[bytes], list[bytes]]:
    """Extract primary (read+write dual-key Codec) and optional secondary candidate keys."""
    primary: list[bytes] = []
    secondary: list[bytes] = []
    seen_primary: set[bytes] = set()
    seen_secondary: set[bytes] = set()

    # 1. wxSQLite3 Codec structure: m_isEncrypted=1, m_hasReadKey=1, readKey[16], m_hasWriteKey, writeKey[16]
    pos = data.find(_CODEC_RW_PREFIX)
    limit = len(data) - 44
    while pos != -1 and pos <= limit:
        key1 = data[pos + 8 : pos + 24]
        if key1 != _ZERO_KEY:
            write_flag = data[pos + 24 : pos + 28]
            key2 = data[pos + 28 : pos + 44]
            if write_flag == _ONE_LE and key1 == key2:
                if key1 not in seen_primary:
                    seen_primary.add(key1)
                    primary.append(key1)
            elif write_flag == _ZERO_LE and key2 == _ZERO_KEY:
                if key1 not in seen_secondary:
                    seen_secondary.add(key1)
                    secondary.append(key1)
        pos = data.find(_CODEC_RW_PREFIX, pos + 4)

    # 2. Fallback SQLite3MultipleCiphers AES128Cipher structure: m_legacy(0|1), m_legacyPageSize(0|4096), m_keyLength(16), m_key[16]
    if include_secondary:
        pos = data.find(_SIXTEEN_LE, 8)
        limit_aes = len(data) - 20
        while pos != -1 and pos <= limit_aes:
            legacy, legacy_ps = struct.unpack_from("<II", data, pos - 8)
            if legacy in (0, 1) and legacy_ps in (0, 4096):
                key = data[pos + 4 : pos + 20]
                if key != _ZERO_KEY and key not in seen_primary and key not in seen_secondary:
                    seen_secondary.add(key)
                    secondary.append(key)
            pos = data.find(_SIXTEEN_LE, pos + 4)

    return primary, secondary


def scan_process_candidate_keys(
    pids: list[int] | None = None, *, include_secondary: bool = False
) -> list[bytes]:
    """Scan ``WXWork.exe`` process memory and return ordered 16-byte candidate keys."""
    if os.name != "nt":
        return []
    if pids is None:
        pids = get_wxwork_pids()

    primary_keys: list[bytes] = []
    secondary_keys: list[bytes] = []
    seen_primary: set[bytes] = set()
    seen_secondary: set[bytes] = set()

    buffer = (ctypes.c_char * _CHUNK_BYTES)()
    mbi = _MEMORY_BASIC_INFORMATION()
    mbi_size = ctypes.sizeof(mbi)

    for pid in pids:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, int(pid))
        if not handle:
            continue
        try:
            address = 0
            while _kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi), mbi_size) == mbi_size:
                if mbi.RegionSize == 0:
                    break
                next_address = mbi.BaseAddress + mbi.RegionSize
                if next_address <= address:
                    break
                is_readable_private = (
                    mbi.State == _MEM_COMMIT
                    and mbi.Type == _MEM_PRIVATE
                    and (mbi.Protect & 0xFF) in _READABLE_PROTECTIONS
                    and not (mbi.Protect & _PAGE_GUARD)
                )
                if is_readable_private:
                    offset = 0
                    overlap = b""
                    while offset < mbi.RegionSize:
                        to_read = min(_CHUNK_BYTES, mbi.RegionSize - offset)
                        bytes_read = ctypes.c_size_t(0)
                        if (
                            _kernel32.ReadProcessMemory(
                                handle,
                                ctypes.c_void_p(mbi.BaseAddress + offset),
                                buffer,
                                to_read,
                                ctypes.byref(bytes_read),
                            )
                            and bytes_read.value > 0
                        ):
                            chunk = overlap + ctypes.string_at(buffer, bytes_read.value)
                            prim, sec = extract_candidate_keys_from_buffer(
                                chunk, include_secondary=include_secondary
                            )
                            for key in prim:
                                if key not in seen_primary:
                                    seen_primary.add(key)
                                    primary_keys.append(key)
                            for key in sec:
                                if key not in seen_primary and key not in seen_secondary:
                                    seen_secondary.add(key)
                                    secondary_keys.append(key)
                            overlap = chunk[-44:] if len(chunk) >= 44 else chunk
                        else:
                            overlap = b""
                        offset += to_read
                address = next_address
                if address >= _MAX_USER_ADDRESS_32:
                    break
        finally:
            _kernel32.CloseHandle(handle)

    return primary_keys + [key for key in secondary_keys if key not in seen_primary]


def _atomic_write_private_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".keys-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def scan_keys(
    data_dir: Path,
    keys_file: Path,
    *,
    extra_candidates: list[bytes] | None = None,
) -> dict:
    """Locate and verify keys for all encrypted SQLite databases under ``data_dir``."""
    data_dir = Path(data_dir)
    keys_file = Path(keys_file)
    started = time.time()

    existing_payload: dict = {}
    if keys_file.is_file():
        try:
            existing_payload = json.loads(keys_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            existing_payload = {}

    candidates: list[bytes] = []
    seen: set[bytes] = set()

    def add_candidate(raw: bytes) -> None:
        if isinstance(raw, (bytes, bytearray)) and len(raw) == 16 and bytes(raw) != _ZERO_KEY:
            key = bytes(raw)
            if key not in seen:
                seen.add(key)
                candidates.append(key)

    for name, item in existing_payload.items():
        if not str(name).startswith("_") and isinstance(item, dict) and "enc_key" in item:
            try:
                add_candidate(bytes.fromhex(str(item["enc_key"])))
            except ValueError:
                continue

    for key in extra_candidates or ():
        add_candidate(key)

    pids = get_wxwork_pids()
    for key in scan_process_candidate_keys(pids, include_secondary=False):
        add_candidate(key)

    db_paths = sorted(data_dir.glob("*.db"))
    cache_mapping_dir = data_dir.parent / "CacheMapping"
    if cache_mapping_dir.is_dir():
        db_paths.extend(sorted(cache_mapping_dir.glob("*.db")))

    verified_entries: dict[str, dict[str, str]] = {}
    plain_databases: list[str] = []
    unresolved_paths: list[Path] = []

    ordered_candidates = list(candidates)

    for db_path in db_paths:
        try:
            if not db_path.is_file() or db_path.stat().st_size < crypto.PAGE_SZ:
                continue
            with db_path.open("rb") as stream:
                page1 = stream.read(crypto.PAGE_SZ)
        except OSError:
            continue
        if len(page1) < crypto.PAGE_SZ:
            continue
        if crypto.is_plain_sqlite_page(page1):
            plain_databases.append(db_path.name)
            continue

        matched_key = None
        for index, candidate in enumerate(ordered_candidates):
            if crypto.verify_wxsqlite3_aes128_key(candidate, page1):
                matched_key = candidate
                if index > 0:
                    ordered_candidates.pop(index)
                    ordered_candidates.insert(0, matched_key)
                break

        if matched_key is not None:
            rel_name = db_path.name if db_path.parent == data_dir else f"{db_path.parent.name}/{db_path.name}"
            verified_entries[db_path.name] = {
                "enc_key": matched_key.hex(),
                "db_rel_path": rel_name,
            }
        else:
            unresolved_paths.append(db_path)

    # Fallback pass if any encrypted database was not unlocked by primary Codec candidates
    if unresolved_paths and pids:
        for key in scan_process_candidate_keys(pids, include_secondary=True):
            add_candidate(key)
        ordered_candidates = list(candidates)
        still_unresolved: list[Path] = []
        for db_path in unresolved_paths:
            with db_path.open("rb") as stream:
                page1 = stream.read(crypto.PAGE_SZ)
            matched_key = next(
                (cand for cand in ordered_candidates if crypto.verify_wxsqlite3_aes128_key(cand, page1)),
                None,
            )
            if matched_key is not None:
                rel_name = db_path.name if db_path.parent == data_dir else f"{db_path.parent.name}/{db_path.name}"
                verified_entries[db_path.name] = {
                    "enc_key": matched_key.hex(),
                    "db_rel_path": rel_name,
                }
            else:
                still_unresolved.append(db_path)
        unresolved_paths = still_unresolved

    summary = {
        "scanned_at": time.time(),
        "duration_seconds": round(time.time() - started, 3),
        "wxwork_pids": len(pids),
        "candidates_tested": len(candidates),
        "verified_databases": len(verified_entries),
        "plain_databases": len(plain_databases),
        "unresolved_databases": [p.name for p in unresolved_paths],
    }
    output_payload = {"_meta": summary, **verified_entries}
    _atomic_write_private_json(keys_file, output_payload)
    return summary
