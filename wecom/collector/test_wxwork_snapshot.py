"""Boundary tests for read-only wxWork DB/WAL snapshot recovery.

All databases and sidecars here are synthetic; the tests never open a user
profile or contact the client.
"""
import hashlib
import os
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from Crypto.Cipher import AES
from unittest import mock

import wxwork_crypto
import wxwork_snapshot


PAGE_SIZE = wxwork_crypto.PAGE_SZ
FRAME_SIZE = 24 + PAGE_SIZE


def _checksum(data, state, byteorder):
    s0, s1 = state
    words = struct.unpack(byteorder + str(len(data) // 4) + "I", data)
    for a, b in zip(words[::2], words[1::2]):
        s0 = (s0 + a + s1) & 0xFFFFFFFF
        s1 = (s1 + b + s0) & 0xFFFFFFFF
    return s0, s1


def _demote_final_commit(wal):
    """Turn the final committed transaction into a checksum-valid WAL tail."""
    data = bytearray(wal)
    page_size = struct.unpack_from(">I", data, 8)[0]
    frame_size = 24 + page_size
    complete, remainder = divmod(len(data) - 32, frame_size)
    if remainder or complete < 2:
        raise AssertionError("fixture must have multiple complete WAL frames")
    commit_indexes = [
        index
        for index in range(complete)
        if struct.unpack_from(">I", data, 32 + index * frame_size + 4)[0]
    ]
    if len(commit_indexes) < 2 or commit_indexes[-1] != complete - 1:
        raise AssertionError("fixture must end with a later committed transaction")

    last_commit_offset = 32 + commit_indexes[-1] * frame_size
    struct.pack_into(">I", data, last_commit_offset + 4, 0)
    magic = struct.unpack_from(">I", data, 0)[0]
    byteorder = "<" if magic == 0x377F0682 else ">"
    state = struct.unpack_from(">II", data, 24)
    for index in range(complete):
        offset = 32 + index * frame_size
        frame_header = data[offset : offset + 24]
        page = data[offset + 24 : offset + frame_size]
        state = _checksum(frame_header[:8] + page, state, byteorder)
        struct.pack_into(">II", data, offset + 16, *state)
    return bytes(data)


def _encrypt_page(page, key, page_number):
    cipher = AES.new(
        wxwork_crypto.derive_wxsqlite3_aes128_page_key(key, page_number),
        AES.MODE_CBC,
        wxwork_crypto.generate_initial_vector(page_number),
    )
    if page_number == 1:
        encrypted_tail = cipher.encrypt(page[16:])
        return b"SYNTHETIC"[:8] + encrypted_tail[:8] + page[16:24] + encrypted_tail[8:]
    return cipher.encrypt(page)


def _encrypt_database(raw, key):
    return b"".join(
        _encrypt_page(raw[offset : offset + PAGE_SIZE], key, offset // PAGE_SIZE + 1)
        for offset in range(0, len(raw), PAGE_SIZE)
    )


def _encrypt_wal(raw, key):
    header = raw[:32]
    if len(raw) < 32 or (len(raw) - 32) % FRAME_SIZE:
        raise AssertionError("fixture WAL must have a complete header and frames")
    magic = struct.unpack_from(">I", header, 0)[0]
    byteorder = "<" if magic == 0x377F0682 else ">"
    state = struct.unpack_from(">II", header, 24)
    result = bytearray(header)
    for offset in range(32, len(raw), FRAME_SIZE):
        frame_header = raw[offset : offset + 24]
        page_number = struct.unpack_from(">I", frame_header, 0)[0]
        page = _encrypt_page(raw[offset + 24 : offset + FRAME_SIZE], key, page_number)
        state = _checksum(frame_header[:8] + page, state, byteorder)
        result.extend(frame_header[:16])
        result.extend(struct.pack(">II", *state))
        result.extend(page)
    return bytes(result)


def _synthetic_wal_database(root):
    db_path = root / "synthetic.db"
    db = sqlite3.connect(db_path)
    try:
        db.execute("PRAGMA page_size=4096")
        db.execute("CREATE TABLE entries(value TEXT NOT NULL)")
        db.execute("INSERT INTO entries VALUES ('base')")
        db.commit()
    finally:
        db.close()
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("INSERT INTO entries VALUES ('committed')")
    connection.commit()
    connection.execute("INSERT INTO entries VALUES ('uncommitted-tail')")
    connection.commit()
    wal_path = Path(str(db_path) + "-wal")
    return db_path, wal_path, connection


class SnapshotRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wxwork-snapshot-synthetic-")
        self.root = Path(self.temp.name)
        self.key = bytes(range(16))

    def tearDown(self):
        self.temp.cleanup()

    def _encrypt_sources(self, db_path, wal_path, *, demote_final=False, partial_tail=b""):
        raw_db = db_path.read_bytes()
        raw_wal = wal_path.read_bytes()
        if demote_final:
            raw_wal = _demote_final_commit(raw_wal)
        if partial_tail:
            raw_wal += partial_tail
        db_path.write_bytes(_encrypt_database(raw_db, self.key))
        complete_end = 32 + ((len(raw_wal) - 32) // FRAME_SIZE) * FRAME_SIZE
        encrypted_frames = _encrypt_wal(raw_wal[:complete_end], self.key)
        wal_path.write_bytes(encrypted_frames + raw_wal[complete_end:])

    def test_encrypted_pages_use_real_page_numbers_and_only_committed_frames(self):
        db_path, wal_path, connection = _synthetic_wal_database(self.root)
        try:
            self._encrypt_sources(db_path, wal_path, demote_final=True, partial_tail=b"partial")
            source_paths = [db_path, wal_path, Path(str(db_path) + "-shm")]
            before = {
                path: hashlib.sha256(path.read_bytes()).digest()
                for path in source_paths
                if path.exists()
            }
            out_path = self.root / "recovered.sqlite"
            report = wxwork_snapshot.recover_snapshot(db_path, out_path, self.key)
            after = {
                path: hashlib.sha256(path.read_bytes()).digest()
                for path in source_paths
                if path.exists()
            }
            self.assertEqual(before, after)
            self.assertTrue(report["encrypted"])
            self.assertGreater(report["wal_committed_frames"], 0)
            self.assertGreater(report["wal_uncommitted_frames"], 0)
            self.assertEqual(report["wal_partial_frame_bytes"], len(b"partial"))
            uri = out_path.resolve().as_uri() + "?mode=ro&immutable=1"
            recovered = sqlite3.connect(uri, uri=True)
            try:
                rows = recovered.execute("SELECT value FROM entries ORDER BY rowid").fetchall()
            finally:
                recovered.close()
            self.assertEqual(rows, [("base",), ("committed",)])
        finally:
            connection.close()

    def test_recycled_wal_tail_preserves_current_and_checkpointed_rows(self):
        db_path, wal_path, connection = _synthetic_wal_database(self.root)
        try:
            connection.execute("PRAGMA wal_checkpoint(RESTART)")
            connection.execute("INSERT INTO entries VALUES ('current-generation')")
            connection.commit()
            raw = wal_path.read_bytes()
            current_salts = raw[16:24]
            stale = any(
                raw[offset + 8:offset + 16] != current_salts
                for offset in range(32, len(raw) - FRAME_SIZE + 1, FRAME_SIZE)
            )
            self.assertTrue(stale, "fixture must retain an earlier WAL generation")
            self._encrypt_sources(db_path, wal_path)
            out_path = self.root / "recycled.sqlite"
            report = wxwork_snapshot.recover_snapshot(db_path, out_path, self.key)
            recovered = sqlite3.connect(out_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                rows = recovered.execute("SELECT value FROM entries ORDER BY rowid").fetchall()
            finally:
                recovered.close()
            self.assertEqual(rows, [("base",), ("committed",), ("uncommitted-tail",), ("current-generation",)])
            self.assertGreater(report["wal_stale_frames"], 0)
        finally:
            connection.close()

    def test_complete_bad_frame_checksum_fails_without_publishing(self):
        db_path, wal_path, connection = _synthetic_wal_database(self.root)
        try:
            self._encrypt_sources(db_path, wal_path)
            corrupt = bytearray(wal_path.read_bytes())
            corrupt[-1] ^= 0x80
            wal_path.write_bytes(corrupt)
            out_path = self.root / "must-not-exist.sqlite"
            with self.assertRaisesRegex(wxwork_snapshot.SnapshotError, "checksum"):
                wxwork_snapshot.recover_snapshot(db_path, out_path, self.key)
            self.assertFalse(out_path.exists())
        finally:
            connection.close()

    def test_changed_main_database_retries_or_fails_explicitly(self):
        db_path = self.root / "plain.sqlite"
        db = sqlite3.connect(db_path)
        try:
            db.execute("CREATE TABLE entries(value TEXT)")
            db.execute("INSERT INTO entries VALUES ('synthetic')")
        finally:
            db.close()
        original_copy = wxwork_snapshot._copy_fd
        changed = False

        def copy_then_change(fd, target):
            nonlocal changed
            result = original_copy(fd, target)
            if not changed:
                changed = True
                st = db_path.stat()
                os.utime(db_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return result

        out_path = self.root / "unstable.sqlite"
        with mock.patch.object(wxwork_snapshot, "_copy_fd", side_effect=copy_then_change):
            with self.assertRaisesRegex(wxwork_snapshot.SnapshotError, "changed"):
                wxwork_snapshot.recover_snapshot(db_path, out_path, self.key, attempts=1)
        self.assertFalse(out_path.exists())

    def test_incomplete_wal_header_is_not_treated_as_empty_wal(self):
        db_path = self.root / "plain.sqlite"
        db = sqlite3.connect(db_path)
        try:
            db.execute("CREATE TABLE entries(value TEXT)")
        finally:
            db.close()
        Path(str(db_path) + "-wal").write_bytes(b"short")
        with self.assertRaisesRegex(wxwork_snapshot.SnapshotError, "header"):
            wxwork_snapshot.recover_snapshot(db_path, self.root / "bad.sqlite", self.key)


if __name__ == "__main__":
    unittest.main()
