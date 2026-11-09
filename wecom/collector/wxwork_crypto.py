"""Clean-room wxSQLite3 AES-128-CBC page cipher and SQLite page-1 validator.

Specification and reference implementation:
- utelle/SQLite3MultipleCiphers (MIT License, Copyright (c) 2006-2026 Ulrich Telle),
  commit 6eefe60ea647268dc6004c8bc884c14efdee5b9d:
  - ``src/codec_algos.c``: ``sqlite3mcAES128`` (per-page MD5 key derivation with
    little-endian 32-bit page number and ``sAlT`` suffix ``0x73,0x41,0x6c,0x54``)
    and ``sqlite3mcGenerateInitialVector`` (L'Ecuyer multiplicative linear
    congruential generator ``MODMULT(52774, 40692, 3791, 2147483399, z)`` seeded
    with ``page + 1``, followed by MD5).
  - ``src/cipher_wxaes128.c``: ``EncryptPageAES128Cipher`` and
    ``DecryptPageAES128Cipher`` (4096-byte pages, zero reserve/HMAC, page-1
    bytes 16..23 preserved in plaintext while encrypted bytes 16..23 are stored
    at bytes 8..15).
- SQLite Database File Format specification (https://www.sqlite.org/fileformat2.html):
  100-byte database header and page-1 B-tree header / cell pointer layout.
"""
from __future__ import annotations

import hashlib
import struct
try:
    from Crypto.Cipher import AES
except ImportError:
    from Cryptodome.Cipher import AES

PAGE_SZ = 4096
KEY_SZ = 16
SQLITE_FILE_HEADER = b"SQLite format 3\x00"
WXSQLITE3_SALT = b"\x73\x41\x6c\x54"  # ASCII "sAlT"


def derive_wxsqlite3_aes128_page_key(key: bytes, page_number: int) -> bytes:
    """Derive the 16-byte per-page AES-128 key for 1-based ``page_number``."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SZ:
        raise ValueError("wxSQLite3 AES-128 raw key must be 16 bytes")
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    nkey = bytes(key) + struct.pack("<I", page_number & 0xFFFFFFFF) + WXSQLITE3_SALT
    return hashlib.md5(nkey).digest()


def generate_initial_vector(page_number: int) -> bytes:
    """Generate the 16-byte per-page AES-CBC IV for 1-based ``page_number``."""
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    z = (page_number + 1) & 0xFFFFFFFF
    if z >= 0x80000000:
        z -= 0x100000000
    initkey = bytearray(16)
    for index in range(4):
        q = int(z / 52774)
        z = 40692 * (z - 52774 * q) - 3791 * q
        if z < 0:
            z += 2147483399
        struct.pack_into("<I", initkey, index * 4, z & 0xFFFFFFFF)
    return hashlib.md5(bytes(initkey)).digest()


def _has_wxsqlite3_page1_plaintext_slice(page1: bytes) -> bool:
    """Check whether bytes 16..23 hold an unencrypted SQLite header slice."""
    if len(page1) < 24:
        return False
    db_header = page1[16:24]
    db_page_size = (db_header[0] << 8) | (db_header[1] << 16)
    return (
        512 <= db_page_size <= 65536
        and ((db_page_size - 1) & db_page_size) == 0
        and db_header[5] == 0x40
        and db_header[6] == 0x20
        and db_header[7] == 0x20
    )


def looks_like_sqlite_page1(page1: bytes) -> bool:
    """Validate SQLite 100-byte file header and root B-tree layout on page 1."""
    if not isinstance(page1, (bytes, bytearray)) or len(page1) != PAGE_SZ:
        return False
    data = bytes(page1)
    if data[:16] != SQLITE_FILE_HEADER:
        return False

    raw_page_size = struct.unpack_from(">H", data, 16)[0]
    page_size = 65536 if raw_page_size == 1 else raw_page_size
    if page_size != PAGE_SZ:
        return False

    # Write/read format versions must be 1 (rollback) or 2 (WAL); reserve=0; fractions=64,32,32
    if data[18] not in (1, 2) or data[19] not in (1, 2):
        return False
    if data[20] != 0 or data[21:24] != b"\x40\x20\x20":
        return False

    schema_format = struct.unpack_from(">I", data, 44)[0]
    text_encoding = struct.unpack_from(">I", data, 56)[0]
    if schema_format not in (0, 1, 2, 3, 4) or text_encoding not in (0, 1, 2, 3):
        return False
    if data[72:92] != b"\x00" * 20:
        return False

    # Offset 100 begins the sqlite_schema table B-tree root page (0x0d leaf or 0x05 interior)
    btree_type = data[100]
    if btree_type not in (0x05, 0x0D):
        return False
    btree_header_size = 8 if btree_type == 0x0D else 12
    freeblock, cell_count, cell_content = struct.unpack_from(">HHH", data, 101)
    content_start = PAGE_SZ if cell_content == 0 else cell_content
    if freeblock != 0 and (freeblock < 100 + btree_header_size or freeblock > PAGE_SZ - 4):
        return False
    pointer_array_end = 100 + btree_header_size + 2 * cell_count
    if pointer_array_end > content_start or content_start > PAGE_SZ:
        return False
    for index in range(cell_count):
        cell_offset = struct.unpack_from(">H", data, 100 + btree_header_size + 2 * index)[0]
        if cell_offset < content_start or cell_offset >= PAGE_SZ:
            return False
    return True


def is_plain_sqlite_page(page: bytes) -> bool:
    """Return True if ``page`` is already an unencrypted, valid SQLite page 1."""
    return (
        isinstance(page, (bytes, bytearray))
        and len(page) == PAGE_SZ
        and bytes(page[:16]) == SQLITE_FILE_HEADER
        and looks_like_sqlite_page1(bytes(page))
    )


def decrypt_wxsqlite3_aes128_page(key: bytes, page: bytes, page_number: int) -> bytes:
    """Decrypt one 4096-byte wxSQLite3 AES-128-CBC database or WAL page."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SZ:
        raise ValueError("wxSQLite3 AES-128 key must be 16 bytes")
    if not isinstance(page, (bytes, bytearray)) or len(page) != PAGE_SZ:
        raise ValueError(f"wxSQLite3 page must be {PAGE_SZ} bytes")
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")

    raw = bytes(page)
    page_key = derive_wxsqlite3_aes128_page_key(bytes(key), page_number)
    iv = generate_initial_vector(page_number)

    if page_number == 1 and _has_wxsqlite3_page1_plaintext_slice(raw):
        db_header = raw[16:24]
        restored_ciphertext = raw[8:16] + raw[24:]
        decrypted_tail = AES.new(page_key, AES.MODE_CBC, iv).decrypt(restored_ciphertext)
        if decrypted_tail[:8] == db_header:
            return SQLITE_FILE_HEADER + decrypted_tail
        return raw[:16] + decrypted_tail

    return AES.new(page_key, AES.MODE_CBC, iv).decrypt(raw)


def encrypt_wxsqlite3_aes128_page(key: bytes, page: bytes, page_number: int) -> bytes:
    """Encrypt one 4096-byte SQLite page using the wxSQLite3 AES-128-CBC scheme."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SZ:
        raise ValueError("wxSQLite3 AES-128 key must be 16 bytes")
    if not isinstance(page, (bytes, bytearray)) or len(page) != PAGE_SZ:
        raise ValueError(f"wxSQLite3 page must be {PAGE_SZ} bytes")
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")

    raw = bytes(page)
    page_key = derive_wxsqlite3_aes128_page_key(bytes(key), page_number)
    iv = generate_initial_vector(page_number)

    if page_number == 1:
        db_header = raw[16:24]
        first_block = AES.new(page_key, AES.MODE_CBC, iv).encrypt(raw[:16])
        encrypted_tail = AES.new(page_key, AES.MODE_CBC, iv).encrypt(raw[16:])
        return first_block[:8] + encrypted_tail[:8] + db_header + encrypted_tail[8:]

    return AES.new(page_key, AES.MODE_CBC, iv).encrypt(raw)


def verify_wxsqlite3_aes128_key(key: bytes, page1: bytes) -> bool:
    """Return True iff ``key`` decrypts ``page1`` into a valid SQLite root page."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SZ:
        return False
    if not isinstance(page1, (bytes, bytearray)) or len(page1) != PAGE_SZ:
        return False
    raw = bytes(page1)
    if is_plain_sqlite_page(raw):
        return False

    page_key = derive_wxsqlite3_aes128_page_key(bytes(key), 1)
    iv = generate_initial_vector(1)
    if _has_wxsqlite3_page1_plaintext_slice(raw):
        first_block = AES.new(page_key, AES.MODE_CBC, iv).decrypt(raw[8:16] + raw[24:32])
        if first_block[:8] != raw[16:24]:
            return False
    else:
        first_block = AES.new(page_key, AES.MODE_CBC, iv).decrypt(raw[:16])
        if first_block != SQLITE_FILE_HEADER:
            return False

    decrypted = decrypt_wxsqlite3_aes128_page(bytes(key), raw, 1)
    return looks_like_sqlite_page1(decrypted)
