"""Instance-merge protocol for exported bundles.

Biology behind the rules (bio-memory-expansion.md §3/§8): fusion is gated by
an explicit identity check (Botryllus fusibility), merging is a competition
for retrieval rights rather than a silent blend (stem-cell competition), and
transferred memory must carry its protocol context (C. elegans provenance).
Accordingly: the operator decides scope fusion explicitly, every conflict is
reported with both sides kept auditable, tombstone unions only ever widen
deletions, and the merge writes its own provenance into the ledger.
"""
import json

from .importer import _ensure as _ensure_ledger, _record
from .store import MemoryError, now as store_now


def merge_bundle(store, bundle, target_scope, provenance=None, now=None):
    """Merge an exported bundle into target_scope of this store. Idempotent."""
    if not isinstance(bundle, dict) or set(bundle) != {"payload", "sha256"}:
        raise MemoryError("恢复包格式错误")
    payload = bundle["payload"]
    if payload.get("format") != "xuyi-memory/2" or not isinstance(target_scope, str) or not target_scope:
        raise MemoryError("合并需要 xuyi-memory/2 包和明确的目标范围")
    if payload.get("scope") == target_scope:
        raise MemoryError("目标范围与来源相同；同范围请直接使用 restore")
    from . import index as xuyi_index
    from .store import digest
    if digest(payload) != bundle["sha256"]:
        raise MemoryError("恢复包校验不匹配")
    _ensure_ledger(store)
    source_scope = payload["scope"]
    now = now or store_now
    counts = {"inserted": 0, "duplicate": 0, "failed": 0, "conflict": 0, "tombstoned": 0,
              "skipped_deleted": 0}
    conflicts = []
    reworded = []
    for tomb in payload["tombstones"]:
        # Either side's deletion wins; re-scoped to the target identity.
        store.db.execute(
            "INSERT OR IGNORE INTO tombstones VALUES(?,?,?)",
            (target_scope, tomb["event_id"], tomb["deleted_at"]))
        store.db.execute(
            "UPDATE state_heads SET revision=revision+1,version=NULL WHERE scope=? "
            "AND version IN (SELECT version FROM state_refs WHERE scope=? AND event_id=?)",
            (target_scope, target_scope, tomb["event_id"]))
        store.db.execute(
            "DELETE FROM state_versions WHERE scope=? AND version IN "
            "(SELECT version FROM state_refs WHERE scope=? AND event_id=?)",
            (target_scope, target_scope, tomb["event_id"]))
        store.db.execute("DELETE FROM events WHERE scope=? AND event_id=?",
                         (target_scope, tomb["event_id"]))
        xuyi_index.forget(store, target_scope, tomb["event_id"])
        counts["tombstoned"] += 1
    with store.db:
        for event in payload["events"]:
            event = dict(event, scope=target_scope)
            try:
                from .store import validate_event
                validate_event(event)
            except MemoryError as exc:
                conflicts.append({"event_id": event.get("event_id"), "error": str(exc)})
                counts["conflict"] += 1
                continue
            if store.db.execute(
                    "SELECT 1 FROM tombstones WHERE scope=? AND event_id=?",
                    (target_scope, event["event_id"])).fetchone():
                # Deletion from either side wins; a stale bundle must not
                # resurrect what a newer deletion removed.
                counts["skipped_deleted"] += 1
                continue
            existing = store.db.execute(
                "SELECT hash FROM events WHERE scope=? AND event_id=?",
                (target_scope, event["event_id"])).fetchone()
            if existing:
                if existing[0] != digest(event):
                    counts["conflict"] += 1
                    conflicts.append({"event_id": event["event_id"],
                                      "error": "同一消息 ID 内容冲突；目标版本保留，来源版本原文见 text",
                                      "source_text": event["text"]})
                else:
                    counts["duplicate"] += 1
                continue
            from .store import canonical
            store.db.execute(
                "INSERT INTO events(scope,event_id,payload,hash) VALUES(?,?,?,?)",
                (target_scope, event["event_id"], canonical(event), digest(event)))
            counts["inserted"] += 1
            reworded.append(event)
        if reworded:
            xuyi_index.update(store, target_scope, reworded)
    _record(store, f"merge:{source_scope}", target_scope,
            source_scope, counts, now(), provenance)
    return {"source_scope": source_scope, "target_scope": target_scope,
            "counts": counts, "conflicts": conflicts[:50],
            "source_state_versions": len(payload.get("state_versions") or []),
            "note": "目标状态头未改动；来源状态版本数供人工复核"}
