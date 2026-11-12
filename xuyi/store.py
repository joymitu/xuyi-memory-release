"""Transactional source archive. Derived memories never replace source events."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


class MemoryError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except (ValueError, AttributeError, TypeError) as exc:
        raise MemoryError("记录时间必须是带时区的 ISO 日期时间") from exc
    return value


def validate_event(event):
    required = {"scope", "event_id", "sender_id", "role", "origin", "recorded_at", "text"}
    if not isinstance(event, dict) or not required <= event.keys() or event.keys() - required - {"supersedes"}:
        raise MemoryError("消息字段不完整或含有未知字段")
    if any(not isinstance(event[k], str) or not event[k].strip() for k in required):
        raise MemoryError("消息字段必须是非空字符串")
    if event["role"] not in {"user", "assistant", "tool", "system"}:
        raise MemoryError("未知消息角色")
    if event["origin"] not in {"human_direct", "assistant", "tool", "quoted", "system"}:
        raise MemoryError("未知消息来源")
    if event["origin"] == "human_direct" and event["role"] != "user":
        raise MemoryError("真人原话必须为 user 角色")
    if "supersedes" in event and (not isinstance(event["supersedes"], str) or not event["supersedes"] or event["supersedes"] == event["event_id"]):
        raise MemoryError("无效的修订来源")
    timestamp(event["recorded_at"])
    return dict(event)


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('schema_version','1');
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                event_id TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL,
                UNIQUE(scope,event_id));
            CREATE TABLE IF NOT EXISTS tombstones(
                scope TEXT NOT NULL,event_id TEXT NOT NULL,deleted_at TEXT NOT NULL,
                PRIMARY KEY(scope,event_id));
            CREATE TABLE IF NOT EXISTS state_versions(
                scope TEXT NOT NULL,version TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(scope,version));
            CREATE TABLE IF NOT EXISTS state_heads(
                scope TEXT PRIMARY KEY,revision INTEGER NOT NULL,version TEXT);
            CREATE TABLE IF NOT EXISTS decision_log(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,kind TEXT NOT NULL,
                subject TEXT NOT NULL,reason TEXT,recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS supersedes_map(
                scope TEXT NOT NULL,supersedes_id TEXT NOT NULL,event_id TEXT NOT NULL,
                PRIMARY KEY(scope,supersedes_id));
            CREATE TABLE IF NOT EXISTS state_refs(
                scope TEXT NOT NULL,version TEXT NOT NULL,event_id TEXT NOT NULL,
                PRIMARY KEY(scope,version,event_id),
                FOREIGN KEY(scope,version) REFERENCES state_versions(scope,version) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS state_refs_by_event ON state_refs(scope,event_id);
        """)
        if self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != "1":
            self.db.close()
            raise MemoryError("不支持的数据库版本")
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='supersedes_map'").fetchone() \
                or self.db.execute("SELECT COUNT(*) FROM supersedes_map").fetchone()[0] == 0:
            backfilled = self.db.execute(
                "SELECT COUNT(*) FROM events WHERE payload LIKE '%supersedes%'").fetchone()[0]
            if backfilled:
                for payload, in self.db.execute(
                        "SELECT payload FROM events WHERE payload LIKE '%supersedes%'"):
                    event = json.loads(payload)
                    self.db.execute("INSERT OR IGNORE INTO supersedes_map VALUES(?,?,?)",
                                    (event["scope"], event["supersedes"], event["event_id"]))
                self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _add(self, event):
        event = validate_event(event)
        key = (event["scope"], event["event_id"])
        if self.db.execute("SELECT 1 FROM tombstones WHERE scope=? AND event_id=?", key).fetchone():
            return "deleted"
        old = self.db.execute("SELECT hash FROM events WHERE scope=? AND event_id=?", key).fetchone()
        if old:
            if old["hash"] != digest(event):
                raise MemoryError("相同消息 ID 的内容冲突；请使用新 ID 和 supersedes 修订")
            return "duplicate"
        if event.get("supersedes") and not self.get(event["scope"], event["supersedes"]):
            removed = self.db.execute("SELECT 1 FROM tombstones WHERE scope=? AND event_id=?", (event['scope'], event['supersedes'])).fetchone()
            if not removed:
                raise MemoryError("修订消息的原记录不存在或不在同一范围")
        self.db.execute("INSERT INTO events(scope,event_id,payload,hash) VALUES(?,?,?,?)", (*key, canonical(event), digest(event)))
        if event.get("supersedes"):
            self.db.execute("INSERT OR IGNORE INTO supersedes_map VALUES(?,?,?)",
                            (event["scope"], event["supersedes"], event["event_id"]))
        return "inserted"

    def add(self, event):
        with self.db:
            return self._add(event)

    def add_many(self, events):
        with self.db:
            return [self._add(event) for event in events]

    def get(self, scope, event_id):
        row = self.db.execute("SELECT payload FROM events WHERE scope=? AND event_id=?", (scope, event_id)).fetchone()
        return json.loads(row["payload"]) if row else None

    def events(self, scope):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM events WHERE scope=? ORDER BY seq", (scope,))]

    def scopes(self):
        return [r[0] for r in self.db.execute("SELECT DISTINCT scope FROM events ORDER BY scope")]

    def _delete(self, scope, event_id, deleted_at):
        timestamp(deleted_at)
        if not isinstance(scope, str) or not scope or not isinstance(event_id, str) or not event_id:
            raise MemoryError("删除必须指定范围和消息 ID")
        self.db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?)", (scope, event_id, deleted_at))
        self.db.execute(
            "UPDATE state_heads SET revision=revision+1,version=NULL WHERE scope=? "
            "AND version IN (SELECT version FROM state_refs WHERE scope=? AND event_id=?)",
            (scope,scope,event_id))
        self.db.execute(
            "DELETE FROM state_versions WHERE scope=? AND version IN "
            "(SELECT version FROM state_refs WHERE scope=? AND event_id=?)",
            (scope,scope,event_id))
        self.db.execute("DELETE FROM events WHERE scope=? AND event_id=?", (scope, event_id))
        self.db.execute("DELETE FROM supersedes_map WHERE scope=? AND event_id=?", (scope, event_id))
        # Late import: the index derives from events and must go stale with them.
        from .index import forget
        forget(self, scope, event_id)

    def forget(self, scope, event_id):
        with self.db:
            self._delete(scope, event_id, now())
            self.log_decision(scope, "forget", event_id, None)

    def log_decision(self, scope, kind, subject, reason):
        """Append-only decision trail: why a memory changed or was skipped."""
        self.db.execute(
            "INSERT INTO decision_log(scope,kind,subject,reason,recorded_at) VALUES(?,?,?,?,?)",
            (scope, kind, subject, reason, now()))

    def export_bundle(self, scope):
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN")
            from .states import States
            states = States(self)
            payload = {"format": "xuyi-memory/2", "scope": scope,
                       "state_versions": [json.loads(r["payload"]) for r in states.versions(scope)],
                       "state_head": states._head(scope)[1],
                       "state_revision": states._head(scope)[0],
                       "events": self.events(scope),
                       "tombstones": [dict(r) for r in self.db.execute("SELECT * FROM tombstones WHERE scope=? ORDER BY event_id", (scope,))]}
        return {"payload": payload, "sha256": digest(payload)}

    def restore(self, bundle):
        if not isinstance(bundle, dict) or set(bundle) != {"payload", "sha256"}:
            raise MemoryError("恢复包格式错误")
        payload = bundle["payload"]
        if digest(payload) != bundle["sha256"]:
            raise MemoryError("恢复包校验不匹配")
        base_fields = {"format", "scope", "events", "tombstones"}
        formats = {"xuyi-memory/1": base_fields, "xuyi-memory/2": base_fields | {"state_versions", "state_head", "state_revision"}}
        if not isinstance(payload, dict) or not isinstance(payload.get("format"), str) or set(payload) != formats.get(payload["format"]):
            raise MemoryError("不支持的恢复包版本或字段")
        scope = payload["scope"]
        if not isinstance(scope, str) or not scope or not isinstance(payload["events"], list) or not isinstance(payload["tombstones"], list):
            raise MemoryError("恢复包字段类型错误")
        keys = set()
        for event in payload["events"]:
            validate_event(event)
            if event["scope"] != scope or event["event_id"] in keys:
                raise MemoryError("恢复包包含跨范围或重复消息")
            keys.add(event["event_id"])
        for tomb in payload["tombstones"]:
            if not isinstance(tomb, dict) or set(tomb) != {"scope", "event_id", "deleted_at"} or tomb["scope"] != scope:
                raise MemoryError("删除记录格式或范围错误")
            timestamp(tomb["deleted_at"])
        with self.db:
            for tomb in payload["tombstones"]:
                self._delete(scope, tomb["event_id"], tomb["deleted_at"])
            results = [self._add(e) for e in payload["events"]]
            if payload["format"] == "xuyi-memory/2":
                from .states import States
                States(self)._restore(scope, payload["state_versions"], payload["state_head"], payload["state_revision"])
        return {status: results.count(status) for status in ("inserted", "duplicate", "deleted")}
