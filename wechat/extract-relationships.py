#!/usr/bin/env python3
"""Extract full group chat and contact relationships from decrypted WeChat databases."""
import sqlite3, json, os, re, hashlib
from pathlib import Path

BASE_DIR = Path("/path/to/wechat-private-web/decrypted_db")
OUT_DIR = Path("/path/to/wechat-private-web/analysis")
OUT_DIR.mkdir(parents=True, exist_ok=True)

contact_conn = sqlite3.connect((BASE_DIR / "contact/contact.db").resolve().as_uri() + "?mode=ro", uri=True)
session_conn = sqlite3.connect((BASE_DIR / "session/session.db").resolve().as_uri() + "?mode=ro", uri=True)
msg_conn = sqlite3.connect((BASE_DIR / "message/message_0.db").resolve().as_uri() + "?mode=ro", uri=True)

contact_cur = contact_conn.cursor()
session_cur = session_conn.cursor()
msg_cur = msg_conn.cursor()

def parse_ext_buffer_members(buf):
    if not buf:
        return {}
    pos = 0
    members = {}
    while pos < len(buf):
        tag_byte = buf[pos]
        wire_type = tag_byte & 7
        field_num = tag_byte >> 3
        pos += 1
        if wire_type == 2:
            length = 0
            shift = 0
            while True:
                b = buf[pos]
                pos += 1
                length |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            data = buf[pos : pos + length]
            pos += length
            if field_num == 1:
                m_pos = 0
                wxid = None
                alias = ""
                while m_pos < len(data):
                    m_tag = data[m_pos]
                    m_wire = m_tag & 7
                    m_fn = m_tag >> 3
                    m_pos += 1
                    if m_wire == 2:
                        m_len = 0
                        m_shift = 0
                        while True:
                            b = data[m_pos]
                            m_pos += 1
                            m_len |= (b & 0x7f) << m_shift
                            if not (b & 0x80):
                                break
                            m_shift += 7
                        s = data[m_pos : m_pos + m_len].decode("utf-8", errors="ignore")
                        m_pos += m_len
                        if m_fn == 1:
                            wxid = s
                        elif m_fn == 2:
                            alias = s
                    elif m_wire == 0:
                        while data[m_pos] & 0x80:
                            m_pos += 1
                        m_pos += 1
                    else:
                        break
                if wxid:
                    members[wxid] = alias
        elif wire_type == 0:
            while buf[pos] & 0x80:
                pos += 1
            pos += 1
        else:
            break
    return members

print("1. Indexing all contacts...")
contact_cur.execute("""
SELECT id, username, alias, remark, nick_name, description, flag, delete_flag
FROM contact
WHERE instr(username, "@chatroom") == 0
""")
contacts_map = {}
for row in contact_cur.fetchall():
    cid, uname, alias, remark, nick, desc, flag, dflag = row
    contacts_map[uname] = {
        "id": cid,
        "username": uname,
        "alias": alias or "",
        "remark": remark or "",
        "nick_name": nick or "",
        "description": desc or "",
        "is_friend": bool(flag & 1),
        "is_deleted": bool(dflag),
        "shared_groups": [],
        "group_aliases": {}
    }

print(f"Loaded {len(contacts_map)} contacts.")

print("2. Indexing group announcements...")
contact_cur.execute("""
SELECT username_, announcement_editor_, announcement_
FROM chat_room_info_detail
""")
announcements_map = {}
for row in contact_cur.fetchall():
    uname, editor, content = row
    if content:
        announcements_map[uname] = {
            "editor": editor or "",
            "announcement": content.strip()
        }

print("3. Indexing chatrooms and member rosters...")
contact_cur.execute("""
SELECT c.username, c.nick_name, c.remark, cr.id, cr.owner, cr.ext_buffer
FROM contact c
JOIN chat_room cr ON c.username = cr.username
WHERE instr(c.username, "@chatroom") > 0
""")
groups = []
group_rows = contact_cur.fetchall()

for row in group_rows:
    uname, nick, remark, room_id, owner, ext_buf = row
    ann = announcements_map.get(uname, {})
    member_aliases = parse_ext_buffer_members(ext_buf)
    
    # Also get members from chatroom_member table
    contact_cur.execute("SELECT member_id FROM chatroom_member WHERE room_id = ?", (room_id,))
    member_cids = [r[0] for r in contact_cur.fetchall()]
    
    # Collect member details
    members_list = []
    # Find usernames for member_cids
    for cid in member_cids:
        # We can find contact by cid
        pass
    
    # Process member_aliases
    sample_members = []
    for m_wxid, m_alias in member_aliases.items():
        c_info = contacts_map.get(m_wxid)
        friend_remark = c_info["remark"] if c_info else ""
        friend_nick = c_info["nick_name"] if c_info else ""
        if c_info:
            c_info["shared_groups"].append(nick or uname)
            if m_alias:
                c_info["group_aliases"][nick or uname] = m_alias
        sample_members.append({
            "wxid": m_wxid,
            "group_alias": m_alias,
            "friend_remark": friend_remark,
            "friend_nick": friend_nick
        })
    
    # Sample recent messages for group context
    msg_table = f"Msg_{hashlib.md5(uname.encode()).hexdigest()}"
    recent_msgs = []
    try:
        msg_cur.execute(f"""
        SELECT local_type, create_time, message_content
        FROM {msg_table}
        WHERE message_content IS NOT NULL AND message_content != 
        ORDER BY create_time DESC LIMIT 15
        """)
        for mrow in msg_cur.fetchall():
            m_type, m_time, m_cnt = mrow
            if m_type == 1 and isinstance(m_cnt, str): # text message
                # remove sender prefix
                clean_text = m_cnt.split(":\n", 1)[-1] if ":\n" in m_cnt else m_cnt
                if clean_text.strip():
                    recent_msgs.append(clean_text.strip().replace("\n", " ")[:120])
    except Exception:
        pass
    
    groups.append({
        "username": uname,
        "name": nick or remark or uname,
        "remark": remark or "",
        "owner": owner or "",
        "member_count": len(member_aliases) or len(member_cids),
        "announcement": ann.get("announcement", ""),
        "announcement_editor": ann.get("editor", ""),
        "members": sample_members,
        "recent_messages_sample": recent_msgs[:10]
    })

print(f"Processed {len(groups)} chatrooms.")

print("4. Filtering active / relevant contacts for profiling...")
relevant_contacts = []
for uname, c in contacts_map.items():
    if c["remark"] or c["is_friend"] or c["shared_groups"] or len(c["alias"]) > 0:
        # Check if there is private chat history
        msg_table = f"Msg_{hashlib.md5(uname.encode()).hexdigest()}"
        p_msgs = []
        try:
            msg_cur.execute(f"""
            SELECT create_time, message_content
            FROM {msg_table}
            WHERE message_content IS NOT NULL AND message_content != 
            ORDER BY create_time DESC LIMIT 8
            """)
            for mrow in msg_cur.fetchall():
                cnt = mrow[1]
                if isinstance(cnt, str) and cnt.strip():
                    p_msgs.append(cnt.strip().replace("\n", " ")[:100])
        except Exception:
            pass
        
        relevant_contacts.append({
            "wxid": c["username"],
            "alias": c["alias"],
            "remark": c["remark"],
            "nick_name": c["nick_name"],
            "description": c["description"],
            "is_friend": c["is_friend"],
            "shared_group_count": len(c["shared_groups"]),
            "shared_groups": c["shared_groups"][:8],
            "group_aliases": c["group_aliases"],
            "recent_private_messages": p_msgs
        })

relevant_contacts.sort(key=lambda x: (bool(x["remark"]), len(x["recent_private_messages"]) > 0, x["shared_group_count"]), reverse=True)

print(f"Identified {len(relevant_contacts)} relevant contacts with identity clues.")

output_data = {
    "total_groups": len(groups),
    "total_relevant_contacts": len(relevant_contacts),
    "groups": groups,
    "contacts": relevant_contacts
}

out_file = OUT_DIR / "wechat_relationship_raw.json"
with open(out_file, "w", encoding="utf-8") as f:
    json.dump(output_data, f, ensure_ascii=False, indent=2)

print(f"Saved complete relationship extraction to {out_file} ({out_file.stat().st_size / 1024 / 1024:.2f} MB)")
