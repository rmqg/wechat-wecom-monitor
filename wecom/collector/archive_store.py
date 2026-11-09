"""SQLite-backed normalized message archive with an append-only event cursor."""

import json
import sqlite3
from pathlib import Path


class ArchiveStore:
    """Persist normalized messages and their meaningful changes."""

    _MESSAGE_FIELDS = (
        "source",
        "id",
        "conversation_id",
        "message_id",
        "server_id",
        "sequence",
        "sender_name",
        "conversation_name",
        "sender_id",
        "sent_at",
        "content_type",
        "kind",
        "text",
        "links",
        "attachments",
        "conversation_type",
        "is_group",
        "peer_id",
        "peer_name",
        "content_type_name",
        "unsupported",
        "member_id",
        "member_name",
        "member_ids",
        "member_names",
    )

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                source TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL,
                raw TEXT,
                first_seen_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                baseline INTEGER NOT NULL,
                PRIMARY KEY (source, id)
            );
            CREATE INDEX IF NOT EXISTS messages_first_seen
                ON messages (first_seen_at, source, id);
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                message_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('new', 'updated')),
                observed_at REAL NOT NULL,
                payload TEXT NOT NULL,
                raw TEXT
            );
            CREATE INDEX IF NOT EXISTS events_observed
                ON events (observed_at, event_id);
            """
        )
        self._db.commit()

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _normalize(cls, record):
        if not isinstance(record, dict):
            raise TypeError("each record must be a dictionary")
        normalized = {key: record[key] for key in cls._MESSAGE_FIELDS if key in record}
        for key in ("source", "id", "conversation_id", "message_id", "server_id", "sequence", "sender_id", "sender_name", "conversation_name", "kind", "text", "conversation_type", "peer_id", "peer_name", "content_type_name", "member_id", "member_name"):
            if key in normalized:
                normalized[key] = str(normalized[key])
        if "source" not in normalized or "id" not in normalized:
            raise ValueError("record requires source and id")
        for key in ("sent_at", "content_type"):
            if key in normalized and normalized[key] is not None:
                normalized[key] = int(normalized[key])
        for key in ("is_group", "unsupported"):
            if key in normalized:
                normalized[key] = bool(normalized[key])
        normalized.setdefault("links", [])
        normalized.setdefault("attachments", [])
        raw = record.get("raw")
        return normalized, raw

    def is_empty(self):
        return self._db.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is None

    def count_messages(self):
        return int(self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def records(self):
        return [json.loads(row["payload"]) for row in self._db.execute("SELECT payload FROM messages ORDER BY first_seen_at, source, id")]

    def pending_message_ids(self):
        result = set()
        for row in self._db.execute("SELECT payload FROM messages"):
            record = json.loads(row["payload"])
            if any(item.get("status") != "ready" for item in record.get("attachments", [])):
                if record.get("message_id"):
                    result.add(int(record["message_id"]))
        return sorted(result)

    def ingest(self, records, *, observed_at, baseline=False, emit_events=True):
        observed_at = float(observed_at)
        added = updated = unchanged = baseline_added = 0
        with self._db:
            for record in records:
                payload, raw = self._normalize(record)
                source, message_id = payload["source"], payload["id"]
                payload_json = self._json(payload)
                raw_json = None if raw is None else self._json(raw)
                existing = self._db.execute(
                    "SELECT payload, baseline FROM messages WHERE source = ? AND id = ?",
                    (source, message_id),
                ).fetchone()
                if existing is None:
                    is_baseline = bool(baseline)
                    self._db.execute(
                        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (source, message_id, payload_json, raw_json, observed_at, observed_at, int(is_baseline)),
                    )
                    if is_baseline:
                        baseline_added += 1
                    else:
                        self._db.execute(
                            "INSERT INTO events (source, message_id, kind, observed_at, payload, raw) VALUES (?, ?, 'new', ?, ?, ?)",
                            (source, message_id, observed_at, payload_json, raw_json),
                        )
                        added += 1
                elif existing["payload"] == payload_json:
                    self._db.execute(
                        "UPDATE messages SET raw = ? WHERE source = ? AND id = ?",
                        (raw_json, source, message_id),
                    )
                    unchanged += 1
                else:
                    self._db.execute(
                        "UPDATE messages SET payload = ?, raw = ?, updated_at = ? WHERE source = ? AND id = ?",
                        (payload_json, raw_json, observed_at, source, message_id),
                    )
                    if emit_events:
                        self._db.execute(
                            "INSERT INTO events (source, message_id, kind, observed_at, payload, raw) VALUES (?, ?, 'updated', ?, ?, ?)",
                            (source, message_id, observed_at, payload_json, raw_json),
                        )
                    updated += 1
        return {
            "new": added,
            "updated": updated,
            "unchanged": unchanged,
            "baseline": baseline_added,
            "last_event_id": self.last_event_id(),
        }

    @staticmethod
    def _record(payload_json, raw_json=None, include_raw=False):
        record = json.loads(payload_json)
        if include_raw and raw_json is not None:
            record["raw"] = json.loads(raw_json)
        return record

    def query_new(self, *, since, limit=100, include_baseline=False, include_updates=False):
        column = "first_seen_at >= ? OR updated_at >= ?" if include_updates else "first_seen_at >= ?"
        parameters = [float(since), float(since)] if include_updates else [float(since)]
        rows = self._db.execute(
            "SELECT payload, raw, first_seen_at, updated_at FROM messages "
            "WHERE (" + column + ") AND (? OR baseline = 0) "
            "ORDER BY updated_at, source, id LIMIT ?",
            (*parameters, int(bool(include_baseline)), int(limit)),
        ).fetchall()
        result = []
        for row in rows:
            record = self._record(row["payload"])
            record["first_seen_at"] = row["first_seen_at"]
            record["updated_at"] = row["updated_at"]
            result.append(record)
        return result

    def events_since(self, after, limit=100, include_raw=False):
        rows = self._db.execute(
            "SELECT event_id, kind, observed_at, payload, raw FROM events "
            "WHERE event_id > ? ORDER BY event_id LIMIT ?",
            (int(after), int(limit)),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "kind": row["kind"],
                "observed_at": row["observed_at"],
                "record": self._record(row["payload"], row["raw"], include_raw),
            }
            for row in rows
        ]

    def backup(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(path))
        try:
            self._db.backup(target)
        finally:
            target.close()
        return path

    def last_event_id(self):
        row = self._db.execute("SELECT COALESCE(MAX(event_id), 0) FROM events").fetchone()
        return int(row[0])
