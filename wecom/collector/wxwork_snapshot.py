"""Read-only recovery of a stable wxWork SQLite main-file/WAL snapshot.

The source database and its ``-wal``/``-shm`` sidecars are only opened for
reading. Output is published atomically only after WAL validation, page
recovery/decryption, and SQLite integrity checking all succeed.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
import tempfile
from pathlib import Path

import wxwork_crypto

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _WindowsFileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("written", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_WindowsFileInfo),
    ]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _windows_open(path, access):
        # Share deletion too: a read-only collector must not block a checkpoint.
        handle = _kernel32.CreateFileW(path, access, 7, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _windows_token(handle):
        info = _WindowsFileInfo()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (
            info.volume,
            (info.index_high << 32) | info.index_low,
            (info.size_high << 32) | info.size_low,
            (info.written.dwHighDateTime << 32) | info.written.dwLowDateTime,
            (info.created.dwHighDateTime << 32) | info.created.dwLowDateTime,
        )


_PAGE_SIZE = wxwork_crypto.PAGE_SZ
_WAL_HEADER_SIZE = 32
_WAL_FRAME_HEADER_SIZE = 24
_WAL_FORMAT_VERSION = 3_007_000
_WAL_MAGIC_LITTLE_CHECKSUM = 0x377F0682
_WAL_MAGIC_BIG_CHECKSUM = 0x377F0683
_COPY_CHUNK_SIZE = 1024 * 1024


class SnapshotError(RuntimeError):
    """The source cannot be recovered as one validated SQLite snapshot."""


class _SourceChanged(Exception):
    """A source sidecar changed during an attempt; the whole attempt is retried."""


def _stat_token(path: str):
    try:
        if os.name == "nt":
            handle = _windows_open(path, 0x80)
            try:
                return _windows_token(handle)
            finally:
                _kernel32.CloseHandle(handle)
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _fd_token(fd: int):
    if os.name == "nt":
        return _windows_token(msvcrt.get_osfhandle(fd))
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _hash_fd(fd: int) -> tuple[bytes, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, _COPY_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.digest(), size


def _copy_fd(fd: int, target) -> tuple[bytes, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(fd, 0, os.SEEK_SET)
    target.seek(0)
    target.truncate(0)
    while True:
        chunk = os.read(fd, _COPY_CHUNK_SIZE)
        if not chunk:
            break
        target.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    target.flush()
    target.seek(0)
    return digest.digest(), size


def _open_source(path: str, initial_token):
    if initial_token is None:
        raise _SourceChanged("source disappeared")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        if os.name == "nt":
            handle = _windows_open(path, 0x80000000)
            try:
                fd = msvcrt.open_osfhandle(handle, flags)
            except BaseException:
                _kernel32.CloseHandle(handle)
                raise
        else:
            fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _SourceChanged("source disappeared") from exc
    try:
        if _fd_token(fd) != initial_token or _stat_token(path) != initial_token:
            raise _SourceChanged("source changed while opening")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _capture_sources(db_path: str, wal_path: str, shm_path: str):
    """Copy DB/WAL to private temporary files and prove they stayed stable."""
    db_token = _stat_token(db_path)
    if db_token is None:
        raise SnapshotError("source database does not exist")
    wal_token = _stat_token(wal_path)
    shm_token = _stat_token(shm_path)

    db_fd = _open_source(db_path, db_token)
    wal_fd = None
    db_copy = None
    wal_copy = None
    try:
        db_copy = tempfile.TemporaryFile(mode="w+b")
        wal_copy = tempfile.TemporaryFile(mode="w+b")
        if wal_token is not None:
            wal_fd = _open_source(wal_path, wal_token)
        elif _stat_token(wal_path) is not None:
            raise _SourceChanged("WAL appeared while opening sources")

        db_digest, db_size = _copy_fd(db_fd, db_copy)
        if wal_fd is not None:
            wal_digest, wal_size = _copy_fd(wal_fd, wal_copy)
        else:
            wal_digest, wal_size = hashlib.sha256(b"").digest(), 0

        if _fd_token(db_fd) != db_token or _stat_token(db_path) != db_token:
            raise _SourceChanged("database changed while copying")
        if wal_fd is not None:
            if _fd_token(wal_fd) != wal_token or _stat_token(wal_path) != wal_token:
                raise _SourceChanged("WAL changed while copying")
        elif _stat_token(wal_path) is not None:
            raise _SourceChanged("WAL appeared while copying")
        if _stat_token(shm_path) != shm_token:
            raise _SourceChanged("SHM generation changed while copying")

        if _hash_fd(db_fd) != (db_digest, db_size):
            raise _SourceChanged("database contents changed while copying")
        if wal_fd is not None and _hash_fd(wal_fd) != (wal_digest, wal_size):
            raise _SourceChanged("WAL contents changed while copying")
        if _fd_token(db_fd) != db_token or _stat_token(db_path) != db_token:
            raise _SourceChanged("database changed during stability check")
        if wal_fd is not None:
            if _fd_token(wal_fd) != wal_token or _stat_token(wal_path) != wal_token:
                raise _SourceChanged("WAL changed during stability check")
        elif _stat_token(wal_path) is not None:
            raise _SourceChanged("WAL appeared during stability check")
        if _stat_token(shm_path) != shm_token:
            raise _SourceChanged("SHM generation changed during stability check")

        db_copy.seek(0)
        wal_copy.seek(0)
        return {
            "db": db_copy,
            "wal": wal_copy,
            "db_size": db_size,
            "wal_size": wal_size,
            "db_token": db_token,
            "wal_token": wal_token,
            "shm_token": shm_token,
            "wal_present": wal_token is not None,
        }
    except BaseException:
        if db_copy is not None:
            db_copy.close()
        if wal_copy is not None:
            wal_copy.close()
        raise
    finally:
        os.close(db_fd)
        if wal_fd is not None:
            os.close(wal_fd)


def _assert_sources_unchanged(db_path: str, wal_path: str, shm_path: str, capture):
    if _stat_token(db_path) != capture["db_token"]:
        raise _SourceChanged("database changed before publishing snapshot")
    if _stat_token(wal_path) != capture["wal_token"]:
        raise _SourceChanged("WAL changed before publishing snapshot")
    if _stat_token(shm_path) != capture["shm_token"]:
        raise _SourceChanged("SHM generation changed before publishing snapshot")


def _checksum(data: bytes, checksum: tuple[int, int], byteorder: str):
    if len(data) % 8:
        raise SnapshotError("WAL checksum input is not word-aligned")
    s1, s2 = checksum
    word_struct = struct.Struct("<II" if byteorder == "little" else ">II")
    for offset in range(0, len(data), 8):
        x0, x1 = word_struct.unpack_from(data, offset)
        s1 = (s1 + x0 + s2) & 0xFFFFFFFF
        s2 = (s2 + x1 + s1) & 0xFFFFFFFF
    return s1, s2


def _read_exact(stream, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise SnapshotError("unexpected end of captured source")
    return data


def _parse_wal(wal_file, wal_size: int):
    """Validate complete WAL frames and retain only the latest commit's pages."""
    if wal_size == 0:
        return {
            "page_offsets": {},
            "database_pages": None,
            "frame_count": 0,
            "committed_frame_count": 0,
            "commit_count": 0,
            "uncommitted_frame_count": 0,
            "partial_frame_bytes": 0,
            "stale_frame_count": 0,
        }
    if wal_size < _WAL_HEADER_SIZE:
        raise SnapshotError("WAL header is incomplete")

    wal_file.seek(0)
    header = _read_exact(wal_file, _WAL_HEADER_SIZE)
    magic = struct.unpack_from(">I", header, 0)[0]
    if magic == _WAL_MAGIC_LITTLE_CHECKSUM:
        byteorder = "little"
    elif magic == _WAL_MAGIC_BIG_CHECKSUM:
        byteorder = "big"
    else:
        raise SnapshotError("WAL magic is invalid")

    version, page_size = struct.unpack_from(">II", header, 4)
    if version != _WAL_FORMAT_VERSION:
        raise SnapshotError("WAL format version is unsupported")
    if page_size != _PAGE_SIZE:
        raise SnapshotError("WAL page size does not match wxwork_crypto.PAGE_SZ")

    salts = struct.unpack_from(">II", header, 16)
    stored_header_checksum = struct.unpack_from(">II", header, 24)
    checksum = _checksum(header[:24], (0, 0), byteorder)
    if checksum != stored_header_checksum:
        raise SnapshotError("WAL header checksum is invalid")

    frame_size = _WAL_FRAME_HEADER_SIZE + page_size
    complete_frames, partial_frame_bytes = divmod(wal_size - _WAL_HEADER_SIZE, frame_size)
    committed_pages = {}
    pending_pages = {}
    checksum_frames = 0
    frames_after_commit = 0
    committed_frame_count = 0
    commit_count = 0
    database_pages = None
    stale_frame_count = 0

    for frame_index in range(complete_frames):
        frame_offset = _WAL_HEADER_SIZE + frame_index * frame_size
        wal_file.seek(frame_offset)
        frame_header = _read_exact(wal_file, _WAL_FRAME_HEADER_SIZE)
        page = _read_exact(wal_file, page_size)
        page_number, commit_pages = struct.unpack_from(">II", frame_header, 0)
        frame_salts = struct.unpack_from(">II", frame_header, 8)
        expected_checksum = struct.unpack_from(">II", frame_header, 16)
        if frame_salts != salts:
            stale_frame_count = complete_frames - frame_index
            break
        if page_number == 0:
            raise SnapshotError("WAL frame has an invalid page number")

        checksum = _checksum(frame_header[:8] + page, checksum, byteorder)
        if checksum != expected_checksum:
            raise SnapshotError("WAL frame checksum is invalid")
        checksum_frames += 1
        pending_pages[page_number] = frame_offset + _WAL_FRAME_HEADER_SIZE
        frames_after_commit += 1

        if commit_pages:
            committed_pages.update(pending_pages)
            pending_pages.clear()
            database_pages = commit_pages
            committed_frame_count += frames_after_commit
            frames_after_commit = 0
            commit_count += 1

    return {
        "page_offsets": committed_pages,
        "database_pages": database_pages,
        "frame_count": checksum_frames,
        "committed_frame_count": committed_frame_count,
        "commit_count": commit_count,
        "uncommitted_frame_count": frames_after_commit,
        "partial_frame_bytes": partial_frame_bytes,
        "stale_frame_count": stale_frame_count,
    }


def _sqlite_header_page_count(page1: bytes, physical_pages: int):
    if len(page1) != _PAGE_SIZE or not wxwork_crypto.looks_like_sqlite_page1(page1):
        raise SnapshotError("database page 1 is not a valid SQLite page")
    page_size = struct.unpack_from(">H", page1, 16)[0]
    if page_size == 1:
        page_size = 65536
    if page_size != _PAGE_SIZE:
        raise SnapshotError("SQLite page size is unsupported by wxwork_crypto")

    change_counter = struct.unpack_from(">I", page1, 24)[0]
    page_count = struct.unpack_from(">I", page1, 28)[0]
    version_valid_for = struct.unpack_from(">I", page1, 92)[0]
    if change_counter == version_valid_for and page_count:
        return page_count
    return physical_pages


def _decrypt_page(key: bytes, page: bytes, page_number: int, encrypted: bool):
    if len(page) != _PAGE_SIZE:
        raise SnapshotError("captured database page is incomplete")
    if not encrypted:
        return page
    try:
        return wxwork_crypto.decrypt_wxsqlite3_aes128_page(key, page, page_number)
    except (ValueError, KeyError) as exc:
        raise SnapshotError("wxSQLite3 page decryption failed") from exc


def _integrity_check(path: str):
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise SnapshotError("SQLite integrity_check could not open or inspect the recovered database") from exc
    if rows != [("ok",)]:
        raise SnapshotError("SQLite integrity_check reported database errors")


def _source_paths(db_path: str):
    return db_path + "-wal", db_path + "-shm"


def _ensure_distinct_output(out_path: str, db_path: str, wal_path: str, shm_path: str):
    output_norm = os.path.normcase(os.path.abspath(out_path))
    for source in (db_path, wal_path, shm_path):
        if output_norm == os.path.normcase(os.path.abspath(source)):
            raise SnapshotError("output path must not name a source DB/WAL/SHM file")
        try:
            if os.path.exists(out_path) and os.path.exists(source) and os.path.samefile(out_path, source):
                raise SnapshotError("output path must not alias a source DB/WAL/SHM file")
        except OSError:
            pass


def _build_output(capture, wal_info, key: bytes, temporary_path: str):
    db_file = capture["db"]
    wal_file = capture["wal"]
    db_size = capture["db_size"]
    physical_pages, remainder = divmod(db_size, _PAGE_SIZE)
    if remainder or physical_pages == 0:
        raise SnapshotError("main database is empty or not page-aligned")

    db_file.seek(0)
    first_raw = _read_exact(db_file, _PAGE_SIZE)
    if wxwork_crypto.is_plain_sqlite_page(first_raw):
        encrypted = False
        first_page = first_raw
    else:
        encrypted = True
        first_page = _decrypt_page(key, first_raw, 1, True)
    if not wxwork_crypto.looks_like_sqlite_page1(first_page):
        raise SnapshotError("database page 1 is invalid (wrong key or damaged source)")
    base_pages = _sqlite_header_page_count(first_page, physical_pages)

    if wal_info["database_pages"] is None:
        database_pages = base_pages
    else:
        database_pages = wal_info["database_pages"]
    if database_pages <= 0:
        raise SnapshotError("recovered database has an invalid page count")

    output_page = 0
    with open(temporary_path, "w+b") as output:
        for page_number in range(1, database_pages + 1):
            wal_offset = wal_info["page_offsets"].get(page_number)
            if wal_offset is not None:
                wal_file.seek(wal_offset)
                raw_page = _read_exact(wal_file, _PAGE_SIZE)
            else:
                if page_number > physical_pages:
                    raise SnapshotError("committed WAL leaves a database page unavailable")
                db_file.seek((page_number - 1) * _PAGE_SIZE)
                raw_page = _read_exact(db_file, _PAGE_SIZE)

            page = _decrypt_page(key, raw_page, page_number, encrypted)
            if page_number == 1:
                if not wxwork_crypto.looks_like_sqlite_page1(page):
                    raise SnapshotError("recovered page 1 is invalid")
                page_size = struct.unpack_from(">H", page, 16)[0]
                if page_size == 1:
                    page_size = 65536
                if page_size != _PAGE_SIZE:
                    raise SnapshotError("recovered SQLite page size is unsupported")
            output.write(page)
            output_page += 1
        output.flush()
        os.fsync(output.fileno())

    if output_page != database_pages:
        raise SnapshotError("recovered database page count is inconsistent")
    _integrity_check(temporary_path)
    return encrypted, base_pages, database_pages


def recover_snapshot(db_path, out_path, key: bytes, *, attempts=3) -> dict:
    """Recover one consistent, committed SQLite snapshot without modifying inputs."""
    if not isinstance(key, bytes) or len(key) != 16:
        raise ValueError("key must be a 16-byte bytes value")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError("attempts must be a positive integer")

    db_path = os.path.abspath(os.fsdecode(os.fspath(db_path)))
    out_path = os.path.abspath(os.fsdecode(os.fspath(out_path)))
    wal_path, shm_path = _source_paths(db_path)
    _ensure_distinct_output(out_path, db_path, wal_path, shm_path)
    output_dir = os.path.dirname(out_path) or os.curdir
    os.makedirs(output_dir, exist_ok=True)

    for attempt_number in range(1, attempts + 1):
        capture = None
        temporary_path = None
        try:
            capture = _capture_sources(db_path, wal_path, shm_path)
            wal_info = _parse_wal(capture["wal"], capture["wal_size"])
            fd, temporary_path = tempfile.mkstemp(
                prefix=".wxwork-snapshot-",
                suffix=".tmp",
                dir=output_dir,
            )
            os.close(fd)
            encrypted, base_pages, database_pages = _build_output(
                capture, wal_info, key, temporary_path
            )
            _assert_sources_unchanged(db_path, wal_path, shm_path, capture)
            os.replace(temporary_path, out_path)
            temporary_path = None
            return {
                "attempts_used": attempt_number,
                "encrypted": encrypted,
                "base_pages": base_pages,
                "database_pages": database_pages,
                "wal_present": capture["wal_present"],
                "wal_frames": wal_info["frame_count"],
                "wal_committed_frames": wal_info["committed_frame_count"],
                "wal_commits": wal_info["commit_count"],
                "wal_uncommitted_frames": wal_info["uncommitted_frame_count"],
                "wal_partial_frame_bytes": wal_info["partial_frame_bytes"],
                "wal_stale_frames": wal_info["stale_frame_count"],
                "integrity_check": "ok",
            }
        except _SourceChanged as exc:
            if attempt_number >= attempts:
                raise SnapshotError(
                    f"source DB/WAL/SHM changed during all {attempts} snapshot attempt(s)"
                ) from exc
        except SnapshotError:
            if capture is not None:
                try:
                    _assert_sources_unchanged(db_path, wal_path, shm_path, capture)
                except _SourceChanged as exc:
                    if attempt_number >= attempts:
                        raise SnapshotError(
                            f"source DB/WAL/SHM changed during all {attempts} snapshot attempt(s)"
                        ) from exc
                    continue
            raise
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
            if capture is not None:
                capture["db"].close()
                capture["wal"].close()

    raise SnapshotError("snapshot attempts exhausted")
