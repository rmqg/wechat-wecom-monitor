#!/usr/bin/env python3
"""Read encrypted local WeChat databases without modifying source files.

Configure local_db.accounts with base_dir, key_file, label and self_id.
Use --classify --monitor-config PATH to apply the shared monitoring rules.
Decrypted copies are held in a temporary directory and removed after reading.
"""
import argparse
import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.environ.get("WECHAT_PYTHON_DEPS", "/path/to/python-deps"))
from Crypto.Cipher import AES
from Crypto.Util import Padding

PAGE_SZ = 4096
SALT_SZ = 16
IV_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b"SQLite format 3\x00"

KEY_FILE_MAIN = "/path/to/wechat-private-web/keys/all_keys.json"
KEY_FILE_SMALL = "/path/to/wechat-digest/runtime/keyscan/all_keys.json"
MAIN_BASE = Path("/path/to/wechat-main/acct_main_example")
SMALL_BASE = Path("/path/to/wechat-secondary/acct_secondary_example")
MAIN_WXID = "acct_main_example"
SMALL_WXID = "acct_secondary_example"
DEFAULT_CONFIG = "/path/to/monitor-data/群聊监控配置.json"

USER_ALIASES = ["你的名字", "你的昵称", "your-id"]  # 按你自己的名字/昵称/ID 填写
# 容易和你的名字撞车的他人群名片，匹配“提到你”时先去掉，避免误命中
EXCLUDE_NICKNAMES = ["别人的相似群名片"]
# 有人的群名片和你的名字很像时，把它列进下面 EXCLUDE_NICKNAMES，匹配时排除
KEYWORDS = ["签到", "作业", "截止", "调课", "停课", "补课", "考试", "点名", "接龙",
            "填表", "值班", "面试", "通知", "交", "提交", "确认", "投票", "查寝",
            "断电", "断网", "门禁", "台风", "封号", "停服", "涨价", "换号", "付款",
            "转账", "收款", "结算", "组会", "训练", "拍摄"]

# --- 内容解码 / 去重 / 链接抓取的常量 ---
LOOKBACK_DEFAULT = 7200
SEEN_DEFAULT = "/tmp/wxmon-seen.json"
SEEN_TTL_SECS = 7 * 86400
MEDIA_OUT = Path("/tmp/wxmon-media")
LINK_KEYWORDS = ["比赛", "报名", "截止", "通知", "招募", "宣讲", "选拔", "征集"]
LINK_SECRET_HINTS = ("password", "passwd", "token", "secret", "api_key", "apikey",
                     "auth=", "sessionid", "code=")
LINK_FETCH_BUDGET = 5
TEXT_PREVIEW_CHARS = 8000
LINK_PREVIEW_CHARS = 800
MMKV_FORMAT_KEY = b"xwechat_crypt_ke"


def load_keys(key_file):
    """读密钥文件（只读，不打印密钥）。两个号各有一份，以 _db_dir 区分，绝不能混用。"""
    with open(key_file, "r", encoding="utf-8") as f:
        raw = json.load(f)
    keys = {}
    for k, v in raw.items():
        if isinstance(v, dict) and "enc_key" in v:
            keys[k.replace("\\", "/")] = bytes.fromhex(v["enc_key"])
    return keys


def decrypt_page(page_data, enc_key, pgno):
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    cipher = AES.new(enc_key, AES.MODE_CBC, iv=iv)
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        dec = cipher.decrypt(encrypted)
        return SQLITE_HDR + dec + b"\x00" * RESERVE_SZ
    encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
    dec = cipher.decrypt(encrypted)
    return dec + b"\x00" * RESERVE_SZ


def decrypt_db_with_wal(db_path, enc_key, workdir):
    """复制 db+wal 到工作目录，解密主库并按 WAL 帧顺序覆盖页面，返回明文库路径。"""
    name = db_path.name
    tmp_db = workdir / name
    shutil.copyfile(db_path, tmp_db)
    wal = db_path.with_name(name + "-wal")
    tmp_wal = workdir / (name + "-wal")
    if wal.exists():
        shutil.copyfile(wal, tmp_wal)
    out_path = workdir / ("plain_" + name)
    total_pages = tmp_db.stat().st_size // PAGE_SZ
    pages = {}
    with open(tmp_db, "rb") as fin:
        for pgno in range(1, total_pages + 1):
            data = fin.read(PAGE_SZ)
            if len(data) < PAGE_SZ:
                break
            pages[pgno] = decrypt_page(data, enc_key, pgno)
    # 拼接 WAL：每帧 24 字节头 + 一页数据，salt 须与 WAL 头一致。
    # 关键：只应用到最后一个 commit 帧为止——commit 帧之后的尾帧是未提交的脏数据，
    # 应用了会损坏数据库（帧头第 2 个字段 commit size 非零即为 commit 帧）。
    if tmp_wal.exists() and tmp_wal.stat().st_size >= 32:
        with open(tmp_wal, "rb") as f:
            hdr = f.read(32)
            magic, _fmt, page_size, _ckpt, salt1, salt2 = struct.unpack(">6I", hdr[:24])
            if magic in (0x377F0682, 0x377F0683) and page_size == PAGE_SZ:
                frames = []
                while True:
                    fh = f.read(24)
                    if len(fh) < 24:
                        break
                    pgno, commit, fs1, fs2 = struct.unpack(">4I", fh[:16])
                    data = f.read(PAGE_SZ)
                    if len(data) < PAGE_SZ:
                        break
                    frames.append((pgno, commit, fs1, fs2, data))
                last_commit = 0
                for i, (_p, commit, _a, _b, _d) in enumerate(frames, start=1):
                    if commit:
                        last_commit = i
                for pgno, _commit, fs1, fs2, data in frames[:last_commit]:
                    if fs1 != salt1 or fs2 != salt2:
                        continue
                    pages[pgno] = decrypt_page(data, enc_key, pgno)
    with open(out_path, "wb") as fout:
        for pgno in range(1, max(pages) + 1):
            fout.write(pages.get(pgno, b"\x00" * PAGE_SZ))
    return out_path


# Reuse the shared attachment parser, including nested protobuf filenames.
_FILE_TEXT_PATH = str(Path(__file__).resolve().parent / "file_text.py")
_file_text_spec = importlib.util.spec_from_file_location("wechat_digest_file_text", _FILE_TEXT_PATH)
_file_text = importlib.util.module_from_spec(_file_text_spec)
_file_text_spec.loader.exec_module(_file_text)
parse_protobuf_strings = _file_text.protobuf_strings
extract_filename_candidates = _file_text.filename_candidates


def pick_attachment_file(base, month, cands, title, size):
    """Returns (name, path). The folder is shared by every chat, so a same-named file of another size is not this one.
    When nothing is on disk the name is only reported: the title, else the innermost candidate (no glued prefix)."""
    for name in cands:
        cand = Path(base) / "msg" / "file" / month / name
        if cand.is_file() and (not size or cand.stat().st_size == size):
            return name, str(cand)
    if title and title in cands:
        return title, None
    return (cands[0] if cands else None), None


def guess_filename(strings):
    """兼容旧名：返回最像文件名的那一个（没有返回 None）。"""
    cands = extract_filename_candidates(strings)
    return cands[-1] if cands else None


# ---------------- 内容解码：卡片 / 图片 ----------------

def split_msg_type(value):
    """local_type 拆成 (base_type, app_subtype)：高 32 位是应用子类型。"""
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        return 0, 0
    if v > 0xFFFFFFFF:
        return v & 0xFFFFFFFF, v >> 32
    return v, 0


_ZSTD = None


def zstd_decompress(data):
    """zstd 解压：优先 zstandard 模块，没有时用宿主 /usr/bin/zstd 命令行。"""
    global _ZSTD
    if _ZSTD is None:
        try:
            import zstandard as _z
            _ZSTD = ("module", _z.ZstdDecompressor())
        except Exception:
            _ZSTD = ("cli", None) if shutil.which("zstd") else (False, None)
    kind, dec = _ZSTD
    if kind == "module":
        try:
            return dec.decompress(data)
        except Exception:
            return None
    if kind == "cli":
        try:
            p = subprocess.run(["zstd", "-d", "-c"], input=data,
                               capture_output=True, timeout=30)
            return p.stdout if p.returncode == 0 else None
        except Exception:
            return None
    return None


def decompress_content(content, ct):
    """按 WCDB 压缩标记解压消息内容：ct==4 为 zstd；其它按 UTF-8 文本解。"""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if ct == 4:
        raw = zstd_decompress(content)
        if raw is None:
            return None
        return raw.decode("utf-8", errors="replace")
    try:
        return content.decode("utf-8", errors="replace")
    except Exception:
        return None


def clean_text(value):
    text = html.unescape(str(value or ""))
    text = text.replace("\x00", " ")
    return re.sub(r"\s+", " ", text).strip()


def split_group_sender(content):
    """群消息正文前的发送人前缀：'wxid:\n正文' 或 'wxid:<xml...'。"""
    if not content:
        return "", ""
    if ":\n" in content:
        sender, body = content.split(":\n", 1)
        if re.fullmatch(r"[A-Za-z0-9_\-@.]+", sender or ""):
            return sender, body
    m = re.match(r"^([A-Za-z0-9_\-@.]+):(<\?xml|<msg|<sysmsg|<voipmsg)", content)
    if m:
        return m.group(1), content[len(m.group(1)) + 1:]
    return "", content


def _xml_node_text(node, path):
    if node is None:
        return ""
    found = node.find(path)
    return clean_text(found.text if found is not None else "")


def parse_app_message(body):
    """解析应用消息 XML：标题/描述/链接/来源/引用。失败时只返回可读文本。"""
    text = str(body or "").strip()
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return {"title": clean_text(re.sub(r"<[^>]+>", " ", html.unescape(text)))[:500],
                "des": "", "url": "", "source": "", "app_name": "", "app_type": ""}
    appmsg = root.find(".//appmsg")
    if appmsg is None:
        return {"title": clean_text(re.sub(r"<[^>]+>", " ", html.unescape(text)))[:500],
                "des": "", "url": "", "source": "", "app_name": "", "app_type": ""}
    out = {
        "app_type": _xml_node_text(appmsg, "type"),
        "title": _xml_node_text(appmsg, "title"),
        "des": _xml_node_text(appmsg, "des"),
        "url": _xml_node_text(appmsg, "url"),
        "source": _xml_node_text(appmsg, "sourcedisplayname"),
        "app_name": _xml_node_text(appmsg, "appname"),
    }
    refer = appmsg.find("refermsg")
    if refer is not None:
        out["quote"] = {
            "sender": _xml_node_text(refer, "displayname") or _xml_node_text(refer, "chatusr"),
            "type": _xml_node_text(refer, "type"),
            "content": _xml_node_text(refer, "content")[:300],
        }
    return out


def parse_img_attrs(body):
    """解析图片消息 XML 里的 <img> 属性：原图 md5/长度、CDN 是否有原图。"""
    text = str(body or "").strip()
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return {}
    img = root.find(".//img")
    if img is None:
        return {}
    a = img.attrib
    return {
        "md5": (a.get("md5") or "").lower() or None,
        "length": a.get("length"),
        "hdlength": a.get("hdlength"),
        "has_cdnbig": bool(a.get("cdnbigimgurl")),
        "has_cdnmid": bool(a.get("cdnmidimgurl")),
    }


def extract_md5_from_packed_info(blob):
    """从 MessageResourceInfo.packed_info 里抠 32 位 hex md5。"""
    if not blob:
        return None
    marker = b"\x12\x22\x0a\x20"
    idx = blob.find(marker)
    if idx >= 0 and idx + len(marker) + 32 <= len(blob):
        candidate = blob[idx + len(marker): idx + len(marker) + 32]
        try:
            value = candidate.decode("ascii")
            int(value, 16)
            return value.lower()
        except (UnicodeDecodeError, ValueError):
            pass
    m = re.search(rb"[0-9a-f]{32}", blob)
    return m.group(0).decode("ascii") if m else None


# ---------------- 图片 .dat 解密 ----------------

V2_MAGIC_FULL = b"\x07\x08V2\x08\x07"
V1_MAGIC_FULL = b"\x07\x08V1\x08\x07"
IMAGE_MAGIC = {
    "png": b"\x89PNG",
    "gif": b"GIF8",
    "webp": b"RIFF",
    "jpg": b"\xff\xd8\xff",
    "tif": b"II*\x00",
}


def detect_image_format(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:3] == b"GIF":
        return "gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] == b"II*\x00":
        return "tif"
    if data[:4].lower() == b"wxgf":
        return "hevc"
    return None


def _aligned_aes_block_size(aes_size):
    if aes_size % 16:
        return aes_size + (16 - aes_size % 16)
    return aes_size + 16


def decrypt_v2(data, aes_key, xor_key):
    """V2 .dat：[6B magic][4B aes_size LE][4B xor_size LE][1B pad]
    [AES-ECB 段][原样中段][XOR 尾段]。返回 (bytes, fmt) 或 (None, None)。"""
    if len(data) < 15:
        return None, None
    sig = data[:6]
    if sig not in (V2_MAGIC_FULL, V1_MAGIC_FULL):
        return None, None
    if sig == V1_MAGIC_FULL:
        key = b"cfcd208495d565ef"
    elif aes_key:
        key = aes_key.encode("ascii")[:16] if isinstance(aes_key, str) else aes_key[:16]
    else:
        return None, None
    if len(key) < 16:
        return None, None
    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    aligned_size = _aligned_aes_block_size(aes_size)
    offset = 15
    if offset + aligned_size > len(data):
        return None, None
    try:
        cipher = AES.new(key, AES.MODE_ECB)
        dec_aes = Padding.unpad(cipher.decrypt(data[offset:offset + aligned_size]), AES.block_size)
    except (ValueError, KeyError):
        return None, None
    offset += aligned_size
    raw_end = len(data) - xor_size
    raw_data = data[offset:raw_end] if offset < raw_end else b""
    dec_xor = bytes(b ^ xor_key for b in data[raw_end:])
    decrypted = dec_aes + raw_data + dec_xor
    fmt = detect_image_format(decrypted[:16])
    if not fmt:
        return None, None
    if fmt == "jpg" and decrypted[-2:] != b"\xff\xd9" and b"\xff\xd9" not in decrypted[2:]:
        return None, None
    if fmt == "png" and b"IEND" not in decrypted[-12:]:
        return None, None
    return decrypted, fmt


def _detect_xor_key(data):
    if len(data) < 4 or data[:4] == b"\x07\x08\x56\x32":
        return None
    for magic in IMAGE_MAGIC.values():
        key = data[0] ^ magic[0]
        if bytes(b ^ key for b in data[:len(magic)]) == magic:
            return key
    return None


def decrypt_dat_bytes(data, aes_key, xor_key):
    """解密一个 .dat 文件内容（V2 或旧单字节 XOR 格式）。"""
    if data[:6] in (V2_MAGIC_FULL, V1_MAGIC_FULL):
        return decrypt_v2(data, aes_key, xor_key)
    key = _detect_xor_key(data)
    if key is None:
        return None, None
    dec = bytes(b ^ key for b in data)
    return dec, detect_image_format(dec[:16])


def image_dimensions(data, fmt):
    try:
        if fmt == "png" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if fmt == "gif" and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if fmt == "webp" and len(data) >= 30 and data[12:16] == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h
        if fmt == "jpg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return struct.unpack(">HH", data[i + 5:i + 9])[::-1]
                size = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + size
    except Exception:
        return None, None
    return None, None


def _mmkv_varint(data, offset):
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << (index * 7)
        if not byte & 128:
            return value, offset
    raise ValueError("invalid varint")


def _mmkv_span(data, offset):
    size, offset = _mmkv_varint(data, offset)
    end = offset + size
    if end > len(data):
        raise ValueError("truncated span")
    return data[offset:end], end


def derive_image_key(xwechat_files):
    """从本号的 MMKV 身份文件推导图片 key（只读、只在内存里，不落盘、不打印）。

    身份文件 all_users/config/global_config 里存着本号登录身份（用户名 + uin），
    图片 key 是这两个值的确定性函数；与数据库密钥不同，分号各算各的。
    任一步失败返回 (None, None)，调用方把图片记为 no_image_key，不崩。
    """
    try:
        config = Path(xwechat_files) / "all_users" / "config"
        data = (config / "global_config").read_bytes()
        metadata = (config / "global_config.crc").read_bytes()
        if len(metadata) < 40 or struct.unpack_from("<I", metadata, 4)[0] != 4:
            return None, None
        size = struct.unpack_from("<I", metadata, 28)[0]
        encrypted = data[4:4 + size]
        if len(encrypted) != size or zlib.crc32(encrypted) & 0xFFFFFFFF != struct.unpack_from("<I", metadata)[0]:
            return None, None
        plain = AES.new(MMKV_FORMAT_KEY, AES.MODE_CFB,
                        iv=metadata[12:28], segment_size=128).decrypt(encrypted)
        _, at = _mmkv_varint(plain, 0)
        entries = {}
        while at < len(plain):
            key, at = _mmkv_span(plain, at)
            value, at = _mmkv_span(plain, at)
            if value:
                entries[key.decode()] = value
            else:
                entries.pop(key.decode(), None)
        username_raw, _ = _mmkv_span(entries["mmkv_key_user_name"], 0)
        username = username_raw.decode()
        uin, end = _mmkv_varint(entries["mmkv_key_latest_login_uin"], 0)
        if end != len(entries["mmkv_key_latest_login_uin"]) or not uin or uin > 0xFFFFFFFF:
            return None, None
        # 身份必须对得上本号目录，防止串号
        account_dirs = [d.name for d in Path(xwechat_files).iterdir() if d.is_dir()]
        if not any(d == username or d.startswith(username + "_") for d in account_dirs):
            return None, None
        aes = hashlib.md5((str(uin) + username).encode()).hexdigest()[:16]
        xor = next(((uin >> shift) & 255 for shift in range(4) if (uin >> shift) & 255), 0)
        return aes, xor
    except Exception:
        return None, None


# ---------------- 文件正文提取 ----------------

def _lib_available(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _clip_text(text, source):
    text = (text or "").strip()
    truncated = len(text) > TEXT_PREVIEW_CHARS
    return {"text_preview": text[:TEXT_PREVIEW_CHARS],
            "text_source": source,
            "text_truncated": truncated,
            "text_chars": len(text)}


def extract_file_text(path):
    """只读提取文件正文前 8000 字。缺库时如实记缺库，不安装依赖。"""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        size = p.stat().st_size
    except OSError:
        return {"text_preview": "未提取：读不到文件", "text_source": "none",
                "text_truncated": False, "text_chars": 0}
    if size > 50 * 1024 * 1024:
        return {"text_preview": "未提取：文件过大（>50MB）", "text_source": "none",
                "text_truncated": False, "text_chars": 0}
    if ext in (".txt", ".md", ".csv", ".log", ".json", ".conf", ".ini", ".yaml", ".yml",
               ".xml", ".html", ".htm"):
        try:
            return _clip_text(p.read_text(encoding="utf-8", errors="replace"), "stdlib-txt")
        except Exception as e:
            return {"text_preview": f"未提取：读失败（{type(e).__name__}）",
                    "text_source": "none", "text_truncated": False, "text_chars": 0}
    if ext == ".pdf":
        if _lib_available("pypdf"):
            pdf_lib, pdf_name = "pypdf", "pypdf"
        elif _lib_available("PyPDF2"):
            pdf_lib, pdf_name = "PyPDF2", "PyPDF2"
        else:
            return {"text_preview": "未提取：缺库 pypdf", "text_source": "none",
                    "text_truncated": False, "text_chars": 0}
        try:
            mod = __import__(pdf_lib)
            reader = mod.PdfReader(str(p))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
            return _clip_text(text, pdf_name)
        except Exception as e:
            return {"text_preview": f"未提取：解析失败（{type(e).__name__}）",
                    "text_source": "none", "text_truncated": False, "text_chars": 0}
    if ext == ".docx":
        if not _lib_available("docx"):
            return {"text_preview": "未提取：缺库 python-docx", "text_source": "none",
                    "text_truncated": False, "text_chars": 0}
        try:
            import docx as _docx
            doc = _docx.Document(str(p))
            parts = [para.text for para in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
            return _clip_text("\n".join(parts), "python-docx")
        except Exception as e:
            return {"text_preview": f"未提取：解析失败（{type(e).__name__}）",
                    "text_source": "none", "text_truncated": False, "text_chars": 0}
    if ext in (".xlsx", ".xls"):
        if not _lib_available("openpyxl"):
            return {"text_preview": "未提取：缺库 openpyxl", "text_source": "none",
                    "text_truncated": False, "text_chars": 0}
        try:
            import openpyxl as _oxl
            wb = _oxl.load_workbook(str(p), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None and str(c) != ""]
                    if cells:
                        parts.append(" | ".join(cells))
            return _clip_text("\n".join(parts), "openpyxl")
        except Exception as e:
            return {"text_preview": f"未提取：解析失败（{type(e).__name__}）",
                    "text_source": "none", "text_truncated": False, "text_chars": 0}
    return {"text_preview": "未提取：不支持的格式", "text_source": "none",
            "text_truncated": False, "text_chars": 0}


# ---------------- 链接抓取 ----------------

def link_is_important(tier, sender_wxid, key_people, app):
    if tier in ("P0", "P1") and sender_wxid in key_people:
        return True
    hay = (app.get("title") or "") + (app.get("des") or "")
    return any(k in hay for k in LINK_KEYWORDS)


def fetch_link_preview(url):
    """只读抓公开页面：标题 + 正文前 800 字。含账密/token 参数、需登录、抓失败
    都如实记录，绝不把没抓到的说成抓到了。"""
    if not url or not url.startswith(("http://", "https://")):
        return {"status": "skip", "reason": "非 http(s) 链接"}
    if any(h in url.lower() for h in LINK_SECRET_HINTS):
        return {"status": "skip", "reason": "链接含账密/token 参数，不抓"}
    try:
        p = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", "10",
             "-A", "Mozilla/5.0 (compatible; wxmon/1.0)",
             "-w", "\n__FINAL_URL__:%{url_effective}", url],
            capture_output=True, timeout=20)
    except Exception as e:
        return {"status": "fetch_failed", "error": f"curl 启动失败：{e}"[:200]}
    if p.returncode != 0:
        return {"status": "fetch_failed",
                "error": p.stderr.decode(errors="replace")[:200] or f"curl rc={p.returncode}"}
    out = p.stdout.decode("utf-8", errors="replace")
    final_url = url
    if "\n__FINAL_URL__:" in out:
        out, final_url = out.rsplit("\n__FINAL_URL__:", 1)
        final_url = final_url.strip()
    if re.search(r"/(login|signin|auth|sso)(/|$|\?)", urlparse(final_url).path, re.I):
        return {"status": "login_required", "title": "", "fetched_url": final_url}
    title_m = re.search(r"<title[^>]*>(.*?)</title>", out, re.S | re.I)
    title = clean_text(re.sub(r"<[^>]+>", "", title_m.group(1))) if title_m else ""
    if re.search(r"登录|请登录|login|sign in", title, re.I) or 'type="password"' in out[:20000]:
        return {"status": "login_required", "title": title[:200], "fetched_url": final_url}
    body = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", out)
    body = re.sub(r"(?is)<[^>]+>", " ", body)
    body = clean_text(html.unescape(body))
    if not body:
        return {"status": "fetch_failed", "error": "页面无正文", "fetched_url": final_url}
    return {"status": "ok", "title": title[:200],
            "text_preview": body[:LINK_PREVIEW_CHARS],
            "text_truncated": len(body) > LINK_PREVIEW_CHARS,
            "fetched_url": final_url}


# ---------------- seen 文件（跨次去重） ----------------

def load_seen(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception:
        return {}


def save_seen(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def message_key(gid, server_id, local_id, create_time):
    if server_id:
        return f"{gid}|{server_id}"
    return f"{gid}|{local_id}|{create_time}"


class AccountReader:
    def __init__(self, base_dir, key_file, workdir, label, self_wxid=""):
        self.base = Path(base_dir)
        self.label = label
        self.self_wxid = self_wxid  # 本号自己的 wxid，用于私聊里区分自己发的消息
        self.msg_db = self.base / "db_storage" / "message" / "message_0.db"
        self.res_db = self.base / "db_storage" / "message" / "message_resource.db"
        self.keys = load_keys(key_file)
        self.workdir = Path(workdir)
        self.conn = None
        self.res_conn = None
        self.chat_id_by_name = {}
        self.sender_by_id = {}
        self._image_key = False  # False=未算过；None=推导失败；(aes, xor)=可用

    def _key_for(self, rel):
        # 只按相对 db_storage 的精确路径取 key；两个号的 key 文件不同，绝不能串用
        return self.keys.get(rel)

    def get_image_key(self):
        """本号图片 key（MMKV 身份推导，内存中）。失败返回 None。"""
        if self._image_key is False:
            aes, xor = derive_image_key(self.base.parent)
            self._image_key = (aes, xor) if aes else None
        return self._image_key

    def open(self):
        key = self._key_for("message/message_0.db")
        if not key or not self.msg_db.exists():
            return False, "no key or no db: %s" % self.msg_db
        plain = decrypt_db_with_wal(self.msg_db, key, self.workdir)
        self.conn = sqlite3.connect("file:%s?mode=ro" % plain, uri=True)
        # 资源库（文件名/发送人映射），有 key 就解，没有也不致命
        rkey = self._key_for("message/message_resource.db")
        if rkey and self.res_db.exists():
            try:
                rplain = decrypt_db_with_wal(self.res_db, rkey, self.workdir)
                self.res_conn = sqlite3.connect("file:%s?mode=ro" % rplain, uri=True)
                for rowid, name in self.res_conn.execute(
                        "SELECT rowid, user_name FROM ChatName2Id"):
                    self.chat_id_by_name[name] = rowid
                for rowid, name in self.res_conn.execute(
                        "SELECT rowid, user_name FROM SenderName2Id"):
                    self.sender_by_id[rowid] = name
            except Exception:
                self.res_conn = None
        return True, "ok"

    def tables(self):
        return {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")}

    def chatroom_names(self):
        try:
            return [r[0] for r in self.conn.execute(
                "SELECT user_name FROM Name2Id WHERE user_name LIKE '%@chatroom'")]
        except Exception:
            return []

    def private_contacts(self):
        """有消息表的私聊联系人 [(wxid, table)]，排除群聊。
        Name2Id 里 458 个私聊联系人大多没有消息表（Linux 端只同步有消息的会话），
        实际有表的只有约 20 个。"""
        try:
            names = [r[0] for r in self.conn.execute("SELECT user_name FROM Name2Id")]
        except Exception:
            return []
        by_table = {}
        for n in names:
            if n.endswith("@chatroom"):
                continue
            by_table["Msg_" + hashlib.md5(n.encode()).hexdigest()] = n
        tables = self.tables()
        return sorted((wxid, t) for t, wxid in by_table.items() if t in tables)

    def _find_dat_files(self, chatroom_id, md5):
        """按 md5 在 msg/attach/<群hash>/*/Img/ 下找 .dat 候选。"""
        if not md5:
            return []
        chat_hash = hashlib.md5(chatroom_id.encode()).hexdigest()
        attach_dir = self.base / "msg" / "attach" / chat_hash
        if not attach_dir.exists():
            return []
        return sorted(attach_dir.glob(f"*/Img/{md5}*.dat"))

    def _resolve_image(self, chatroom_id, img_meta, resource_md5):
        """解密本地图片：原图优先，其次高清，只有缩略图时如实标注。"""
        aliases = []
        for m in (resource_md5, (img_meta or {}).get("md5")):
            if m and m not in aliases:
                aliases.append(m)
        candidates = []
        for m in aliases:
            candidates.extend(self._find_dat_files(chatroom_id, m))
        if not candidates:
            return {"status": "missing_file", "md5": (img_meta or {}).get("md5")}
        key = self.get_image_key()
        if not key:
            return {"status": "no_image_key", "md5": (img_meta or {}).get("md5"),
                    "candidates": len(candidates)}
        aes_key, xor_key = key

        def _rank(p):
            if p.name.endswith("_t.dat"):
                return 2
            if p.name.endswith("_h.dat"):
                return 0
            return 1
        candidates.sort(key=_rank)
        for cand in candidates:
            try:
                raw = cand.read_bytes()
            except OSError:
                continue
            decoded, fmt = decrypt_dat_bytes(raw, aes_key, xor_key)
            if not decoded:
                continue
            is_thumb = cand.name.endswith("_t.dat")
            quality = "thumbnail"
            if not is_thumb:
                xml_md5 = (img_meta or {}).get("md5")
                xml_len = (img_meta or {}).get("length")
                if (xml_md5 and hashlib.md5(decoded).hexdigest() == xml_md5.lower()
                        and (not xml_len or len(decoded) == int(xml_len))):
                    quality = "original"
                else:
                    quality = "client_full"
            MEDIA_OUT.mkdir(parents=True, exist_ok=True)
            out_path = MEDIA_OUT / f"{(img_meta or {}).get('md5') or cand.stem}.{fmt}"
            try:
                if not out_path.exists() or out_path.stat().st_size != len(decoded):
                    out_path.write_bytes(decoded)
                    os.chmod(out_path, 0o600)
            except OSError:
                pass
            w, h = image_dimensions(decoded, fmt)
            return {"status": "ready", "md5": (img_meta or {}).get("md5"),
                    "format": fmt, "width": w, "height": h,
                    "quality": quality, "size": len(decoded),
                    "path": str(out_path), "source_dat": cand.name}
        return {"status": "decode_failed", "md5": (img_meta or {}).get("md5"),
                "candidates": len(candidates)}

    def read_group(self, chatroom_id, since, is_private=False):
        """读一个群自 since（epoch 秒）以来的新消息。返回 (rows, error)。
        is_private=True 时读私聊：不过滤，自己发的也留着（调用方要完整上下文），
        发件人按 real_sender_id 定——1 是自己发的（本号），其他是对方发的。"""
        table = "Msg_" + hashlib.md5(chatroom_id.encode()).hexdigest()
        if table not in self.tables():
            return None, "no table"
        try:
            cur = self.conn.execute(
                f"SELECT local_id, server_id, local_type, real_sender_id, create_time, "
                f"message_content, WCDB_CT_message_content, compress_content "
                f"FROM {table} WHERE create_time > ? ORDER BY create_time ASC",
                (since,))
            rows_raw = cur.fetchall()
            has_ct = True
        except Exception:
            # 极少数表没有 WCDB_CT 列时退回旧查询
            has_ct = False
            try:
                cur = self.conn.execute(
                    f"SELECT local_id, server_id, local_type, real_sender_id, create_time, "
                    f"message_content FROM {table} WHERE create_time > ? ORDER BY create_time ASC",
                    (since,))
                rows_raw = [(r[0], r[1], r[2], r[3], r[4], r[5], None, None) for r in cur.fetchall()]
            except Exception as e:
                return None, str(e)
        chat_id = self.chat_id_by_name.get(chatroom_id)
        sender_re = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_\-]{5,40}):\n")
        rows = []
        for local_id, server_id, local_type, sender_id, ctime, content, ct, compress in rows_raw:
            btype, subtype = split_msg_type(local_type)
            sender = ""
            text = ""
            app = None
            img_meta = None
            if btype == 1:
                s = decompress_content(content, ct)
                if s is None and compress is not None:
                    s = decompress_content(compress, None)
                s = s or ""
                # 群文本正文前可能带二进制前缀，用正则找第一个像 wxid 的发送人
                m = sender_re.search(s[:300])
                if m:
                    sender = m.group(1)
                    text = s[m.end():]
                else:
                    text = s
            elif btype == 49:
                s = decompress_content(content, ct)
                if s is None and compress is not None:
                    s = decompress_content(compress, None)
                s = s or ""
                sender, body = split_group_sender(s)
                app = parse_app_message(body)
                parts = [app.get("title") or "", app.get("des") or ""]
                if app.get("source"):
                    parts.append(f"来源 {app['source']}")
                if app.get("quote", {}).get("content"):
                    q = app["quote"]
                    parts.append(f"引用 {q.get('sender') or '对方'}: {q['content']}")
                text = clean_text("；".join(p for p in parts if p))
            elif btype == 3:
                s = decompress_content(content, ct)
                if s is None and compress is not None:
                    s = decompress_content(compress, None)
                s = s or ""
                sender, body = split_group_sender(s)
                img_meta = parse_img_attrs(body)
                text = ""
            sender = sender or self.sender_by_id.get(sender_id, "")
            if is_private:
                # 私聊：按 real_sender_id 定发件人——1 是自己发的，其他是对方。
                # sender_by_id 在私聊表里常为空，不能靠它。
                sender = self.self_wxid if sender_id == 1 else chatroom_id
            row = {
                "local_id": local_id, "server_id": server_id,
                "type": btype, "app_subtype": subtype,
                "sender_wxid": sender,
                "create_time": ctime, "text": (text or "")[:2000],
                "file": None,
            }
            if app is not None:
                row["app"] = app
            if img_meta is not None:
                row["image_meta"] = img_meta
            # 文件/图片/视频：从资源库找文件名与本地路径
            resource_md5 = None
            if btype in (3, 43, 49) and self.res_conn is not None and chat_id:
                try:
                    info = self.res_conn.execute(
                        "SELECT message_id, packed_info FROM MessageResourceInfo "
                        "WHERE chat_id=? AND message_local_id=?",
                        (chat_id, local_id)).fetchone()
                    if info:
                        resource_md5 = extract_md5_from_packed_info(info[1])
                        det = self.res_conn.execute(
                            "SELECT type, size, packed_info FROM MessageResourceDetail "
                            "WHERE message_id=?", (info[0],)).fetchall()
                        for dtype, size, packed in det:
                            cands = extract_filename_candidates(
                                parse_protobuf_strings(packed or b""))
                            title = ""
                            if app is not None:
                                # 文件类卡片：标题常是文件名，放进候选
                                title = app.get("title") or ""
                                if ("." in title and "/" not in title and "\\" not in title
                                        and 1 < len(title) < 200 and title not in cands):
                                    cands.append(title)
                            # 文件按真实文件名存在 msg/file/YYYY-MM/ 下；以磁盘为准挑
                            import datetime
                            mm = datetime.datetime.fromtimestamp(
                                ctime).strftime("%Y-%m")
                            fname, fpath = pick_attachment_file(self.base, mm, cands, title, size)
                            file_info = {
                                "name": fname, "size": size,
                                "resource_type": dtype, "path": fpath}
                            if fpath:
                                file_info.update(extract_file_text(fpath))
                            row["file"] = file_info
                            break
                except Exception:
                    pass
            if btype == 3:
                row["image"] = self._resolve_image(chatroom_id, img_meta, resource_md5)
            rows.append(row)
        return rows, None


def collect_local(config, since=0, *, classify=False, monitor_config=None, seen_file=None):
    """Return a local report; config is the monitor config dictionary.

    local_db.accounts is a list of {base_dir, key_file, label, self_id};
    local_db also accepts lookback and friends. seen_file=None disables legacy
    deduplication so the root monitor owns delivery state. Source DBs are read-only.
    """
    import contextlib
    import io
    local = config.get("local_db") or {}
    argv = ["--since", str(since), "--lookback", str(local.get("lookback", LOOKBACK_DEFAULT))]
    if seen_file is None:
        argv.append("--no-seen")
    else:
        argv.extend(["--seen-file", str(seen_file)])
    if local.get("friends"):
        argv.extend(["--friends", str(local["friends"])])
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        main(argv, config=config, classify=classify, monitor_config=monitor_config)
    return json.loads(stream.getvalue())


def main(argv=None, *, config=None, classify=False, monitor_config=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, required=True)
    ap.add_argument("--backfill", action="store_true",
                    help="回填模式：只输出 @用户或含关键词的消息（及 P0 群文件）")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--friends", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--classify", action="store_true", help="Apply shared monitor rules")
    ap.add_argument("--monitor-config", help="Shared monitor configuration JSON")
    ap.add_argument("--no-seen", action="store_true", help="Disable legacy seen reads and writes")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DEFAULT,
                    help="往回多看的秒数（防迟同步漏消息），默认 7200")
    ap.add_argument("--seen-file", default=SEEN_DEFAULT,
                    help="跨次去重文件（NAS 路径），默认 /tmp/wxmon-seen.json")
    args = ap.parse_args(argv)

    effective_since = args.since - max(0, args.lookback)
    seen = {} if args.no_seen else load_seen(args.seen_file)
    seen_now = int(time.time())
    deduped_skipped = 0

    if config is None:
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = config
    local = cfg.get("local_db") or {}
    accounts = local.get("accounts") or [
        {"base_dir": str(MAIN_BASE), "key_file": KEY_FILE_MAIN, "label": "main", "self_id": MAIN_WXID},
        {"base_dir": str(SMALL_BASE), "key_file": KEY_FILE_SMALL, "label": "secondary", "self_id": SMALL_WXID},
    ]
    groups = cfg.get("groups", [])
    monitored = [g for g in groups if g.get("tier") in ("P0", "P1", "P2")]
    by_id = {g["id"]: g for g in monitored}
    key_people = {p.get("wxid") for p in cfg.get("key_people", []) if p.get("wxid")}

    # 好友显示名（wxid -> 备注/昵称），有就用，没有就用 wxid
    friend_names = {}
    if args.friends and os.path.exists(args.friends):
        import csv
        with open(args.friends, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                wx = (row.get("wxid") or "").strip()
                # 当前表头是“显示名/微信昵称”，旧表头是“备注/昵称”，都兼容
                nm = (row.get("显示名") or row.get("微信昵称")
                      or row.get("备注") or row.get("昵称") or row.get("name") or "").strip()
                if wx and nm:
                    friend_names[wx] = nm

    report = {"accounts": {}, "groups": {}, "private": {}, "new_watermark": args.since,
              "errors": [], "missing_tables": [],
              "lookback_seconds": args.lookback, "effective_since": effective_since,
              "deduped_skipped": 0}
    max_time = args.since
    seen_new = {}
    coverage_read = []
    coverage_missing = []
    private_total_set = set()
    private_new_set = set()

    with tempfile.TemporaryDirectory(prefix="wxmon-") as td:
        for account in accounts:
            base = Path(account["base_dir"])
            key_file = account["key_file"]
            label = account.get("label", "local")
            self_wxid = account.get("self_id", "")
            reader = AccountReader(base, key_file, td, label, self_wxid)
            ok, msg = reader.open()
            acct = {"ok": ok, "note": msg, "chatrooms_in_db": 0, "messages": 0}
            if not ok:
                report["accounts"][label] = acct
                report["errors"].append(f"{label}: {msg}")
                continue
            acct["chatrooms_in_db"] = len(reader.chatroom_names())
            for gid, g in by_id.items():
                rows, err = reader.read_group(gid, effective_since)
                if rows is None:
                    if err == "no table":
                        report["missing_tables"].append(
                            {"account": label, "group": gid, "name": g.get("name")})
                        coverage_missing.append(gid)
                    else:
                        report["errors"].append(f"{label} {gid}: {err}")
                    continue
                if gid not in coverage_read:
                    coverage_read.append(gid)
                kept = []
                for r in rows:
                    if r["create_time"] > max_time:
                        max_time = r["create_time"]
                    key = message_key(gid, r.get("server_id"), r["local_id"], r["create_time"])
                    seen_new[key] = r["create_time"]
                    if key in seen:
                        deduped_skipped += 1
                        continue
                    if args.backfill:
                        text = r["text"] or ""
                        # 先去掉容易误命中的他人群名片再匹配，避免误命中
                        probe = text
                        for _nick in EXCLUDE_NICKNAMES:
                            probe = probe.replace(_nick, "")
                        hit_mention = any(a in probe for a in USER_ALIASES)
                        hit_kw = any(k in text for k in KEYWORDS)
                        hit_file = r["file"] is not None and g.get("tier") == "P0"
                        if not (hit_mention or hit_kw or hit_file):
                            continue
                    r["sender_name"] = friend_names.get(r["sender_wxid"], "")
                    kept.append(r)
                if kept:
                    slot = report["groups"].setdefault(gid, {
                        "name": g.get("name"), "tier": g.get("tier"), "messages": []})
                    slot["messages"].extend(kept)
                    acct["messages"] += len(kept)
            # Preserve private-chat context, including outgoing messages.
            for wxid, _table in reader.private_contacts():
                private_total_set.add(wxid)
                rows, err = reader.read_group(wxid, effective_since, is_private=True)
                if rows is None:
                    continue
                kept = []
                for r in rows:
                    if r["create_time"] > max_time:
                        max_time = r["create_time"]
                    key = message_key(wxid, r.get("server_id"), r["local_id"], r["create_time"])
                    seen_new[key] = r["create_time"]
                    if key in seen:
                        deduped_skipped += 1
                        continue
                    if args.backfill:
                        # 私聊本身就是 1 对 1，不用再匹配 @；只留关键词或文件
                        text = r["text"] or ""
                        hit_kw = any(k in text for k in KEYWORDS)
                        hit_file = r["file"] is not None
                        if not (hit_kw or hit_file):
                            continue
                    r["sender_name"] = friend_names.get(r["sender_wxid"], "")
                    kept.append(r)
                if kept:
                    slot = report["private"].setdefault(wxid, {
                        "name": friend_names.get(wxid, wxid), "wxid": wxid, "messages": []})
                    slot["messages"].extend(kept)
                    acct["messages"] += len(kept)
                    private_new_set.add(wxid)
            report["accounts"][label] = acct

    # 跨号去重：同一条消息会同时出现在主号和小号库里，server_id 是服务端唯一标识
    seen_srv = set()
    for section in ("groups", "private"):
        for gid, slot in report.get(section, {}).items():
            uniq = []
            for r in slot["messages"]:
                key = (gid, r.get("server_id")) if r.get("server_id") else (
                    gid, r["local_id"], r["create_time"], r["sender_wxid"])
                if key in seen_srv:
                    continue
                seen_srv.add(key)
                uniq.append(r)
            slot["messages"] = uniq

    # 重要链接抓取（去重之后再做，避免两号重复抓；每轮最多抓 LINK_FETCH_BUDGET 个）
    fetch_budget = LINK_FETCH_BUDGET
    for section in ("groups", "private"):
        for gid, slot in report.get(section, {}).items():
            # 私聊按 P1 算：关键人发的链接要抓
            tier = slot.get("tier") or ("P1" if section == "private" else "")
            for r in slot["messages"]:
                if fetch_budget <= 0:
                    break
                app = r.get("app") or {}
                url = app.get("url") or ""
                if not url:
                    continue
                if not link_is_important(tier, r.get("sender_wxid"), key_people, app):
                    continue
                r["link_preview"] = fetch_link_preview(url)
                fetch_budget -= 1
            if fetch_budget <= 0:
                break
        if fetch_budget <= 0:
            break

    report["new_watermark"] = max_time
    report["deduped_skipped"] = deduped_skipped

    # 覆盖情况：一个群只要任一号能读到就算读到；两号都无表才算缺失
    read_gids = set(coverage_read)
    missing_gids = set(coverage_missing) - read_gids
    p0_missing = sorted({by_id[gid].get("name") for gid in missing_gids
                         if by_id.get(gid, {}).get("tier") == "P0" and by_id[gid].get("name")})
    report["coverage"] = {
        "read_count": len(read_gids),
        "missing_count": len(missing_gids),
        "total_monitored": len(monitored),
        "read": sorted({by_id[gid].get("name") for gid in read_gids
                        if by_id.get(gid, {}).get("name")}),
        "missing": sorted({by_id[gid].get("name") for gid in missing_gids
                           if by_id.get(gid, {}).get("name")}),
        "p0_missing": p0_missing,
    }
    report["private_coverage"] = {
        "contacts_with_tables": len(private_total_set),
        "contacts_with_new": len(private_new_set),
        "names_with_new": sorted({report["private"][w]["name"]
                                  for w in private_new_set if w in report.get("private", {})}),
    }

    # 写回 seen：本次读到的全部 key 都记进去，7 天以前的旧 key 清掉
    try:
        merged = dict(seen)
        merged.update(seen_new)
        cutoff = seen_now - SEEN_TTL_SECS
        merged = {k: v for k, v in merged.items() if v >= cutoff}
        if not args.no_seen:
            save_seen(args.seen_file, merged)
            report["seen_file"] = args.seen_file
            report["seen_keys"] = len(merged)
    except Exception as e:
        report["errors"].append(f"seen-file write failed: {e}")

    if args.classify or classify:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from monitor_engine import classify_message, load_config
        rule_path = args.monitor_config or monitor_config
        rules = load_config(rule_path) if rule_path else cfg
        for section in ("groups", "private"):
            for conversation_id, slot in report[section].items():
                conversation = {k: v for k, v in slot.items() if k != "messages"}
                conversation.update({"id": conversation_id, "kind": "group" if section == "groups" else "private"})
                for message in slot["messages"]:
                    normalized = dict(message, sender_id=message.get("sender_wxid", ""),
                                      type={1: "text", 3: "image", 43: "video", 49: "app", 10000: "system"}.get(message.get("type"), "unknown"))
                    message["classification"] = classify_message(normalized, conversation, rules)

    out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
    else:
        print(out)


if __name__ == "__main__":
    main()
