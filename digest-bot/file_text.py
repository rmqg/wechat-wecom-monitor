"""附件正文提取（供 digest.py 调用）。

独立模块，只依赖 stdlib + pypdf / python-docx / openpyxl（都在 /deps 里）。
digest.py 只需 import file_text，并在 _message_rows 里传 file_resolver。

设计要点：
- 只读：不写库、不发消息、不调模型。
- 找不到文件 / 提不出正文 / 库缺失，一律返回 None，不抛错、不编造。
- 正文截断 4000 字；>50MB 的文件不读。
- 文件定位沿用每小时检查脚本已验证的办法：
  message_resource.db 的 ChatName2Id(rowid=chat_id) +
  MessageResourceInfo(chat_id, message_local_id) -> message_id +
  MessageResourceDetail(type, size, packed_info) 里抠文件名，
  按 <账号根目录>/msg/file/YYYY-MM/<文件名> 校验磁盘存在。
"""
import collections
import datetime as dt
import json
import os
import re
import signal
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

FILE_TEXT_CHARS = 4000
FILE_MAX_BYTES = 50 * 1024 * 1024
# Bound attachment processing to avoid exhausting container resources; a SIGKILL
# leaves decrypted databases behind, so one attachment must not be able to use up either of them.
FILE_MAX_UNPACKED_BYTES = 256 * 1024 * 1024
FILE_EXTRACT_SECONDS = 60
STATS = collections.Counter()
MISSING_LIBS = set()


def _lib_available(name):
    try:
        __import__(name)
        return True
    except Exception as exc:
        # Report dependencies that cannot be imported from the mounted directory.
        MISSING_LIBS.add(f'{name}: {type(exc).__name__}')
        return False


def clip_file_text(text):
    text = (text or "").strip()
    if len(text) > FILE_TEXT_CHARS:
        return text[:FILE_TEXT_CHARS] + "……（正文已截断）"
    return text


def note_setup_failure(exc):
    STATS['setup_failed'] += 1
    STATS['setup_failed_' + type(exc).__name__] += 1


def write_status(runtime_dir):
    """Counts only, no names or text: lets a silent loss of attachment text be noticed after the run."""
    try:
        status = {'at': int(time.time()), 'counts': dict(STATS), 'missing_libs': sorted(MISSING_LIBS)}
        path = Path(runtime_dir) / 'file-text-status.json'
        temporary = path.with_name(path.name + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(status, stream, ensure_ascii=False)
        os.replace(temporary, path)
    except OSError:
        pass


def _zip_too_large(path):
    try:
        with zipfile.ZipFile(path) as archive:
            return sum(item.file_size for item in archive.infolist()) > FILE_MAX_UNPACKED_BYTES
    except (OSError, zipfile.BadZipFile):
        return False  # the reader below reports its own failure


class _Deadline:
    """SIGALRM limit for one extraction; digest calls this from its main thread only."""

    def __enter__(self):
        self.active = threading.current_thread() is threading.main_thread()
        if self.active:
            def expired(_signum, _frame):
                STATS['extract_timeout'] += 1
                raise TimeoutError('attachment extraction deadline')
            self.previous = signal.signal(signal.SIGALRM, expired)
            signal.alarm(FILE_EXTRACT_SECONDS)
        return self

    def __exit__(self, *_):
        if self.active:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self.previous)
        return False


def extract_file_text(path):
    with _Deadline():
        return _extract_file_text(path)


def _extract_file_text(path):
    """只读提取文件正文（前 FILE_TEXT_CHARS 字）；提不出返回 None。"""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        size = p.stat().st_size
    except OSError:
        return None
    if size > FILE_MAX_BYTES:
        return None
    if ext in (".txt", ".md", ".csv", ".log", ".json", ".conf", ".ini", ".yaml", ".yml",
               ".xml", ".html", ".htm"):
        try:
            # 4 bytes is the longest UTF-8 character; no need to load the rest of the file.
            with p.open("rb") as stream:
                return clip_file_text(stream.read(FILE_TEXT_CHARS * 4 + 4).decode("utf-8", errors="replace"))
        except Exception:
            return None
    if ext == ".pdf":
        lib = "pypdf" if _lib_available("pypdf") else ("PyPDF2" if _lib_available("PyPDF2") else None)
        if lib is None:
            return None
        try:
            mod = __import__(lib)
            reader = mod.PdfReader(str(p))
            parts, total = [], 0
            for page in reader.pages:
                piece = page.extract_text() or ""
                parts.append(piece)
                total += len(piece)
                if total > FILE_TEXT_CHARS:
                    break
            return clip_file_text("\n".join(parts))
        except Exception:
            return None
    if ext == ".docx":
        if not _lib_available("docx"):
            return None
        if _zip_too_large(p):
            STATS['too_large_unpacked'] += 1
            return None
        try:
            import docx as _docx
            doc = _docx.Document(str(p))
            return clip_file_text("\n".join(paragraph.text for paragraph in doc.paragraphs))
        except Exception:
            return None
    if ext == ".xlsx":
        if not _lib_available("openpyxl"):
            return None
        if _zip_too_large(p):
            STATS['too_large_unpacked'] += 1
            return None
        try:
            import openpyxl as _oxl
            wb = _oxl.load_workbook(str(p), read_only=True, data_only=True)
            try:
                parts, total = [], 0
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        cells = [str(c) for c in row if c is not None and str(c).strip()]
                        if cells:
                            parts.append(" ".join(cells))
                            total += len(parts[-1]) + 1
                        if total > FILE_TEXT_CHARS:
                            return clip_file_text("\n".join(parts))
                return clip_file_text("\n".join(parts))
            finally:
                wb.close()
        except Exception:
            return None
    return None


def protobuf_strings(blob, _depth=0):
    """从 protobuf packed_info 里抠出所有 length-delimited 字符串。

    注意：微信的 packed_info 是嵌套结构——外层 field 1 的 payload 本身又是一段
    protobuf（内层才是文件名等字段）。只解析一层会把内层整个 blob 当成一个字符串，
    导致文件名前面粘上内层字段的长度字节（文件名 ≥32 字节时该字节是可打印字符，
    抠出来就带上 '@'、'6'、'+' 这类前缀，磁盘上找不到文件）。
    所以对每个 length-delimited payload 都递归再解析一层（最多 4 层），
    内外层字符串都收进结果；调用方用磁盘存在性校验兜底，多余候选无害。
    """
    out = []
    i = 0
    n = len(blob)
    while i < n:
        try:
            tag = 0
            shift = 0
            while i < n:
                b = blob[i]
                i += 1
                tag |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            wire = tag & 0x07
            if wire == 2:
                ln = 0
                shift = 0
                while i < n:
                    b = blob[i]
                    i += 1
                    ln |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                s = blob[i:i + ln]
                i += ln
                if _depth < 4:
                    out.extend(protobuf_strings(s, _depth + 1))
                try:
                    out.append(s.decode("utf-8"))
                except UnicodeDecodeError:
                    pass
            elif wire == 0:
                while i < n and (blob[i] & 0x80):
                    i += 1
                i += 1
            elif wire == 5:
                i += 4
            elif wire == 1:
                i += 8
            else:
                break
        except Exception:
            break
    return out


def filename_candidates(strings):
    """packed_info 字符串里所有像文件名的片段（去控制字符、去重、保序）。"""
    out = []
    for s in strings:
        for seg in re.split(r"[\x00-\x1f\x7f]+", s):
            seg = seg.strip()
            if ("." in seg and "/" not in seg and "\\" not in seg
                    and 1 < len(seg) < 200 and not seg.isdigit()
                    and seg not in out):
                out.append(seg)
    return out


class FileResolver:
    """附件定位：持解密后的 message_resource.db 连接 + 微信账号根目录（只读）。"""

    def __init__(self, resource_conn, base_dir):
        self.conn = resource_conn
        self.base = Path(base_dir)
        self.chat_id_by_name = {}
        try:
            for rowid, name in self.conn.execute("SELECT rowid, user_name FROM ChatName2Id"):
                self.chat_id_by_name[name] = rowid
        except Exception:
            pass

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def resolve_text(self, group_id, local_id, create_time, title):
        """返回附件正文预览（str）或 None；找不到文件/提不出正文都返回 None。"""
        if self.conn is None or not create_time:
            return None
        chat_id = self.chat_id_by_name.get(group_id)
        if chat_id is None:
            return None
        try:
            info = self.conn.execute(
                "SELECT message_id, packed_info FROM MessageResourceInfo "
                "WHERE chat_id=? AND message_local_id=?",
                (chat_id, local_id)).fetchone()
            if not info:
                return None
            rows = self.conn.execute(
                "SELECT type, size, packed_info FROM MessageResourceDetail "
                "WHERE message_id=?", (info[0],)).fetchall()
        except Exception:
            return None
        month = dt.datetime.fromtimestamp(create_time).strftime("%Y-%m")
        for _dtype, size, packed in rows:
            cands = filename_candidates(protobuf_strings(packed or b""))
            if (title and "." in title and "/" not in title and "\\" not in title
                    and 1 < len(title) < 200 and title not in cands):
                cands.append(title)
            for name in cands:
                cand = self.base / "msg" / "file" / month / name
                if not cand.is_file():
                    continue
                # The folder is shared by every chat; verify the recorded size to
                # avoid selecting a same-named attachment from another chat.
                if size and cand.stat().st_size != size:
                    STATS['size_mismatch'] += 1
                    continue
                STATS['file_found'] += 1
                text = extract_file_text(cand)
                STATS['extracted' if text else 'no_text'] += 1
                return text
        return None


def build_file_resolver(decrypted_resource_db, base_dir):
    """用已解密的 message_resource.db 建 resolver；失败返回 None（不致命）。"""
    try:
        conn = sqlite3.connect("file:" + str(decrypted_resource_db) + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        base = Path(base_dir)
        if not (base / "msg" / "file").exists():
            conn.close()
            return None
        return FileResolver(conn, base)
    except Exception:
        return None
