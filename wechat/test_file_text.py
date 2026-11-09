#!/usr/bin/env python3
"""file_text.py 的离线单元测试（VM 上跑，不碰 NAS）。

造一个假的微信账号根目录 + 假 message_resource.db，验证：
1. extract_file_text 能从 PDF/DOCX/XLSX/TXT 提正文；
2. protobuf_strings + filename_candidates 能从 packed_info 抠文件名；
3. FileResolver.resolve_text 能按 (chat_id, local_id) 定位文件并提正文；
4. 找不到文件 / 坏文件 / 不支持的后缀返回 None，不抛错。
"""
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import file_text as ft


def _len_delim(s):
    b = s.encode("utf-8")
    return b"\x0a" + bytes([len(b)]) + b  # field 1, wire 2


def make_packed(name):
    # 模拟 packed_info：field 1 = 文件名，field 2 = 一串杂项
    return _len_delim(name) + _len_delim("weixin://file/" + name) + b"\x10\x01"


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ft-test-"))
    base = tmp / "acct_test_123"
    (base / "msg" / "file" / "2026-10").mkdir(parents=True)

    # --- 造文件 ---
    pdf_path = base / "msg" / "file" / "2026-10" / "测试文档.pdf"
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, StreamObject, DictionaryObject, DecodedStreamObject
    w = PdfWriter()
    page = w.add_blank_page(200, 200)
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 20 180 Td (Hello PDF Zhengwen 12345) Tj ET")
    content.update({NameObject("/Type"): NameObject("/XObject")})
    font = DictionaryObject()
    font.update({NameObject("/Type"): NameObject("/Font"),
                 NameObject("/Subtype"): NameObject("/Type1"),
                 NameObject("/BaseFont"): NameObject("/Helvetica")})
    resources = DictionaryObject()
    resources.update({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = w._add_object(content)
    with open(pdf_path, "wb") as f:
        w.write(f)

    docx_path = base / "msg" / "file" / "2026-10" / "通知.docx"
    import docx as _docx
    d = _docx.Document()
    d.add_paragraph("职业规划大赛通知正文第一行")
    d.add_paragraph("第二行内容 ABC")
    d.save(str(docx_path))

    xlsx_path = base / "msg" / "file" / "2026-10" / "汇总表.xlsx"
    import openpyxl as _oxl
    wb = _oxl.Workbook()
    ws = wb.active
    ws.append(["姓名", "班级"])
    ws.append(["张三", "示例1班"])
    wb.save(str(xlsx_path))

    txt_path = base / "msg" / "file" / "2026-10" / "notes.txt"
    txt_path.write_text("纯文本正文 hello world", encoding="utf-8")

    bad_path = base / "msg" / "file" / "2026-10" / "坏文件.pdf"
    bad_path.write_bytes(b"not a pdf at all")

    # --- 造假 resource 库 ---
    db_path = tmp / "message_resource.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ChatName2Id (user_name TEXT)")
    conn.execute("CREATE TABLE MessageResourceInfo (chat_id INTEGER, message_local_id INTEGER, message_id INTEGER, packed_info BLOB)")
    conn.execute("CREATE TABLE MessageResourceDetail (message_id INTEGER, type INTEGER, size INTEGER, packed_info BLOB)")
    conn.execute("INSERT INTO ChatName2Id (rowid, user_name) VALUES (7, '12345678901@chatroom')")
    cases = [
        (101, "测试文档.pdf", pdf_path),
        (102, "通知.docx", docx_path),
        (103, "汇总表.xlsx", xlsx_path),
        (104, "notes.txt", txt_path),
        (105, "坏文件.pdf", bad_path),
        (106, "不存在的文件.pdf", None),  # 磁盘上没有
    ]
    for local_id, name, _p in cases:
        conn.execute("INSERT INTO MessageResourceInfo VALUES (7, ?, ?, ?)",
                     (local_id, 1000 + local_id, make_packed(name)))
        conn.execute("INSERT INTO MessageResourceDetail VALUES (?, 6, 123, ?)",
                     (1000 + local_id, make_packed(name)))
    conn.commit()
    conn.close()

    # --- 测 extract_file_text ---
    pdf_t = ft.extract_file_text(pdf_path)
    assert pdf_t and "Hello PDF" in pdf_t, f"PDF failed: {pdf_t!r}"
    print("PDF ok:", pdf_t[:60].replace("\n", " / "))
    docx_t = ft.extract_file_text(docx_path)
    assert docx_t and "职业规划大赛通知正文第一行" in docx_t, f"DOCX failed: {docx_t!r}"
    print("DOCX ok:", docx_t[:60].replace("\n", " / "))
    xlsx_t = ft.extract_file_text(xlsx_path)
    assert xlsx_t and "张三" in xlsx_t and "示例1班" in xlsx_t, f"XLSX failed: {xlsx_t!r}"
    print("XLSX ok:", xlsx_t[:60].replace("\n", " / "))
    txt_t = ft.extract_file_text(txt_path)
    assert txt_t and "纯文本正文" in txt_t, f"TXT failed: {txt_t!r}"
    print("TXT ok:", txt_t)
    assert ft.extract_file_text(bad_path) is None or ft.extract_file_text(bad_path) == "", "坏 PDF 应返回 None/空"
    print("坏 PDF ok: None/空")
    assert ft.extract_file_text(base / "msg" / "file" / "2026-10" / "nope.zip") is None
    print("不支持后缀 ok: None")

    # --- 测 protobuf + 候选名 ---
    strs = ft.protobuf_strings(make_packed("测试文档.pdf"))
    assert "测试文档.pdf" in strs, f"protobuf failed: {strs}"
    cands = ft.filename_candidates(strs)
    assert "测试文档.pdf" in cands, f"cands failed: {cands}"
    print("protobuf/cands ok:", cands)

    # --- 嵌套 protobuf（真实 packed_info 的结构）：外层 field 1 包一段内层 protobuf ---
    def _varint(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    def _field(num, payload):
        return bytes([(num << 3) | 2]) + _varint(len(payload)) + payload

    long_name = "附件3_某某大学2025-2026学年优良学风班申请表.xlsx"  # 64 字节，长度字节 '@' 是可打印字符
    assert len(long_name.encode()) == 64
    inner = _field(1, long_name.encode()) + _field(2, long_name.encode())
    nested = _field(1, inner)
    nstrs = ft.protobuf_strings(nested)
    assert long_name in nstrs, f"nested protobuf failed: {nstrs}"
    ncands = ft.filename_candidates(nstrs)
    # 干净名必须在场且排第一（递归先收内层字符串）；外层 blob 解出的带 '@' 脏候选
    # 也会留下来，但 resolver 会用磁盘存在性校验跳过它，无害。
    assert ncands[0] == long_name, f"nested cands order failed: {ncands}"
    print("nested protobuf ok:", ncands)

    # --- 测 FileResolver 全链 ---
    rconn = sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True)
    resolver = ft.FileResolver(rconn, base)
    assert len(resolver.chat_id_by_name) == 1 and resolver.chat_id_by_name.get("12345678901@chatroom") == 7
    t0 = 1791000000  # 2026-10-02 左右（月份 2026-10）
    r1 = resolver.resolve_text("12345678901@chatroom", 101, t0, "测试文档.pdf")
    assert r1 and "Hello PDF" in r1, f"resolver PDF failed: {r1!r}"
    print("resolver PDF ok")
    r2 = resolver.resolve_text("12345678901@chatroom", 102, t0, "通知.docx")
    assert r2 and "职业规划大赛" in r2
    print("resolver DOCX ok")
    r3 = resolver.resolve_text("12345678901@chatroom", 103, t0, "汇总表.xlsx")
    assert r3 and "张三" in r3
    print("resolver XLSX ok")
    r4 = resolver.resolve_text("12345678901@chatroom", 104, t0, "notes.txt")
    assert r4 and "纯文本正文" in r4
    print("resolver TXT ok")
    assert resolver.resolve_text("12345678901@chatroom", 105, t0, "坏文件.pdf") is None
    print("resolver 坏文件 ok: None")
    assert resolver.resolve_text("12345678901@chatroom", 106, t0, "不存在的文件.pdf") is None
    print("resolver 缺文件 ok: None")
    assert resolver.resolve_text("99999999@chatroom", 101, t0, "测试文档.pdf") is None
    print("resolver 未知群 ok: None")
    resolver.close()
    print("ALL PASS")
    return 0


def test_file_text_pipeline():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
