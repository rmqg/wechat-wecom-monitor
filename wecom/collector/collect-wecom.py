#!/usr/bin/env python3
"""Local WeCom incremental collector; does not call official chat archive APIs."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
DEPS = HERE / '.deps'
if DEPS.is_dir() and str(DEPS) not in sys.path:
    sys.path.insert(0, str(DEPS))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from archive_store import ArchiveStore
from chat_export import export_chat_v1
import wxwork_crypto as crypto
from wxwork_keys import get_wxwork_pids, scan_keys

TABLES = ('message_table', 'message_small_table', 'kf_message_tableV1')


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configuration(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    root = path.parent
    for key, default in (
        ('keys_file', 'state/database-keys.private.json'),
        ('archive_file', 'archive/messages.sqlite3'),
        ('media_dir', 'archive/media'),
        ('state_dir', 'state'),
        ('export_dir', 'exports'),
        ('chat_export_dir', 'chat-export'),
    ):
        candidate = Path(value.get(key, default))
        value[key] = candidate if candidate.is_absolute() else root / candidate
    value['data_dir'] = Path(value['data_dir'])
    value['source'] = value.get('source', 'wecom-windows:' + value['data_dir'].parent.name)
    return value


@contextmanager
def exclusive_collection(path, wait_seconds=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        deadline = time.monotonic() + max(0.0, float(wait_seconds or 0))
        while True:
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def validated_keys(data_dir, keys_file, required):
    def load():
        return json.loads(keys_file.read_text(encoding='utf-8-sig')) if keys_file.is_file() else {}

    def select(keys, name):
        with (data_dir / name).open('rb') as stream:
            first = stream.read(4096)
        if crypto.is_plain_sqlite_page(first):
            return bytes(16)
        entry = keys.get(name)
        candidates = ([entry] if isinstance(entry, dict) else []) + [
            value for key, value in keys.items() if not key.startswith('_') and isinstance(value, dict)
        ]
        for item in candidates:
            try:
                key = bytes.fromhex(item['enc_key'])
            except (KeyError, TypeError, ValueError):
                continue
            if crypto.verify_wxsqlite3_aes128_key(key, first):
                return key
        return None

    keys = load()
    selected = {name: select(keys, name) for name in required}
    refreshed = None
    if any(key is None for key in selected.values()):
        refreshed = scan_keys(data_dir, keys_file)
        keys = load()
        selected = {name: select(keys, name) for name in required}
    missing = [name for name, key in selected.items() if key is None]
    if missing:
        raise RuntimeError('No validated database key for: ' + ', '.join(missing))
    return selected, keys, refreshed


class FileIndex:
    """Metadata index from file.db; the file card payload is often lossy."""

    def __init__(self, path):
        self.by_message = defaultdict(list)
        path = Path(path)
        if not path.is_file():
            return
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
        connection.row_factory = sqlite3.Row
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


def table_maxima(snapshot):
    path = Path(snapshot) / 'message.db'
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}
        for table in TABLES:
            if table in tables:
                result[table] = int(connection.execute('SELECT COALESCE(MAX(message_id), 0) FROM ' + table).fetchone()[0])
        return result
    finally:
        connection.close()


def load_watermark(state):
    path = Path(state) / 'watermark.json'
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    return {str(key): int(number) for key, number in value.get('tables', {}).items()}


def collect(config, *, full=False, no_events=False, wait_lock=0):
    from media import MediaIndex
    from messages import load_records
    from wxwork_snapshot import recover_snapshot

    state = config['state_dir']
    state.mkdir(parents=True, exist_ok=True)
    started = time.time()
    status = {
        'source': config['source'],
        'started_at': started,
        'stage': 'collecting',
        'mode': 'full' if full else 'incremental',
        'emit_updates': not no_events,
    }
    status_file = state / 'last-run.json'
    with exclusive_collection(state / 'collect.lock', wait_lock):
        try:
            processes = get_wxwork_pids()
            status['client_processes'] = len(processes)
            required = ('message.db', 'file.db', 'message_lookup.db', 'session.db', 'user.db')
            selected, key_entries, refreshed = validated_keys(config['data_dir'], config['keys_file'], required)
            status['key_refresh'] = refreshed
            with tempfile.TemporaryDirectory(prefix='snapshot-', dir=state) as temporary:
                snapshots = Path(temporary)
                recovery = {}
                for name, key in selected.items():
                    recovery[name] = recover_snapshot(config['data_dir'] / name, snapshots / name, key)
                known = [
                    bytes.fromhex(item['enc_key'])
                    for name, item in key_entries.items()
                    if not name.startswith('_') and isinstance(item, dict) and 'enc_key' in item
                ]
                for path in (config['data_dir'].parent / 'CacheMapping').glob('*.db'):
                    with path.open('rb') as stream:
                        first = stream.read(4096)
                    key = (
                        bytes(16)
                        if crypto.is_plain_sqlite_page(first)
                        else next((key for key in known if crypto.verify_wxsqlite3_aes128_key(key, first)), None)
                    )
                    if key is None:
                        raise RuntimeError('Cache mapping has no validated key')
                    name = 'cache-' + path.name
                    recovery[name] = recover_snapshot(path, snapshots / name, key)
                file_index = FileIndex(snapshots / 'file.db')
                media = MediaIndex(snapshots, config['data_dir'].parent, config['media_dir'])
                maxima = table_maxima(snapshots)
                watermark = None if full else load_watermark(state)
                if watermark and any(maxima.get(table, 0) < value for table, value in watermark.items()):
                    status['watermark_reset'] = True
                    watermark = None
                with ArchiveStore(config['archive_file']) as archive:
                    pending_ids = archive.pending_message_ids()
                    if watermark is None:
                        records, errors, loaded = load_records(snapshots, config['source'], media, file_index)
                        status['mode'] = 'full'
                    else:
                        records, errors, loaded = load_records(
                            snapshots, config['source'], media, file_index, after_ids=watermark
                        )
                        status['mode'] = 'incremental'
                    if pending_ids:
                        pending_records, pending_errors, _ = load_records(
                            snapshots, config['source'], media, file_index, message_ids=pending_ids
                        )
                        merged = {record['id']: record for record in records}
                        merged.update({record['id']: record for record in pending_records})
                        records = list(merged.values())
                        errors.extend(pending_errors)
                    baseline = archive.is_empty() and config.get('baseline_first_run', True)
                    outcome = archive.ingest(
                        records, observed_at=time.time(), baseline=baseline, emit_events=not no_events
                    )
                    new_watermark = {
                        table: max(value, maxima.get(table, 0)) for table, value in (watermark or {}).items()
                    }
                    for table, value in maxima.items():
                        new_watermark[table] = max(new_watermark.get(table, 0), value)
                    atomic_json(state / 'watermark.json', {'tables': new_watermark, 'updated_at': time.time()})
                    cursor_file = state / 'export-cursor.json'
                    cursor = json.loads(cursor_file.read_text())['event_id'] if cursor_file.exists() else 0
                    events = archive.events_since(cursor, limit=1_000_000)
                    if events:
                        config['export_dir'].mkdir(parents=True, exist_ok=True)
                        destination = config['export_dir'] / (
                            'events-%d-%d.jsonl' % (events[0]['event_id'], events[-1]['event_id'])
                        )
                        data = ''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in events)
                        descriptor, tempname = tempfile.mkstemp(prefix='.events-', dir=destination.parent)
                        try:
                            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                                stream.write(data)
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.replace(tempname, destination)
                        finally:
                            Path(tempname).unlink(missing_ok=True)
                        atomic_json(cursor_file, {'event_id': events[-1]['event_id']})
                    all_records = archive.records()
                    pending_after = archive.pending_message_ids()
                    total = archive.count_messages()
                    attachments = [attachment for record in records for attachment in record['attachments']]
                    if config.get('chat_export_dir'):
                        account_id = config['data_dir'].parent.name
                        status['chat_export'] = export_chat_v1(
                            all_records,
                            Path(config['chat_export_dir']),
                            account_id=account_id,
                        )
                    status.update(
                        stage='ok' if not errors else 'decode_errors',
                        completed_at=time.time(),
                        messages=total,
                        processed=len(records),
                        kinds=dict(Counter(record['kind'] for record in records)),
                        content_types=dict(Counter(record['content_type_name'] for record in records)),
                        links=sum(len(record['links']) for record in records),
                        attachments=dict(Counter(item['status'] for item in attachments)),
                        attachment_kinds=dict(Counter(item['kind'] for item in attachments)),
                        pending=len(pending_after),
                        pending_message_ids=pending_after,
                        decode_errors=errors,
                        incremental=outcome,
                        watermark=new_watermark,
                        snapshots=recovery,
                    )
                    if not records and not pending_ids:
                        status['stage'] = 'no_change'
                        status['processed'] = 0
            status['completed_at'] = status.get('completed_at', time.time())
            status['last_success_at'] = status['completed_at']
            status['duration'] = status['completed_at'] - started
            atomic_json(status_file, status)
            return status, 0 if status['stage'] in ('ok', 'no_change') else 2
        except Exception as error:
            status.update(
                stage='failed', completed_at=time.time(), error_type=type(error).__name__, error=str(error)
            )
            atomic_json(status_file, status)
            raise


def since_value(value):
    if value[-1:] in ('s', 'm', 'h', 'd'):
        return time.time() - float(value[:-1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[value[-1]]
    try:
        return float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('An ISO timestamp must include its timezone')
        return parsed.timestamp()


def verify_archive(config, hashes=False):
    from urllib.parse import urlsplit
    import hashlib

    with ArchiveStore(config['archive_file']) as store:
        records = store.records()
        last_event = store.last_event_id()
    kinds, content_types, conversation_types = Counter(), Counter(), Counter()
    unsupported_types = Counter()
    missing_conversation_name = missing_peer_name = 0
    link_count = bad_links = unsupported = 0
    ready = pending = missing = size_bad = hash_bad = 0
    problems = []
    for record in records:
        kinds[record.get('kind', '?')] += 1
        content_types[record.get('content_type_name', '?')] += 1
        conversation_types[record.get('conversation_type', '?')] += 1
        if record.get('unsupported'):
            unsupported += 1
            unsupported_types[record.get('content_type_name', '?')] += 1
        if not record.get('conversation_name'):
            missing_conversation_name += 1
        if record.get('conversation_type') == 'direct' and not record.get('peer_name'):
            missing_peer_name += 1
        for link in record.get('links', ()):
            link_count += 1
            url = str(link.get('url') or '')
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in ('http', 'https') and bool(parsed.netloc)
            except ValueError:
                valid = False
            if not valid:
                bad_links += 1
                problems.append({'message': record.get('id'), 'problem': 'bad_link', 'url': url})
        for attachment in record.get('attachments', ()):
            if attachment.get('status') != 'ready':
                pending += 1
                continue
            ready += 1
            path = Path(attachment.get('path') or '')
            if not path.is_file():
                missing += 1
                problems.append({'message': record.get('id'), 'problem': 'missing_attachment', 'path': str(path)})
                continue
            actual_size = path.stat().st_size
            if attachment.get('size') and int(attachment['size']) != actual_size:
                size_bad += 1
                problems.append({'message': record.get('id'), 'problem': 'size_mismatch', 'path': str(path)})
            if hashes and attachment.get('sha256'):
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                if digest.hexdigest() != attachment['sha256']:
                    hash_bad += 1
                    problems.append({'message': record.get('id'), 'problem': 'sha256_mismatch', 'path': str(path)})
    result = {
        'ok': not (bad_links or missing or size_bad or hash_bad),
        'messages': len(records),
        'last_event_id': last_event,
        'kinds': dict(kinds),
        'content_types': dict(content_types),
        'conversation_types': dict(conversation_types),
        'links': link_count,
        'bad_links': bad_links,
        'unsupported': unsupported,
        'unsupported_types': dict(unsupported_types),
        'missing_conversation_name': missing_conversation_name,
        'missing_peer_name': missing_peer_name,
        'attachments': {
            'ready': ready,
            'pending': pending,
            'missing': missing,
            'size_mismatch': size_bad,
            'sha256_mismatch': hash_bad,
        },
        'problems': problems[:20],
    }
    return result, 0 if result['ok'] else 1


def health(config, max_age_minutes):
    path = config['state_dir'] / 'last-run.json'
    reasons = []
    if not path.is_file():
        return {'healthy': False, 'reasons': ['no last-run.json']}, 1
    status = json.loads(path.read_text(encoding='utf-8'))
    now = time.time()
    completed = float(status.get('completed_at') or 0)
    if status.get('stage') not in ('ok', 'no_change'):
        reasons.append('stage=' + str(status.get('stage')))
    if status.get('decode_errors'):
        reasons.append('decode_errors=%d' % len(status['decode_errors']))
    if not completed or now - completed > max_age_minutes * 60:
        reasons.append('stale_last_run=%.0fs' % (now - completed if completed else -1))
    if status.get('client_processes') == 0:
        reasons.append('wecom_process_not_running')
    result = {'healthy': not reasons, 'reasons': reasons, 'last_run': status}
    return result, 0 if not reasons else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='wecom-export 0.1.0')
    parser.add_argument('--config', type=Path, default=HERE / 'config.json')
    commands = parser.add_subparsers(dest='command', required=True)
    collect_command = commands.add_parser('collect')
    collect_command.add_argument('--full', action='store_true', help='re-read the complete local history')
    collect_command.add_argument('--no-events', action='store_true', help='suppress updated events during a replay/migration')
    collect_command.add_argument('--wait-lock', type=float, default=0, help='wait up to N seconds for a running collection instead of failing')
    query = commands.add_parser('query')
    query.add_argument('--since', default='1h')
    query.add_argument('--limit', type=int, default=100)
    query.add_argument('--include-baseline', action='store_true')
    query.add_argument('--include-updates', action='store_true')
    events = commands.add_parser('events')
    events.add_argument('--after', type=int, default=0)
    events.add_argument('--limit', type=int, default=100)
    snapshot = commands.add_parser('snapshot')
    snapshot.add_argument('--output', type=Path, default=None)
    verify = commands.add_parser('verify')
    verify.add_argument('--hashes', action='store_true', help='recompute SHA-256 for ready attachments')
    status_command = commands.add_parser('status')
    status_command.add_argument('--check', action='store_true', help='exit nonzero when the collector is unhealthy')
    status_command.add_argument('--max-age-minutes', type=float, default=15)
    export_v1 = commands.add_parser('export-v1')
    export_v1.add_argument('--output', type=Path, default=None, help='output directory for chat-export/v1')
    demo_cmd = commands.add_parser('demo', help='run offline synthetic end-to-end demo without account or network')
    demo_cmd.add_argument('--output', type=Path, required=True, help='output directory for demo archive and chat-export/v1')
    doctor_cmd = commands.add_parser('doctor', help='run environment, cryptography, and configuration self-check')
    doctor_cmd.add_argument('--mode', choices=['auto', 'offline', 'guest', 'host'], default='auto')
    args = parser.parse_args()
    if args.command == 'demo':
        from demo_synthetic import run_demo
        result = run_demo(args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('ok') else 1
    if args.command == 'doctor':
        from doctor import run_doctor
        result, code = run_doctor(args.config if args.config.is_file() else None, mode=args.mode)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    config = configuration(args.config)
    if args.command == 'collect':
        result, code = collect(config, full=args.full, no_events=args.no_events, wait_lock=args.wait_lock)
    elif args.command == 'snapshot':
        output = args.output or (config['state_dir'] / 'archive-snapshot.sqlite3')
        with ArchiveStore(config['archive_file']) as store:
            result = {'output': str(store.backup(output))}
        code = 0
    elif args.command == 'verify':
        result, code = verify_archive(config, args.hashes)
    elif args.command == 'export-v1':
        out_dir = args.output or Path(config['chat_export_dir'])
        with ArchiveStore(config['archive_file']) as store:
            all_records = store.records()
        result = export_chat_v1(all_records, out_dir, account_id=config['data_dir'].parent.name)
        code = 0
    elif args.command == 'status':
        if args.check:
            result, code = health(config, args.max_age_minutes)
        else:
            result = json.loads((config['state_dir'] / 'last-run.json').read_text(encoding='utf-8'))
            code = 0
    else:
        with ArchiveStore(config['archive_file']) as store:
            result = (
                store.query_new(
                    since=since_value(args.since),
                    limit=args.limit,
                    include_baseline=args.include_baseline,
                    include_updates=args.include_updates,
                )
                if args.command == 'query'
                else store.events_since(args.after, limit=args.limit)
            )
        code = 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr is not None:
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except Exception as error:
        err_msg = str(error)
        remediation = "./bin/wecom-export doctor --mode offline"
        if isinstance(error, FileNotFoundError) and "config.json" in err_msg:
            remediation = "cp config.example.json collector/config.json && chmod 600 collector/config.json"
        elif "No validated database key" in err_msg:
            remediation = "请确认 Windows 虚拟机内已启动并登录企业微信 (WXWork.exe)，然后重新运行 collect"
        elif isinstance(error, PermissionError) or "lock" in err_msg.lower():
            remediation = "检测到另一采集实例正在运行，请追加 --wait-lock 30 参数等待锁释放"
        print(
            json.dumps(
                {
                    'stage': 'failed',
                    'error_type': type(error).__name__,
                    'error': err_msg,
                    'remediation': remediation,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
