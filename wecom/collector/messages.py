"""Normalize locally verified WeCom message formats.

The decoder is intentionally conservative: anything it cannot prove is kept as
``unsupported`` with the raw payload preserved by the caller, instead of being
silently reported as an empty message.
"""
from __future__ import annotations

import base64
from pathlib import Path
import re
import sqlite3
from urllib.parse import unquote, urlsplit

from proto import fields, blobs, blob, integer, text, strings

URL_PATTERN = re.compile(r'https?://[^\s<>"\x00-\x1f\x7f-\uffff]+')
TRAILING = '.,;:!?)]}\'"`*_'
CONTENT_TYPE_NAMES = {
    0: 'text', 2: 'text', 13: 'link_card', 14: 'image', 15: 'file',
    36: 'file_card', 38: 'link_card', 40: 'call', 78: 'mini_program',
    4: 'merged_chat', 123: 'mixed', 561: 'app_notice', 1002: 'member_event',
    1011: 'document_edit', 1022: 'system_text', 2101: 'file_assist',
}


def normalize_url(raw):
    """Return a clean http(s) URL or an empty string."""
    value = str(raw or '').strip().replace('&amp;', '&')
    value = value.strip('<>"\'`*_').strip()
    if not value:
        return ''
    cut = next((index for index, char in enumerate(value) if ord(char) > 0x7f), len(value))
    value = value[:cut]
    while value and value[-1] in TRAILING:
        if value[-1] == ')' and value.count('(') >= value.count(')'):
            break
        if value[-1] == ']' and value.count('[') >= value.count(']'):
            break
        value = value[:-1]
    while value.endswith(')') and value.count(')') > value.count('('):
        value = value[:-1]
    while value.endswith(']') and value.count(']') > value.count('['):
        value = value[:-1]
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return ''
    except ValueError:
        return ''
    return value


def extract_urls(value):
    result, seen = [], set()
    for match in URL_PATTERN.findall(str(value or '')):
        url = normalize_url(match)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _merge_links(links, *values):
    seen = {item.get('url') for item in links}
    for value in values:
        for url in extract_urls(value):
            if url not in seen:
                seen.add(url)
                links.append({'url': url})
    return links


def _clean_link(url, title='', description=''):
    normalized = normalize_url(url)
    if not normalized:
        return None
    link = {'url': normalized}
    if title:
        link['title'] = title
    if description:
        link['description'] = description
    if normalized != str(url or '').strip():
        link['original_url'] = str(url or '').strip()
    return link


def decode_text(payload):
    wrappers = blobs(fields(payload), 1)
    if not wrappers:
        raise ValueError('Text message has no segments')
    visible = []
    for encoded in wrappers:
        wrapper = fields(encoded)
        mode = integer(wrapper, 1)
        body = fields(blob(wrapper, 2))
        if mode in (0, 3):
            visible.append(text(body, 1))
        elif mode == 5:
            mention = text(body, 2)
            visible.append(mention if mention.startswith('@') else '@' + mention)
        else:
            raise ValueError('Unsupported text segment mode: ' + str(mode))
    return ''.join(visible)


def plain_utf8(payload):
    """Return printable UTF-8 text, or an empty string for binary content."""
    try:
        value = payload.decode('utf-8')
    except UnicodeError:
        return ''
    if not value:
        return ''
    if all(char.isprintable() or char in '\r\n\t' for char in value):
        return value
    return ''


def _textish(payload):
    """Decode content that is either plain UTF-8 or a simple protobuf text."""
    value = plain_utf8(payload)
    if value:
        return value
    try:
        parsed = fields(payload)
    except ValueError:
        return ''
    for number in (1, 2, 3):
        try:
            candidate = text(parsed, number)
        except UnicodeError:
            candidate = ''
        if candidate:
            return candidate
    for path, candidate in strings(payload):
        if candidate.strip():
            return candidate
    return ''


def _link38(payload):
    parsed = fields(payload)
    link = _clean_link(text(parsed, 3), text(parsed, 1), text(parsed, 2))
    if link is None:
        raise ValueError('Link card has no URL')
    return link


def _link13(payload):
    parsed = fields(payload)
    link = _clean_link(text(parsed, 1), text(parsed, 3), text(parsed, 4))
    if link is None:
        for path, value in strings(payload):
            if value.startswith(('http://', 'https://')):
                link = _clean_link(value)
                if link is not None:
                    break
    if link is None:
        raise ValueError('Link card has no URL')
    return link


def _file_card_entries(payload, file_index, message_id):
    if file_index is not None and message_id is not None:
        entries = file_index.for_message(message_id)
        if entries:
            return entries
    entries = []
    for encoded in blobs(fields(payload), 1):
        item = fields(encoded)
        info = fields(blob(item, 103))
        name = text(info, 2)
        if not name:
            continue
        entries.append({
            'name': name,
            'size': integer(info, 4) or integer(info, 3),
            'md5': text(info, 10),
            'file_id': text(info, 1),
            'source': 'payload',
        })
    return entries


def _attachment(media_index, *, kind, name, size, digest, file_id='', local_extra=b''):
    if media_index is None:
        return {'kind': kind, 'name': name, 'size': size, 'md5': digest,
                'status': 'pending_download', 'reason': 'no_media_index'}
    return media_index.resolve_metadata(
        kind=kind, name=name, size=size, digest=digest,
        file_id=file_id, local_extra=local_extra,
    )


def _inner_blob(item, *numbers):
    for number in numbers:
        value = blob(item, number)
        if value:
            return value
    return b''


def _decode_merged_chat(payload, local_extra, media_index):
    """Decode WeCom content_type=4 (a forwarded chat record)."""
    result = {'kind': 'merged', 'text': '', 'links': [], 'attachments': [],
              'unsupported': False}
    root = fields(payload)
    title = text(root, 2) or '群聊'
    lines = ['[聊天记录] ' + title]
    for encoded in blobs(root, 1):
        item = fields(encoded)
        content_type = integer(item, 5)
        sender = text(item, 11) or ''
        body = ''
        if content_type in (0, 2):
            value = _inner_blob(item, 101, 21)
            try:
                body = decode_text(value) if value else ''
            except (ValueError, UnicodeError):
                body = _textish(value) if value else ''
        elif content_type == 13:
            value = _inner_blob(item, 105, 103)
            try:
                card = _link13(value)
            except (ValueError, UnicodeError):
                card = None
            if card:
                result['links'].append(card)
                body = '\n'.join(value for value in (card.get('title'), card.get('description')) if value)
        elif content_type in (14, 15, 36):
            value = _inner_blob(item, 103, 105, 101)
            kind = 'image' if content_type == 14 else 'file'
            inner_name, inner_size, inner_md5 = '', 0, ''
            if value:
                try:
                    inner = fields(value)
                    inner_name = text(inner, 2)
                    inner_size = integer(inner, 4)
                    inner_md5 = text(inner, 10)
                except (ValueError, UnicodeError):
                    pass
            attachment = _attachment(media_index, kind=kind, name=inner_name,
                                     size=inner_size, digest=inner_md5,
                                     local_extra=local_extra)
            if value and media_index is not None:
                try:
                    attachment = media_index.resolve(kind, value, local_extra)
                except (ValueError, OSError, UnicodeError):
                    pass
            if attachment.get('name'):
                body = ('[图片] ' if kind == 'image' else '[文件] ') + str(attachment['name'])
            else:
                body = '[图片]' if kind == 'image' else '[文件]'
            result['attachments'].append(attachment)
        elif content_type == 4:
            value = _inner_blob(item, 101, 103, 105, 17)
            nested = _decode_merged_chat(value, local_extra, media_index)
            body = nested.get('text', '')
            result['links'].extend(nested.get('links', []))
            result['attachments'].extend(nested.get('attachments', []))
        else:
            value = _inner_blob(item, 101, 103, 105, 17)
            body = _textish(value) if value else ''
        if not body:
            body = '[空内容]'
        lines.append((sender + ': ' if sender else '') + body)
    if len(lines) == 1:
        result['unsupported'] = True
    result['text'] = '\n'.join(lines)
    return result


def decode_payload(content_type, payload, local_extra, media_index, *, file_index=None, message_id=None):
    result = {'kind': 'other', 'text': '', 'links': [], 'attachments': [], 'unsupported': False}
    if content_type in (0, 2):
        result['kind'] = 'text'
        result['text'] = decode_text(payload)
        _merge_links(result['links'], result['text'])
    elif content_type == 38:
        result['kind'] = 'link'
        card = _link38(payload)
        result['links'] = [card]
        result['text'] = '\n'.join(value for value in (card.get('title'), card.get('description')) if value)
    elif content_type in (14, 15):
        result['kind'] = 'image' if content_type == 14 else 'file'
        result['attachments'] = [media_index.resolve(result['kind'], payload, local_extra)]
    elif content_type == 4:
        result = _decode_merged_chat(payload, local_extra, media_index)
    elif content_type == 123:
        result['kind'] = 'mixed'
        texts = []
        for encoded_part in blobs(fields(payload), 1):
            part = fields(encoded_part)
            subtype = integer(part, 5)
            if subtype in (0, 2):
                value = decode_text(blob(part, 101))
                texts.append(value)
                _merge_links(result['links'], value)
            elif subtype in (14, 15):
                kind = 'image' if subtype == 14 else 'file'
                encoded_media = blob(part, 103 if subtype == 14 else 104)
                if not encoded_media:
                    raise ValueError('Mixed media message has no verified media payload')
                result['attachments'].append(media_index.resolve(kind, encoded_media, local_extra))
            elif subtype == 38:
                cards = []
                for number, values in part.items():
                    if number < 100:
                        continue
                    for wire_type, value in values:
                        if wire_type != 2:
                            continue
                        try:
                            card = _link38(value)
                        except (ValueError, UnicodeError):
                            continue
                        if card['url'].startswith(('http://', 'https://')):
                            cards.append(card)
                if len(cards) != 1:
                    raise ValueError('Mixed link card union is not uniquely decodable')
                result['links'].extend(cards)
                texts.extend(value for value in (cards[0].get('title'), cards[0].get('description')) if value)
        result['text'] = '\n'.join(texts)
    elif content_type == 13:
        result['kind'] = 'link'
        card = _link13(payload)
        result['links'] = [card]
        result['text'] = '\n'.join(value for value in (card.get('title'), card.get('description')) if value)
        _merge_links(result['links'], result['text'])
    elif content_type == 36:
        result['kind'] = 'file'
        entries = _file_card_entries(payload, file_index, message_id)
        for entry in entries:
            result['attachments'].append(_attachment(
                media_index, kind='file', name=entry.get('name', 'file'),
                size=entry.get('size', 0), digest=entry.get('md5', ''),
                file_id=entry.get('file_id', ''), local_extra=local_extra,
            ))
        if not entries:
            result['unsupported'] = True
    elif content_type == 78:
        result['kind'] = 'link'
        info = fields(blob(fields(payload), 107))
        title = text(info, 7)
        source = text(info, 10)
        path = text(info, 3)
        decoded = unquote(path)
        urls = extract_urls(decoded.replace('\\/', '/'))
        if not urls:
            match = re.search(r'(?:checkLink|url)=([^&]+)', decoded)
            if match:
                candidate = normalize_url(unquote(match.group(1)))
                if candidate:
                    urls = [candidate]
        result['links'] = []
        for url in urls:
            link = _clean_link(url, title)
            if link is not None:
                result['links'].append(link)
        result['text'] = '\n'.join(value for value in (title, source) if value)
        if not result['links'] and not result['text']:
            result['unsupported'] = True
    elif content_type == 561:
        parsed = fields(payload)
        title = text(parsed, 1)
        body = text(parsed, 2).replace('\\/', '/')
        template = text(parsed, 4)
        result['kind'] = 'app'
        if template:
            link = _clean_link(template, title)
            if link:
                result['links'].append(link)
        _merge_links(result['links'], body)
        result['text'] = title
        if not result['links'] and not title:
            result['unsupported'] = True
    elif content_type in (1011, 1022):
        result['kind'] = 'system'
        result['text'] = _textish(payload)
        _merge_links(result['links'], result['text'])
        if not result['text']:
            result['unsupported'] = True
    elif content_type == 40:
        parsed = fields(payload)
        result['kind'] = 'call'
        result['text'] = text(parsed, 3)
        if not result['text']:
            result['unsupported'] = True
    elif content_type == 1002:
        result['kind'] = 'system'
        result['unsupported'] = True
        value = plain_utf8(payload)
        member_ids = [item for item in str(value or '').split(';') if item.isdigit()]
        if member_ids:
            result['member_ids'] = member_ids
            result['member_id'] = member_ids[0]
    elif content_type == 2101:
        result['kind'] = 'system'
        result['unsupported'] = True
    else:
        value = _textish(payload)
        if value:
            result['text'] = value
            result['kind'] = 'text'
            _merge_links(result['links'], value)
        else:
            candidates = [value for path, value in strings(payload)]
            if candidates:
                result['text'] = '\n'.join(dict.fromkeys(candidates))
        result['unsupported'] = True
    _merge_links(result['links'], result['text'])
    return result


def _open(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _names(snapshot_dir):
    users, conversations = {}, {}
    user_db = Path(snapshot_dir) / 'user.db'
    if user_db.is_file():
        db = _open(user_db)
        try:
            for row in db.execute('SELECT id,name,real_name FROM user_table'):
                users[str(row['id'])] = row['real_name'] or row['name'] or ''
        finally:
            db.close()
    session_db = Path(snapshot_dir) / 'session.db'
    if session_db.is_file():
        db = _open(session_db)
        try:
            for row in db.execute('SELECT id,name,roomname_remark FROM conversation_table'):
                conversations[str(row['id'])] = row['roomname_remark'] or row['name'] or ''
        finally:
            db.close()
    return users, conversations


def conversation_meta(conversation_id, conversation_name, users):
    """Return group/direct/official/app metadata without relying on ID guesses."""
    conversation_id = str(conversation_id or '')
    prefix = conversation_id.split(':', 1)[0] if ':' in conversation_id else conversation_id
    peer_id = peer_name = ''
    if prefix == 'R':
        conversation_type = 'group'
    elif prefix == 'S':
        conversation_type = 'direct'
        tail = conversation_id.split(':', 1)[1] if ':' in conversation_id else ''
        peer_id = tail.split('_', 1)[0]
        peer_name = users.get(peer_id, '')
        if not conversation_name:
            conversation_name = peer_name
    elif prefix == 'O':
        conversation_type = 'official'
    elif prefix == 'M':
        conversation_type = 'app'
    elif conversation_id == 'FILEASSIST':
        conversation_type = 'self'
    else:
        conversation_type = 'unknown'
    return {
        'conversation_type': conversation_type,
        'is_group': conversation_type == 'group',
        'peer_id': peer_id,
        'peer_name': peer_name,
        'conversation_name': conversation_name,
    }


def _identity(table, row):
    conversation = str(row['conversation_id'])
    server_id = str(row['server_id'] or 0)
    sequence = str(row['sequence'] or 0)
    message_id = str(row['message_id'])
    if server_id not in ('0', ''):
        return conversation + '|server|' + server_id
    if sequence not in ('0', ''):
        return conversation + '|sequence|' + sequence
    return conversation + '|local|' + table + '|' + message_id


def load_records(snapshot_dir, source, media_index, file_index=None, *, message_ids=None, after_ids=None):
    """Load records, optionally only new rows or specific message ids."""
    users, conversations = _names(snapshot_dir)
    connection = _open(Path(snapshot_dir) / 'message.db')
    records = {}
    errors = []
    maximum = {}
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('message_table', 'message_small_table', 'kf_message_tableV1'):
            if table not in tables:
                continue
            clauses, parameters = [], []
            if message_ids is not None:
                values = [int(value) for value in message_ids]
                if not values:
                    continue
                clauses.append('message_id IN (%s)' % ','.join('?' * len(values)))
                parameters.extend(values)
            elif after_ids is not None and table in after_ids:
                clauses.append('message_id > ?')
                parameters.append(int(after_ids[table]))
            where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            query = 'SELECT * FROM ' + table + where + ' ORDER BY message_id'
            for row in connection.execute(query, parameters):
                content_type = int(row['content_type'])
                message_id = str(row['message_id'])
                identity = _identity(table, row)
                if identity in records:
                    continue
                raw = {'table': table}
                for column in ('content', 'extra_content', 'local_extra_content'):
                    value = row[column] or b''
                    raw[column] = base64.b64encode(value).decode()
                try:
                    decoded = decode_payload(
                        content_type, row['content'] or b'', row['local_extra_content'] or b'',
                        media_index, file_index=file_index, message_id=int(row['message_id']),
                    )
                except (ValueError, UnicodeError, OSError) as error:
                    decoded = {'kind': 'other', 'text': '', 'links': [], 'attachments': [], 'unsupported': True}
                    raw['decode_error'] = str(error)
                    errors.append({'id': identity, 'content_type': content_type, 'error_type': type(error).__name__})
                meta = conversation_meta(str(row['conversation_id']), conversations.get(str(row['conversation_id']), ''), users)
                if decoded.get('member_ids'):
                    decoded['member_names'] = [users.get(str(item), '') for item in decoded['member_ids']]
                    if not decoded.get('member_id') and decoded['member_ids']:
                        decoded['member_id'] = decoded['member_ids'][0]
                if decoded.get('member_id'):
                    decoded['member_name'] = users.get(str(decoded['member_id']), '')
                records[identity] = {
                    'source': source, 'id': identity, 'conversation_id': str(row['conversation_id']),
                    'conversation_name': meta['conversation_name'],
                    'conversation_type': meta['conversation_type'], 'is_group': meta['is_group'],
                    'peer_id': meta['peer_id'], 'peer_name': meta['peer_name'],
                    'message_id': message_id, 'server_id': str(row['server_id'] or 0),
                    'sequence': str(row['sequence'] or 0),
                    'sender_id': str(row['sender_id']), 'sender_name': users.get(str(row['sender_id']), ''),
                    'sent_at': int(row['send_time']), 'content_type': content_type,
                    'content_type_name': CONTENT_TYPE_NAMES.get(content_type, 'type_%d' % content_type),
                    **decoded, 'raw': raw,
                }
                maximum[table] = max(maximum.get(table, 0), int(row['message_id']))
    finally:
        connection.close()
    return list(records.values()), errors, maximum


class FileIndex:
    """Metadata index from file.db; the file card payload is often lossy."""

    def __init__(self, path):
        from collections import defaultdict
        self.by_message = defaultdict(list)
        path = Path(path)
        if not path.is_file():
            return
        connection = _open(path)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'file_table4' not in tables:
                return
            for row in connection.execute(
                'SELECT message_id,file_index,name,size,md5,server_id FROM file_table4 ORDER BY message_id,file_index'
            ):
                try:
                    message_id = int(row['message_id'])
                except (TypeError, ValueError):
                    continue
                name = str(row['name'] or '')
                if not name:
                    continue
                self.by_message[message_id].append({
                    'name': name,
                    'size': int(row['size'] or 0),
                    'md5': str(row['md5'] or ''),
                    'file_index': int(row['file_index'] or 0),
                    'server_id': str(row['server_id'] or ''),
                })
        finally:
            connection.close()

    def for_message(self, message_id):
        try:
            return list(self.by_message.get(int(message_id), ()))
        except (TypeError, ValueError):
            return []
