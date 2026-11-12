"""把TA的记忆带走：备份、导入（换电脑 / 重装）、换了之后自检。

「续忆」的本意是换了身体也能续上：会话、程序、模型、电脑都可以换，记忆在档案里。
- 备份：所有联系人与群的档案（各自是带 SHA256 校验的导出包）+ 用户在「TA的记忆」里的开关。
- 导入：逐个联系人恢复（已有的算重复、不覆盖），再重建索引与强度。只认同一个 QQ 机器人下的
  联系人编号；换了机器人，编号就不同，需要用命令行 merge 指定目标。
- 自检：把「TA的记忆」页上的每一件事换成一个问句去问一遍，看还能不能找回原话。零模型调用。
"""
from . import index
from .store import MemoryError, digest

FORMAT = "xuyi-backup/1"


def backup(store):
    """整本档案 + 开关，一个可以存成文件的字典。"""
    from .strength import _ensure
    _ensure(store)
    contacts = [store.export_bundle(scope) for scope in store.scopes()]
    switches = [dict(row) for row in store.db.execute(
        "SELECT scope, event_id, pinned, muted, wrong, updated_at FROM memory_overrides ORDER BY scope, event_id")]
    body = {"format": FORMAT, "contacts": contacts, "switches": switches}
    return dict(body, sha256=digest({"contacts": contacts, "switches": switches}))


def restore_backup(store, packed):
    """导入一份备份；可以重复导入。每个联系人包各自校验，任何一处不对就整份拒绝、什么都不写。"""
    if not isinstance(packed, dict) or packed.get("format") != FORMAT:
        raise MemoryError("这不是续忆的备份文件")
    contacts, switches = packed.get("contacts"), packed.get("switches")
    if not isinstance(contacts, list) or not isinstance(switches, list):
        raise MemoryError("备份文件内容不完整")
    if digest({"contacts": contacts, "switches": switches}) != packed.get("sha256"):
        raise MemoryError("备份文件被改动过或已损坏")
    for bundle in contacts:
        if not isinstance(bundle, dict) or digest(bundle.get("payload")) != bundle.get("sha256"):
            raise MemoryError("备份文件被改动过或已损坏")
    totals = {"contacts": 0, "inserted": 0, "duplicate": 0, "deleted": 0}
    scopes = []
    for bundle in contacts:
        result = store.restore(bundle)
        totals["contacts"] += 1
        for key in ("inserted", "duplicate", "deleted"):
            totals[key] += result.get(key, 0)
        scopes.append(bundle["payload"]["scope"])
    from .strength import _ensure, rebuild
    _ensure(store)
    with store.db:
        for row in switches:
            if not isinstance(row, dict) or row.get("scope") not in scopes or not isinstance(row.get("event_id"), str):
                continue
            store.db.execute(
                "INSERT OR REPLACE INTO memory_overrides(scope,event_id,pinned,muted,wrong,updated_at)"
                " VALUES(?,?,?,?,?,?)", (row["scope"], row["event_id"], int(bool(row.get("pinned"))),
                                         int(bool(row.get("muted"))), int(bool(row.get("wrong"))),
                                         str(row.get("updated_at") or "")))
    from .consolidate import refresh_corrections
    for scope in scopes:
        index.rebuild(store, scope)
        refresh_corrections(store, scope)
    rebuild(store, scopes)
    return totals


def _probe(retriever, scope, item):
    """换成几种自然问法去问（「我的猫的名字是什么」「我最喜欢的饮品是什么」「饮品」），任一能找回原话或答案就算续上。"""
    label, value = str(item.get("label") or ""), str(item.get("value") or "")
    plain = value.split("，")[0].split("改到")[0].strip()
    for question in (f"我的{label}是什么？", f"我最喜欢的{label}是什么？", label):
        out = retriever.recall(scope, question, 1600, focus=True)
        found = {piece.get("event_id") for piece in out.get("sources", [])}
        if item.get("event_id") in found or (plain and plain in out.get("context", "")):
            return True
    return False


def health(store):
    """装好以后续忆到底有没有真用上（引导最后一步自动体检）。

    recording：私聊里你说的话进了续忆的档案（QQ 连上了不代表续忆收到了）；
    recall：拿最近那句话去想，能找回原话。只看私聊，群里收到的不算。
    """
    import json
    from .retrieval import Retriever
    row = store.db.execute(
        "SELECT scope, payload FROM events WHERE scope LIKE 'qq:c2c:%' "
        "AND json_extract(payload,'$.role')='user' ORDER BY seq DESC LIMIT 1").fetchone()
    count = store.db.execute(
        "SELECT COUNT(*) FROM events WHERE scope LIKE 'qq:c2c:%'").fetchone()[0]
    if not row:
        return {"recording": False, "recall": False, "ok": False, "messages": count, "last_at": None}
    scope, event = row[0], json.loads(row[1])
    recalled = False
    try:
        if not index.fresh(store, scope):
            index.rebuild(store, scope)
        # 普通回忆（不是模型主动查询）：那条路会把「正在回答的那句」排除，刚收到的消息会被误判成想不起来。
        out = Retriever(store).recall(scope, event["text"].strip() + "？", 1600)
        recalled = any(piece.get("event_id") == event["event_id"] for piece in out.get("sources", []))
    except Exception:
        recalled = False
    return {"recording": True, "recall": recalled, "ok": recalled, "messages": count,
            "last_at": event.get("recorded_at")}


def self_check(store, scope):
    """「TA还记得吗」：逐件问一遍，报几件续上了、哪几件没续上。"""
    from .board import board
    from .retrieval import Retriever
    if not index.fresh(store, scope):
        index.rebuild(store, scope)
    retriever = Retriever(store)
    items = [dict(item, group=group["name"]) for group in board(store, scope)["memory"] for item in group["items"]]
    missed = [{"group": item["group"], "label": item["label"], "value": item["value"]}
              for item in items if not _probe(retriever, scope, item)]
    return {"checked": len(items), "ok": len(items) - len(missed), "missed": missed}
