"""Reversible candidate state snapshots, grounded in archived message quotes."""
from datetime import date
import json

from .store import MemoryError, canonical, digest


class States:
    def __init__(self, store):
        self.store = store
        self.db = store.db

    def _head(self, scope):
        row = self.db.execute(
            "SELECT revision, version FROM state_heads WHERE scope=?", (scope,)
        ).fetchone()
        return (row["revision"], row["version"]) if row else (0, None)

    def view(self, scope):
        revision, version = self._head(scope)
        row = self.db.execute(
            "SELECT payload FROM state_versions WHERE scope=? AND version=?", (scope, version)
        ).fetchone()
        latest = self.db.execute(
            "SELECT event_id FROM events WHERE scope=? ORDER BY seq DESC LIMIT 1", (scope,)
        ).fetchone()
        candidate = json.loads(row[0]) if row else None
        return {
            "revision": revision,
            "version": version,
            "candidate": candidate,
            "verification": "unverified",
            "stale": bool(latest and (not candidate or candidate["through_id"] != latest[0])),
        }

    def versions(self, scope):
        return [dict(row) for row in self.db.execute(
            "SELECT version, payload FROM state_versions WHERE scope=? ORDER BY rowid", (scope,)
        )]

    def _insert(self, scope, payload):
        if not isinstance(payload, dict) or set(payload) != {"through_id", "extractor", "records"}:
            raise MemoryError("状态快照字段不完整")
        through = payload["through_id"]
        if not isinstance(through, str) or not isinstance(payload["extractor"], str) or not payload["extractor"].strip():
            raise MemoryError("状态快照需要处理进度和提取器名称")
        boundary = self.db.execute(
            "SELECT seq FROM events WHERE scope=? AND event_id=?", (scope, through)
        ).fetchone()
        if not boundary or not isinstance(payload["records"], list):
            raise MemoryError("状态快照的进度或记录无效")
        refs = {through}
        fields = {"subject_id", "attribute", "value", "status", "effective_at", "sources"}
        for record in payload["records"]:
            if not isinstance(record, dict) or set(record) != fields:
                raise MemoryError("状态条目字段无效")
            for key in ("subject_id", "attribute", "value"):
                if not isinstance(record[key], str) or not record[key].strip():
                    raise MemoryError("状态对象、属性、值必须为非空字符串")
            if record["status"] not in ("confirmed", "planned", "cancelled", "uncertain"):
                raise MemoryError("状态类型无效")
            if record["effective_at"] is not None:
                try:
                    if date.fromisoformat(record["effective_at"]).isoformat() != record["effective_at"]:
                        raise ValueError()
                except (TypeError, ValueError) as exc:
                    raise MemoryError("生效日期必须为 YYYY-MM-DD") from exc
            if not isinstance(record["sources"], list) or not record["sources"]:
                raise MemoryError("状态必须附上原文来源")
            for source in record["sources"]:
                if not isinstance(source, dict) or set(source) != {"event_id", "quote"}:
                    raise MemoryError("来源必须包含消息 ID 和原文摘录")
                mid, quote = source["event_id"], source["quote"]
                if not isinstance(mid, str) or not isinstance(quote, str) or not quote.strip():
                    raise MemoryError("来源格式无效")
                event = self.db.execute(
                    "SELECT seq,payload FROM events WHERE scope=? AND event_id=?", (scope, mid)
                ).fetchone()
                if not event or event["seq"] > boundary[0]:
                    raise MemoryError("引用已删除、跨范围或尚未处理的消息")
                data = json.loads(event["payload"])
                if data["origin"] != "human_direct" or quote not in data["text"]:
                    raise MemoryError("状态来源必须为真人消息中的逐字摘录")
                refs.add(mid)
        version = digest(payload)
        self.db.execute(
            "INSERT OR IGNORE INTO state_versions VALUES(?,?,?)", (scope, version, canonical(payload))
        )
        self.db.executemany(
            "INSERT OR IGNORE INTO state_refs VALUES(?,?,?)",
            ((scope, version, mid) for mid in refs),
        )
        return version

    def _set(self, scope, version, expected_revision):
        current, _ = self._head(scope)
        if type(expected_revision) is not int or current != expected_revision:
            raise MemoryError("状态版本已变化，请重新读取后重试")
        self.db.execute(
            "INSERT INTO state_heads VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE "
            "SET revision=excluded.revision,version=excluded.version",
            (scope, current+1, version),
        )

    def propose(self, scope, payload, expected_revision):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            version = self._insert(scope, payload)
            self._set(scope, version, expected_revision)
        return self.view(scope)

    def rollback(self, scope, version, expected_revision):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if version is not None and not self.db.execute(
                "SELECT 1 FROM state_versions WHERE scope=? AND version=?", (scope, version)
            ).fetchone():
                raise MemoryError("目标版本不存在或已因删除失效")
            self._set(scope, version, expected_revision)
            self.store.log_decision(scope, "rollback", version or "none", None)
        return self.view(scope)

    def _restore(self, scope, versions, head, revision):
        if not isinstance(versions, list) or (head is not None and not isinstance(head, str)):
            raise MemoryError("恢复包状态字段无效")
        if type(revision) is not int or revision < 0 or (head and not revision):
            raise MemoryError("恢复包状态修订号无效")
        seen = set()
        for payload in versions:
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                raise MemoryError("恢复包状态版本无效")
            if not isinstance(payload.get("through_id"), str):
                raise MemoryError("恢复包缺少处理进度")
            ids = {payload["through_id"]}
            for record in payload["records"]:
                if not isinstance(record, dict) or not isinstance(record.get("sources"), list):
                    raise MemoryError("恢复包状态来源无效")
                for source in record["sources"]:
                    if not isinstance(source, dict) or not isinstance(source.get("event_id"), str):
                        raise MemoryError("恢复包状态来源无效")
                    ids.add(source["event_id"])
            version = digest(payload)
            seen.add(version)
            if any(self.db.execute(
                "SELECT 1 FROM tombstones WHERE scope=? AND event_id=?", (scope, mid)
            ).fetchone() for mid in ids):
                continue
            self._insert(scope, payload)
        if head is not None and head not in seen:
            raise MemoryError("恢复包的状态指针缺失")
        if revision and self._head(scope)[0] == 0:
            available = head and self.db.execute(
                "SELECT 1 FROM state_versions WHERE scope=? AND version=?", (scope, head)
            ).fetchone()
            self.db.execute("INSERT INTO state_heads VALUES(?,?,?)",
                            (scope, revision, head if available else None))
