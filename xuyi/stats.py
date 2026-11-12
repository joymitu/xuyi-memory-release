"""Read-only counts and states for the archive."""
from . import index as xuyi_index
from .highlights import FAMILIES, current_highlight
from .retrieval import chunks


def stats(store, scope):
    events = store.events(scope)
    superseded = {event["supersedes"] for event in events if "supersedes" in event}
    peer = scope.removeprefix("qq:c2c:") if scope.startswith("qq:c2c:") else None
    families = {}
    for family_index, query in ((0, "我的猫叫什么？"), (0, "以后怎么称呼我？"),
                                (1, "我现在最爱喝什么？"), (2, "和小林的安排还要赴约吗？"),
                                (3, "最近有什么打算？"), (4, "我今天心情怎么样？")):
        highlight = current_highlight(scope, query, events) if peer else None
        if highlight:
            record, source_events, _replaced = highlight
            families[type(FAMILIES[family_index]).__name__.strip("_Family").lower()] = {
                "值": record.get("值"), "event_id": source_events[0]["event_id"]}
    open_loops = FAMILIES[3].open_loops(peer, events, superseded) if peer else []
    log_rows = 0
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='access_log'").fetchone():
        log_rows = store.db.execute(
            "SELECT COUNT(*) FROM access_log WHERE scope=?", (scope,)).fetchone()[0]
    recorded = sorted(event["recorded_at"] for event in events)
    return {"scope": scope, "事件数": len(events),
            "最早": recorded[0][:16] if recorded else None,
            "最近": recorded[-1][:16] if recorded else None,
            "家族候选": families,
            "开放环": [loop["值"] for loop in open_loops],
            "索引": {"fresh": xuyi_index.fresh(store, scope),
                    "chunks": xuyi_index.chunk_count(store, scope)},
            "交互日志": log_rows,
            "历史状态版本": len(store.db.execute(
                "SELECT version FROM state_versions WHERE scope=?", (scope,)).fetchall())}
