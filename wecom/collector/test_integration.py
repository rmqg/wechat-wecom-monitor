"""Integration test: group/private rows flow through load_records and events."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive_store import ArchiveStore
from messages import load_records


def vint(value):
    encoded = bytearray()
    while value > 127:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def data(number, value):
    if isinstance(value, str):
        value = value.encode()
    return vint(number * 8 + 2) + vint(len(value)) + value


def number(field, value):
    return vint(field * 8) + vint(value)


def text_payload(value):
    return data(1, number(1, 0) + data(2, data(1, value)))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshots = self.root / 'snapshots'
        self.snapshots.mkdir()
        user = sqlite3.connect(self.snapshots / 'user.db')
        user.execute('CREATE TABLE user_table(id, name, real_name)')
        user.execute('INSERT INTO user_table VALUES(200, "徐静", "徐静")')
        user.commit(); user.close()
        session = sqlite3.connect(self.snapshots / 'session.db')
        session.execute('CREATE TABLE conversation_table(id, name, roomname_remark)')
        session.execute('INSERT INTO conversation_table VALUES("R:100", "测试群", "")')
        session.commit(); session.close()
        message = sqlite3.connect(self.snapshots / 'message.db')
        message.execute('''CREATE TABLE message_table(
            message_id INTEGER PRIMARY KEY, server_id INTEGER, sequence INTEGER,
            sender_id INTEGER, conversation_id TEXT, content_type INTEGER,
            send_time INTEGER, flag INTEGER, content BLOB, devinfo INTEGER,
            from_app_id TEXT, msg_from_devinfo INTEGER, extra_content BLOB,
            local_extra_content BLOB, client_id TEXT)''')
        message.execute('INSERT INTO message_table VALUES(1, 1001, 1, 200, "R:100", 2, 10, 0, ?, 0, "", 0, ?, ?, "")',
                        (text_payload('群消息'), b'', b''))
        message.execute('INSERT INTO message_table VALUES(2, 1002, 2, 200, "S:200_300", 2, 11, 0, ?, 0, "", 0, ?, ?, "")',
                        (text_payload('私聊消息'), b'', b''))
        message.commit(); message.close()

    def tearDown(self):
        self.temporary.cleanup()

    def test_group_and_private_metadata_and_new_events(self):
        records, errors, _ = load_records(self.snapshots, 'synthetic', None)
        self.assertEqual(errors, [])
        by_text = {record['text']: record for record in records}
        group = by_text['群消息']
        direct = by_text['私聊消息']
        self.assertEqual((group['conversation_type'], group['is_group'], group['conversation_name']), ('group', True, '测试群'))
        self.assertEqual((direct['conversation_type'], direct['is_group']), ('direct', False))
        self.assertEqual((direct['peer_id'], direct['peer_name'], direct['conversation_name']), ('200', '徐静', '徐静'))
        archive_path = self.root / 'archive.sqlite3'
        with ArchiveStore(archive_path) as store:
            outcome = store.ingest(records, observed_at=100)
            self.assertEqual(outcome['new'], 2)
            events = store.events_since(0)
            kinds = sorted(event['record']['conversation_type'] for event in events)
            self.assertEqual(kinds, ['direct', 'group'])

    def test_incremental_watermark_reads_only_new_rows(self):
        records, _, maximum = load_records(self.snapshots, 'synthetic', None)
        self.assertEqual(maximum['message_table'], 2)
        new_records, _, _ = load_records(self.snapshots, 'synthetic', None, after_ids={'message_table': 1})
        self.assertEqual([record['text'] for record in new_records], ['私聊消息'])
        records, _, _ = load_records(self.snapshots, 'synthetic', None, message_ids=[1, 2])
        self.assertEqual(len(records), 2)


if __name__ == '__main__':
    unittest.main()
