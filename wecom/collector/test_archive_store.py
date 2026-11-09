"""Synthetic behavioral tests for ArchiveStore; no user data is used."""

import tempfile
import unittest
from pathlib import Path

from archive_store import ArchiveStore


def message(source, message_id, text, *, sent_at=1, attachments=None, raw=None):
    record = {
        "source": source,
        "id": message_id,
        "conversation_id": "conversation-1",
        "message_id": message_id,
        "server_id": "server-" + str(message_id),
        "sequence": str(message_id),
        "sender_id": "sender-1",
        "sent_at": sent_at,
        "content_type": 1,
        "kind": "text",
        "text": text,
        "links": [],
        "attachments": attachments or [],
    }
    if raw is not None:
        record["raw"] = raw
    return record


class ArchiveStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "nested" / "archive.sqlite"
        self.store = ArchiveStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_duplicate_delivery_and_same_id_in_different_sources(self):
        first = message("account-a", "900719925474099312345", "hello", raw={"private": "one"})
        result = self.store.ingest([first], observed_at=10)
        self.assertEqual((result["new"], result["updated"], result["unchanged"]), (1, 0, 0))
        replay = message("account-a", "900719925474099312345", "hello", raw={"private": "two"})
        result = self.store.ingest([replay, replay], observed_at=20)
        self.assertEqual((result["new"], result["updated"], result["unchanged"]), (0, 0, 2))
        self.assertEqual(result["last_event_id"], 1)
        self.assertEqual(self.store.ingest([message("account-b", "900719925474099312345", "other")], observed_at=21)["new"], 1)

        events = self.store.events_since(0)
        self.assertEqual([event["kind"] for event in events], ["new", "new"])
        self.assertEqual([event["record"]["source"] for event in events], ["account-a", "account-b"])
        self.assertEqual(events[0]["record"]["id"], "900719925474099312345")
        self.assertNotIn("raw", events[0]["record"])

        self.assertEqual(self.store.events_since(0, include_raw=True)[0]["record"]["raw"], {"private": "one"})

    def test_baseline_is_excluded_and_late_message_uses_observed_time(self):
        baseline_result = self.store.ingest([message("acct", "baseline", "old")], observed_at=100, baseline=True)
        self.assertEqual((baseline_result["baseline"], baseline_result["new"], baseline_result["last_event_id"]), (1, 0, 0))
        self.assertEqual(self.store.last_event_id(), 0)
        self.assertEqual(self.store.query_new(since=0), [])
        baseline = self.store.query_new(since=0, include_baseline=True)
        self.assertEqual([(record["id"], record["first_seen_at"], record["updated_at"]) for record in baseline], [("baseline", 100.0, 100.0)])

        self.store.ingest([message("acct", "fresh", "new", sent_at=-500)], observed_at=101)
        results = self.store.query_new(since=0)
        self.assertEqual([record["id"] for record in results], ["fresh"])
        self.assertEqual(results[0]["first_seen_at"], 101.0)
        self.assertEqual(self.store.events_since(0)[0]["observed_at"], 101.0)
        self.assertEqual(self.store.events_since(0)[0]["record"]["sent_at"], -500)

    def test_pending_attachment_becoming_ready_emits_one_update(self):
        pending = [{"kind": "file", "name": "report.txt", "size": 12, "status": "pending_download", "path": None}]
        ready = [{"kind": "file", "name": "report.txt", "size": 12, "status": "ready", "path": "files/report.txt", "sha256": "a" * 64}]
        self.store.ingest([message("acct", "m1", "see file", attachments=pending)], observed_at=200)
        unchanged = self.store.ingest([message("acct", "m1", "see file", attachments=pending)], observed_at=201)
        self.assertEqual((unchanged["updated"], unchanged["last_event_id"]), (0, 1))
        changed = self.store.ingest([message("acct", "m1", "see file", attachments=ready)], observed_at=202)
        self.assertEqual((changed["new"], changed["updated"], changed["last_event_id"]), (0, 1, 2))
        repeat = self.store.ingest([message("acct", "m1", "see file", attachments=ready)], observed_at=203)
        self.assertEqual((repeat["updated"], repeat["last_event_id"]), (0, 2))
        events = self.store.events_since(0)
        self.assertEqual([(item["event_id"], item["kind"]) for item in events], [(1, "new"), (2, "updated")])
        self.assertEqual(events[1]["record"]["attachments"][0]["status"], "ready")
        record = self.store.query_new(since=0)[0]
        self.assertEqual(record["updated_at"], 202.0)


    def test_mixed_content_is_preserved_as_one_message(self):
        record = message("acct", "mixed-1", "image plus note")
        record["content_type"] = 123
        record["kind"] = "mixed"
        record["attachments"] = [{"kind": "image", "name": "photo.png", "size": 7, "status": "thumbnail_only"}]
        self.store.ingest([record], observed_at=250)
        events = self.store.events_since(0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["record"]["kind"], "mixed")
        self.assertEqual(events[0]["record"]["content_type"], 123)
        self.assertEqual(events[0]["record"]["text"], "image plus note")
        self.assertEqual(len(events[0]["record"]["attachments"]), 1)

    def test_message_body_change_emits_update_not_duplicate_new(self):
        self.store.ingest([message("acct", "m1", "before")], observed_at=300)
        result = self.store.ingest([message("acct", "m1", "after")], observed_at=301)
        self.assertEqual((result["new"], result["updated"]), (0, 1))
        self.assertEqual([event["kind"] for event in self.store.events_since(0)], ["new", "updated"])
        self.assertEqual(self.store.events_since(0)[1]["record"]["text"], "after")

    def test_failed_batch_rolls_back_messages_and_events(self):
        with self.assertRaises(ValueError):
            self.store.ingest(
                [message("acct", "will-rollback", "temporary"), {"id": "missing-source"}],
                observed_at=400,
            )
        self.assertEqual(self.store.query_new(since=0, include_baseline=True), [])
        self.assertEqual(self.store.events_since(0), [])
        self.assertEqual(self.store.last_event_id(), 0)

    def test_event_cursor_continues_without_replaying_previous_events(self):
        self.store.ingest([message("acct", "one", "one"), message("acct", "two", "two")], observed_at=500)
        page = self.store.events_since(0, limit=1)
        self.assertEqual([(event["event_id"], event["record"]["id"]) for event in page], [(1, "one")])
        continued = self.store.events_since(page[-1]["event_id"], limit=1)
        self.assertEqual([(event["event_id"], event["record"]["id"]) for event in continued], [(2, "two")])
        self.assertEqual(self.store.events_since(continued[-1]["event_id"]), [])

    def test_replay_mode_suppresses_only_update_events(self):
        self.store.ingest([message("acct", "1", "first")], observed_at=10)
        result = self.store.ingest([message("acct", "1", "changed")], observed_at=20, emit_events=False)
        self.assertEqual((result["new"], result["updated"]), (0, 1))
        events = self.store.events_since(0)
        self.assertEqual([event["kind"] for event in events], ["new"])
        self.assertEqual(self.store.query_new(since=0)[0]["text"], "changed")

    def test_pending_message_ids_and_include_updates(self):
        pending = message("acct", "900", "", attachments=[{"kind": "file", "status": "pending_download"}])
        self.store.ingest([pending], observed_at=10)
        self.assertEqual(self.store.pending_message_ids(), [900])
        ready = message("acct", "900", "", attachments=[{"kind": "file", "status": "ready"}])
        self.store.ingest([ready], observed_at=20)
        self.assertEqual(self.store.pending_message_ids(), [])
        self.assertEqual(len(self.store.query_new(since=15, include_updates=False)), 0)
        self.assertEqual(len(self.store.query_new(since=15, include_updates=True)), 1)



if __name__ == "__main__":
    unittest.main()
